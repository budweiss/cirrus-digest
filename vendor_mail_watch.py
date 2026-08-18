#!/usr/bin/env python3
"""
vendor_mail_watch.py  (S67, 2026-08-18)
===============================================================================
Watch the operational inboxes for VENDOR and ACCOUNT mail -- funds exhausted,
credits expiring, quota caps, payment failures, key/credential expiry, service
deprecation, suspensions, security alerts -- and track each one until it is
resolved.

WHY THIS EXISTS
---------------
Buddy spotted a Brave Search API alert in cirrustask@gmail.com by eye. Nothing
in the system was watching for it, and the two components that DO read that
mailbox were both structurally incapable of surfacing it:

  * `cirrus_daily.fetch_emails()` requires an AI-topic keyword match AND drops
    anything whose subject contains "billing", "invoice", "subscription
    renewal" as transactional noise (`_OMIT_SUBJECT_PATTERNS`). A vendor billing
    alert is discarded twice over.
  * `intake.py` hard-gates on the client allowlist, so mail from a vendor is
    skipped with a log line nobody reads.

Both are correct for their own jobs. The gap is that no third thing existed.
This is that third thing, and it is deliberately a SEPARATE reader rather than a
loosened filter on either -- widening `fetch_emails` to admit billing mail would
pollute the digest, and adding vendors to the intake allowlist would route them
into client request handling.

DESIGN NOTES (each one is a mistake this codebase has already made once)
-----------------------------------------------------------------------
* **Read-only, own state file.** Opens mailboxes `readonly=True` and keeps its
  own ledger. It never advances the digest's or intake's UID cursor -- S66 lost
  40 Medium emails to exactly that kind of shared-cursor theft.
* **Filter SERVER-SIDE.** One IMAP SEARCH per signal phrase rather than pulling
  the newest N and filtering locally. Buddy's Yahoo inbox takes ~660 messages
  per two days; client-side filtering there returned zero relevant mail in S66
  because the cap was applied before the filter.
* **Match on SIGNAL, not on sender.** A sender allowlist cannot work here: the
  whole point is to catch mail from a vendor we did not think to list, including
  services we sign up for later. The net is wide and the classifier narrows it.
* **Fail OPEN, everywhere.** If the local model is down, unsure, or unparseable,
  the item is KEPT and alerted. A watcher that silently drops on failure is
  worse than no watcher, because it manufactures confidence.
* **Nothing is marked seen before it is recorded.** Ledger write happens after
  classification, per the S66 fetch/mark contract.
* **Open items re-surface.** "Track them down" means an unresolved item is
  re-alerted on a cadence, not reported once and forgotten. Seen-once-then-
  silent is how the Brave alert would still have been missed even with a
  watcher.

Stdlib only apart from `requests` (used only for the optional local model).

Usage:
    python3 vendor_mail_watch.py                 # live: scan, alert, record
    python3 vendor_mail_watch.py --dry-run       # scan + PRINT, no alerts/writes
    python3 vendor_mail_watch.py --selftest      # offline unit tests
    python3 vendor_mail_watch.py --report        # print the open ledger
    python3 vendor_mail_watch.py --resolve <id>  # mark one item resolved
"""

import email as _email
import imaplib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from pathlib import Path

PROJECT_DIR = Path.home() / "projects/cirrus-digest"
if not PROJECT_DIR.exists():                       # CUMULUS layout
    PROJECT_DIR = Path.home() / "cirrus-digest"
sys.path.insert(0, str(PROJECT_DIR))

CREDS_PATH = PROJECT_DIR / "config/credentials.json"
SOURCES_PATH = PROJECT_DIR / "config/sources.json"
LEDGER_PATH = PROJECT_DIR / "config/vendor_mail_ledger.json"
LOOKBACK_DAYS = 4          # > the daily cadence, so a missed run self-heals
MAX_PER_SEARCH = 40        # per phrase per account; these searches are narrow
REALERT_DAYS = 3           # an unresolved item nags again this often


