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

REQUEST_RX = re.compile(r"^\s*(re:\s*)?request\s*:\s*", re.IGNORECASE)


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
    """'REQUEST: faster bids' → 'faster bids'; otherwise the subject as-is."""
    return REQUEST_RX.sub("", subject or "").strip() or "(no subject)"


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
    try:
        token, chat = creds["telegram_bot_token"], creds["telegram_user_id"]
        data = json.dumps({"chat_id": int(chat), "text": text,
                           "parse_mode": "Markdown"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data,
            headers={"Content-Type": "application/json",
                     "User-Agent": "CirrusIntake/1.0"})
        urllib.request.urlopen(req, timeout=30).read()
        return True
    except Exception as e:
        log(f"telegram send failed: {e}")
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
    seen_ids = set(state.get("seen_message_ids", [])[-500:])
    for uid in new_uids:
        try:
            _, msg_data = mail.uid("fetch", str(uid), "(BODY.PEEK[])")
            msg = email.message_from_bytes(msg_data[0][1])
            from_addr = (email.utils.parseaddr(msg.get("From", ""))[1] or "").lower()
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
    return out


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
        messages = scan_inbox(account, password, allowlist, state, rescan=rescan)
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
                if rec["kind"] == "research":
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

    if processed or limited:
        lines = [f"📥 *Intake*: {len(processed)} new request(s)"]
        for r in processed:
            flag = "🚫 REFUSED (never-auto)" if r["status"] == "refused" else \
                   f"tier {r['tier']} ({r['tier_name']})"
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

    # rate limiting
    st = {}
    check("under limit initially", under_limit(st, "bill", 2))
    bump_count(st, "bill"); bump_count(st, "bill")
    check("limit enforced", not under_limit(st, "bill", 2))
    check("other sender unaffected", under_limit(st, "aggie", 2))

    print(f"selftest: {'OK' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if "selftest" in args:
        sys.exit(selftest())
    sys.exit(run(dry_run="--dry-run" in args, rescan="--rescan" in args))
