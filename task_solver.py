"""task_solver.py — narrow first slice of the Autonomous Task Solver (S63).

Capabilities, all deliberately narrow relative to the full design in
docs/CIRRUS-Autonomous-Task-Solver.md (Phases A-E, none previously built):

1. try_auto_apply_source(rec) — Tier-0 auto-apply, scoped to exactly what
   dev_loop.classify_risk already treats as Tier 0: a request to add/
   subscribe/monitor an RSS/Atom source. Reuses the SAME validation the
   existing `source-add` runner command (runner/run-command.sh) already does
   live — fetch, confirm it's really a feed, append to
   config/sources.local.json (git-external overlay, reversible by deleting
   the entry).

2. solve_and_answer(rec, creds, to_addr, orig_subject) — live answer for
   request_kind == "answer" (explicit per-sender opt-in in
   config/intake_senders.json). S65: checks entity_kb.py first (via
   try_entity_kb_answer) for a grounded, sourced recap from a project's own
   researched data — free, no LLM call, no hallucination risk. If the
   question also asks for UPDATED/fresh info (wants_fresh_research), runs a
   live deep_research.deep_research_entity() pass first — search + fetch +
   4-LLM-council extraction against the open web, written back into
   entity_kb — so the reply reflects a fresh look, not just what was
   already on file. Falls back to the general 4-provider LLM council
   (ensemble.best_answer(), which already handles its own budget-aware
   degradation) only when nothing in the KB matches the question at all.
   A cheap deterministic quality gate runs before sending either way (the
   one part of the never-built Sanity Supervisor design —
   docs/CIRRUS-Sanity-Supervisor.md's "v0 hard sniff-tests" — cheap enough
   to include here for free). PROJECT_TO_KB is the multi-tenant boundary:
   a sender only ever searches the entity_kb project(s) their own intake
   "projects" tag maps to, so one client's questions can never surface
   another client's data.

3. classify_capability(rec) + try_auto_resend(rec, creds, to_addr, subject)
   (S63, later same session) — the first entry in a small CAPABILITY
   registry: known request shapes intake can solve on its own regardless of
   the sender's static `request_kind`. Today the registry has exactly two
   members (source-add via #1's URL detector, resend via this one). This is
   the safe, real version of "Skywarden looks at a request and decides whether it
   can build a solution": a request either matches an already-built,
   already-tested capability (auto-solved, zero-click) or it doesn't (told
   plainly it needs review — see intake.py's ack_body "needs_review"
   branch). It does NOT mean the agent writes and ships new arbitrary code
   unattended for a request shape nobody's seen before — that's a much
   larger, still-open piece of the original design (sandboxed execution, a
   real tool-registry planning loop) and deliberately isn't what this ships.
   Growing this registry is the intended pattern for "recursive
   development": each time a new request shape recurs, a session builds and
   registers a narrow, tested handler for it, same as this one.

All are called from intake.py's run(), after dev_loop.classify_risk and the
sender-allowlist gate have already run — never invoked on unclassified input.
"""
from __future__ import annotations  # dict|None syntax needs 3.10+; CIRRUS's
                                     # system python3 is 3.9.6 -- this makes
                                     # annotations lazy/string-only so it runs
                                     # fine back to 3.7. Caught live: deploying
                                     # without this broke CIRRUS's intake
                                     # LaunchAgent outright (selftest crashed
                                     # at import time before ever reaching the
                                     # allowlist gate).
import email
import email.utils
import imaplib
import json
import re
import smtplib
import urllib.request
from datetime import datetime, timedelta
from email.header import decode_header
from email.mime.text import MIMEText
from pathlib import Path

import deep_research
import dev_loop
import ensemble
import entity_kb

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


# Maps an intake sender's "projects" tag (config/intake_senders.json) to the
# entity_kb.py project string to search for that sender's questions. Extend
# this dict, not the matching logic, when a new project gets its own KB.
# This IS the multi-tenant boundary: a sender only ever searches the
# project(s) their own "projects" tag maps to -- e.g. Alyssa's pedagogy
# senders would map only to a pedagogy_* entity_kb project, never
# hoa_leads_bill, so her lookups can never surface Bill's data or vice versa.
PROJECT_TO_KB = {
    "snow": "hoa_leads_bill",
    "property-management": "hoa_leads_bill",
}