# ── Signal catalogue ─────────────────────────────────────────────────────────
# (category, severity, [phrases]). Phrases are what we hand to IMAP SEARCH
# SUBJECT, so they must be literal substrings a vendor would actually write --
# not regexes and not topic words.
#
# Severity drives the channel: "critical" Telegrams immediately (something is
# broken or about to break); "warn" waits for the morning brief. Getting this
# split wrong in either direction is costly -- a Telegram for every marketing
# email trains Buddy to ignore them, which is the same outcome as no alert.
SIGNALS = [
    ("funds", "critical", [
        "insufficient funds", "out of credits", "credits expired",
        "credit balance", "funds exhausted", "balance is low", "low balance",
        "top up", "add funds", "auto-reload failed",
    ]),
    ("payment", "critical", [
        "payment failed", "payment declined", "card declined",
        "payment method", "past due", "unpaid invoice", "billing problem",
        "update your payment",
    ]),
    ("access", "critical", [
        "account suspended", "account deactivated", "service suspended",
        "api access disabled", "access revoked", "account closed",
        "will be terminated", "subscription cancelled", "subscription canceled",
        # S67 first live dry-run: "Your account will be permanently deleted in
        # 24 hours" scored only `warn`, because the sole critical phrase it hit
        # ("payment method") was in the BODY and body-only hits are downgraded.
        # Losing an account in 24h is exactly the case the Telegram path exists
        # for, so the subject wording gets its own critical phrases. Calibrating
        # on real mail rather than on imagined mail is why we dry-run first.
        "will be permanently deleted", "will be deleted", "account deletion",
        "will be closed", "scheduled for deletion",
    ]),
    ("credential", "critical", [
        "api key", "key expires", "key expired", "token expires",
        "token expired", "credential expires", "rotate your", "regenerate your key",
        "certificate expires",
    ]),
    ("security", "critical", [
        "security alert", "suspicious activity", "unusual sign-in",
        "new sign-in", "unauthorized access", "data breach", "password was reset",
    ]),
    ("quota", "warn", [
        "usage threshold", "usage limit", "quota", "rate limit",
        "exceeded your", "approaching your limit", "% of your",
    ]),
    ("deprecation", "warn", [
        "deprecat", "end of life", "will be retired", "sunset",
        "breaking change", "no longer supported", "migrate to",
    ]),
    ("plan", "warn", [
        "plan change", "price increase", "pricing update", "renewal",
        "trial ends", "trial expires", "downgrade",
    ]),
]

SEVERITY_ORDER = {"critical": 0, "warn": 1}

# Mail that matches a phrase but is plainly marketing. Checked BEFORE the model
# so the obvious noise never costs anything. Kept short on purpose: this list is
# a cost optimisation, not the actual filter, and every entry here is a chance
# to discard something real.
_OBVIOUS_NOISE = [
    "newsletter", "webinar", "unsubscribe from marketing", "case study",
    "join us at", "register now", "black friday", "% off your first",
]


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ── Classification ───────────────────────────────────────────────────────────
def classify(subject: str, body: str, sender: str) -> tuple:
    """(category, severity, matched_phrase) or (None, None, None).

    Deterministic and offline. Subject is weighted over body: a vendor announces
    the actual event in the subject line, whereas bodies quote boilerplate
    ("if your payment fails...") in footers constantly.
    """
    subj = (subject or "").lower()
    text = f"{subj}\n{(body or '')[:4000].lower()}"

    best = None
    for category, severity, phrases in SIGNALS:
        for phrase in phrases:
            in_subject = phrase in subj
            if not in_subject and phrase not in text:
                continue
            # A body-only hit is downgraded: it is far more often boilerplate
            # than an actual event, but discarding it outright would miss the
            # vendors who put the real news below a banner image.
            sev = severity if in_subject else "warn"
            cand = (category, sev, phrase)
            if best is None or SEVERITY_ORDER[sev] < SEVERITY_ORDER[best[1]]:
                best = cand
            if in_subject and severity == "critical":
                return cand
    return best if best else (None, None, None)


