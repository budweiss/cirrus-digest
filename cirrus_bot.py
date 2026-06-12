#!/usr/bin/env python3
"""
CIRRUS Telegram Bot
Listens for commands from Buddy's Telegram account only.
Allows remote control of CIRRUS digest system from iPhone.

Commands:
  /help       - show available commands
  /status     - show CIRRUS status and last digest times
  /disk       - show disk usage
  /daily      - run daily digest now
  /weekly     - run weekly digest now
  /sources    - list all monitored sources
  /latest     - show latest digest summary
  /actions    - show latest action items
  /approve    - review and approve pending recommendations
"""

import json
import os
import re
import requests
import subprocess
import shutil
import time
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────

CONFIG_PATH = Path.home() / "projects/cirrus-digest/config/sources.json"
CREDS_PATH  = Path.home() / "projects/cirrus-digest/config/credentials.json"

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

with open(CREDS_PATH) as f:
    CREDS = json.load(f)

DIGEST_CFG    = CONFIG["digest"]
OUTPUT_DIR    = Path(DIGEST_CFG["output_dir"])
LOG_DIR       = Path(DIGEST_CFG["log_dir"])
ACTIONS_DIR   = OUTPUT_DIR / "actions"
WHISPER_CACHE = Path.home() / ".cache" / "whisper"
PROJECT_DIR   = Path.home() / "projects/cirrus-digest"

BOT_TOKEN     = CREDS["telegram_bot_token"]
ALLOWED_ID    = int(CREDS["telegram_user_id"])
API_URL       = f"https://api.telegram.org/bot{BOT_TOKEN}"
OLLAMA_HOST   = DIGEST_CFG["ollama_host"]
MODEL         = DIGEST_CFG["ollama_model"]

# ── Helpers ──────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    log_file = LOG_DIR / "bot.log"
    with open(log_file, "a") as f:
        f.write(line + "\n")

def api_call(method, params=None):
    url = f"{API_URL}/{method}"
    if params:
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data)
    else:
        req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log(f"API error ({method}): {e}")
        return {}

def send_message(chat_id, text):
    # Split long messages into chunks of 4000 chars
    max_len = 4000
    for i in range(0, len(text), max_len):
        chunk = text[i:i+max_len]
        api_call("sendMessage", {
            "chat_id": chat_id,
            "text": chunk
        })
        if len(text) > max_len:
            time.sleep(0.5)

def folder_size_gb(path: Path) -> float:
    if not path.exists():
        return 0.0
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / (1024 ** 3)

def free_disk_gb() -> float:
    usage = shutil.disk_usage(Path.home())
    return usage.free / (1024 ** 3)

def find_latest(pattern):
    files = sorted(OUTPUT_DIR.glob(pattern), reverse=True)
    return files[0] if files else None

def find_latest_action(prefix):
    if not ACTIONS_DIR.exists():
        return None
    files = sorted(ACTIONS_DIR.glob(f"{prefix}-*.md"), reverse=True)
    return files[0] if files else None

# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_help():
    return """🤖 *CIRRUS Bot — Available Commands*

/status — CIRRUS status and last digest times
/disk — disk usage and file counts
/daily — run daily digest now
/weekly — run weekly digest now
/sources — list all monitored sources
/latest — show latest daily digest summary
/actions — show latest action items
/gitpull - pull latest updates from GitHub
/approve — review and approve pending recommendations
/knowledge — show RAG knowledge base stats
/ask <question> — ask CIRRUS a question using past digest memory
/help — show this message

*Approval replies:*
`approve 1` — approve item 1
`reject 2` — reject item 2
`approve all` — approve everything"""

def cmd_status():
    # Last digest files
    last_daily  = find_latest("daily-*.md")
    last_weekly = find_latest("digest-*.md")

    daily_time  = datetime.fromtimestamp(last_daily.stat().st_mtime).strftime("%Y-%m-%d %H:%M") if last_daily else "Never"
    weekly_time = datetime.fromtimestamp(last_weekly.stat().st_mtime).strftime("%Y-%m-%d %H:%M") if last_weekly else "Never"

    daily_count  = len(list(OUTPUT_DIR.glob("daily-*.md")))
    weekly_count = len(list(OUTPUT_DIR.glob("digest-*.md")))
    free_gb      = free_disk_gb()

    return f"""🖥 *CIRRUS Status*

*Last daily digest:* {daily_time}
*Last weekly digest:* {weekly_time}

*Files stored:* {weekly_count} weekly, {daily_count} daily
*Free disk:* {free_gb:.1f} GB
*Time on CIRRUS:* {datetime.now().strftime("%Y-%m-%d %H:%M")}

✅ CIRRUS is running"""