# S66: a bare entity-name search on refresh can collide with an unrelated
# same-named org elsewhere (see hoa_daily_research.py's _hoa_query_terms for
# the live examples that caught this) -- per-kb_project search/extraction
# grounding for deep_research_entity(), same fix applied to the live client-
# refresh path here. Extend this dict (not the logic) for a future kb
# project; one with no entry gets deep_research's ungrounded default.
KB_RESEARCH_CONTEXT = {
    "hoa_leads_bill": {
        "extra_query_terms": "Delaware HOA homeowners association",
        "context_hint": (
            "This must be a homeowners association / residential community "
            "in Delaware. Ignore any source about a same-named entity in a "
            "different U.S. state, or about a different kind of thing "
            "entirely (a business, farm, sporting event/championship, or "
            "municipal government) that merely shares this name."
        ),
    },
}

_DETERMINER2 = r"(the|this|that|it|our|my)"
_REFRESH_RX = re.compile(
    # "updated/latest/current/new/fresh info(rmation)/status/news" on X
    r"\b(updat(e[ds]?|ing)|latest|current|new|fresh)\b[^.!?\n]{0,40}\b(info(rmation)?|update|status|news|details)\b"
    r"|\b(any\s+)?(updates?|news)\s+on\b"
    r"|\brefresh\b|\bre-?research\b|\bdig\s+(deeper|further)\b"
    r"|\blook\s+into\s+" + _DETERMINER2 + r"\b[^.!?\n]{0,20}\b(again|further|more|deeper)\b"
    r"|\bcheck\s+(again|for\s+updates)\b",
    re.IGNORECASE,
)


def wants_fresh_research(text: str) -> bool:
    """Deterministic, offline-testable: does this question ask for
    NEW/updated research, vs. just 'what do you have on record'? Same
    pattern-match approach as classify_capability's resend detector above."""
    return bool(_REFRESH_RX.search(text))


def try_entity_kb_answer(rec: dict, creds: dict = None, db_path: str = None) -> str | None:
    """If the question plausibly names something already researched (per
    the sender's project -> entity_kb project mapping above), answer from
    the KB -- grounded and sourced, costs nothing, and can't hallucinate a
    management company that isn't in our records. If the question also asks
    for UPDATED/fresh info (wants_fresh_research) and `creds` is supplied,
    runs a live deep_research_entity() pass first so the reply reflects a
    fresh search, not just what was already on file.

    Returns None (caller falls back to the council) when no entity_kb
    project applies to this sender or nothing matches. `creds=None` skips
    the refresh path even if the question asks for it (selftest never
    passes creds, so it never triggers live network/LLM calls). `db_path`
    overrides entity_kb's default per-project path -- only ever passed by
    selftest(), never by the live call site."""
    question = f"{rec.get('title', '')} {rec.get('body_head', '')}"
    kb_projects = {PROJECT_TO_KB[p] for p in rec.get("projects", []) if p in PROJECT_TO_KB}
    for kb_project in kb_projects:
        try:
            matches = entity_kb.search_entities(kb_project, question, db_path=db_path, limit=3)
        except Exception:
            continue
        if len(matches) > 1:
            names = "; ".join(m["name"] for m in matches)
            return (f"I found a few possible matches in our records — could you "
                     f"let me know which one you mean? {names}")
        if len(matches) == 1:
            entity = matches[0]
            if creds and wants_fresh_research(question):
                try:
                    ctx = KB_RESEARCH_CONTEXT.get(kb_project, {})
                    recap, found = deep_research.deep_research_entity(
                        kb_project, entity["name"], entity["slug"], creds,
                        extra_query_terms=ctx.get("extra_query_terms", ""),
                        context_hint=ctx.get("context_hint", ""), db_path=db_path)
                    prefix = ("I did a fresh search and found some updates — here's the "
                              "latest:\n\n" if found else
                              "I looked for updated information but didn't find anything new "
                              "since our last check — here's what's currently on record:\n\n")
                    return prefix + recap
                except Exception:
                    pass  # fall through to the plain (non-refreshed) recap below
            return entity_kb.recap_text(kb_project, entity["slug"], db_path=db_path)
    return None


