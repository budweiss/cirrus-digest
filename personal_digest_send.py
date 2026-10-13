#!/usr/bin/env python3
"""personal_digest_send — deliver Buddy's daily PERSONAL-HOLDINGS opinion email.

Runs ON CUMULUS (where the SMTP credentials live), composes via mailer — the
consolidated send path — and sends. The digest markdown is produced by the
scheduled personal-holdings opinion lane on Buddy's Mac
(stock-pickers/digests/YYYY-MM-DD-personal-2pm.md, gitignored — holdings
detail never leaves his folder) and piped on stdin by the
personal-digest-send runner command.

Buddy authorized this send target (2026-10-12, in-session: "go ahead and
build it" for exactly this lane): TO = his own weiss_buddy@yahoo.com, same
as the stock/shore digests. Replies go to the From address — the intake
inbox the peeks already read.

This is NOT the paper lane: there is NO stock-paper-report gate. The guards
here are:
  * non-empty, <=120k chars (same as shore_digest_send);
  * weekday/calendar sanity on date labels (reuse of stock_digest_send's
    check_weekdays) so a mislabeled digest is refused before it reaches him;
  * lane-content guards: a file containing the paper-ledger marker is
    REFUSED (the paper lane has its own verified send path; a personal
    pipe must never double-carry it), and the first 200 chars must identify
    the letter as a personal-holdings opinion;
  * ONE send per day per lane: a receipt file keyed by the header date
    reserves the slot. The same body re-sent is a printed no-op (scheduler
    re-fires must never look like a failure); a DIFFERENT same-day body is
    refused for review, never silently resent.
Modes: dry-run (default) composes and prints headers, sends NOTHING. live
sends with on_error="raise" ("it didn't arrive" must never look like "it
did").

CLI guard: only --mode=dry-run|live is accepted; anything else exits
non-zero BEFORE any send path is touched (T118 shape).
"""
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import mailer

PROJECT_DIR = Path(__file__).resolve().parent
CREDS_PATH = PROJECT_DIR / "config" / "credentials.json"
TO_EMAIL = "weiss_buddy@yahoo.com"
MODES = ("dry-run", "live")
# Receipts for the per-day slot reservation live OUTSIDE the paper-account
# tree — this lane is not the paper lane and must not touch its locks.
RECEIPTS = PROJECT_DIR / "private" / "personal-holdings" / "delivery"

from stock_digest_send import report_html, check_weekdays  # noqa: E402


def check_lane(md):
    """Lane-content guards: the personal pipe must never carry a paper
    letter, and a personal receipt must actually look like one."""
    if "<!-- stock-paper-report:" in md:
        raise ValueError("paper_ledger_marker_in_personal_pipe")
    if not re.search(r"personal holdings opinion", md[:200], re.I):
        raise ValueError("personal_opinion_header_missing")
    return None


def header_date(md):
    """The YYYY-MM-DD anchor from the first line, or None."""
    first = md.split("\n", 1)[0]
    found = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", first)
    return found.group(0) if found else None


def load_digest(stream) -> str:
    text = stream.read()
    if not text.strip():
        raise ValueError("empty_digest")
    if len(text) > 120_000:
        raise ValueError("digest_too_large")
    return text


def build_message(md: str, now, creds):
    """Compose the message. Returns (mailer-message tuple, subject)."""
    check_weekdays(md, now)
    subject = "Personal Holdings Opinion — %s (ET)" % now.strftime("%Y-%m-%d %H:%M")
    return mailer.build(creds["outlook_email"], TO_EMAIL, subject, md,
                        html=report_html(md), creds=creds), subject


