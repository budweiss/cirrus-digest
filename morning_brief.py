#!/usr/bin/env python3
"""
CIRRUS Morning Brief  (S49, 2026-08-01)
===============================================================================
Server-side replacement for the MacBook-tied Cowork scheduled task
'cirrus-morning-review'. Runs ON CIRRUS at 07:30 via com.cirrus.morningbrief,
i.e. AFTER the 07:00 daily digest. It composes a short health + digest +
action-items + pending-decisions brief PURELY from local files on CIRRUS (no
web fetch, no external LLM required) and delivers it two ways:

  * email  — reuses send_digest.send_email() (Gmail SMTP → Buddy.Weiss@outlook.com)
  * Telegram — sendMessage to the owner chat id

Because it reads CIRRUS's own files, it does not depend on the MacBook being
awake — which was the whole point of the migration (see
docs/CIRRUS-Scheduled-Task-Migration-Plan.md).

Usage:
  python3 morning_brief.py            # compose + SEND (email + telegram)
  python3 morning_brief.py --dry-run  # compose + PRINT to stdout, send nothing
"""

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

# ── Config / paths (mirror send_digest.py) ─────────────────────────────────────
PROJECT_DIR  = Path.home() / "projects/cirrus-digest"
CONFIG_PATH  = PROJECT_DIR / "config/sources.json"
CREDS_PATH   = PROJECT_DIR / "config/credentials.json"
PENDING_FILE = PROJECT_DIR / "config/pending_approvals.json"

# Make sibling modules importable when launched by full path from launchd.
sys.path.insert(0, str(PROJECT_DIR))

CONFIG = json.load(open(CONFIG_PATH))
CREDS  = json.load(open(CREDS_PATH))

DIGEST_CFG  = CONFIG["digest"]
OUTPUT_DIR  = Path(DIGEST_CFG["output_dir"])
LOG_DIR     = Path(DIGEST_CFG["log_dir"])
ACTIONS_DIR = OUTPUT_DIR / "actions"

TG_TOKEN = CREDS.get("telegram_bot_token", "")
TG_USER  = str(CREDS.get("telegram_user_id", "")).strip()

TODAY     = datetime.now().strftime("%Y-%m-%d")
DAY_NAME  = datetime.now().strftime("%A, %B %d")

# ── Small helpers ──────────────────────────────────────────────────────────────
def _read(p):
    try:
        return Path(p).read_text()
    except Exception:
        return ""

def _find_latest(pattern):
    try:
        fs = sorted(OUTPUT_DIR.glob(pattern), reverse=True)
        return fs[0] if fs else None
    except Exception:
        return None

def _find_latest_action(prefix):
    try:
        fs = sorted(ACTIONS_DIR.glob(f"{prefix}-*.md"), reverse=True)
        return fs[0] if fs else None
    except Exception:
        return None

def _bullets_under(md_text, header):
    """Return the '- ...' bullet lines under a '## HEADER' section."""
    out, grab = [], False
    for line in md_text.splitlines():
        s = line.strip()
        if s.startswith("## "):
            grab = header.lower() in s.lower()
            continue
        if grab and s.startswith(("-", "*", "•")):
            out.append("- " + s.lstrip("-*• ").strip())
    return out

# ── Gatherers (each is defensive; a failure degrades one line, not the brief) ──
def gather_digest():
    f = _find_latest("daily-*.md")
    if not f:
        return {"ok": False, "line": "⚠️ No daily digest file found in output dir.",
                "notable": [], "dated_today": False}
    txt = f.read_text(errors="ignore")
    dated_today = TODAY in f.name
    m = re.search(r"Items processed:\s*([0-9]+)", txt)
    count = m.group(1) if m else "?"
    # best-effort "notable" article titles: h2/h3 headings, minus section labels
    skip = ("run stats", "links visited", "access needed", "action item",
            "disk status", "cirrus", "today", "digest", "improvement",
            "recommendation", "follow-up", "interesting tools")
    raw = [t.strip() for t in re.findall(r"^#{2,3}\s+(.+)$", txt, flags=re.MULTILINE)]
    # digest headings often join several headlines with " / " — split + flatten
    titles, seen = [], set()
    for t in raw:
        for part in t.split(" / "):
            p = part.strip()
            key = p.lower()
            if p and key not in seen and not any(s in key for s in skip):
                seen.add(key)
                titles.append(p if len(p) <= 90 else p[:87] + "…")
    kw = ("claude", "anthropic", "llama", "qwen", "deepseek", "local", "ollama",
          "open-weight", "open weight", "gpt", "mistral", "gemma", "model")
    fav = [t for t in titles if any(k in t.lower() for k in kw)]
    notable = (fav or titles)[:3]
    line = (f"{'✅' if dated_today else '⚠️'} Digest {f.name} — {count} items"
            + ("" if dated_today else " (NOT dated today!)"))
    return {"ok": dated_today, "line": line, "notable": notable, "dated_today": dated_today}

