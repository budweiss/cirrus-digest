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
  /omit       - add a sender to the email omit list
  /omitlist   - show the current email omit list
"""

import warnings
warnings.filterwarnings("ignore", category=Warning, module="urllib3")

import json
import os
import re
import requests
import subprocess
import threading
import shutil
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
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
PROPOSALS_DIR = OUTPUT_DIR / "proposals"
WHISPER_CACHE = Path.home() / ".cache" / "whisper"
PROJECT_DIR   = Path.home() / "projects/cirrus-digest"
EMAIL_OMIT_PATH = PROJECT_DIR / "config/email_omit.txt"

BOT_TOKEN     = CREDS["telegram_bot_token"]
ALLOWED_ID    = int(CREDS["telegram_user_id"])
API_URL       = f"https://api.telegram.org/bot{BOT_TOKEN}"
OLLAMA_HOST   = DIGEST_CFG["ollama_host"]
MODEL         = DIGEST_CFG["ollama_model"]

# ── External LLM fallback (added 2026-06-14) ────────────────────────────────
# When the local qwen2.5:72b model is uncertain or unavailable for /ask, fall
# back to a hosted API in this order: Gemini -> Grok -> Claude. Any key left
# blank/missing in credentials.json simply disables that tier (no error).
# Model names are overridable via credentials.json since provider model
# names/availability change over time - verify these are still current.
GEMINI_API_KEY    = CREDS.get("gemini_api_key", "")
GROK_API_KEY      = CREDS.get("grok_api_key", "")
ANTHROPIC_API_KEY = CREDS.get("anthropic_api_key", "")

GEMINI_MODEL = CREDS.get("gemini_model", "gemini-2.0-flash")
GROK_MODEL   = CREDS.get("grok_model", "grok-3-mini")
CLAUDE_MODEL = CREDS.get("claude_model", "claude-haiku-4-5-20251001")

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
    # Socket timeout must EXCEED any Telegram long-poll timeout param,
    # otherwise every quiet getUpdates poll (timeout=30) hits the 30s socket
    # limit and logs "read operation timed out" — the cause of the constant
    # timeout spam in bot.log.
    poll_timeout = int(params.get("timeout", 0)) if params else 0
    try:
        with urllib.request.urlopen(req, timeout=max(30, poll_timeout + 15)) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # Return the error body so callers can inspect ok/error_code
        log(f"API error ({method}): {e}")
        try:
            return json.loads(e.read())
        except Exception:
            return {"ok": False, "error_code": e.code}
    except Exception as e:
        log(f"API error ({method}): {e}")
        return {}

def send_message(chat_id, text):
    # Split long messages into chunks of 4000 chars
    max_len = 4000
    for i in range(0, len(text), max_len):
        chunk = text[i:i+max_len]
        result = api_call("sendMessage", {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "Markdown"
        })
        # Telegram rejects messages with unmatched/invalid markdown (400 error).
        # Retry as plain text so tool output with tabs, dashes, etc. always sends.
        if not result.get("ok"):
            api_call("sendMessage", {
                "chat_id": chat_id,
                "text": chunk,
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

# ── External LLM fallback helpers ───────────────────────────────────────────

UNCERTAIN_PATTERNS = [
    r"\bdon'?t know\b",
    r"\bdo not know\b",
    r"\bnot sure\b",
    r"\bcannot determine\b",
    r"\bunable to determine\b",
    r"\bdon'?t have (?:access|enough)\b",
    r"\bdo not have (?:access|enough)\b",
    # Catches "does/doesn't/don't ... contain/have ... information/knowledge",
    # regardless of what comes between (e.g. "contain information about X",
    # "contain enough information", "have any information").
    r"\b(?:does not|doesn'?t|do not|don'?t)\b[^.\n]{0,40}\b(?:contain|have)\b[^.\n]{0,40}\b(?:information|knowledge|data|details)\b",
    r"\bno (?:relevant )?(?:past )?(?:information|knowledge|data)\b",
    r"\bnot enough context\b",
]

UNCERTAIN_RE = re.compile("|".join(UNCERTAIN_PATTERNS), re.IGNORECASE)

# Matches a leading sentence where the model comments on the provided context
# rather than answering — e.g. "The provided context does not contain
# information about X." followed by the real answer. Stripped before
# is_uncertain() so a correct answer isn't rejected due to the preamble.
CONTEXT_DISCLAIMER_RE = re.compile(
    r'^[^.!?]*\b(?:provided context|background context|context (?:provided|given|above))\b'
    r'[^.!?]*[.!?]\s*',
    re.IGNORECASE
)

def is_uncertain(answer: str) -> bool:
    """True if `answer` is empty/too short or hedges in a way that suggests
    the local model couldn't really answer the question."""
    if not answer or len(answer.strip()) < 10:
        return True
    return bool(UNCERTAIN_RE.search(answer))