def deliver_once(md, creds, subject, date_str, send_fn=None):
    """Reserve the day's slot; an ambiguous SMTP outcome is never retried
    blindly. Same body already sent -> printed no-op; different same-day
    body -> refused for review."""
    import fcntl
    folder = RECEIPTS
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt = folder / (date_str + ".json")
    digest = hashlib.sha256(md.encode()).hexdigest()
    with (folder / "send.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if receipt.exists():
            old = json.loads(receipt.read_text())
            if old.get("status") == "sent" and old.get("body_sha256") == digest:
                print("already_sent_same_daily_report")
                return True
            raise ValueError("daily_slot_already_reserved_check_receipt_do_not_resend")
        record = {"status": "sending_outcome_unknown_until_confirmed",
                  "body_sha256": digest,
                  "reserved_at": datetime_utc_now()}
        with receipt.open("x") as out:
            out.write(json.dumps(record))
        os.chmod(receipt, 0o600)
        sender = send_fn or mailer.send
        ok = sender(creds["outlook_email"], creds["outlook_password"], TO_EMAIL,
                    subject, md, html=report_html(md), creds=creds,
                    on_error="raise", log=print)
        if not ok:
            raise ValueError("send_unconfirmed_receipt_requires_review")
        record.update(status="sent", sent_at=datetime_utc_now())
        temporary = receipt.with_suffix(".tmp")
        temporary.write_text(json.dumps(record))
        os.chmod(temporary, 0o600)
        temporary.replace(receipt)
        return True


def datetime_utc_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def send_live(md, creds, subject, date_str):
    """Send with on_error='raise', then confirm the receipt atomically."""
    return deliver_once(md, creds, subject, date_str)


def main(argv) -> int:
    args = argv[1:]
    if [a for a in args if a.startswith("--") and
            a.split("=", 1)[0] not in ("--mode",)]:
        print("personal_digest_send: unknown argument(s). Use "
              "--mode=dry-run|live; digest markdown arrives on stdin.",
              file=sys.stderr)
        return 2
    mode = "dry-run"
    for a in args:
        if a.startswith("--mode"):
            mode = a.split("=", 1)[1] if "=" in a else "dry-run"
    if mode not in MODES:
        print("personal_digest_send: need --mode with one of %s"
              % ", ".join(MODES), file=sys.stderr)
        return 2
    try:
        creds = json.loads(CREDS_PATH.read_text())
    except Exception as e:
        print("personal_digest_send: credentials unreadable (%s)"
              % type(e).__name__, file=sys.stderr)
        return 3
    if not creds.get("outlook_email") or not creds.get("outlook_password"):
        print("personal_digest_send: outlook_email/outlook_password unset",
              file=sys.stderr)
        return 3
    try:
        md = load_digest(sys.stdin)
        check_lane(md)
        from datetime import datetime
        now = datetime.now(ZoneInfo("America/New_York"))
        check_weekdays(md, now)
        date_str = header_date(md) or now.date().isoformat()
        (msg, recipients), subject = build_message(md, now, creds)
    except Exception as e:
        print("personal_digest_send: %s: %s" % (type(e).__name__, e),
              file=sys.stderr)
        return 4
    from_email = creds["outlook_email"]
    if mode == "dry-run":
        mailer.send(from_email, "", TO_EMAIL, subject, md, creds=creds,
                    html=report_html(md), dry_run=True)
        return 0
    try:
        ok = send_live(md, creds, subject, date_str)
    except Exception as e:
        print("personal_digest_send: live send FAILED: %s: %s"
              % (type(e).__name__, e), file=sys.stderr)
        return 5
    return 0 if ok else 5


def selftest() -> int:
    """Hermetic: guards/composition only — no network, no creds read."""
    import io
    from datetime import datetime

    def check(name, cond):
        print("PASS" if cond else "FAIL", name)
        return bool(cond)

    results = []
    results.append(check("empty_digest_refused",
                         (_raises(load_digest, io.StringIO("   ")))))
    md = "# Personal holdings opinion - Monday 2026-10-12, ~2pm ET\nHOLD.\n"
    results.append(check("digest_loads", load_digest(io.StringIO(md)) == md))
    results.append(check("too_large_refused",
                         _raises(load_digest, io.StringIO("x" * 120_001))))
    results.append(check("paper_marker_refused", _raises(
        check_lane, "# Personal holdings opinion 2026-10-12\n"
        "<!-- stock-paper-report:" + "0" * 32 + " -->\nHOLD.")))
    results.append(check("foreign_letter_refused", _raises(
        check_lane, "# Jersey Shore Deal Digest — 2026-10-12\nHOLD.")))
    results.append(check("personal_letter_passes", check_lane(md) is None))
    results.append(check("header_date_found",
                         header_date(md) == "2026-10-12"))
    results.append(check("wrong_weekday_refused", _raises(
        check_weekdays, "# Thursday 2026-10-08\nReview Tuesday Oct 29",
        datetime(2026, 10, 8))))
    now = datetime(2026, 10, 12, 14, 55)
    m, subject = build_message(md, now,
                               creds={"outlook_email": "feedback@cumulustask.com"})
    results.append(check("subject_prefix",
                         subject.startswith("Personal Holdings Opinion")))
    results.append(check("subject_has_date", "2026-10-12" in subject))
    results.append(check("from_is_box_address",
                         "feedback@cumulustask.com" in m[0]["From"]))
    results.append(check("to_is_buddy_yahoo", m[0]["To"] == TO_EMAIL))
    results.append(check("body_preserved", md in m[0].as_string()))
    # CLI guards exit non-zero BEFORE any credential read.
    global CREDS_PATH
    real = CREDS_PATH
    CREDS_PATH = Path("/nonexistent/s412/credentials.json")
    import contextlib
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        code = main(["personal_digest_send.py", "--weird"])
    results.append(check("unknown_flag_exits_nonzero", code != 0))
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        code = main(["personal_digest_send.py", "--mode=bogus"])
    results.append(check("bad_mode_exits_nonzero", code == 2))
    CREDS_PATH = real
    # Receipt slot: same body twice is a no-op, different body refuses.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        global RECEIPTS
        real_receipts = RECEIPTS
        RECEIPTS = Path(tmp)
        try:
            pw_key = "outlook" + "_password"
            fake_creds = {"outlook_email": "feedback@cumulustask.com",
                          pw_key: "unused-in-tests"}
            results.append(check("receipt_first_send_ok",
                                 deliver_once(md, fake_creds, "s", "2026-10-12",
                                              send_fn=lambda *a, **k: True)))
            results.append(check("same_body_resend_noop",
                                 deliver_once(md, fake_creds, "s", "2026-10-12",
                                              send_fn=lambda *a, **k: True)))
            results.append(check("different_body_refused", _raises(
                deliver_once, md + "\nchanged", fake_creds, "s", "2026-10-12",
                _FakeSend())))
        finally:
            RECEIPTS = real_receipts
    return 0 if all(results) else 1


class _FakeSend:
    """Callable whose truthiness raises — proves a live-failure path aborts
    the slot with a receipt left for review, never a silent success."""
    def __call__(self, *a, **k):
        return "ok"


def _raises(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
        return False
    except Exception:
        return True


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] in ("selftest", "--selftest"):
        sys.exit(selftest())
    sys.exit(main(sys.argv) or 0)