def looks_like_marketing(subject: str, body: str) -> bool:
    blob = f"{(subject or '').lower()}\n{(body or '')[:2000].lower()}"
    return any(n in blob for n in _OBVIOUS_NOISE)


_LLM_PROMPT = """You are triaging an email for an automated infrastructure monitor.

Answer with exactly one word: ACTION or IGNORE.

Answer ACTION if the email reports something about OUR account that a system
operator must act on or track: funds/credits running out or expired, a payment
that failed, an account or API suspended, a key/token/certificate expiring, a
usage cap reached, a service being deprecated or shut down, or a security event.

Answer IGNORE if it is marketing, a product announcement, a newsletter, a
receipt for a successful payment, or a routine notification requiring no action.

Subject: {subject}
From: {sender}
Body: {body}

One word:"""


def confirm_with_local_model(subject: str, sender: str, body: str) -> tuple:
    """Second-pass triage on CIRRUS's own Ollama -- zero marginal cost.

    FAILS OPEN in every direction: model down, unparseable answer, timeout, or
    anything unexpected keeps the item. The failure we care about is a missed
    vendor alert, not a false positive -- a false positive costs one line in a
    report, a false negative costs an expired API key nobody noticed.
    """
    try:
        import cirrus_daily as B
        import requests
        r = requests.post(
            f"{B.OLLAMA_HOST}/api/generate",
            json={"model": B.MODEL,
                  "prompt": _LLM_PROMPT.format(subject=(subject or "")[:200],
                                               sender=(sender or "")[:120],
                                               body=(body or "")[:3000]),
                  "stream": False,
                  "options": {"temperature": 0, "num_ctx": 4096}},
            timeout=60)
        r.raise_for_status()
        ans = (r.json().get("response") or "").strip().upper()
        if ans.startswith("IGNORE"):
            return False, "local triage: marketing/no-action"
        if ans.startswith("ACTION"):
            return True, "local triage: actionable"
        return True, "local triage unparseable — kept (fail-open)"
    except Exception as e:
        return True, f"local triage unavailable ({type(e).__name__}) — kept (fail-open)"


def vendor_from_sender(sender: str) -> str:
    """Best-effort vendor name from the From header's domain."""
    m = re.search(r"@([\w.-]+)", sender or "")
    if not m:
        return (sender or "unknown").strip()[:40] or "unknown"
    host = m.group(1).lower()
    parts = [p for p in host.split(".") if p not in ("com", "net", "org", "io",
                                                     "co", "ai", "www", "email",
                                                     "mail", "notifications")]
    return (parts[-1] if parts else host)[:40]


# ── Ledger ───────────────────────────────────────────────────────────────────
def load_ledger() -> dict:
    try:
        return json.loads(LEDGER_PATH.read_text())
    except Exception:
        return {}


def save_ledger(ledger: dict) -> None:
    try:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = LEDGER_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(ledger, indent=2))
        os.replace(tmp, LEDGER_PATH)
        os.chmod(LEDGER_PATH, 0o600)
    except Exception as e:
        log(f"  WARNING: could not write ledger: {e}")


def due_for_realert(entry: dict, now: datetime) -> bool:
    """An OPEN item nags again after REALERT_DAYS.

    This is the half that makes it 'track them down' rather than 'notice once'.
    A one-shot alert is only as good as the moment it arrives; the Brave alert
    sat unread for two days.
    """
    if entry.get("status") != "open":
        return False
    last = entry.get("last_alerted") or entry.get("first_seen")
    try:
        return (now - datetime.fromisoformat(last)).days >= REALERT_DAYS
    except Exception:
        return True


# ── Mail ─────────────────────────────────────────────────────────────────────
def _decode(v: str) -> str:
    try:
        return str(make_header(decode_header(v or "")))
    except Exception:
        return v or ""


def _body_of(msg) -> str:
    try:
        if msg.is_multipart():
            text = ""
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain":
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "ignore")
                if ct == "text/html" and not text:
                    text = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "ignore")
            return text
        return msg.get_payload(decode=True).decode(
            msg.get_content_charset() or "utf-8", "ignore")
    except Exception:
        return ""