def gather_actions():
    f = _find_latest_action("daily-actions")
    if not f:
        return {"actions": [], "notes": [], "file": None}
    txt = f.read_text(errors="ignore")
    return {
        "actions": _bullets_under(txt, "ACTION ITEMS")[:5],
        "notes":   _bullets_under(txt, "CIRRUS IMPROVEMENT NOTES")[:5],
        "file":    f.name,
    }

def gather_pending():
    try:
        items = json.loads(PENDING_FILE.read_text())
    except Exception:
        return []
    pend = [i for i in items if i.get("status") == "pending"]
    out = []
    for it in pend:
        det = (it.get("detail") or "").strip().replace("\n", " ")
        out.append(f"{it.get('type','?')}: {det[:90]}")
    return out

def gather_attention():
    flags = []
    # bot.log: real errors, ignoring the benign getUpdates long-poll timeouts
    bl = _read(LOG_DIR / "bot.log")
    if bl:
        tail = bl.splitlines()[-600:]
        # Only flag errors logged TODAY — stale lines shouldn't keep the
        # verdict red forever. bot.log lines are stamped "[YYYY-MM-DD ...]".
        errs = [l for l in tail
                if TODAY in l
                and ("error" in l.lower() or "traceback" in l.lower())
                and "getupdates" not in l.lower()]
        if errs:
            flags.append(f"bot.log: {len(errs)} error line(s) today — e.g. {errs[-1][-120:].strip()}")
    # paywalls: any hits logged today
    pw = _read(LOG_DIR / "paywalls.log")
    if pw:
        hits = [l for l in pw.splitlines() if TODAY in l]
        if hits:
            flags.append(f"paywalls: {len(hits)} hit(s) today (cookies may need refresh)")
    return flags

# ── Compose ────────────────────────────────────────────────────────────────────
def compose():
    dig = gather_digest()
    act = gather_actions()
    pend = gather_pending()
    att = gather_attention()

    healthy = dig["dated_today"] and not att
    verdict = "✅ CIRRUS healthy" if healthy else "⚠️ Needs a look"

    lines = [f"# ☀️ CIRRUS Morning Brief — {DAY_NAME}", "", f"**{verdict}**", "",
             dig["line"]]
    if dig["notable"]:
        lines.append("Notable: " + "; ".join(dig["notable"]))
    lines.append("")

    lines.append(f"**Pending decisions ({len(pend)})**")
    if pend:
        lines += [f"- {p}" for p in pend[:6]]
        if len(pend) > 6:
            lines.append(f"- …and {len(pend) - 6} more")
    else:
        lines.append("- None — /accept queue is clear")
    lines.append("")

    if act["actions"]:
        lines.append("**Today's action items**")
        lines += act["actions"]
        lines.append("")
    if act["notes"]:
        lines.append("**CIRRUS improvement notes**")
        lines += act["notes"]
        lines.append("")

    lines.append("**Attention**")
    lines += ([f"- {a}" for a in att] if att else ["- Nothing flagged"])
    lines.append("")

    # one suggested next action
    if not dig["dated_today"]:
        nxt = "Investigate the 7am digest — today's file is missing or misdated."
    elif pend:
        nxt = f"Review the {len(pend)} pending /accept item(s)."
    elif att:
        nxt = "Check the attention flag(s) above."
    else:
        nxt = "Nothing needs you this morning."
    lines.append(f"**Next:** {nxt}")
    lines.append("")
    lines.append("*Composed by CIRRUS on-box (morning_brief.py) — no MacBook required.*")

    subject = f"☀️ CIRRUS Morning Brief — {DAY_NAME}  ({verdict})"
    return subject, "\n".join(lines)

# ── Delivery ───────────────────────────────────────────────────────────────────
def send_telegram(text):
    if not TG_TOKEN or not TG_USER:
        return "telegram: no token/user configured — skipped"
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    # keep well under Telegram's 4096 hard cap
    chunk = text if len(text) <= 3900 else text[:3900] + "\n…(truncated)"
    data = urllib.parse.urlencode({"chat_id": TG_USER, "text": chunk,
                                   "parse_mode": "Markdown"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
            ok = json.loads(r.read()).get("ok")
        if ok:
            return "telegram: sent"
        # retry without markdown
        data = urllib.parse.urlencode({"chat_id": TG_USER, "text": chunk}).encode()
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
            ok = json.loads(r.read()).get("ok")
        return "telegram: sent (plain)" if ok else "telegram: failed"
    except Exception as e:
        return f"telegram: error {e}"

def send_all(subject, body):
    results = []
    # email (reuse the tested digest sender)
    try:
        from send_digest import send_email
        send_email(subject, body)
        results.append("email: sent")
    except Exception as e:
        results.append(f"email: error {e}")
    # telegram
    results.append(send_telegram(body))
    return results

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    dry = "--dry-run" in sys.argv or "--dry" in sys.argv
    subject, body = compose()
    if dry:
        print("=== DRY RUN — nothing sent ===")
        print("SUBJECT:", subject)
        print("-" * 70)
        print(body)
        return
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] sending morning brief…")
    for r in send_all(subject, body):
        print("  ", r)
    print("done.")

if __name__ == "__main__":
    main()
