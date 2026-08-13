"""task_solver.py — narrow first slice of the Autonomous Task Solver (S63).

Two capabilities, both deliberately narrow relative to the full design in
docs/CIRRUS-Autonomous-Task-Solver.md (Phases A-E, none previously built):

1. try_auto_apply_source(rec) — Tier-0 auto-apply, scoped to exactly what
   dev_loop.classify_risk already treats as Tier 0: a request to add/
   subscribe/monitor an RSS/Atom source. Reuses the SAME validation the
   existing `source-add` runner command (runner/run-command.sh) already does
   live — fetch, confirm it's really a feed, append to
   config/sources.local.json (git-external overlay, reversible by deleting
   the entry).

2. solve_and_answer(rec, creds, to_addr, orig_subject) — live LLM-council
   answer for request_kind == "answer" (explicit per-sender opt-in in
   config/intake_senders.json). Reuses ensemble.best_answer() as-is — it
   already handles its own budget-aware degradation (falls back to a
   cheaper single-provider call under budget pressure rather than failing),
   so no separate budget gate is needed here. A cheap deterministic quality
   gate runs before sending (the one part of the never-built Sanity
   Supervisor design — docs/CIRRUS-Sanity-Supervisor.md's "v0 hard
   sniff-tests" — cheap enough to include here for free).

Both are called from intake.py's run(), after dev_loop.classify_risk and the
sender-allowlist gate have already run — never invoked on unclassified input.
"""
import json
import re
import smtplib
import urllib.request
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

import dev_loop
import ensemble

PROJECT_DIR = Path.home() / "projects/cirrus-digest"
SOURCES_OVERLAY = PROJECT_DIR / "config/sources.local.json"

# Matches the existing convention in client_mail.py (cc Buddy on client sends)
# rather than a credentials.json field — same address, same pattern.
CC_ADDR = "Buddy.Weiss@outlook.com"

MIN_ANSWER_CHARS = 80
_REFUSAL_MARKERS = (
    "i cannot", "i can't", "i'm unable", "i am unable", "as an ai",
    "i don't have access", "i do not have access",
)


# ── Tier-0 auto-apply: source/feed add ───────────────────────────────────────

