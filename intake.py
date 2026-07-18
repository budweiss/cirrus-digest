#!/usr/bin/env python3
"""
CIRRUS End-User Direct Intake — P1
===================================
Bill & Aggie email enhancement requests straight to the system instead of
through Buddy. Design: docs/END-USER-DIRECT-INTAKE.md (approved 2026-07-15).

P1 scope (this file): IMAP scan of the research inbox for allowlisted
senders → route to project → classify risk (dev_loop) → append to the
intake backlog → ACK email to the sender → Telegram summary to Buddy.
NO auto-build yet — that's P2 (/admin/ticket → dev-loop build queue).

Safety model:
- Sender allowlist is a hard gate (config/intake_senders.json, created by
  Buddy via vi on CIRRUS — never in git). Missing/placeholder file = intake
  is a silent no-op (exit 0), so deploying before the config exists is safe.
- Per-sender daily rate limit (default 10) — a flooded/compromised sender
  stops being processed, Buddy gets a Telegram note.
- NEVER-pattern requests (credentials, deletion, financial, access —
  dev_loop.classify_risk) are refused with a polite ack + Telegram to Buddy.
- Reads with BODY.PEEK and keeps its own UID state (config/intake_state.json)
  so the 7am digest's email ingestion (email_state.json) is untouched.
- Every intake event goes to the self-changes ledger (logs/self-changes/).

Modes:
  python3 intake.py             normal run (LaunchAgent com.cirrus.intake)
  python3 intake.py --dry-run   scan + classify + report; NO acks, NO
                                telegram, NO state/backlog writes
  python3 intake.py selftest    offline unit tests, no network
"""

import email
import email.utils
import imaplib
import json
import re
import smtplib
import sys
import urllib.request
from datetime import datetime, timedelta
from email.header import decode_header
from email.mime.text import MIMEText
from pathlib import Path

import dev_loop

# ── Paths & config ────────────────────────────────────────────────────────────

PROJECT_DIR  = Path.home() / "projects/cirrus-digest"
CONFIG_PATH  = PROJECT_DIR / "config/sources.json"
CREDS_PATH   = PROJECT_DIR / "config/credentials.json"
SENDERS_PATH = PROJECT_DIR / "config/intake_senders.json"
STATE_PATH   = PROJECT_DIR / "config/intake_state.json"
INTAKE_DIR   = PROJECT_DIR / "logs/intake"
LOG_PATH     = PROJECT_DIR / "logs/intake.log"

INTAKE_ACCOUNT_LABEL = "gmail-research"   # sources.json email.accounts[].label
DAYS_BACK            = 3                  # IMAP search window (state bounds real work)
BODY_HEAD_CHARS      = 2000               # how much body to keep/classify
DEFAULT_DAILY_LIMIT  = 10

REQUEST_RX = re.compile(r"^\s*(re:\s*)?request\b\s*:?\s*", re.IGNORECASE)

# Projects that are research-only (a request from these senders is ALWAYS a
# focus topic for the project's research digest, never a build/dev ticket).
# This is a safety net: even if a sender's request_kind is mis-set to 'build'
# in intake_senders.json, their requests still reach the research topic queue.
RESEARCH_PROJECTS = {"pedagogy"}
BOUNCE_FROM_RX = re.compile(r"^(mailer-daemon|postmaster)@", re.IGNORECASE)
EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def log(msg: str):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Config loading (all fail-soft: intake must never crash the box) ──────────

def load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def load_allowlist(path: Path = SENDERS_PATH):
    """Returns {email_lower: {name, projects, limit}} or {} if not configured.
    Placeholder (FILL-IN) entries are ignored, so the shipped template is inert."""
    raw = load_json(path)
    if not isinstance(raw, dict):
        return {}
    allow = {}
    for name, entry in raw.items():
        if name.startswith("_") or not isinstance(entry, dict):
            continue
        for addr in entry.get("emails", []):
            addr = str(addr).strip().lower()
            if not addr or "fill-in" in addr or "example.com" in addr:
                continue
            allow[addr] = {
                "name": name,
                "projects": entry.get("projects", []),
                "limit": int(entry.get("max_requests_per_day", DEFAULT_DAILY_LIMIT)),
                # If true, only subjects matching 'REQUEST:' enter intake —
                # for senders whose mail ALSO feeds the digest (e.g. Buddy's
                # research forwards). Default false: everything is a request.
                "require_prefix": bool(entry.get("require_request_prefix", False)),
                # "build" (default): backlog + dev ticket queue (Bill/Aggie).
                # "research": requests are FOCUS TOPICS for that project's
                # research digest (Alyssa/pedagogy) → config/topics-<project>.json,
                # no dev ticket. See pedagogy/PEDAGOGY-SPEC.md.
                "request_kind": (entry.get("request_kind") or "build")
                                 if entry.get("request_kind") in (None, "build", "research")
                                 else "build",
            }
    return allow