def cmd_disk():
    digest_gb  = folder_size_gb(OUTPUT_DIR)
    whisper_gb = folder_size_gb(WHISPER_CACHE)
    free_gb    = free_disk_gb()

    daily_count  = len(list(OUTPUT_DIR.glob("daily-*.md"))) if OUTPUT_DIR.exists() else 0
    weekly_count = len(list(OUTPUT_DIR.glob("digest-*.md"))) if OUTPUT_DIR.exists() else 0
    action_count = len(list(ACTIONS_DIR.glob("*.md"))) if ACTIONS_DIR.exists() else 0

    warnings = []
    if digest_gb > 1.0:
        warnings.append("⚠️ Digest folder exceeds 1GB")
    if whisper_gb > 5.0:
        warnings.append("⚠️ Whisper cache exceeds 5GB")
    if free_gb < 50.0:
        warnings.append("⚠️ Free disk below 50GB")

    status = f"""💾 *CIRRUS Disk Usage*

*Digest folder:* {digest_gb:.2f} GB
*Whisper cache:* {whisper_gb:.2f} GB
*Free disk:* {free_gb:.1f} GB

*Files:* {weekly_count} weekly, {daily_count} daily, {action_count} action files
"""
    if warnings:
        status += "\n" + "\n".join(warnings)
    else:
        status += "\n✅ All within normal limits"

    return status

def cmd_sources():
    web_sources = CONFIG.get("web_sources", [])
    podcasts    = CONFIG.get("podcasts", [])
    email_cfg   = CONFIG.get("email", {})

    medium_sources   = [s["name"] for s in web_sources if s["type"] == "medium"]
    substack_sources = [s["name"] for s in web_sources if s["type"] == "substack"]
    podcast_names    = [p["name"] for p in podcasts]
    email_senders    = email_cfg.get("senders", [])

    result = "📡 *CIRRUS Monitored Sources*\n\n"
    result += "*📰 Medium:*\n" + "\n".join(f"• {s}" for s in medium_sources) + "\n\n"
    result += "*📬 Substack:*\n" + "\n".join(f"• {s}" for s in substack_sources) + "\n\n"
    result += "*🎙 Podcasts:*\n" + "\n".join(f"• {s}" for s in podcast_names) + "\n\n"
    result += "*✉️ Email newsletters:*\n" + "\n".join(f"• {s}" for s in email_senders)
    return result

def cmd_latest():
    latest = find_latest("daily-*.md")
    if not latest:
        return "No daily digest found yet."
    content = latest.read_text()
    # Return first 3000 chars
    preview = content[:3000]
    if len(content) > 3000:
        preview += "\n\n_...truncated. Check your email for the full digest._"
    return f"📄 *{latest.name}*\n\n{preview}"

def cmd_actions():
    latest = find_latest_action("daily-actions")
    if not latest:
        latest = find_latest_action("weekly-actions")
    if not latest:
        return "No action items found yet."
    content = latest.read_text()
    preview = content[:3000]
    if len(content) > 3000:
        preview += "\n\n_...truncated. Check your email for the full list._"
    import re
    preview_clean = re.sub(r'\*+', '', preview)
    return f"📋 {latest.name}\n\n{preview_clean}"

def cmd_gitpull():
    try:
        result = subprocess.run(
            ["git", "pull"],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=30
        )
        output = result.stdout.strip() or result.stderr.strip()
        # Delay restart so response can be sent first
        subprocess.Popen(
            ["bash", "-c", "sleep 5 && launchctl stop com.cirrus.bot && sleep 2 && launchctl start com.cirrus.bot"]
        )
        return f"✅ Git pull complete:\n{output}\n\nBot restarting in 5 seconds..."
    except Exception as e:
        return f"❌ Git pull failed: {e}"

def cmd_run_daily(chat_id):
    send_message(chat_id, "⏳ Running daily digest now... This may take a few minutes.")
    try:
        result = subprocess.run(
            ["python3", str(PROJECT_DIR / "cirrus_daily.py")],
            capture_output=True, text=True, timeout=600
        )
        if result.returncode == 0:
            return "✅ Daily digest complete! Check your email."
        else:
            return f"❌ Daily digest failed:\n```{result.stderr[-500]}```"
    except subprocess.TimeoutExpired:
        return "⏱ Daily digest timed out after 10 minutes."
    except Exception as e:
        return f"❌ Error: {e}"