def solve_and_answer(rec: dict, creds: dict, to_addr: str, orig_subject: str) -> dict:
    """Answer a request_kind=='answer' message live -- from entity_kb.py if
    the question matches something already researched (try_entity_kb_answer),
    else via the 4-provider council. Returns {"answered": bool,
    "cost_usd": float|None, "reason": str}. On a quality-gate failure or any
    hard error, falls back to a normal dev-ticket queue entry (via
    dev_loop.ticket_create) rather than silently dropping the request — the
    ack the caller already sent told the requester it's being worked on."""
    question = rec.get("body_head", "") or rec.get("title", "")
    result = {"answered": False, "cost_usd": None, "reason": ""}

    kb_text = try_entity_kb_answer(rec, creds=creds)
    if kb_text is not None:
        text = kb_text
        meta = {"est_cost_usd": 0.0, "reason": "entity_kb lookup",
                 "degraded": False, "members": ["entity_kb"]}
    else:
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


# ── Capability registry: request-shape detection ─────────────────────────────
# Deterministic pattern matches, not a live LLM call, on purpose: this runs on
# EVERY allowlisted message regardless of static request_kind, so it needs to
# be cheap, offline-testable, and auditable — same bar as the URL detector
# already used for source-add. A request that matches nothing here is exactly
# the "needs review" case intake.py's ack_body flags to the sender.

_DETERMINER = r"(my|our|the|all|those|these|past|previous|old|missed|prior)"
_MAILWORD = r"(email|emails|digest|digests|message|messages|newsletter)s?"
_RESEND_RX = re.compile(
    # "resend"/"re-send" gated by an object determiner ("my", "the", "all", …)
    # before the mail-word — NOT just bare proximity, so a mention of the
    # unrelated "Resend" email-API product ("Resend vs SendGrid pricing")
    # doesn't false-positive just because "email" appears nearby.
    r"\bre-?send\b[^.!?\n]{0,50}\b" + _DETERMINER + r"\b[^.!?\n]{0,30}\b" + _MAILWORD + r"\b"
    r"|\b" + _DETERMINER + r"\b[^.!?\n]{0,30}\b" + _MAILWORD + r"\b[^.!?\n]{0,50}\bre-?sen[dt]\b"
    r"|\bsend\s*(it|them|everything)?\s*again\b[^.!?\n]{0,50}\b" + _MAILWORD + r"\b"
    r"|\b" + _MAILWORD + r"\b[^.!?\n]{0,50}\bsend\s*(it|them|everything)?\s*again\b"
    r"|\b(didn'?t|did\s+not|never)\s+(receive[d]?|get|got)\b[^.!?\n]{0,60}\b" + _MAILWORD + r"\b"
    r"|\bmissed\s+(all\s+)?(my\s+)?" + _MAILWORD + r"\b"
    r"|\b(went|goes|going)\s+(straight\s+)?to\s+(my\s+)?(junk|spam)\b",
    re.IGNORECASE,
)


def classify_capability(rec: dict) -> str:
    """Returns 'resend', 'source_add', or 'none'. Runs BEFORE any per-sender
    static request_kind is applied, so a known request shape (e.g. Alyssa,
    statically 'research', asking to resend past mail) is caught regardless
    of the sender's default routing."""
    text = f"{rec.get('title', '')} {rec.get('body_head', '')}"
    if _RESEND_RX.search(text):
        return "resend"
    if dev_loop._URL_RX.search(rec.get("body_head", "") or ""):
        return "source_add"
    return "none"


# ── Resend: find + redeliver our own past sent mail ──────────────────────────

IMAP_SERVER = "imap.gmail.com"          # same Gmail infra the outbound side
                                        # uses (smtplib.SMTP("smtp.gmail.com"))
MAX_RESEND = 30                         # cap a single batch — avoid a flood
SELF_EXCLUDE_MINUTES = 10               # skip anything sent in this run's own
                                        # ack window (else the ack we just
                                        # sent shows up as a "past email")


def _decode_hdr(value: str) -> str:
    if not value:
        return ""
    out = []
    for part, enc in decode_header(value):
        out.append(part.decode(enc or "utf-8", errors="replace")
                   if isinstance(part, bytes) else part)
    return "".join(out)