def load_state():
    state = load_json(STATE_PATH)
    return state if isinstance(state, dict) else {}


def save_state(state: dict):
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception as e:
        log(f"WARNING: could not save intake state: {e}")


# ── Message parsing helpers ───────────────────────────────────────────────────

def decode_hdr(value: str) -> str:
    if not value:
        return ""
    out = []
    for part, enc in decode_header(value):
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(part)
    return "".join(out)


def body_text(msg) -> str:
    """First text/plain part (fallback: stripped text/html), head only."""
    def _decode(part):
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                return ""
            return payload.decode(part.get_content_charset() or "utf-8",
                                  errors="replace")
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

    text = plain or re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()[:BODY_HEAD_CHARS]


def parse_request_title(subject: str) -> str:
    """'REQUEST: faster bids' → 'faster bids'; otherwise the subject as-is.
    Prefix match is case-insensitive and the colon is optional
    ('REQUEST morphology' works the same as 'request: morphology')."""
    return REQUEST_RX.sub("", subject or "").strip() or "(no subject)"


def research_topic_title(subject: str, body: str) -> str:
    """Topic title for a research-intake sender.

    If the subject carries a REQUEST prefix, use the cleaned subject. But a
    research sender often mails a bare keyword subject (e.g. 'RESEARCH') with
    the real ask in the body — in that case derive the topic from the first
    meaningful line/sentence of the body so the focus-topic queue isn't
    polluted with junk titles like 'RESEARCH'."""
    if REQUEST_RX.match(subject or ""):
        return parse_request_title(subject)
    cleaned = (subject or "").strip()
    # Bare/short/keyword subject → prefer the body's first sentence.
    if len(cleaned) < 12 or cleaned.lower() in ("research", "request", "topic", "(no subject)"):
        first = re.split(r"(?<=[.!?])\s+|\n", (body or "").strip())[0].strip()
        if len(first) >= 8:
            return first[:140]
    return cleaned or "(no subject)"


def extract_bounced_recipient(msg, own_address: str) -> str:
    """Best-effort: who did our email fail to reach? Checks the
    X-Failed-Recipients header, then the first email address in the bounce
    body that isn't our own sender. Returns '' if none found."""
    failed = msg.get("X-Failed-Recipients", "")
    if failed and "@" in failed:
        return failed.strip()
    own = (own_address or "").lower()
    for addr in EMAIL_RX.findall(body_text(msg)):
        a = addr.lower()
        if a != own and not BOUNCE_FROM_RX.match(a):
            return addr
    return ""


# ── Classification (wraps dev_loop) ───────────────────────────────────────────

def classify(sender_name: str, projects, subject: str, body: str):
    """Returns (record dict) — tier via dev_loop.classify_risk + make_spec.
    Type USER_REQUEST → dev_loop type-baseline default = Tier 1 (confirm),
    NEVER patterns always win."""
    title = parse_request_title(subject)
    detail = title if len(title) >= 8 else f"{title} — {body[:120]}"
    tier, reason = dev_loop.classify_risk("USER_REQUEST", detail, body[:300])
    spec = dev_loop.make_spec(
        {"type": "USER_REQUEST", "detail": detail, "source_line": body[:200]},
        idx=int(datetime.now().strftime("%H%M%S")))
    spec["origin"] = "user-intake"
    spec["requester"] = sender_name
    spec["projects"] = list(projects)
    return {
        "requester": sender_name,
        "projects": list(projects),
        "title": title,
        "subject": subject,
        "body_head": body[:400],
        "tier": tier,
        "tier_name": dev_loop.TIER_NAME.get(tier, str(tier)),
        "tier_reason": reason,
        "dev_spec": spec,
        "received": datetime.now().isoformat(timespec="seconds"),
        "status": "refused" if tier == dev_loop.TIER_NEVER else "backlogged",
    }


# ── Outputs: backlog, ledger, ack email, telegram ────────────────────────────