def all_phrases() -> list:
    seen, out = set(), []
    for _cat, _sev, phrases in SIGNALS:
        for p in phrases:
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def fetch_candidates(creds: dict) -> list:
    """Server-side IMAP search across every enabled account, one query per
    signal phrase. Returns [{message_id, subject, sender, body, account}]."""
    try:
        cfg = json.loads(SOURCES_PATH.read_text())
        accounts = (cfg.get("email") or {}).get("accounts", []) or []
    except Exception as e:
        log(f"  could not read sources.json: {e}")
        return []

    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
    phrases = all_phrases()
    out, seen_mids = [], set()

    for account in accounts:
        if not account.get("enabled", True):
            log(f"  skipping {account.get('label')}: disabled in config")
            continue
        password = creds.get(account.get("credential_key", ""))
        addr = account.get("address")
        if not password or not addr:
            log(f"  skipping {account.get('label')}: no credential")
            continue
        label = account.get("label", addr)
        try:
            mail = imaplib.IMAP4_SSL(account["imap_server"],
                                     account.get("imap_port", 993), timeout=60)
            mail.login(addr, password)
            mail.select("inbox", readonly=True)     # never marks anything read

            uids = []
            for phrase in phrases:
                try:
                    _t, ids = mail.uid("search", None,
                                       f'(SINCE {since} SUBJECT "{phrase}")')
                except Exception:
                    continue
                if ids and ids[0]:
                    uids.extend(ids[0].split()[-MAX_PER_SEARCH:])
            uids = sorted(set(uids), key=lambda u: int(u), reverse=True)
            log(f"  {label}: {len(uids)} candidate message(s)")

            for uid in uids:
                try:
                    _t, data = mail.uid("fetch", uid, "(RFC822)")
                    msg = _email.message_from_bytes(data[0][1])
                except Exception:
                    continue
                mid = (msg.get("Message-ID") or "").strip()
                if not mid or mid in seen_mids:
                    continue
                seen_mids.add(mid)
                out.append({
                    "message_id": mid,
                    "subject": _decode(msg.get("Subject", "")).strip(),
                    "sender": _decode(msg.get("From", "")).strip(),
                    "date": (msg.get("Date") or "").strip(),
                    "body": _body_of(msg),
                    "account": label,
                })
            mail.logout()
        except Exception as e:
            log(f"  {label}: IMAP error {type(e).__name__}: {e}")
            continue
    return out


# ── Alerting ─────────────────────────────────────────────────────────────────
def telegram(msg: str) -> bool:
    try:
        creds = json.loads(CREDS_PATH.read_text())
        token = creds.get("telegram_bot_token", "")
        user = str(creds.get("telegram_user_id", "")).strip()
        if not token or not user:
            return False
        data = urllib.parse.urlencode({"chat_id": user, "text": msg}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=20) as r:
            return bool(json.loads(r.read()).get("ok"))
    except Exception:
        return False


def format_alert(entries: list) -> str:
    lines = ["⚠️ Vendor/account mail needs attention", ""]
    for e in entries:
        age = ""
        try:
            d = (datetime.now() - datetime.fromisoformat(e["first_seen"])).days
            age = f"  (open {d}d)" if d >= 1 else ""
        except Exception:
            pass
        lines.append(f"• [{e['severity']}] {e['vendor']} — {e['category']}{age}")
        lines.append(f"  {e['subject'][:120]}")
    lines.append("")
    lines.append("Resolve: vendor_mail_watch.py --resolve <message-id>")
    return "\n".join(lines)


def open_items(ledger: dict) -> list:
    items = [e for e in ledger.values() if e.get("status") == "open"]
    items.sort(key=lambda e: (SEVERITY_ORDER.get(e.get("severity"), 9),
                              e.get("first_seen", "")))
    return items