def call_gemini(prompt: str, timeout: int = 60):
    if not GEMINI_API_KEY:
        return None
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
    # google_search grounding gives Gemini live web access (same as the
    # iPhone Gemini app) — required for real-time questions like sports
    # scores, current news, etc. Works with gemini-2.x models.
    resp = requests.post(url, json={
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
    }, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    # Gemini splits responses containing code blocks into MULTIPLE parts.
    # Taking only parts[0] silently truncated every proposal at its first
    # code fence (the cause of proposals missing their sketch + Risks
    # sections). Join ALL text parts.
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()

def call_grok(prompt: str, timeout: int = 60):
    if not GROK_API_KEY:
        return None
    resp = requests.post(
        "https://api.x.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROK_API_KEY}"},
        json={"model": GROK_MODEL, "messages": [{"role": "user", "content": prompt}]},
        timeout=timeout
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()

def call_claude(prompt: str, timeout: int = 60):
    if not ANTHROPIC_API_KEY:
        return None
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={"model": CLAUDE_MODEL, "max_tokens": 1024,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=timeout
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"].strip()

FALLBACK_CHAIN = [
    ("Gemini", call_gemini),
    ("Grok", call_grok),
    ("Claude", call_claude),
]

def ask_with_fallback(prompt: str):
    """Try each configured external LLM in order. Returns (answer, name) for
    the first one that responds with a non-empty, non-hedging answer, or
    (None, None) if none are configured or all fail."""
    for name, fn in FALLBACK_CHAIN:
        try:
            answer = fn(prompt)
        except Exception as e:
            log(f"Fallback {name} error: {e}")
            continue
        if answer:
            # Strip any leading context-disclaimer sentence before checking
            # uncertainty — Gemini sometimes prefixes a correct answer with
            # "The provided context does not contain information about X."
            # which would otherwise trip is_uncertain() and discard the answer.
            cleaned = CONTEXT_DISCLAIMER_RE.sub("", answer).strip()
            if cleaned and not is_uncertain(cleaned):
                return cleaned, name
        log(f"Fallback {name} answer rejected (empty or uncertain): {answer!r}")
    return None, None

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
/approve — review and approve pending recommendations
/proposals — list generated implementation proposals
/knowledge — show RAG knowledge base stats
/ask <question> — ask CIRRUS a question using past digest memory (falls back to Gemini/Grok/Claude if the local model is unsure)
/research <topic> — search the web, fetch ~5 sources, and reply with a research brief (runs in background)
/todo <text> — add a new item to the work queue (shows up in /approve)
/detail <keyword> :: <context> — add more detail to an existing pending item
/pullmodel <name> — pull an Ollama model on CIRRUS (runs in background, notifies when done)
/omit <sender> — skip future emails from this sender/address
/omitlist — show the current email omit list
/help — show this message

*Approval replies:*
`approve 1` — approve item 1
`reject 2` — reject item 2
`approve all` — approve everything
`reject all` — reject everything"""

STALE_PROPOSAL_DAYS = 7

def stale_proposal_count(days=STALE_PROPOSAL_DAYS) -> int:
    """Count proposal-*.md files older than `days` whose checklist still
    has 'Reviewed by Buddy' unchecked."""
    if not PROPOSALS_DIR.exists():
        return 0
    cutoff = datetime.now() - timedelta(days=days)
    count = 0
    for f in PROPOSALS_DIR.glob("proposal-*.md"):
        try:
            date_str = "-".join(f.stem.split("-")[1:4])  # YYYY-MM-DD
            file_date = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        if file_date < cutoff:
            text = f.read_text()
            if "- [ ] Reviewed by Buddy" in text:
                count += 1
    return count

def cmd_status():
    # Last digest files
    last_daily  = find_latest("daily-*.md")
    last_weekly = find_latest("digest-*.md")

    daily_time  = datetime.fromtimestamp(last_daily.stat().st_mtime).strftime("%Y-%m-%d %H:%M") if last_daily else "Never"
    weekly_time = datetime.fromtimestamp(last_weekly.stat().st_mtime).strftime("%Y-%m-%d %H:%M") if last_weekly else "Never"

    daily_count  = len(list(OUTPUT_DIR.glob("daily-*.md")))
    weekly_count = len(list(OUTPUT_DIR.glob("digest-*.md")))
    free_gb      = free_disk_gb()
    stale        = stale_proposal_count()

    status_text = f"""🖥 *CIRRUS Status*

*Last daily digest:* {daily_time}
*Last weekly digest:* {weekly_time}

*Files stored:* {weekly_count} weekly, {daily_count} daily
*Free disk:* {free_gb:.1f} GB
*Time on CIRRUS:* {datetime.now().strftime("%Y-%m-%d %H:%M")}

✅ CIRRUS is running"""

    if stale:
        status_text += f"\n\n📋 {stale} proposal(s) pending review for " \
                        f"{STALE_PROPOSAL_DAYS}+ days — check /proposals"

    return status_text

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

def cmd_todo(text: str) -> str:
    """Add a new CIRRUS_NOTE item to the pending approvals queue manually.
    Usage: /todo <description of the work item>
    """
    text = text.strip()
    if not text:
        return "Usage: /todo <description>\nExample: /todo Add dark mode to digest emails"

    from datetime import date
    items = load_pending()
    new_item = {
        "type": "CIRRUS_NOTE",
        "detail": text,
        "source": f"manual:/todo ({date.today()})",
        "status": "pending"
    }
    items.append(new_item)
    save_pending(items)
    log(f"/todo added: {text[:80]}")
    return f"✅ Added to work queue:\n_{text}_\n\nView with /approve."


def cmd_detail(arg: str) -> str:
    """Add more context to an existing pending item by keyword match.
    Usage: /detail <keyword> :: <extra context>
    Example: /detail reference extraction :: only happens on articles > 500 words
    """
    if "::" not in arg:
        return (
            "Usage: /detail <keyword> :: <extra context>\n"
            "Example: /detail cookie sync :: medium.com needs uid and sid cookies\n\n"
            "The keyword is matched against existing pending items."
        )

    keyword, extra = arg.split("::", 1)
    keyword = keyword.strip().lower()
    extra = extra.strip()

    if not keyword or not extra:
        return "Both keyword and extra context are required."

    items = load_pending()
    matched = [
        i for i, item in enumerate(items)
        if item.get("status") == "pending"
        and keyword in item.get("detail", "").lower()
    ]

    if not matched:
        return (
            f"❌ No pending item found matching: _{keyword}_\n"
            "Check spelling or use /approve to see current items."
        )

    if len(matched) > 3:
        previews = "\n".join(
            f"• {items[i]['detail'][:80]}" for i in matched[:5]
        )
        return (
            f"⚠️ Found {len(matched)} matching items — be more specific:\n{previews}"
        )

    updated = []
    for i in matched:
        original = items[i]["detail"]
        items[i]["detail"] = f"{original} — {extra}"
        updated.append(items[i]["detail"][:100])

    save_pending(items)
    log(f"/detail updated {len(matched)} item(s) matching '{keyword}'")

    result = "\n".join(f"• _{d}_" for d in updated)
    return f"✅ Updated {len(matched)} item(s):\n{result}"


def cmd_omit(arg: str) -> str:
    """Add a sender (address or substring) to config/email_omit.txt.

    cirrus_daily.py re-reads this file from scratch on every run (via
    load_omit_senders()), so no restart is needed — the next daily run will
    pick up the new entry.
    """
    sender = arg.strip()
    if not sender:
        return ("Usage: `/omit <sender email or substring>`\n\n"
                "Example: `/omit newsletter@spammydomain.com`")

    existing = []
    if EMAIL_OMIT_PATH.exists():
        existing = [l.strip() for l in EMAIL_OMIT_PATH.read_text().splitlines()]

    active = [l.lower() for l in existing if l and not l.startswith("#")]
    if sender.lower() in active:
        return f"`{sender}` is already on the omit list."

    EMAIL_OMIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(EMAIL_OMIT_PATH, "a") as f:
        # Ensure the new entry starts on its own line even if the file
        # doesn't currently end with a newline.
        if existing and existing[-1] != "":
            f.write("\n")
        f.write(f"{sender}\n")

    log(f"Added to email omit list: {sender}")
    return (f"🚫 Added `{sender}` to the email omit list.\n\n"
            f"Future emails whose From header contains this will be skipped "
            f"starting with tomorrow's daily run (or run /daily now to apply immediately).")

def cmd_omitlist() -> str:
    """Show the current contents of config/email_omit.txt (entries only)."""
    if not EMAIL_OMIT_PATH.exists():
        return "Email omit list is empty."

    entries = [l.strip() for l in EMAIL_OMIT_PATH.read_text().splitlines()
               if l.strip() and not l.strip().startswith("#")]

    if not entries:
        return "Email omit list is empty."

    msg = f"🚫 *Email Omit List* ({len(entries)})\n\n"
    msg += "\n".join(f"• `{e}`" for e in entries)
    msg += "\n\n_Add more with `/omit <sender>`._"
    return msg

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
    return f"📋 *{latest.name}*\n\n{preview}"

def cmd_proposals():
    if not PROPOSALS_DIR.exists():
        return "No proposals generated yet."
    files = sorted(PROPOSALS_DIR.glob("proposal-*.md"), reverse=True)
    if not files:
        return "No proposals generated yet."

    pending, done = [], []
    for f in files:
        content = f.read_text()
        if ("[x] Rejected" in content or
                "[x] Implemented and deployed" in content):
            done.append(f)
        else:
            pending.append(f)

    if not pending:
        return (f"✅ No open proposals — all {len(done)} proposal(s) are "
                f"implemented or rejected.")

    msg = f"📝 *{len(pending)} Open Proposal(s)*"
    if done:
        msg += f" _(+{len(done)} closed)_"
    msg += "\n\n"
    for f in pending[:10]:
        content = f.read_text()
        status_match = re.search(r"\*\*Status:\*\*\s*(.+)", content)
        status = status_match.group(1).strip() if status_match else "unknown"
        title_match = re.search(r"^#\s*Proposal:\s*(.+)", content, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else f.stem
        msg += f"• `{f.name}` — _{status}_\n  {title[:90]}\n\n"
    if len(pending) > 10:
        msg += f"_...and {len(pending) - 10} more. Check digests/proposals/ on CIRRUS._"
    else:
        msg += "_Review these with Claude in your next Cowork session._"
    return msg

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

def _pull_model_background(model: str, chat_id: int):
    """Run ollama pull in background and notify when done."""
    log(f"Starting ollama pull: {model}")
    try:
        result = subprocess.run(
            ["ollama", "pull", model],
            capture_output=True, text=True, timeout=3600
        )
        if result.returncode == 0:
            send_message(chat_id, f"✅ Model pulled successfully: `{model}`")
            log(f"ollama pull complete: {model}")
        else:
            err = result.stderr[-300:] if result.stderr else "unknown error"
            send_message(chat_id, f"❌ Pull failed for `{model}`:\n```{err}```")
            log(f"ollama pull failed: {model} — {err}")
    except subprocess.TimeoutExpired:
        send_message(chat_id, f"⏱ Pull timed out after 60 minutes for `{model}`.")
        log(f"ollama pull timed out: {model}")
    except Exception as e:
        send_message(chat_id, f"❌ Pull error for `{model}`: {e}")
        log(f"ollama pull error: {model} — {e}")

def cmd_pullmodel(model: str, chat_id: int):
    if not model:
        return "Usage: /pullmodel <model>\nExample: `/pullmodel llama3.3:70b`\n\nSee available models at ollama.com/library"
    # Basic safety check — only allow word chars, colon, dot, hyphen
    if not re.match(r'^[\w\.\-:]+$', model):
        return f"❌ Invalid model name: `{model}`"
    send_message(chat_id,
        f"⏳ Starting pull for `{model}`...\n"
        f"This runs in the background — I'll notify you when it's done.\n"
        f"Large models (70b+) may take 20-40 minutes.")
    t = threading.Thread(target=_pull_model_background, args=(model, chat_id), daemon=True)
    t.start()
    return f"🔄 Pull started for `{model}`. You'll get a notification when it completes."

# ── Research Command ──────────────────────────────────────────────────────────

RESEARCH_DIR = OUTPUT_DIR / "research"

def _research_background(topic: str, chat_id: int):
    """Run a web research task in the background and reply when done.

    Reuses search_web / fetch_article_content from cirrus_daily.py, so it
    gets cookie injection (Medium/Substack access) and paywall logging
    automatically. Fetches up to 5 readable sources, summarizes with
    Gemini (fallback: local Ollama), saves a research file, replies in
    Telegram with the findings and the list of URLs visited.
    """
    try:
        import sys as _sys
        if str(PROJECT_DIR) not in _sys.path:
            _sys.path.insert(0, str(PROJECT_DIR))
        from cirrus_daily import search_web, fetch_article_content, is_article_url

        log(f"/research started: {topic}")
        urls = search_web(topic, max_results=8)
        if not urls:
            send_message(chat_id, f"❌ Research: web search returned no results for _{topic}_")
            return

        fetched, paywalled_urls = [], []
        for url in urls:
            if not is_article_url(url):
                continue
            content, paywalled = fetch_article_content(url)
            if paywalled:
                paywalled_urls.append(url)
            if len(content) > 300:
                fetched.append((url, content[:4000]))
            if len(fetched) >= 5:
                break

        if not fetched:
            send_message(chat_id,
                f"❌ Research: no readable sources found for _{topic}_.\n"
                + (f"🔒 {len(paywalled_urls)} hit paywalls." if paywalled_urls else ""))
            return

        sources_block = "\n\n".join(
            f"--- SOURCE {i}: {u} ---\n{c}" for i, (u, c) in enumerate(fetched, 1)
        )
        prompt = f"""You are CIRRUS, researching a topic requested by Buddy.

TOPIC: {topic}

Below are {len(fetched)} web sources fetched just now. Write a research brief with:
1. **Key Findings** — the most important specific facts, tools, models, or developments (cite sources as [1], [2], ...)
2. **Differing Views** — where sources disagree or emphasize different angles (skip if none)
3. **Actionable Takeaways** — 2-3 concrete next steps or things worth trying

Be specific — name tools, models, versions, numbers. Do not pad.

{sources_block}

Write the research brief now:"""

        body = None
        try:
            body = call_gemini(prompt, timeout=120)
            if body:
                log("/research summarized via Gemini")
        except Exception as e:
            log(f"/research Gemini error, falling back to Ollama: {e}")
        if not body:
            resp = requests.post(
                f"{OLLAMA_HOST}/api/generate",
                json={"model": MODEL, "prompt": prompt, "stream": False,
                      "options": {"num_ctx": 16384}},
                timeout=600
            )
            resp.raise_for_status()
            body = resp.json().get("response", "").strip()

        # Save research file
        RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r'[^\w]+', '-', topic.lower()).strip('-')[:40]
        ts = datetime.now().strftime("%Y-%m-%d-%H%M")
        path = RESEARCH_DIR / f"research-{ts}-{slug}.md"
        sources_list = "\n".join(f"{i}. {u}" for i, (u, _) in enumerate(fetched, 1))
        path.write_text(
            f"# Research: {topic}\n\n**Date:** {ts}\n\n{body}\n\n"
            f"---\n\n## Sources Visited\n\n{sources_list}\n"
            + (f"\n## Paywalled (no access)\n\n"
               + "\n".join(f"- {u}" for u in paywalled_urls) + "\n"
               if paywalled_urls else "")
        )

        reply = f"🔎 *Research: {topic}*\n\n{body}\n\n*Sources:*\n{sources_list}"
        if paywalled_urls:
            reply += f"\n\n🔒 {len(paywalled_urls)} source(s) hit paywalls (logged)."
        reply += f"\n\n_Saved: {path.name}_"
        send_message(chat_id, reply)
        log(f"/research complete: {path.name}")
    except Exception as e:
        log(f"/research error: {e}")
        send_message(chat_id, f"❌ Research error: {e}")

def cmd_research(topic: str, chat_id: int) -> str:
    if not topic.strip():
        return ("Usage: /research <topic>\n"
                "Example: `/research best local coding models under 32B in 2026`")
    t = threading.Thread(target=_research_background,
                         args=(topic.strip(), chat_id), daemon=True)
    t.start()
    return (f"🔎 Researching: _{topic.strip()}_\n"
            f"Searching the web, fetching ~5 sources, summarizing... "
            f"I'll reply here when done (usually 2-5 minutes).")

# ── Approval System ───────────────────────────────────────────────────────────

PENDING_FILE = PROJECT_DIR / "config/pending_approvals.json"

def extract_recommendations(actions_file: Path) -> list:
    """Parse action items file and extract actionable recommendations.

    Each item carries `source` (the actions file it came from, which names
    the digest date) and `added` (date extracted) so /approve can show
    where and when every recommendation originated.
    """
    content = actions_file.read_text()
    recommendations = []
    added_date = datetime.now().strftime("%Y-%m-%d")

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
                        "source": actions_file.name,
                        "added": added_date,
                        "status": "pending"
                    })

    # Also pick up self-improvement style suggestions from the CIRRUS notes
    # section. extract_actions.py asks the LLM to write this section but the
    # exact heading varies ("CIRRUS Improvement Notes", "Cirrus Notes", etc.)
    # and the "→ CIRRUS NOTE:" prefix is rarely reproduced verbatim.
    # Match any section whose heading contains "cirrus" OR "improvement" OR
    # "action" (but NOT "recommendations" alone — those are general reading
    # takeaways that flooded /approve with low-relevance items).
    # Pick up bullet lines under those sections as CIRRUS_NOTE items.
    CIRRUS_SECTIONS = re.compile(
        r'\b(cirrus|improvement|action\s*item)\b', re.IGNORECASE
    )
    SKIP_SECTIONS = re.compile(r'^recommendations?$', re.IGNORECASE)

    current_section = None
    for line in content.split("\n"):
        stripped = line.strip()
        if re.match(r'^#{1,3}\s+', stripped):
            heading = re.sub(r'^#{1,3}\s+', '', stripped).strip()
            if CIRRUS_SECTIONS.search(heading) and not SKIP_SECTIONS.match(heading):
                current_section = "CIRRUS"
            else:
                current_section = None
            continue
        if current_section == "CIRRUS" and stripped.startswith("- "):
            # Handle bold-label bullets:  - **Label**: rest text
            # and plain bullets:          - Plain text here
            # Skip "Source:" citation lines.
            bold_match = re.match(r"-\s*\*\*(.+?)\*\*:?\s*(.*)", stripped)
            if bold_match:
                label = bold_match.group(1).strip()
                rest = bold_match.group(2).strip()
                if label.lower() == "source":
                    continue  # citation, not a recommendation
                if label.lower() in ("suggestion", "note", "task", "description"):
                    detail = rest[:150]
                else:
                    detail = (label + (" — " + rest if rest else ""))[:150]
            else:
                # Plain bullet — strip the leading "- " and any trailing source ref
                detail = re.sub(r'\s*\(Source:.*?\)\s*$', '', stripped[2:]).strip()[:150]
                # Skip source-attribution lines: "Source: X" or "*Source*: X"
                if re.match(r'\*?source\*?\s*:', detail, re.IGNORECASE):
                    continue

            if not detail:
                continue
            key = f"CIRRUS_NOTE:{detail}"
            if key not in seen:
                seen.add(key)
                recommendations.append({
                    "type": "CIRRUS_NOTE",
                    "detail": detail,
                    "source_line": stripped[:160],
                    "source": actions_file.name,
                    "added": added_date,
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

# ── Proposal generation (3b) ────────────────────────────────────────────────

PROJECT_CONTEXT = """CIRRUS is an AI digest system running on a Mac Studio M4 Max (macOS Tahoe).
Project directory: ~/projects/cirrus-digest/

Key components:
- cirrus_digest.py: fetches RSS/web/podcast sources, summarizes with Ollama (qwen2.5:72b),
  writes digests/daily-YYYY-MM-DD.md and digests/weekly-*.md
- extract_actions.py: parses digests into digests/actions/daily-actions-*.md /
  weekly-actions-*.md with sections: ACTION ITEMS, RECOMMENDATIONS,
  CIRRUS IMPROVEMENT NOTES, INTERESTING TOOLS/MODELS, FOLLOW-UP READING
- cirrus_bot.py: Telegram bot for remote control (/status, /disk, /daily, /weekly,
  /sources, /latest, /actions, /approve, /knowledge, /ask)
- cirrus_rag.py: RAG knowledge base over past digests, used by /ask
- config/sources.json (RSS/podcast sources), config/credentials.json (secrets,
  not in git), config/pending_approvals.json (approval queue)
- launchd jobs: com.cirrus.bot, com.cirrus.daily (7am), com.cirrus.digest
  (Sunday 7am weekly), com.cirrus.api (Flask API on port 5001)
- All code lives in git (budweiss/cirrus-digest, gitleaks pre-commit hook).
  Deploy flow: edit -> scp to CIRRUS -> git add/commit/push -> launchctl
  kickstart -k gui/$(id -u)/<job-label> to restart the relevant service.
"""

CONVENTIONS_PATH = PROJECT_DIR / "CIRRUS-CONVENTIONS.md"

def load_conventions() -> str:
    """Load CIRRUS-CONVENTIONS.md (ground-truth architecture facts) so
    proposal generation is grounded in the real codebase instead of
    generic patterns. Returns "" if the file isn't present."""
    try:
        return CONVENTIONS_PATH.read_text().strip()
    except Exception:
        return ""

def next_proposal_path() -> Path:
    """Return the next proposal-YYYY-MM-DD-N.md path for today."""
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    existing = sorted(PROPOSALS_DIR.glob(f"proposal-{today}-*.md"))
    n = len(existing) + 1
    return PROPOSALS_DIR / f"proposal-{today}-{n}.md"

def generate_proposal(item: dict) -> Path:
    """Ask the local LLM to draft a scoped implementation proposal for an
    approved CIRRUS_NOTE recommendation, and save it for human review.
    Does NOT modify or deploy any code itself."""
    detail = item["detail"]
    source_line = item.get("source_line", "")
    origin = item.get("source", "")
    added = item.get("added", "")

    conventions = load_conventions()
    conventions_block = (
        f"\nThe following conventions document is ground truth for this "
        f"project — any proposal that contradicts it (wrong framework, "
        f"wrong scheduling mechanism, etc.) is not a fit:\n\n{conventions}\n"
        if conventions else ""
    )

    prompt = f"""{PROJECT_CONTEXT}
{conventions_block}
A self-improvement recommendation was extracted from a recent digest and approved by Buddy for further consideration:

RECOMMENDATION: {detail}
SOURCE CONTEXT: {source_line}
ORIGIN: {origin or "unknown"} (extracted {added or "date unknown"})

Write a concrete, scoped implementation proposal for applying this recommendation to the CIRRUS project above. Be specific:
1. Which file(s) would need to change
2. What the change would do, in plain terms
3. A rough sketch of the code/config change (pseudocode or a short snippet is fine — this is a proposal, not final code)
4. Risks or things to verify before deploying

Hard requirements:
- COMPLETE all three sections — never stop after a heading. The code sketch and Risks section are mandatory.
- Strictly follow the conventions document above: correct file paths, no new frameworks, no new scheduling mechanisms, prefer small additive changes.
- BEFORE proposing to add a source, check whether it already appears in the conventions/source list — if it may already be monitored, say so and propose verification instead.
- If the recommendation duplicates an obvious existing capability, recommend rejection.

If the recommendation is too vague, too broad, or not realistically actionable for this specific project, say so honestly instead of inventing a change — a "no good fit yet" verdict is a valid and useful proposal.

Respond in markdown with these headings: ## Analysis, ## Proposed Change, ## Risks / Things to Verify"""

    # Try Gemini first (faster, higher quality); fall back to local Ollama.
    body = None
    try:
        body = call_gemini(prompt, timeout=120)
        if body:
            log("Proposal generated via Gemini")
    except Exception as e:
        log(f"Gemini proposal error, falling back to Ollama: {e}")
    if not body:
        try:
            resp = requests.post(
                f"{OLLAMA_HOST}/api/generate",
                json={"model": MODEL, "prompt": prompt, "stream": False,
                      "options": {"num_ctx": 8192}},
                timeout=300
            )
            resp.raise_for_status()
            body = resp.json().get("response", "").strip()
            if body:
                log("Proposal generated via Ollama (Gemini fallback)")
        except Exception as e:
            body = f"_(LLM error generating proposal — fill in manually: {e})_"

    today = datetime.now().strftime("%Y-%m-%d")
    path = next_proposal_path()
    content = f"""# Proposal: {detail[:80]}

**Date generated:** {today}
**Status:** pending review
**Source recommendation:** {detail}
**Source context:** {source_line}

---

{body}

---

## Review Checklist
- [ ] Reviewed by Buddy
- [ ] Reviewed by Claude (Cowork)
- [ ] Implemented and deployed
- [ ] Rejected (not a good fit)
"""
    path.write_text(content)
    log(f"Generated proposal: {path.name}")
    # Notify Buddy in Telegram so proposals don't pile up unnoticed between
    # Cowork sessions. Sends directly to ALLOWED_ID (Buddy's private chat).
    try:
        send_message(ALLOWED_ID,
            f"📝 *New proposal generated:* `{path.name}`\n\n"
            f"_{detail[:150]}_\n\n"
            f"Review with `/proposals` or in your next Cowork session.")
    except Exception as e:
        log(f"Proposal notification error: {e}")
    return path

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
        try:
            path = generate_proposal(item)
            return (f"📝 Proposal drafted: `{path.name}`\n"
                    f"Saved to `digests/proposals/` — review with Claude next Cowork session. "
                    f"No code was changed.")
        except Exception as e:
            return f"⚠️ CIRRUS note logged, but proposal generation failed: {e}"

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
    """Answer a question using the agent tool loop (if Claude API key present)
    or RAG memory + Ollama with external fallback chain.

    Priority:
    1. Claude API tool loop  — can call live system tools (speedtest, disk, etc.)
    2. RAG + local Ollama    — answers from past digest knowledge
    3. External fallback     — Gemini → Grok → Claude for general knowledge
    """
    try:
        # ── 1. Claude API tool loop ───────────────────────────────────────────
        # Load RAG context first — useful even for tool-loop questions
        from cirrus_rag import retrieve
        results = retrieve(question, top_k=3)
        context = "\n\n".join([f"[{r['date']}]: {r['text']}" for r in results]) if results else ""
        sources_note = f"\n\n_Sources: {', '.join(sorted(set(r['date'] for r in results)))}_" if results else ""

        try:
            import sys as _sys
            _tools_dir = str(Path.home() / "projects/cirrus-digest/tools")
            if _tools_dir not in _sys.path:
                _sys.path.insert(0, _tools_dir)
            from registry import ask_with_tools, CLAUDE_API_KEY
            if CLAUDE_API_KEY:
                tool_answer, tool_model = ask_with_tools(question, context=context)
                if tool_answer and not is_uncertain(tool_answer):
                    log(f"/ask answered via Claude tool loop ({tool_model})")
                    return (
                        f"🛠️ *CIRRUS Agent Answer*\n\n{tool_answer}"
                        f"{sources_note}"
                    )
                elif tool_answer:
                    log(f"/ask tool loop uncertain, falling through to Gemini: {tool_answer[:80]!r}")
        except Exception as e:
            log(f"/ask tool loop unavailable: {e}")

        # ── 2. RAG + local Ollama ─────────────────────────────────────────────
        prompt = f"""You are CIRRUS, an AI assistant with memory of past digests.

Answer the following question using only the past digest knowledge provided below.
Be concise and specific. If the knowledge doesn't contain enough information, say so.

PAST KNOWLEDGE:
{context if context else "(no relevant past knowledge found)"}

QUESTION: {question}

Answer:"""

        answer = ""
        try:
            resp = requests.post(
                f"{OLLAMA_HOST}/api/generate",
                json={"model": MODEL, "prompt": prompt, "stream": False,
                      "options": {"num_ctx": 8192}},
                timeout=120
            )
            resp.raise_for_status()
            answer = resp.json().get("response", "").strip()
        except Exception as e:
            log(f"Ollama /ask error: {e}")

        if not is_uncertain(answer):
            return f"🧠 *CIRRUS Memory Answer*\n\n{answer}{sources_note}"

        # ── 3. External fallback chain ────────────────────────────────────────
        fallback_prompt = (
            f"Background context from past digests (may or may not be relevant "
            f"to the question):\n\n{context}\n\n"
            f"Question: {question}\n\n"
            f"Answer the question directly and concisely using your own knowledge. "
            f"Do not mention or comment on the background context, or whether it "
            f"is relevant or sufficient - just answer the question."
            if context else
            f"Question: {question}\n\nAnswer directly and concisely using your own knowledge."
        )
        fb_answer, fb_name = ask_with_fallback(fallback_prompt)
        if fb_answer:
            return (f"🧠 *CIRRUS Answer (via {fb_name} fallback)*\n\n{fb_answer}"
                    f"{sources_note}\n\n_Local model was uncertain — escalated to {fb_name}._")

        if answer:
            return f"🧠 *CIRRUS Memory Answer*\n\n{answer}{sources_note}"
        if not results:
            return "No relevant past knowledge found for that question, and no fallback LLM is configured/available."
        return "❌ Local model gave no answer, and no fallback LLM is configured/available."
    except Exception as e:
        return f"❌ Error: {e}"

def cmd_approve(chat_id):
    """Scan latest action items and present pending recommendations."""
    # Load existing pending (includes already-approved/rejected history)
    pending = load_pending()

    # Always re-scan the latest action files for new recommendations and
    # merge in anything not already tracked (deduped by type+detail).
    # Previously this only ran when `pending` was completely empty, so once
    # any item (even an old approved/rejected one) existed in the file, new
    # recommendations from later digests were never picked up.
    existing_keys = {f"{p['type']}:{p['detail']}" for p in pending}
    for prefix in ["daily-actions", "weekly-actions"]:
        latest = find_latest_action(prefix)
        if latest:
            for item in extract_recommendations(latest):
                key = f"{item['type']}:{item['detail']}"
                if key not in existing_keys:
                    pending.append(item)
                    existing_keys.add(key)

    save_pending(pending)

    active = [p for p in pending if p.get("status", "pending") == "pending"]

    if not active:
        return "✅ No pending recommendations to review."

    msg = f"📋 *{len(active)} Pending Recommendations*\n\nReply with the number to approve, or `reject N` to reject:\n\n"
    for i, item in enumerate(active, 1):
        emoji = {"PULL_MODEL": "🤖", "INSTALL_PACKAGE": "📦", "ADD_SOURCE": "📡", "CIRRUS_NOTE": "💡"}.get(item["type"], "•")
        msg += f"{emoji} *{i}. {item['type']}*\n`{item['detail']}`\n"
        src = item.get("source", "")
        added = item.get("added", "")
        if src or added:
            parts = [p for p in (src, added) if p]
            msg += f"_from: {' — '.join(parts)}_\n"
        msg += "\n"

    msg += "_Reply: `approve 1` or `reject 2` or `approve all` or `reject all`_"
    return msg

def handle_approval_reply(text: str, chat_id: str) -> str:
    """Handle approve/reject replies."""
    pending = load_pending()
    active = [p for p in pending if p.get("status", "pending") == "pending"]

    if not active:
        return "No pending items to approve."

    text = text.strip().lower()

    if text == "reject all":
        for item in active:
            item["status"] = "rejected"
        save_pending(pending)
        return f"❌ Rejected all {len(active)} pending items."

    if text == "approve all":
        note_count = sum(1 for item in active if item["type"] == "CIRRUS_NOTE")
        if note_count:
            send_message(chat_id, f"⏳ Approving {len(active)} items, including {note_count} CIRRUS notes — "
                                   f"each note drafts a proposal via qwen2.5:72b, this may take several minutes total...")
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
            item["status"] = "approved" if action == "approve" else "rejected"
            save_pending(pending)
            if action == "approve":
                if item["type"] == "CIRRUS_NOTE":
                    send_message(chat_id, "⏳ Drafting implementation proposal via qwen2.5:72b — this can take a couple minutes...")
                return execute_action(item)
            else:
                return f"❌ Rejected: `{item['detail']}`"
        else:
            return f"Invalid number. Choose 1-{len(active)}."

    return "Use: `approve 1`, `reject 2`, or `approve all`"

# ── Bot Loop ──────────────────────────────────────────────────────────────────

def handle_message(message, chat_id):
    text = message.get("text", "").strip()
    cmd  = text.lower().split()[0] if text else ""

    # Normalize a leading "/" so "/approve 8" behaves the same as "approve 8".
    # Without this, "/approve 8" matched the "/approve" branch below (which
    # ignores any extra words) and just re-displayed the list instead of
    # approving item 8.
    normalized = text.lstrip("/").strip()

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
    elif cmd == "/approve" and len(text.split()) == 1:
        return cmd_approve(chat_id)
    elif cmd == "/proposals":
        return cmd_proposals()
    elif cmd == "/knowledge":
        return cmd_knowledge()
    elif cmd == "/todo":
        item = " ".join(text.split()[1:])
        return cmd_todo(item)
    elif cmd == "/detail":
        arg = " ".join(text.split()[1:])
        return cmd_detail(arg)
    elif cmd == "/ask":
        question = " ".join(text.split()[1:])
        if not question:
            return "Usage: /ask <your question>"
        return cmd_ask(question)
    elif cmd == "/research":
        topic = " ".join(text.split()[1:])
        return cmd_research(topic, chat_id)
    elif cmd == "/pullmodel":
        model = " ".join(text.split()[1:]).strip()
        return cmd_pullmodel(model, chat_id)
    elif cmd == "/omit":
        sender = " ".join(text.split()[1:])
        return cmd_omit(sender)
    elif cmd == "/omitlist":
        return cmd_omitlist()
    elif re.match(r"^(approve|reject)(\s+(\d+|all))?$", normalized, re.IGNORECASE):
        return handle_approval_reply(normalized, chat_id)
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