def append_backlog(rec: dict):
    """Append to logs/intake/intake.jsonl + per-person markdown backlog."""
    INTAKE_DIR.mkdir(parents=True, exist_ok=True)
    with open(INTAKE_DIR / "intake.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")

    md = INTAKE_DIR / f"REQUESTS-FROM-{rec['requester'].upper()}.md"
    if not md.exists():
        md.write_text(f"# Intake requests — {rec['requester']}\n\n"
                      "Appended automatically by intake.py (P1). Sync notable "
                      "items to the Cowork backlog files in a session.\n")
    with open(md, "a") as f:
        f.write(f"\n---\n## {rec['received']} — {rec['title']}\n"
                f"- Tier: {rec['tier']} ({rec['tier_name']}) — {rec['tier_reason']}\n"
                f"- Projects: {', '.join(rec['projects']) or '-'}\n"
                f"- Status: {rec['status']}\n"
                f"- Spec id: {rec['dev_spec']['id']}\n"
                f"- Body: {rec['body_head'][:300]}\n")


def append_topic(project: str, title: str, requester: str) -> Path:
    """Add a focus topic to config/topics-<project>.json (research intake).
    Dedupe: an identical active topic isn't added twice."""
    safe = re.sub(r"[^a-z0-9_-]", "", (project or "general").lower()) or "general"
    path = PROJECT_DIR / f"config/topics-{safe}.json"
    data = load_json(path) or {"topics": []}
    for t in data["topics"]:
        if t.get("status") == "active" and t.get("topic", "").lower() == title.lower():
            return path  # already queued
    data["topics"].append({
        "topic": title, "requested_by": requester, "status": "active",
        "added": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    return path


def ack_body(rec: dict) -> str:
    name = rec["requester"].capitalize()
    if rec["status"] == "refused":
        return (f"Hi {name},\n\nThanks for your request:\n\n"
                f"    {rec['title']}\n\n"
                "This one falls in a category that needs a human decision "
                "(things like credentials, deletions, purchases, or access "
                "changes are never automated). Buddy has been notified and "
                "will follow up with you directly.\n\n— CIRRUS")
    if rec.get("kind") == "feedback":
        return (f"Hi {name},\n\nThanks for the note — Buddy and I have it "
                "and will review. If you'd like a specific subject researched, "
                "send a fresh email with the subject line "
                "\"REQUEST: your topic\" and it goes straight into the "
                "research queue.\n\n— CIRRUS")
    if rec.get("kind") == "research":
        return (f"Hi {name},\n\nGot it — your topic has been added to the "
                f"research queue:\n\n    {rec['title']}\n\n"
                "It'll be covered in an upcoming digest. Send as many topics "
                "as you like — one email per topic works best.\n\n— CIRRUS")
    if rec["tier"] >= dev_loop.TIER_DESIGN:
        eta = "It's been scheduled for an upcoming working session."
    else:
        eta = ("It's queued as a minor change and will be picked up in an "
               "upcoming build cycle.")
    return (f"Hi {name},\n\nGot it — your request has been received and logged:\n\n"
            f"    {rec['title']}\n\n{eta}\n"
            "You'll get another email when it ships. If anything about it is "
            "wrong, just reply to this email.\n\n— CIRRUS")


def send_ack(to_addr: str, rec: dict, creds: dict, orig_subject: str) -> bool:
    """SMTP ack from the research inbox. NOTE: creds keys outlook_* are
    legacy-misnamed — they hold the cirrustask@gmail.com sender (see
    COWORK-CONVENTIONS.md)."""
    try:
        from_email = creds["outlook_email"]
        password = creds["outlook_password"]
        msg = MIMEText(ack_body(rec))
        subj = orig_subject or rec["title"]
        msg["Subject"] = subj if subj.lower().startswith("re:") else f"Re: {subj}"
        msg["From"] = from_email
        msg["To"] = to_addr
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=60) as server:
            server.ehlo(); server.starttls(); server.ehlo()
            server.login(from_email, password)
            server.sendmail(from_email, [to_addr], msg.as_string())
        return True
    except Exception as e:
        log(f"ack send failed to {to_addr}: {e}")
        return False


def telegram(text: str, creds: dict) -> bool:
    """Send to Buddy. Tries Markdown first; on HTTP 400 (Telegram rejects
    unbalanced _/*/` entities) retries as plain text — same pattern as
    cirrus_bot.send_message. An alert must never be lost to formatting."""
    try:
        token, chat = creds["telegram_bot_token"], creds["telegram_user_id"]
    except Exception as e:
        log(f"telegram unavailable (creds: {e})")
        return False

    def _post(payload: dict) -> bool:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data,
            headers={"Content-Type": "application/json",
                     "User-Agent": "CirrusIntake/1.0"})
        urllib.request.urlopen(req, timeout=30).read()
        return True

    base = {"chat_id": int(chat), "text": text}
    try:
        return _post({**base, "parse_mode": "Markdown"})
    except Exception as e:
        log(f"telegram markdown send failed ({e}) — retrying plain")
        try:
            return _post(base)
        except Exception as e2:
            log(f"telegram send failed: {e2}")
            return False