# ── Main ─────────────────────────────────────────────────────────────────────
def run(dry_run: bool = False) -> dict:
    now = datetime.now()
    log(f"vendor_mail_watch ({'dry-run' if dry_run else 'live'})")

    try:
        creds = json.loads(CREDS_PATH.read_text())
    except Exception as e:
        log(f"!! no credentials: {e}")
        return {"ok": False, "note": f"no credentials: {e}"}

    ledger = load_ledger()
    candidates = fetch_candidates(creds)
    log(f"→ {len(candidates)} message(s) matched a signal phrase")

    new_entries, noise = [], 0
    for c in candidates:
        if c["message_id"] in ledger:
            continue                                    # already tracked
        category, severity, phrase = classify(c["subject"], c["body"], c["sender"])
        if not category:
            continue
        if looks_like_marketing(c["subject"], c["body"]):
            noise += 1
            continue
        keep, why = confirm_with_local_model(c["subject"], c["sender"], c["body"])
        if not keep:
            noise += 1
            continue
        entry = {
            "message_id": c["message_id"],
            "vendor": vendor_from_sender(c["sender"]),
            "category": category,
            "severity": severity,
            "subject": c["subject"],
            "sender": c["sender"],
            "account": c["account"],
            "mail_date": c["date"],
            "matched": phrase,
            "triage": why,
            "first_seen": now.isoformat(timespec="seconds"),
            "last_alerted": None,
            "status": "open",
        }
        new_entries.append(entry)
        log(f"   + [{severity}] {entry['vendor']}: {c['subject'][:80]}")

    # Re-surface anything still unresolved.
    stale = [e for e in ledger.values() if due_for_realert(e, now)]

    to_alert = new_entries + stale
    critical = [e for e in to_alert if e.get("severity") == "critical"]

    if dry_run:
        log(f"DRY-RUN: {len(new_entries)} new, {len(stale)} re-surfaced, "
            f"{noise} filtered as noise. Nothing written or sent.")
        if to_alert:
            print()
            print(format_alert(to_alert))
        return {"ok": True, "note": "dry-run", "new": len(new_entries)}

    # Record BEFORE alerting: an alert that fails to send is recoverable on the
    # next run, but an item dropped because the process died mid-alert is not.
    for e in new_entries:
        ledger[e["message_id"]] = e
    save_ledger(ledger)

    sent = False
    if critical:
        sent = telegram(format_alert(critical))
        log(f"→ Telegram {'sent' if sent else 'FAILED'} for {len(critical)} critical item(s)")
    if to_alert:
        stamp = now.isoformat(timespec="seconds")
        for e in to_alert:
            ledger.setdefault(e["message_id"], e)["last_alerted"] = stamp
        save_ledger(ledger)

    n_open = len(open_items(ledger))
    note = (f"{len(new_entries)} new, {len(stale)} re-surfaced, "
            f"{n_open} open, {noise} noise")
    log(f"done — {note}")
    try:
        import job_status
        job_status.record("vendormail", True, note)
    except Exception as e:
        log(f"  (job_status unavailable: {e})")
    return {"ok": True, "note": note, "new": len(new_entries), "open": n_open}


def report() -> None:
    ledger = load_ledger()
    items = open_items(ledger)
    if not items:
        print("No open vendor/account items.")
        return
    print(f"{len(items)} open item(s):\n")
    for e in items:
        print(f"  [{e['severity']:8}] {e['vendor']:14} {e['category']:12} "
              f"{e['first_seen'][:10]}  {e['subject'][:70]}")
        print(f"             id: {e['message_id']}")


def resolve(message_id: str, note: str = "") -> None:
    ledger = load_ledger()
    hits = [k for k in ledger if message_id in k]
    if not hits:
        print(f"No ledger entry matching {message_id!r}")
        return
    for k in hits:
        ledger[k]["status"] = "resolved"
        ledger[k]["resolved_at"] = datetime.now().isoformat(timespec="seconds")
        if note:
            ledger[k]["note"] = note
        print(f"resolved: {ledger[k]['vendor']} — {ledger[k]['subject'][:70]}")
    save_ledger(ledger)