def _plain_text(msg) -> str:
    """First text/plain part (fallback: stripped text/html) — no length cap;
    unlike intake.py's body_text() this is for redelivering the ORIGINAL
    content, not classifying it."""
    def _decode(part):
        try:
            payload = part.get_payload(decode=True)
            return payload.decode(part.get_content_charset() or "utf-8",
                                  errors="replace") if payload else ""
        except Exception:
            return ""
    plain, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain" and not plain:
                plain = _decode(part)
            elif ctype == "text/html" and not html:
                html = _decode(part)
    else:
        if msg.get_content_type() == "text/html":
            html = _decode(msg)
        else:
            plain = _decode(msg)
    return plain or re.sub(r"<[^>]+>", " ", html)


def _find_sent_folder(mail: imaplib.IMAP4_SSL) -> str:
    """Locate the Sent-mail folder by its \\Sent special-use attribute (works
    regardless of account locale/naming); falls back to Gmail's default
    English name if the attribute lookup fails."""
    try:
        typ, data = mail.list()
        if typ == "OK":
            for line in data or []:
                line_s = line.decode(errors="replace") if isinstance(line, bytes) else str(line)
                if "\\Sent" in line_s:
                    m = re.search(r'"([^"]+)"\s*$', line_s)
                    if m:
                        return m.group(1)
    except Exception:
        pass
    return "[Gmail]/Sent Mail"


def try_auto_resend(rec: dict, creds: dict, to_addr: str, orig_subject: str) -> dict:
    """Find CUMULUS's own past sent mail to `to_addr` (its Sent folder — the
    same account intake sends acks/digests from) and redeliver it verbatim.
    Read-only IMAP search + a bounded batch of outbound resends; no new
    destructive surface. Falls back to the normal ticket queue on any
    failure or if nothing is found, same pattern as solve_and_answer — the
    ack already sent told the requester it's being worked on."""
    result = {"resent": False, "count": 0, "skipped_older": 0, "reason": ""}
    from_email = creds.get("outlook_email", "")
    password = creds.get("outlook_password", "")
    if not from_email or not password:
        result["reason"] = "no send credentials configured"
        _fallback_to_ticket(rec)
        return result

    mail = None
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, 993, timeout=30)
        mail.login(from_email, password)
        folder = _find_sent_folder(mail)
        typ, _ = mail.select(f'"{folder}"', readonly=True)
        if typ != "OK":
            raise RuntimeError(f"could not open sent folder '{folder}'")
        typ, msg_ids = mail.search(None, "TO", f'"{to_addr}"')
        if typ != "OK":
            raise RuntimeError("IMAP search failed")
        ids = msg_ids[0].split()

        cutoff = datetime.now() - timedelta(minutes=SELF_EXCLUDE_MINUTES)
        candidates = []
        for mid in ids:
            try:
                typ, hdr_data = mail.fetch(mid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE)])")
                if typ != "OK" or not hdr_data or not hdr_data[0]:
                    continue
                hdr = email.message_from_bytes(hdr_data[0][1])
                subj = _decode_hdr(hdr.get("Subject", ""))
                date_hdr = hdr.get("Date")
                sent_dt = email.utils.parsedate_to_datetime(date_hdr) if date_hdr else None
                if sent_dt and sent_dt.tzinfo:
                    sent_dt = sent_dt.astimezone().replace(tzinfo=None)
                if sent_dt and sent_dt > cutoff:
                    continue  # excludes mail sent by this very run (the ack)
                candidates.append((mid, subj, sent_dt))
            except Exception:
                continue

        if not candidates:
            result["reason"] = "no prior sent mail found to this address"
            _fallback_to_ticket(rec)
            return result

        candidates.sort(key=lambda c: c[2] or datetime.min)
        batch = candidates[-MAX_RESEND:]
        result["skipped_older"] = len(candidates) - len(batch)

        sent_count = 0
        for mid, subj, sent_dt in batch:
            try:
                typ, full = mail.fetch(mid, "(BODY.PEEK[])")
                if typ != "OK" or not full or not full[0]:
                    continue
                orig = email.message_from_bytes(full[0][1])
                body = _plain_text(orig)
                when = sent_dt.strftime("%Y-%m-%d") if sent_dt else "an earlier date"
                resend_subj = f"[Resent] {subj}" if subj else "[Resent] CUMULUS email"
                if _send_mail(from_email, password, to_addr, CC_ADDR, resend_subj,
                             f"(Resending at your request — originally sent {when})\n\n{body}"):
                    sent_count += 1
            except Exception:
                continue
    except Exception as e:
        result["reason"] = f"could not read/resend sent mail: {e}"
        _fallback_to_ticket(rec)
        return result
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass

    result["resent"] = sent_count > 0
    result["count"] = sent_count
    if not result["resent"]:
        result["reason"] = "found candidates but every resend attempt failed"
        _fallback_to_ticket(rec)
        return result
    result["reason"] = (f"resent {sent_count}/{len(batch)}"
                        + (f", {result['skipped_older']} older one(s) skipped "
                           f"(cap {MAX_RESEND}/request)" if result["skipped_older"] else ""))
    dev_loop.ledger_append({
        "event": "auto-resent", "requester": rec.get("requester"),
        "to": to_addr, "count": sent_count, "skipped_older": result["skipped_older"],
    }, PROJECT_DIR)
    return result