# ── IMAP scan ─────────────────────────────────────────────────────────────────

def find_account(config: dict):
    for acct in config.get("email", {}).get("accounts", []):
        if acct.get("label") == INTAKE_ACCOUNT_LABEL:
            return acct
    return None


def scan_inbox(account: dict, password: str, allowlist: dict, state: dict,
               rescan: bool = False):
    """Yields (uid, from_addr, subject, body, message_id) for new mail from
    allowlisted senders. Uses BODY.PEEK (no \\Seen flag) and its own UID
    cursor so the digest pipeline is untouched."""
    mail = imaplib.IMAP4_SSL(account["imap_server"],
                             account.get("imap_port", 993), timeout=60)
    mail.login(account["address"], password)
    mail.select("inbox", readonly=True)

    uidvalidity = None
    try:
        typ, data = mail.status("inbox", "(UIDVALIDITY)")
        if typ == "OK" and data and data[0]:
            m = re.search(rb"UIDVALIDITY (\d+)", data[0])
            if m:
                uidvalidity = int(m.group(1))
    except Exception:
        pass

    last_uid = state.get("last_uid", 0)
    if uidvalidity is not None and state.get("uidvalidity") != uidvalidity:
        last_uid = 0
    if rescan:
        log("  rescan: ignoring last_uid (Message-ID dedupe still applies)")
        last_uid = 0
    state["uidvalidity"] = uidvalidity

    since = (datetime.now() - timedelta(days=DAYS_BACK)).strftime("%d-%b-%Y")
    _, msg_ids = mail.uid("search", None, f"SINCE {since}")
    uids = sorted(int(u) for u in msg_ids[0].split())
    new_uids = [u for u in uids if u > last_uid]
    log(f"inbox: {len(uids)} in window, {len(new_uids)} new (last_uid={last_uid})")

    out = []
    bounces = []
    seen_ids = set(state.get("seen_message_ids", [])[-500:])
    for uid in new_uids:
        try:
            _, msg_data = mail.uid("fetch", str(uid), "(BODY.PEEK[])")
            msg = email.message_from_bytes(msg_data[0][1])
            from_addr = (email.utils.parseaddr(msg.get("From", ""))[1] or "").lower()
            if BOUNCE_FROM_RX.match(from_addr):
                mid = msg.get("Message-ID", f"uid-{uid}")
                if mid not in seen_ids:
                    seen_ids.add(mid)
                    rcpt = extract_bounced_recipient(msg, account.get("address", ""))
                    subj = decode_hdr(msg.get("Subject", ""))[:80]
                    log(f"  BOUNCE detected: to={rcpt or 'unknown'} — '{subj}'")
                    bounces.append({"recipient": rcpt or "unknown", "subject": subj})
                continue
            if from_addr not in allowlist:
                log(f"  skipped (not allowlisted): {from_addr} — "
                    f"'{decode_hdr(msg.get('Subject', ''))[:60]}'")
                continue
            mid = msg.get("Message-ID", f"uid-{uid}")
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            out.append((uid, from_addr, decode_hdr(msg.get("Subject", "")),
                        body_text(msg), mid))
        except Exception as e:
            log(f"uid {uid}: parse error, skipping ({e})")
    try:
        mail.logout()
    except Exception:
        pass

    state["last_uid"] = max([state.get("last_uid", 0)] + new_uids) if new_uids else last_uid
    state["seen_message_ids"] = list(seen_ids)[-500:]
    return out, bounces


# ── Rate limiting ─────────────────────────────────────────────────────────────

def under_limit(state: dict, sender_name: str, limit: int) -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    counts = state.setdefault("daily_counts", {})
    if counts.get("_date") != today:
        counts.clear()
        counts["_date"] = today
    return counts.get(sender_name, 0) < limit


def bump_count(state: dict, sender_name: str):
    counts = state.setdefault("daily_counts", {})
    counts[sender_name] = counts.get(sender_name, 0) + 1


# ── Main run ──────────────────────────────────────────────────────────────────