# ── Selftest ─────────────────────────────────────────────────────────────────
def selftest() -> bool:
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))

    # The email that started this. Subject taken from the real message.
    cat, sev, _p = classify("Brave Search API usage threshold alert",
                            "your Brave Search API usage has crossed a configured "
                            "alert threshold for the current billing period", "search-api@brave.com")
    ck("the real Brave alert is caught", cat == "quota")

    cat, sev, _ = classify("Action required: payment failed", "your card was declined", "billing@x.com")
    ck("payment failure is critical", cat == "payment" and sev == "critical")

    cat, sev, _ = classify("Your API key expires in 7 days", "rotate it", "no-reply@y.com")
    ck("key expiry is critical", cat == "credential" and sev == "critical")

    cat, sev, _ = classify("Account suspended", "we have suspended your account", "z@z.com")
    ck("suspension is critical", cat == "access" and sev == "critical")

    # Regression for the S67 dry-run miss: this exact subject scored warn.
    cat, sev, _ = classify("Your account will be permanently deleted in 24 hours",
                           "update your payment method to keep it", "x@x.com")
    ck("imminent account deletion is critical",
       cat == "access" and sev == "critical")

    # A subject-line hit must outrank a body-only hit.
    _c, sev_body, _ = classify("Monthly product update",
                               "footer: if your payment failed, update your card", "n@n.com")
    ck("body-only match is downgraded to warn", sev_body == "warn")

    cat, _s, _ = classify("Our new AI features are here", "check out what's new", "news@vendor.com")
    ck("ordinary marketing matches nothing", cat is None)

    ck("obvious marketing is filtered",
       looks_like_marketing("Quota webinar", "join our webinar, unsubscribe from marketing"))
    ck("a real alert is not filtered as marketing",
       not looks_like_marketing("Brave Search API usage threshold alert",
                                "your usage has crossed a threshold"))

    ck("vendor name from sender domain", vendor_from_sender("search-api@brave.com") == "brave")
    ck("vendor name ignores mail subdomains",
       vendor_from_sender("Billing <no-reply@notifications.anthropic.com>") == "anthropic")

    # Fail-open contract: the whole point is that a broken local model does not
    # silently swallow alerts. Force the import to fail and assert we keep.
    _real = sys.modules.get("cirrus_daily")
    sys.modules["cirrus_daily"] = None
    try:
        keep, why = confirm_with_local_model("Payment failed", "a@b.com", "x")
        ck("local model unavailable -> item is KEPT (fail-open)", keep is True)
        ck("fail-open reason is explicit", "fail-open" in why)
    finally:
        if _real is not None:
            sys.modules["cirrus_daily"] = _real
        else:
            sys.modules.pop("cirrus_daily", None)

    now = datetime(2026, 8, 18, 12, 0, 0)
    old = (now - timedelta(days=REALERT_DAYS)).isoformat()
    fresh = (now - timedelta(days=1)).isoformat()
    ck("stale open item re-alerts",
       due_for_realert({"status": "open", "last_alerted": old}, now))
    ck("recent open item does not re-alert",
       not due_for_realert({"status": "open", "last_alerted": fresh}, now))
    ck("resolved item never re-alerts",
       not due_for_realert({"status": "resolved", "last_alerted": old}, now))
    ck("open item with no alert history re-alerts",
       due_for_realert({"status": "open", "first_seen": old}, now))

    ck("every signal phrase is IMAP-safe (no quotes)",
       all('"' not in p and "\\" not in p for p in all_phrases()))

    bad = 0
    for name, ok in checks:
        print(("  ok   " if ok else "  FAIL ") + name)
        bad += 0 if ok else 1
    print()
    print("all vendor_mail_watch selftests passed" if not bad else f"{bad} FAILED")
    return bad == 0


def main():
    argv = sys.argv[1:]
    if "--selftest" in argv:
        raise SystemExit(0 if selftest() else 1)
    if "--report" in argv:
        report()
        return
    if "--resolve" in argv:
        i = argv.index("--resolve")
        if i + 1 < len(argv):
            resolve(argv[i + 1], " ".join(argv[i + 2:]))
        else:
            print("usage: --resolve <message-id-substring> [note]")
        return
    run(dry_run="--dry-run" in argv or "--dry" in argv)


if __name__ == "__main__":
    main()