# ── Selftest (offline, no network) ───────────────────────────────────────────
# Covers try_entity_kb_answer only -- solve_and_answer/try_auto_resend need
# live network (council/SMTP/IMAP), same reason intake.py's own selftest
# stays offline-only.

def selftest() -> int:
    import os
    import tempfile

    failures = 0

    def check(name, cond):
        nonlocal failures
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            failures += 1

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)
    kb_project = "hoa_leads_bill"  # matches PROJECT_TO_KB's real mapping
    try:
        entity_kb.upsert_entity(
            kb_project, "mermaid-run", "Mermaid Run Condominium Association",
            fields={"current_mgmt_co": "RE/MAX Associates"}, lead_state="warm",
            db_path=db_path)
        entity_kb.upsert_entity(
            kb_project, "le-parc", "Le Parc Community Association",
            fields={"current_mgmt_co": "Premier Property Management"},
            lead_state="cold", db_path=db_path)

        rec_snow = {"projects": ["snow"], "title": "REQUEST: recap on Mermaid Run",
                    "body_head": "What do you have on Mermaid Run before I visit?"}
        answer = try_entity_kb_answer(rec_snow, db_path=db_path)
        check("known-project sender + matching entity returns a KB recap",
              answer is not None and "RE/MAX Associates" in answer)

        rec_unmapped = {"projects": ["realestate"], "title": "REQUEST: recap on Mermaid Run",
                         "body_head": "What do you have on Mermaid Run?"}
        check("sender project not in PROJECT_TO_KB returns None (falls back to council)",
              try_entity_kb_answer(rec_unmapped, db_path=db_path) is None)

        rec_no_match = {"projects": ["snow"], "title": "REQUEST",
                         "body_head": "What's your rate for snow removal this winter?"}
        check("no matching entity returns None (falls back to council)",
              try_entity_kb_answer(rec_no_match, db_path=db_path) is None)

        rec_ambiguous = {"projects": ["property-management"], "title": "REQUEST",
                          "body_head": "Any updates on our management communities?"}
        # neither name/slug appears verbatim and word-overlap is too generic to
        # match either seeded entity -- documents the honest limit of substring
        # matching rather than asserting a specific (fragile) ranking behavior.
        check("a vague question with no entity name mentioned returns None",
              try_entity_kb_answer(rec_ambiguous, db_path=db_path) is None)

        # wants_fresh_research: pure regex, no I/O
        check("'any updates on X' reads as a refresh request",
              wants_fresh_research("Do you have any updates on Mermaid Run?"))
        check("'latest info' reads as a refresh request",
              wants_fresh_research("What's the latest information on this property?"))
        check("'refresh your research' reads as a refresh request",
              wants_fresh_research("Can you refresh your research on this one?"))
        check("a plain lookup question does NOT read as a refresh request",
              not wants_fresh_research("What do you have on Mermaid Run?"))

        # Refresh phrasing WITHOUT creds must never trigger a live search --
        # falls through to the plain on-file recap, same as a non-refresh ask.
        rec_refresh_no_creds = {"projects": ["snow"], "title": "REQUEST",
                                  "body_head": "Any updates on Mermaid Run before I visit?"}
        answer = try_entity_kb_answer(rec_refresh_no_creds, creds=None, db_path=db_path)
        check("refresh phrasing with creds=None returns the plain recap, no live search",
              answer is not None and "RE/MAX Associates" in answer
              and "fresh search" not in answer)
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)

    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    import sys
    sys.exit(selftest())