def cmd_run_weekly(chat_id):
    send_message(chat_id, "⏳ Running weekly digest now... This may take 30-60 minutes due to podcast transcription.")
    try:
        result = subprocess.run(
            ["python3", str(PROJECT_DIR / "cirrus_digest.py")],
            capture_output=True, text=True, timeout=3600
        )
        if result.returncode == 0:
            return "✅ Weekly digest complete! Check your email."
        else:
            return f"❌ Weekly digest failed:\n```{result.stderr[-500]}```"
    except subprocess.TimeoutExpired:
        return "⏱ Weekly digest timed out after 60 minutes."
    except Exception as e:
        return f"❌ Error: {e}"

# ── Approval System ───────────────────────────────────────────────────────────

PENDING_FILE = PROJECT_DIR / "config/pending_approvals.json"

def extract_recommendations(actions_file: Path) -> list:
    """Parse action items file and extract actionable recommendations."""
    content = actions_file.read_text()
    recommendations = []

    # Look for patterns indicating actionable items
    patterns = [
        (r"(?:pull|download|install|upgrade|switch to)\s+(qwen[\w\.:]+|llama[\w\.:]+|mistral[\w\.:]+|gemma[\w\.:]+)", "PULL_MODEL"),
        (r"(?:add|subscribe|monitor|follow)\s+(?:source|feed|newsletter|podcast)?[:\s]+([^\n]+)", "ADD_SOURCE"),
        (r"pip\s+install\s+([\w\-]+)", "INSTALL_PACKAGE"),
        (r"→\s*CIRRUS NOTE:\s*([^\n]+)", "CIRRUS_NOTE"),
    ]

    seen = set()
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        for pattern, action_type in patterns:
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                detail = match.group(1).strip()[:100]
                key = f"{action_type}:{detail}"
                if key not in seen:
                    seen.add(key)
                    recommendations.append({
                        "type": action_type,
                        "detail": detail,
                        "source_line": line[:120],
                        "status": "pending"
                    })
    return recommendations

def load_pending() -> list:
    if PENDING_FILE.exists():
        with open(PENDING_FILE) as f:
            return json.load(f)
    return []

def save_pending(items: list):
    with open(PENDING_FILE, "w") as f:
        json.dump(items, f, indent=2)

def execute_action(item: dict) -> str:
    """Execute an approved action on CIRRUS."""
    action_type = item["type"]
    detail = item["detail"]

    if action_type == "PULL_MODEL":
        log(f"Executing: ollama pull {detail}")
        result = subprocess.run(
            ["ollama", "pull", detail],
            capture_output=True, text=True, timeout=600
        )
        if result.returncode == 0:
            return f"✅ Model `{detail}` pulled successfully"
        else:
            return f"❌ Failed to pull `{detail}`: {result.stderr[:200]}"

    elif action_type == "INSTALL_PACKAGE":
        log(f"Executing: pip3 install {detail}")
        result = subprocess.run(
            ["pip3", "install", detail],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            return f"✅ Package `{detail}` installed successfully"
        else:
            return f"❌ Failed to install `{detail}`: {result.stderr[:200]}"

    elif action_type == "ADD_SOURCE":
        return f"ℹ️ Source suggestion noted: `{detail}`. Add manually via sources.json or ask CIRRUS to find the RSS feed."

    elif action_type == "CIRRUS_NOTE":
        return f"ℹ️ CIRRUS note logged: `{detail}`"

    return f"⚠️ Unknown action type: {action_type}"

def cmd_knowledge():
    """Show RAG knowledge base stats."""
    try:
        from cirrus_rag import kb_stats
        stats = kb_stats()
        return f"""🧠 *CIRRUS Knowledge Base*

*Total chunks indexed:* {stats['total_chunks']}
*Files indexed:* {stats['total_files']} ({stats['weekly_digests']} weekly, {stats['daily_digests']} daily)
*Vector dimensions:* {stats['vector_shape'][1] if len(stats['vector_shape']) > 1 else 0}
*Storage size:* {stats['kb_size_mb']} MB

CIRRUS uses this memory to connect dots across past digests when summarizing new content."""
    except Exception as e:
        return f"❌ Knowledge base error: {e}"

def cmd_ask(question: str) -> str:
    """Answer a question using RAG memory + Ollama."""
    try:
        from cirrus_rag import retrieve
        results = retrieve(question, top_k=3)
        if not results:
            return "No relevant past knowledge found for that question."

        context = "\n\n".join([f"[{r['date']}]: {r['text']}" for r in results])

        prompt = f"""You are CIRRUS, an AI assistant with memory of past digests.

Answer the following question using only the past digest knowledge provided below.
Be concise and specific. If the knowledge doesn't contain enough information, say so.

PAST KNOWLEDGE:
{context}

QUESTION: {question}

Answer:"""

        resp = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": MODEL, "prompt": prompt, "stream": False},
            timeout=120
        )
        resp.raise_for_status()
        answer = resp.json().get("response", "").strip()
        return f"🧠 *CIRRUS Memory Answer*\n\n{answer}\n\n_Sources: {', '.join(set(r['date'] for r in results))}_"
    except Exception as e:
        return f"❌ Error: {e}"