def run(dry_run: bool = False, rescan: bool = False) -> int:
    allowlist = load_allowlist()
    if not allowlist:
        log("no configured senders in config/intake_senders.json — nothing to do "
            "(create it via vi on CIRRUS; template: intake_senders.template.json)")
        return 0

    config = load_json(CONFIG_PATH) or {}
    creds = load_json(CREDS_PATH) or {}
    account = find_account(config)
    if not account:
        log(f"ERROR: no '{INTAKE_ACCOUNT_LABEL}' account in sources.json")
        return 1
    password = creds.get(account.get("credential_key", ""))
    if not password:
        log(f"ERROR: no '{account.get('credential_key')}' in credentials.json")
        return 1

    state = load_state()
    try:
        messages, bounces = scan_inbox(account, password, allowlist, state,
                                       rescan=rescan)
    except Exception as e:
        log(f"ERROR: inbox scan failed: {e}")
        if not dry_run:
            telegram(f"⚠️ *Intake*: inbox scan failed: `{e}`", creds)
        return 1

    processed, limited = [], []
    for uid, from_addr, subject, body, mid in messages:
        entry = allowlist[from_addr]
        if entry["require_prefix"] and not REQUEST_RX.match(subject or ""):
            log(f"  skipped (no REQUEST: prefix, sender requires it): "
                f"{entry['name']} — '{(subject or '')[:60]}'")
            continue
        if not under_limit(state, entry["name"], entry["limit"]):
            limited.append((entry["name"], subject))
            log(f"rate-limited: {entry['name']} over {entry['limit']}/day — skipping '{subject}'")
            continue
        rec = classify(entry["name"], entry["projects"], subject, body)
        rec["from"] = from_addr
        rec["message_id"] = mid
        rec["kind"] = entry["request_kind"]
        # Research senders: a REPLY without an explicit REQUEST: subject is
        # feedback (e.g. Alyssa answering the intro email), not a topic —
        # don't pollute the topic queue with "Re: ..." subjects.
        if (rec["kind"] == "research"
                and (subject or "").lower().lstrip().startswith("re:")
                and not REQUEST_RX.match(subject or "")):
            rec["kind"] = "feedback"
        # Safety net: a sender on a research-only project (e.g. pedagogy)
        # ALWAYS routes as research, even if their request_kind is mis-set to
        # 'build'. Guarantees literacy requests reach the topic queue instead
        # of silently landing in the dev-ticket queue.
        if (rec["kind"] != "feedback"
                and any(p in RESEARCH_PROJECTS for p in entry["projects"])):
            if rec["kind"] != "research":
                log(f"  routing override: {entry['name']} is on a research-only "
                    f"project → treating as research (was '{rec['kind']}')")
            rec["kind"] = "research"
        # Research topics: when the subject is a bare keyword (e.g. 'RESEARCH')
        # with no REQUEST prefix, title the topic from the body instead of the
        # useless subject so the focus-topic queue stays meaningful.
        if rec["kind"] == "research":
            rec["title"] = research_topic_title(subject, body)
        log(f"request from {entry['name']}: '{rec['title']}' → tier {rec['tier']} "
            f"({rec['tier_name']}) [{rec['status']}]")
        if not dry_run:
            bump_count(state, entry["name"])
            append_backlog(rec)
            dev_loop.ledger_append({
                "event": "user-intake", "requester": entry["name"],
                "title": rec["title"], "tier": rec["tier"],
                "status": rec["status"], "spec_id": rec["dev_spec"]["id"],
            }, PROJECT_DIR)
            # Route by kind: research requests become focus topics for the
            # project's research digest; build requests (default) also land
            # in the ticket queue for the (future) dev-agent wiring.
            if rec["status"] != "refused":
                if rec["kind"] == "feedback":
                    log("  → feedback (reply) — backlogged for manual review, no topic")
                elif rec["kind"] == "research":
                    try:
                        proj = (entry["projects"] or ["general"])[0]
                        append_topic(proj, rec["title"], entry["name"])
                        log(f"  → focus topic queued for '{proj}'")
                    except Exception as e:
                        log(f"  topic append failed (backlog still recorded): {e}")
                else:
                    try:
                        ticket = dev_loop.ticket_create(
                            entry["name"], entry["projects"], rec["title"],
                            rec["body_head"][:400], origin="user-intake",
                            project_dir=PROJECT_DIR)
                        rec["ticket_id"] = ticket["id"]
                    except Exception as e:
                        log(f"  ticket enqueue failed (backlog still recorded): {e}")
            rec["ack_sent"] = send_ack(from_addr, rec, creds, subject)
        processed.append(rec)

    if not dry_run:
        save_state(state)

    # Delivery-failure alert: any outgoing mail from this inbox that bounced
    # (client acks, digests, intro emails) — tell Buddy who it failed to reach.
    if bounces:
        blines = [f"⚠️ *Intake*: {len(bounces)} delivery failure(s) in "
                  f"{account['address']}:"]
        for b in bounces:
            blines.append(f"• bounced: `{b['recipient']}` — _{b['subject']}_")
        blines.append("Check the address (intake_senders.json / recipient "
                      "config) and resend.")
        if dry_run:
            log("DRY RUN — would telegram:\n" + "\n".join(blines))
        else:
            telegram("\n".join(blines), creds)

    if processed or limited:
        lines = [f"📥 *Intake*: {len(processed)} new request(s)"]
        for r in processed:
            if r["status"] == "refused":
                flag = "🚫 REFUSED (never-auto)"
            elif r.get("kind") == "feedback":
                flag = "💬 FEEDBACK reply — review in logs/intake/"
            else:
                flag = f"tier {r['tier']} ({r['tier_name']})"
            ack = "" if r.get("ack_sent", True) else " — ⚠️ ack FAILED"
            lines.append(f"• {r['requester']}: _{r['title']}_ — {flag}{ack}")
        for name, subj in limited:
            lines.append(f"• ⚠️ {name} hit the daily rate limit (skipped: _{subj}_)")
        lines.append("Backlog: `logs/intake/` — build pipeline lands in P2.")
        if dry_run:
            log("DRY RUN — would telegram:\n" + "\n".join(lines))
        else:
            telegram("\n".join(lines), creds)
    else:
        log("no new intake requests")
    return 0


