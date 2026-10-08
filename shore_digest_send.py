#!/usr/bin/env python3
"""shore_digest_send — deliver a Jersey Shore deal digest to Buddy's inbox.

Runs ON CUMULUS (where the SMTP credentials live), composes via mailer — the
consolidated send path — and sends. The digest markdown is produced by the
desktop Jersey Shore sessions (jersey-shore/digests/, Buddy's folder) and
piped on stdin by the shore-digest-send runner command.

Buddy authorized this send target (2026-10-08): TO = his own
weiss_buddy@yahoo.com, same as the stock digests. Replies go to the From
address, which is the intake inbox this repo already reads.

Modes: dry-run (default) composes and prints headers, sends NOTHING.
live sends with on_error="raise" ("it didn't arrive" must never look like
"it did").

Compared with stock_digest_send there is NO paper-account gate (that is
stock-specific); the guards here are: non-empty, <=120k chars, and a
calendar/weekday sanity check on date labels in the digest so a mislabeled
digest is refused before it reaches Buddy.

CLI guard: only --mode=dry-run|live is accepted; anything else exits non-zero
BEFORE any send path is touched (T118 shape).
"""
import json
import re
import sys
from pathlib import Path

import mailer

PROJECT_DIR = Path(__file__).resolve().parent
CREDS_PATH = PROJECT_DIR / "config" / "credentials.json"
TO_EMAIL = "weiss_buddy@yahoo.com"
MODES = ("dry-run", "live")

# Reuse the exact renderer and calendar guard proven in stock_digest_send.
from stock_digest_send import report_html, check_weekdays  # noqa: E402


def build_message(md: str, now=None, creds: dict = None):
    """Compose the message. Returns (mailer-message tuple, subject)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = now or datetime.now(ZoneInfo('America/New_York'))
    check_weekdays(md, now)
    subject = "Jersey Shore Deal Digest — %s (ET)" % now.strftime("%Y-%m-%d %H:%M")
    creds = creds or {}
    return mailer.build(creds["outlook_email"], TO_EMAIL, subject, md,
                        html=report_html(md), creds=creds), subject


def load_digest(stream) -> str:
    text = stream.read()
    if not text.strip():
        raise ValueError("empty_digest")
    if len(text) > 120_000:
        raise ValueError("digest_too_large")
    return text


def send_live(md, creds, subject):
    """Send with on_error='raise' — a silent miss must never look like a send."""
    result = mailer.send(creds["outlook_email"], creds["outlook_password"],
                         TO_EMAIL, subject, md, html=report_html(md),
                         creds=creds, on_error="raise", log=print)
    if not result:
        raise ValueError("send_unconfirmed_receipt_requires_review")
    return result


def main(argv) -> int:
    args = argv[1:]
    if [a for a in args if a.startswith("--") and
            a.split("=", 1)[0] not in ("--mode",)]:
        print("shore_digest_send: unknown argument(s). Use --mode=dry-run|live; "
              "digest markdown arrives on stdin.", file=sys.stderr)
        return 2
    mode = "dry-run"
    for a in args:
        if a.startswith("--mode"):
            mode = a.split("=", 1)[1] if "=" in a else "dry-run"
    if mode not in MODES:
        print("shore_digest_send: need --mode with one of %s" % ", ".join(MODES),
              file=sys.stderr)
        return 2
    try:
        creds = json.loads(CREDS_PATH.read_text())
    except Exception as e:
        print("shore_digest_send: credentials unreadable (%s)" % type(e).__name__,
              file=sys.stderr)
        return 3
    if not creds.get("outlook_email") or not creds.get("outlook_password"):
        print("shore_digest_send: outlook_email/outlook_password unset",
              file=sys.stderr)
        return 3
    try:
        md = load_digest(sys.stdin)
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo('America/New_York'))
        check_weekdays(md, now)
        _, subject = build_message(md, now=now, creds=creds)
    except Exception as e:
        print("shore_digest_send: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 4
    from_email = creds["outlook_email"]
    if mode == "dry-run":
        mailer.send(from_email, "", TO_EMAIL, subject, md, creds=creds,
                    html=report_html(md), dry_run=True)
        return 0
    try:
        ok = send_live(md, creds, subject)
    except Exception as e:
        print("shore_digest_send: live send FAILED: %s: %s" %
              (type(e).__name__, e), file=sys.stderr)
        return 5
    return 0 if ok else 5


def selftest() -> int:
    """Hermetic: guards/composition only — no network, no creds read."""
    import io
    import tempfile
    from datetime import datetime

    def check(name, cond):
        print("PASS" if cond else "FAIL", name)
        return bool(cond)

    results = []
    results.append(check("empty_digest_refused",
                         (_raises(load_digest, io.StringIO("   ")))))
    md = "# Jersey Shore Deal Digest\n\nBody row.\n"
    results.append(check("digest_loads", load_digest(io.StringIO(md)) == md))
    results.append(check("too_large_refused",
                         _raises(load_digest, io.StringIO("x" * 120_001))))
    # Calendar guard still catches wrong weekday labels via the stock module.
    results.append(check("wrong_weekday_refused", _raises(
        check_weekdays, '# Thursday 2026-10-08\nReview Tuesday Oct 29',
        datetime(2026, 10, 8))))
    m, subject = build_message(md, now=datetime(2026, 10, 13, 8, 0),
                               creds={"outlook_email": "feedback@cumulustask.com"})
    results.append(check("subject_prefix",
                         subject.startswith("Jersey Shore Deal Digest")))
    results.append(check("subject_has_date", "2026-10-13" in subject))
    results.append(check("from_is_box_address", "feedback@cumulustask.com" in m[0]["From"]))
    results.append(check("to_is_buddy_yahoo", m[0]["To"] == TO_EMAIL))
    results.append(check("body_preserved", md in m[0].as_string()))
    # CLI guards exit non-zero BEFORE any credential read.
    global CREDS_PATH
    real = CREDS_PATH
    CREDS_PATH = Path("/nonexistent/s388/credentials.json")
    import contextlib
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        code = main(["shore_digest_send.py", "--weird"])
    results.append(check("unknown_flag_exits_nonzero", code != 0))
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        code = main(["shore_digest_send.py", "--mode=bogus"])
    results.append(check("bad_mode_exits_nonzero", code == 2))
    CREDS_PATH = real
    return 0 if all(results) else 1


def _raises(fn, *args):
    try:
        fn(*args)
        return False
    except Exception:
        return True


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] in ("selftest", "--selftest"):
        sys.exit(selftest())
    sys.exit(main(sys.argv) or 0)