def _fetch_is_feed(url: str, timeout: int = 20) -> bool:
    """Identical check to the existing source-add runner command: HTTP 200 +
    an RSS/Atom/XML marker in the head of the response."""
    req = urllib.request.Request(url, headers={"User-Agent": "CIRRUS-digest/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            return False
        head = r.read(2000).decode(errors="replace").lower()
    return "<rss" in head or "<feed" in head or "<?xml" in head


def try_auto_apply_source(rec: dict) -> dict | None:
    """Attempt Tier-0 auto-apply for a source/feed-add request. Returns a
    result dict {"url","name"} on success, None on any failure to find/
    validate a URL — caller falls back to normal queuing either way, never
    forces a broken auto-apply."""
    if rec.get("tier") != dev_loop.TIER_AUTO or rec.get("kind") != "build":
        return None
    body = rec.get("body_head", "") or ""
    m = dev_loop._URL_RX.search(body)
    if not m:
        return None
    url = m.group(0)
    try:
        if not _fetch_is_feed(url):
            return None
    except Exception:
        return None

    try:
        overlay = json.loads(SOURCES_OVERLAY.read_text()) if SOURCES_OVERLAY.exists() else []
    except Exception:
        overlay = []
    if any(s.get("rss") == url for s in overlay):
        return {"url": url, "name": rec.get("title", url), "already_present": True}
    overlay.append({
        "name": (rec.get("title") or url)[:60], "rss": url, "type": "blog",
        "added_by": "task_solver:auto-apply", "added": datetime.now().strftime("%Y-%m-%d"),
    })
    SOURCES_OVERLAY.parent.mkdir(parents=True, exist_ok=True)
    SOURCES_OVERLAY.write_text(json.dumps(overlay, indent=2) + "\n")
    return {"url": url, "name": rec.get("title", url), "already_present": False}


# ── Live answer via the 4-provider LLM council ───────────────────────────────

_ANSWER_SYSTEM = (
    "You are answering a direct question from one of Buddy's clients, sent by "
    "email. Answer clearly and completely in plain prose (no markdown headers, "
    "this goes straight into an email body). If the question is genuinely "
    "outside what you can answer from general knowledge, say so plainly rather "
    "than guessing."
)


def _quality_ok(text: str) -> bool:
    """Cheap, deterministic checks before an LLM answer is auto-sent to a
    client — no extra LLM call. Mirrors the Sanity Supervisor design's v0
    hard sniff-tests (the only part of that never-built system cheap enough
    to include here for free)."""
    if not text or len(text.strip()) < MIN_ANSWER_CHARS:
        return False
    low = text.lower()
    return not any(marker in low[:200] for marker in _REFUSAL_MARKERS)


def _send_mail(from_email: str, password: str, to_addr: str, cc_addr: str,
               subject: str, body: str) -> bool:
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = from_email
        msg["To"] = to_addr
        if cc_addr:
            msg["Cc"] = cc_addr
        recipients = [to_addr] + ([cc_addr] if cc_addr else [])
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=60) as server:
            server.ehlo(); server.starttls(); server.ehlo()
            server.login(from_email, password)
            server.sendmail(from_email, recipients, msg.as_string())
        return True
    except Exception:
        return False


def solve_and_answer(rec: dict, creds: dict, to_addr: str, orig_subject: str) -> dict:
    """Answer a request_kind=='answer' message live via the 4-provider
    council. Returns {"answered": bool, "cost_usd": float|None, "reason": str}.
    On a quality-gate failure or any hard error, falls back to a normal
    dev-ticket queue entry (via dev_loop.ticket_create) rather than silently
    dropping the request — the ack the caller already sent told the
    requester it's being worked on."""
    question = rec.get("body_head", "") or rec.get("title", "")
    result = {"answered": False, "cost_usd": None, "reason": ""}
    try:
        meta, text = ensemble.best_answer(
            _ANSWER_SYSTEM, question, creds, task="intake-answer",
            session_id=f"intake-answer-{rec.get('message_id', datetime.now().isoformat())}",
            app_dir=PROJECT_DIR, mode="council")
    except Exception as e:
        result["reason"] = f"council call failed: {e}"
        _fallback_to_ticket(rec)
        return result

    result["cost_usd"] = meta.get("est_cost_usd")
    if not _quality_ok(text):
        result["reason"] = "quality gate failed (empty/short/refusal-shaped answer)"
        _fallback_to_ticket(rec)
        return result

    from_email = creds.get("outlook_email", "")
    password = creds.get("outlook_password", "")
    subj = orig_subject or rec.get("title", "your request")
    subj = subj if subj.lower().startswith("re:") else f"Re: {subj}"
    sent = _send_mail(from_email, password, to_addr, CC_ADDR, subj, text)
    if not sent:
        result["reason"] = "answer generated but send failed"
        _fallback_to_ticket(rec)
        return result

    result["answered"] = True
    result["reason"] = meta.get("reason", "")
    dev_loop.ledger_append({
        "event": "auto-answered", "requester": rec.get("requester"),
        "title": rec.get("title"), "cost_usd": result["cost_usd"],
        "degraded": meta.get("degraded"), "members": meta.get("members"),
    }, PROJECT_DIR)
    return result


def _fallback_to_ticket(rec: dict):
    """Best-effort: if a live answer couldn't be produced/sent, don't drop
    the request — queue it the normal way (same path build-kind requests
    already use)."""
    try:
        dev_loop.ticket_create(
            rec.get("requester", ""), rec.get("projects", []), rec.get("title", ""),
            (rec.get("body_head") or "")[:400], origin="user-intake-answer-fallback",
            project_dir=PROJECT_DIR)
    except Exception:
        pass