# ── Selftest (offline, no network) ───────────────────────────────────────────

def selftest() -> int:
    import tempfile
    failures = 0

    def check(name, cond):
        nonlocal failures
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            failures += 1

    # allowlist: placeholders inert, real entries parsed, case-insensitive
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "senders.json"
        p.write_text(json.dumps({
            "_comment": "x",
            "bill": {"emails": ["FILL-IN@example.com"], "projects": ["snow"]},
        }))
        check("template placeholders are inert", load_allowlist(p) == {})
        p.write_text(json.dumps({
            "bill": {"emails": ["Bill@Knight.COM"], "projects": ["snow"],
                      "max_requests_per_day": 2},
            "aggie": {"emails": ["ag@re.com"], "projects": ["realestate"]},
        }))
        al = load_allowlist(p)
        check("allowlist lowercased", "bill@knight.com" in al)
        check("projects routed", al["bill@knight.com"]["projects"] == ["snow"])
        check("custom limit kept", al["bill@knight.com"]["limit"] == 2)
        check("default limit", al["ag@re.com"]["limit"] == DEFAULT_DAILY_LIMIT)
        check("prefix default false", al["bill@knight.com"]["require_prefix"] is False)
        p.write_text(json.dumps({
            "buddy": {"emails": ["b@y.com"], "projects": ["snow"],
                       "require_request_prefix": True},
        }))
        al2 = load_allowlist(p)
        check("prefix flag parsed", al2["b@y.com"]["require_prefix"] is True)
        check("prefix gate: REQUEST passes", bool(REQUEST_RX.match("REQUEST: x")))
        check("prefix gate: Re: REQUEST passes", bool(REQUEST_RX.match("Re: request: x")))
        check("prefix gate: Fwd blocked", not REQUEST_RX.match("Fwd: research stuff"))
        check("prefix gate: plain blocked", not REQUEST_RX.match("more salt data"))
        p.write_text(json.dumps({
            "alyssa": {"emails": ["a@school.org"], "projects": ["pedagogy"],
                        "request_kind": "research"},
            "bill": {"emails": ["b@k.com"], "projects": ["snow"]},
            "bad": {"emails": ["x@y.com"], "request_kind": "banana"},
        }))
        al3 = load_allowlist(p)
        check("research kind parsed", al3["a@school.org"]["request_kind"] == "research")
        check("kind default build", al3["b@k.com"]["request_kind"] == "build")
        check("invalid kind falls back to build", al3["x@y.com"]["request_kind"] == "build")

    # research ack copy
    rec_r = classify("alyssa", ["pedagogy"], "REQUEST: multisyllabic decoding strategies", "")
    rec_r["kind"] = "research"
    check("research ack mentions research queue", "research queue" in ack_body(rec_r))

    # reply-as-feedback routing (research senders)
    def _route_kind(kind, subject):
        if (kind == "research" and (subject or "").lower().lstrip().startswith("re:")
                and not REQUEST_RX.match(subject or "")):
            return "feedback"
        return kind
    check("reply → feedback", _route_kind("research", "Re: Introducing your literacy research assistant") == "feedback")
    check("Re: REQUEST: stays research", _route_kind("research", "Re: REQUEST: fluency ideas") == "research")
    check("fresh subject stays research", _route_kind("research", "phonics small groups") == "research")
    check("build kind unaffected by Re:", _route_kind("build", "Re: bid spreadsheet") == "build")
    rec_f = classify("alyssa", ["pedagogy"], "Re: Introducing your literacy research assistant", "looks great!")
    rec_f["kind"] = "feedback"
    check("feedback ack thanks, offers REQUEST:", "Thanks for the note" in ack_body(rec_f)
          and "REQUEST:" in ack_body(rec_f))

    # topic append + dedupe (redirect PROJECT_DIR-relative path via monkeypatch)
    import tempfile as _tf
    global PROJECT_DIR
    _orig = PROJECT_DIR
    with _tf.TemporaryDirectory() as td2:
        PROJECT_DIR = Path(td2)
        try:
            path = append_topic("pedagogy", "fluency practice ideas", "alyssa")
            append_topic("pedagogy", "Fluency Practice Ideas", "alyssa")  # dupe, case-insens
            append_topic("pedagogy", "morphology", "alyssa")
            data = json.loads(path.read_text())
            check("topic file created", path.name == "topics-pedagogy.json")
            check("topics appended", len(data["topics"]) == 2)
            check("topic dedupe (case-insensitive)",
                  sum(1 for t in data["topics"]
                      if t["topic"].lower() == "fluency practice ideas") == 1)
            check("topic active status", all(t["status"] == "active" for t in data["topics"]))
        finally:
            PROJECT_DIR = _orig

    # subject parsing
    check("REQUEST: stripped", parse_request_title("REQUEST: faster bids") == "faster bids")
    check("Re: REQUEST: stripped", parse_request_title("Re: request:  x") == "x")
    check("plain subject kept", parse_request_title("more salt data") == "more salt data")
    check("empty subject", parse_request_title("") == "(no subject)")

    # classification: normal request = buildable tier, NEVER pattern refused
    rec = classify("bill", ["snow"], "REQUEST: add per-inch column to bids", "please")
    check("normal request backlogged", rec["status"] == "backlogged")
    check("normal request tier >= 0", rec["tier"] >= 0)
    check("spec origin tagged", rec["dev_spec"]["origin"] == "user-intake")
    rec2 = classify("bill", ["snow"], "REQUEST: delete all my old bid data", "")
    check("NEVER pattern refused", rec2["status"] == "refused"
          and rec2["tier"] == dev_loop.TIER_NEVER)
    rec3 = classify("aggie", ["realestate"], "REQUEST: share my login password", "")
    check("credential pattern refused", rec3["status"] == "refused")

    # ack copy
    check("refused ack mentions human", "human decision" in ack_body(rec2))
    check("minor ack mentions build",
          "build cycle" in ack_body(rec) or "scheduled" in ack_body(rec))

    # bounce detection
    check("mailer-daemon matches", bool(BOUNCE_FROM_RX.match("mailer-daemon@googlemail.com")))
    check("postmaster matches", bool(BOUNCE_FROM_RX.match("POSTMASTER@outlook.com")))
    check("normal sender no match", not BOUNCE_FROM_RX.match("bill@knight.com"))
    from email.message import EmailMessage
    bm = EmailMessage()
    bm["From"] = "Mail Delivery Subsystem <mailer-daemon@googlemail.com>"
    bm["Subject"] = "Delivery Status Notification (Failure)"
    bm["X-Failed-Recipients"] = "alyssa.wrong@avonworth.k12.pa.us"
    bm.set_content("Your message wasn't delivered to alyssa.wrong@avonworth.k12.pa.us "
                   "because the address couldn't be found.")
    check("bounce rcpt via header",
          extract_bounced_recipient(bm, "cirrustask@gmail.com")
          == "alyssa.wrong@avonworth.k12.pa.us")
    bm2 = EmailMessage()
    bm2["From"] = "mailer-daemon@googlemail.com"
    bm2["Subject"] = "Delivery Status Notification (Failure)"
    bm2.set_content("Sorry, delivery failed permanently.\n"
                    "Final-Recipient: rfc822; teacher@school.org\n"
                    "From: cirrustask@gmail.com")
    check("bounce rcpt via body (skips own address)",
          extract_bounced_recipient(bm2, "cirrustask@gmail.com") == "teacher@school.org")

    # rate limiting
    st = {}
    check("under limit initially", under_limit(st, "bill", 2))
    bump_count(st, "bill"); bump_count(st, "bill")
    check("limit enforced", not under_limit(st, "bill", 2))
    check("other sender unaffected", under_limit(st, "aggie", 2))

    print(f"selftest: {'OK' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


def peek(name_filter: str = "") -> int:
    """READ-ONLY operator view: print the latest allowlisted intake emails
    (full body head) without sending acks, writing state, or marking mail
    read. Mailbox is opened readonly + BODY.PEEK; the scan state is a
    throwaway dict that is never saved. Optional name_filter narrows to one
    sender (case-insensitive substring of the allowlist name)."""
    allowlist = load_allowlist()
    if not allowlist:
        log("no allowlist configured — nothing to peek")
        return 0
    config = load_json(CONFIG_PATH) or {}
    creds = load_json(CREDS_PATH) or {}
    account = next((a for a in config.get("email", {}).get("accounts", [])
                    if a.get("label") == INTAKE_ACCOUNT_LABEL), None)
    if not account:
        log(f"ERROR: no '{INTAKE_ACCOUNT_LABEL}' account in sources.json")
        return 1
    password = creds.get(account.get("credential_key", ""), "")
    if not password:
        log("ERROR: no credential for intake account")
        return 1
    throwaway = {}  # never saved — no cursor/seen-id writes
    try:
        messages, _bounces = scan_inbox(account, password, allowlist,
                                        throwaway, rescan=True)
    except Exception as e:
        log(f"peek scan failed: {e}")
        return 1
    shown = 0
    for _uid, from_addr, subject, body, _mid in messages:
        entry = allowlist[from_addr]
        if name_filter and name_filter.lower() not in entry["name"].lower():
            continue
        shown += 1
        print("=" * 70)
        print(f"FROM   : {entry['name']} <{from_addr}>")
        print(f"SUBJECT: {subject}")
        print(f"KIND   : {entry['request_kind']}")
        print(f"WOULD-QUEUE-AS: {research_topic_title(subject, body)}")
        print("-" * 70)
        print(body or "(empty body)")
    print("=" * 70)
    print(f"peek: {shown} allowlisted message(s)"
          f"{' matching ' + name_filter if name_filter else ''}"
          " — read-only, no acks/state writes")
    return 0


def requeue(name_filter: str) -> int:
    """Recover a mis-routed request: re-queue the LATEST allowlisted message
    from <name_filter> as a research focus topic (topics-<project>.json).

    Mailbox is opened readonly + BODY.PEEK; the ONLY write is the topic
    append (which itself dedupes). No ack email, no dev ticket, no state or
    cursor write. Use after a routing fix to land a request that was
    previously mis-classified as 'build'."""
    if not name_filter:
        log("requeue: a sender name is required (e.g. --requeue alyssa)")
        return 1
    allowlist = load_allowlist()
    if not allowlist:
        log("no allowlist configured")
        return 1
    config = load_json(CONFIG_PATH) or {}
    creds = load_json(CREDS_PATH) or {}
    account = next((a for a in config.get("email", {}).get("accounts", [])
                    if a.get("label") == INTAKE_ACCOUNT_LABEL), None)
    if not account:
        log(f"ERROR: no '{INTAKE_ACCOUNT_LABEL}' account in sources.json")
        return 1
    password = creds.get(account.get("credential_key", ""), "")
    if not password:
        log("ERROR: no credential for intake account")
        return 1
    throwaway = {}  # never saved
    try:
        messages, _bounces = scan_inbox(account, password, allowlist,
                                        throwaway, rescan=True)
    except Exception as e:
        log(f"requeue scan failed: {e}")
        return 1
    latest = None
    for m in messages:  # scan_inbox yields ascending UID → last match = newest
        entry = allowlist[m[1]]
        if name_filter.lower() in entry["name"].lower():
            latest = (m, entry)
    if not latest:
        log(f"requeue: no allowlisted message from '{name_filter}' in window")
        return 1
    (_uid, _from, subject, body, _mid), entry = latest
    title = research_topic_title(subject, body)
    proj = (entry["projects"] or ["general"])[0]
    try:
        path = append_topic(proj, title, entry["name"])
    except Exception as e:
        log(f"requeue: topic append failed: {e}")
        return 1
    log(f"requeue: queued research topic for '{entry['name']}' → {proj}")
    log(f"  topic: {title}")
    log(f"  file : {path}")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    names = [a for a in args if not a.startswith("-")]
    if "selftest" in args:
        sys.exit(selftest())
    if "--peek" in args:
        sys.exit(peek(names[0] if names else ""))
    if "--requeue" in args:
        sys.exit(requeue(names[0] if names else ""))
    sys.exit(run(dry_run="--dry-run" in args, rescan="--rescan" in args))
