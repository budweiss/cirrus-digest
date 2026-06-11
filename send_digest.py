#!/usr/bin/env python3
"""
CIRRUS Email Delivery
Sends daily or weekly digest + action items + disk space status
from cirrustask@outlook.com to Buddy.Weiss@outlook.com
"""

import json
import shutil
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────

CONFIG_PATH = Path.home() / "projects/cirrus-digest/config/sources.json"
CREDS_PATH  = Path.home() / "projects/cirrus-digest/config/credentials.json"

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

with open(CREDS_PATH) as f:
    CREDS = json.load(f)

DIGEST_CFG  = CONFIG["digest"]
OUTPUT_DIR  = Path(DIGEST_CFG["output_dir"])
LOG_DIR     = Path(DIGEST_CFG["log_dir"])
ACTIONS_DIR = OUTPUT_DIR / "actions"
WHISPER_CACHE = Path.home() / ".cache" / "whisper"

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT   = 587
FROM_EMAIL  = CREDS["outlook_email"]
FROM_PASS   = CREDS["outlook_password"]
TO_EMAIL    = "Buddy.Weiss@outlook.com"

# ── Helpers ──────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")

def folder_size_gb(path: Path) -> float:
    if not path.exists():
        return 0.0
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / (1024 ** 3)

def free_disk_gb() -> float:
    usage = shutil.disk_usage(Path.home())
    return usage.free / (1024 ** 3)

def get_disk_status() -> str:
    digest_gb  = folder_size_gb(OUTPUT_DIR)
    whisper_gb = folder_size_gb(WHISPER_CACHE)
    free_gb    = free_disk_gb()

    daily_count  = len(list(OUTPUT_DIR.glob("daily-*.md"))) if OUTPUT_DIR.exists() else 0
    weekly_count = len(list(OUTPUT_DIR.glob("digest-*.md"))) if OUTPUT_DIR.exists() else 0
    action_count = len(list(ACTIONS_DIR.glob("*.md"))) if ACTIONS_DIR.exists() else 0

    warnings = []
    if digest_gb > 1.0:
        warnings.append(f"⚠️ Digest folder exceeds 1GB")
    if whisper_gb > 5.0:
        warnings.append(f"⚠️ Whisper cache exceeds 5GB")
    if free_gb < 50.0:
        warnings.append(f"⚠️ Free disk below 50GB")

    status = f"""## 💾 CIRRUS Disk Status

| Item | Size |
|---|---|
| Digest folder | {digest_gb:.2f} GB |
| Whisper cache | {whisper_gb:.2f} GB |
| Free disk space | {free_gb:.1f} GB |

**Files stored:** {weekly_count} weekly digests, {daily_count} daily digests, {action_count} action files
"""
    if warnings:
        status += "\n**Warnings:**\n" + "\n".join(f"- {w}" for w in warnings) + "\n"
    else:
        status += "\n✅ All disk usage within normal limits\n"

    return status

def read_file(path: Path) -> str:
    if path and path.exists():
        return path.read_text().strip()
    return ""

def find_latest(pattern: str) -> Path:
    files = sorted(OUTPUT_DIR.glob(pattern), reverse=True)
    return files[0] if files else None

def find_latest_action(prefix: str) -> Path:
    files = sorted(ACTIONS_DIR.glob(f"{prefix}-*.md"), reverse=True)
    return files[0] if files else None

def markdown_to_html(text: str) -> str:
    """Basic markdown to HTML conversion for email."""
    import re
    # Headers
    text = re.sub(r'^### (.+)$', r'<h3>\1</h3>', text, flags=re.MULTILINE)
    text = re.sub(r'^## (.+)$', r'<h2>\1</h2>', text, flags=re.MULTILINE)
    text = re.sub(r'^# (.+)$', r'<h1>\1</h1>', text, flags=re.MULTILINE)
    # Bold
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    # Italic
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    # Links
    text = re.sub(r'\[(.+?)\]\((.+?)\)', r'<a href="\2">\1</a>', text)
    # Code
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    # Horizontal rules
    text = re.sub(r'^---$', r'<hr>', text, flags=re.MULTILINE)
    # Line breaks
    text = text.replace('\n', '<br>\n')
    return text

# ── Email Builder ─────────────────────────────────────────────────────────────

def build_email(mode: str) -> tuple:
    """Build subject and body for daily or weekly email. Returns (subject, html_body)."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    day_name = datetime.now().strftime("%A, %B %d")
    disk_status = get_disk_status()

    if mode == "daily":
        digest_file  = find_latest("daily-*.md")
        actions_file = find_latest_action("daily-actions")
        subject = f"☀️ CIRRUS Daily Digest — {day_name}"

        digest_content  = read_file(digest_file)  or "_No articles found today._"
        actions_content = read_file(actions_file) or "_No action items extracted._"

        body = f"""# ☀️ CIRRUS Daily Digest
### {day_name}

---

{digest_content}

---

# 📋 Today's Action Items

{actions_content}

---

{disk_status}

---
*Sent by CIRRUS — your local AI assistant running on Mac Studio M4 Max*
"""

    else:  # weekly
        digest_file  = find_latest("digest-*.md")
        actions_file = find_latest_action("weekly-actions")
        subject = f"📬 CIRRUS Weekly Digest — {day_name}"

        digest_content  = read_file(digest_file)  or "_No content found this week._"
        actions_content = read_file(actions_file) or "_No action items extracted._"

        body = f"""# 📬 CIRRUS Weekly Digest
### {day_name}

---

{digest_content}

---

# 📋 This Week's Action Items

{actions_content}

---

{disk_status}

---
*Sent by CIRRUS — your local AI assistant running on Mac Studio M4 Max*
"""

    return subject, body

# ── Sender ────────────────────────────────────────────────────────────────────

def send_email(subject: str, body: str):
    """Send email via Outlook SMTP."""
    log(f"Sending email: {subject}")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = FROM_EMAIL
    msg["To"]      = TO_EMAIL

    # Plain text version
    msg.attach(MIMEText(body, "plain"))

    # HTML version
    html_body = f"""<!DOCTYPE html>
<html>
<head>
<style>
  body {{ font-family: -apple-system, Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; color: #333; }}
  h1 {{ color: #1a1a2e; border-bottom: 2px solid #e0e0e0; padding-bottom: 10px; }}
  h2 {{ color: #16213e; margin-top: 30px; }}
  h3 {{ color: #0f3460; }}
  hr {{ border: none; border-top: 1px solid #e0e0e0; margin: 20px 0; }}
  code {{ background: #f4f4f4; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }}
  table {{ border-collapse: collapse; width: 100%; margin: 10px 0; }}
  th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; }}
  th {{ background: #f4f4f4; }}
  a {{ color: #0f3460; }}
  blockquote {{ border-left: 3px solid #ccc; margin: 0; padding-left: 15px; color: #666; }}
</style>
</head>
<body>
{markdown_to_html(body)}
</body>
</html>"""
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(FROM_EMAIL, FROM_PASS)
            server.sendmail(FROM_EMAIL, TO_EMAIL, msg.as_string())
        log(f"Email sent successfully to {TO_EMAIL}")
    except Exception as e:
        log(f"Email send failed: {e}")
        raise

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if mode not in ("daily", "weekly"):
        print("Usage: python3 send_digest.py [daily|weekly]")
        sys.exit(1)

    log(f"=== CIRRUS Email Delivery ({mode}) ===")
    subject, body = build_email(mode)
    send_email(subject, body)
    log("=== Done ===")

if __name__ == "__main__":
    main()
