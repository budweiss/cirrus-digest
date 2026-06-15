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

import json
import os
import re
import requests
import subprocess
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
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
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
            "text": chunk,
            "parse_mode": "Markdown"
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
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()

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

    # Also pick up self-improvement style suggestions from the
    # "## CIRRUS IMPROVEMENT NOTES" section. extract_actions.py reformats the
    # digest's "→ CIRRUS NOTE:" lines into bullet points under this heading
    # without the prefix, so the regex patterns above never match them —
    # handle that here as CIRRUS_NOTE items.
    # NOTE: "## RECOMMENDATIONS" is intentionally excluded — those are
    # general reading takeaways from source articles, not CIRRUS-specific
    # self-improvement ideas, and including them flooded /approve with ~6
    # low-relevance items per daily digest.
    current_section = None
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("## "):
            current_section = stripped[3:].strip().upper()
            continue
        if current_section == "CIRRUS IMPROVEMENT NOTES" and stripped.startswith("- **"):
            # Bullets come in two shapes:
            #   - **Full sentence recommendation.** (Source: ...)
            #   - **Note**: actual text here     <- "Note"/"Suggestion"/"Source" are
            #   - **Source**: [link]                just sub-labels, not the content
            bullet_match = re.match(r"-\s*\*\*(.+?)\*\*:?\s*(.*)", stripped)
            if bullet_match:
                label = bullet_match.group(1).strip()
                rest = bullet_match.group(2).strip()
                if label.lower() == "source":
                    # Just a citation link for the preceding item, not its own recommendation.
                    continue
                if label.lower() in ("suggestion", "note", "task", "description"):
                    detail = rest[:150]
                else:
                    detail = label[:150]
                if not detail:
                    continue
                key = f"CIRRUS_NOTE:{detail}"
                if key not in seen:
                    seen.add(key)
                    recommendations.append({
                        "type": "CIRRUS_NOTE",
                        "detail": detail,
                        "source_line": stripped[:160],
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

Write a concrete, scoped implementation proposal for applying this recommendation to the CIRRUS project above. Be specific:
1. Which file(s) would need to change
2. What the change would do, in plain terms
3. A rough sketch of the code/config change (pseudocode or a short snippet is fine — this is a proposal, not final code)
4. Risks or things to verify before deploying

If the recommendation is too vague, too broad, or not realistically actionable for this specific project, say so honestly instead of inventing a change — a "no good fit yet" verdict is a valid and useful proposal.

Respond in markdown with these headings: ## Analysis, ## Proposed Change, ## Risks / Things to Verify"""

    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": MODEL, "prompt": prompt, "stream": False,
                  "options": {"num_ctx": 8192}},
            timeout=300
        )
        resp.raise_for_status()
        body = resp.json().get("response", "").strip()
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
    """Answer a question using RAG memory + Ollama. If the local model is
    uncertain (or RAG found nothing), escalate to Gemini -> Grok -> Claude
    (whichever are configured in credentials.json)."""
    try:
        from cirrus_rag import retrieve
        results = retrieve(question, top_k=3)
        context = "\n\n".join([f"[{r['date']}]: {r['text']}" for r in results]) if results else ""
        sources_note = f"\n\n_Sources: {', '.join(sorted(set(r['date'] for r in results)))}_" if results else ""

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

        # Local model couldn't answer confidently (or errored/timed out) -
        # escalate to external fallback chain. Include digest context if we
        # have it; otherwise just ask the question directly. Explicitly tell
        # the fallback model not to comment on the context's relevance -
        # otherwise it tends to preface its (often correct) answer with
        # "the provided context doesn't mention X", which trips
        # is_uncertain() and discards an otherwise-good answer.
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
                    f"{sources_note}\n\n_Local model (qwen2.5) was uncertain — "
                    f"escalated to {fb_name}._")

        # Nothing else worked - return whatever the local model said (even if
        # hedged), or a final "nothing found" message.
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

    active = [p for p in pending if p["status"] == "pending"]

    if not active:
        return "✅ No pending recommendations to review."

    msg = f"📋 *{len(active)} Pending Recommendations*\n\nReply with the number to approve, or `reject N` to reject:\n\n"
    for i, item in enumerate(active, 1):
        emoji = {"PULL_MODEL": "🤖", "INSTALL_PACKAGE": "📦", "ADD_SOURCE": "📡", "CIRRUS_NOTE": "💡"}.get(item["type"], "•")
        msg += f"{emoji} *{i}. {item['type']}*\n`{item['detail']}`\n\n"

    msg += "_Reply: `approve 1` or `reject 2` or `approve all` or `reject all`_"
    return msg

def handle_approval_reply(text: str, chat_id: str) -> str:
    """Handle approve/reject replies."""
    pending = load_pending()
    active = [p for p in pending if p["status"] == "pending"]

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
    elif cmd == "/ask":
        question = " ".join(text.split()[1:])
        if not question:
            return "Usage: /ask <your question>"
        return cmd_ask(question)
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