def cmd_approve(chat_id):
    """Scan latest action items and present pending recommendations."""
    # Load existing pending
    pending = load_pending()

    # Scan latest action files if no pending items
    if not pending:
        for prefix in ["daily-actions", "weekly-actions"]:
            latest = find_latest_action(prefix)
            if latest:
                new_items = extract_recommendations(latest)
                pending.extend(new_items)

    pending = [p for p in pending if p["status"] == "pending"]

    if not pending:
        return "✅ No pending recommendations to review."

    save_pending(pending)

    msg = f"📋 *{len(pending)} Pending Recommendations*\n\nReply with the number to approve, or `reject N` to reject:\n\n"
    for i, item in enumerate(pending, 1):
        emoji = {"PULL_MODEL": "🤖", "INSTALL_PACKAGE": "📦", "ADD_SOURCE": "📡", "CIRRUS_NOTE": "💡"}.get(item["type"], "•")
        msg += f"{emoji} *{i}. {item['type']}*\n`{item['detail']}`\n\n"

    msg += "_Reply: `approve 1` or `reject 2` or `approve all`_"
    return msg

def handle_approval_reply(text: str, chat_id: str) -> str:
    """Handle approve/reject replies."""
    pending = load_pending()
    active = [p for p in pending if p["status"] == "pending"]

    if not active:
        return "No pending items to approve."

    text = text.strip().lower()

    if text == "approve all":
        results = []
        for item in active:
            item["status"] = "approved"
            result = execute_action(item)
            results.append(result)
        save_pending(pending)
        return "Results:\n" + "\n".join(results)

    match = re.match(r"(approve|reject)\s+(\d+)", text)
    if match:
        action = match.group(1)
        idx = int(match.group(2)) - 1
        if 0 <= idx < len(active):
            item = active[idx]
            item["status"] = action + "d"
            save_pending(pending)
            if action == "approve":
                return execute_action(item)
            else:
                return f"❌ Rejected: `{item['detail']}`"
        else:
            return f"Invalid number. Choose 1-{len(active)}."

    return "Use: `approve 1`, `reject 2`, or `approve all`"

# ── Bot Loop ──────────────────────────────────────────────────────────────────

def handle_message(message, chat_id):
    text = message.get("text", "").strip()
    cmd  = text.lower().split()[0].split("@")[0] if text else ""

    if cmd == "/help":
        return cmd_help()
    elif cmd == "/status":
        return cmd_status()
    elif cmd == "/disk":
        return cmd_disk()
    elif cmd == "/sources":
        return cmd_sources()
    elif cmd == "/latest":
        return cmd_latest()
    elif cmd == "/actions":
        return cmd_actions()
    elif cmd == "/daily":
        return cmd_run_daily(chat_id)
    elif cmd == "/weekly":
        return cmd_run_weekly(chat_id)
    elif cmd == "/approve":
        return cmd_approve(chat_id)
    elif cmd == "/knowledge":
        return cmd_knowledge()
    elif cmd == "/ask":
        question = " ".join(text.split()[1:])
        if not question:
            return "Usage: /ask <your question>"
        return cmd_ask(question)
    elif cmd == "/gitpull":
        return cmd_gitpull()
    elif cmd in ("approve", "reject") or text.lower() == "approve all":
        return handle_approval_reply(text, chat_id)
    else:
        return "Unknown command. Type /help to see available commands."

def run_bot():
    log("=== CIRRUS Telegram Bot Starting ===")
    log(f"Allowed user ID: {ALLOWED_ID}")

    offset = 0
    while True:
        try:
            result = api_call("getUpdates", {"offset": offset, "timeout": 30})
            updates = result.get("result", [])

            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message", {})
                if not message:
                    continue

                chat_id = message.get("chat", {}).get("id")
                user_id = message.get("from", {}).get("id")
                text    = message.get("text", "")

                # Security check — only respond to Buddy
                if user_id != ALLOWED_ID:
                    log(f"Ignored message from unauthorized user: {user_id}")
                    continue

                log(f"Command from {user_id}: {text}")
                response = handle_message(message, chat_id)
                send_message(chat_id, response)

        except KeyboardInterrupt:
            log("Bot stopped.")
            break
        except Exception as e:
            log(f"Bot error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    run_bot()
