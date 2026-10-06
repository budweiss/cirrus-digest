#!/usr/bin/env python3
"""stock_digest_send — deliver a Stock Pickers digest to Buddy's Yahoo inbox.

The digests are produced by the desktop Stock Pickers sessions on Buddy's
Mac (stock-pickers/digests/, gitignored — picks/sizes never leave his
folder), which pipes ONE digest file's markdown on stdin. This script runs
ON CUMULUS (where the SMTP credentials live), composes via mailer — the
consolidated send path — and sends.

Buddy authorized this send target (2026-10-06): TO = his own
weiss_buddy@yahoo.com, so privacy is bounded by construction. Replies to the
digest go to the From address, which is the intake inbox this repo already
reads — Buddy's feedback is therefore picked up by the normal inbox peeks.

Modes: dry-run (default) composes and prints headers, sends NOTHING — an
external send is always inspected first. live sends with on_error="raise"
("it didn't arrive" must never look like "it did").

CLI guard: mode is whitelisted to exactly {dry-run, live}; anything else
exits non-zero BEFORE any send path is touched (T118 shape).
"""
import json
import sys
from pathlib import Path

import mailer

PROJECT_DIR = Path(__file__).resolve().parent
CREDS_PATH = PROJECT_DIR / "config" / "credentials.json"
TO_EMAIL = "weiss_buddy@yahoo.com"
MODES = ("dry-run", "live")


def load_digest(stream) -> str:
    text = stream.read()
    if not text.strip():
        raise ValueError("empty_digest")
    if len(text) > 120_000:
        raise ValueError("digest_too_large")
    return text


def build_message(md: str, now=None, creds: dict = None):
    """Compose the message the same way the scheduled digests would. Returns
    (mailer-message tuple, subject) so selftest can inspect without network."""
    from datetime import datetime
    now = now or datetime.now()
    subject = "Stock Pickers digest — %s (paper calls)" % now.strftime("%Y-%m-%d %H:%M ET")
    creds = creds or {}
    return mailer.build(creds["outlook_email"], TO_EMAIL, subject, md, creds=creds), subject


def main(argv) -> int:
    args = argv[1:]
    if [a for a in args if a.startswith("--") and
            a.split("=", 1)[0] not in ("--mode",)]:
        print("stock_digest_send: unknown argument(s). Use --mode=dry-run|live; "
              "digest markdown arrives on stdin.", file=sys.stderr)
        return 2  # fail-closed CLI guard: a typo must not reach a send
    mode = "dry-run"
    for a in args:
        if a.startswith("--mode"):
            mode = a.split("=", 1)[1] if "=" in a else "dry-run"
    if mode not in MODES:
        print("stock_digest_send: need --mode with one of %s" % ", ".join(MODES),
              file=sys.stderr)
        return 2
    try:
        creds = json.loads(CREDS_PATH.read_text())
    except Exception as e:
        print("stock_digest_send: credentials unreadable (%s)" % type(e).__name__,
              file=sys.stderr)
        return 3
    if not creds.get("outlook_email") or not creds.get("outlook_password"):
        print("stock_digest_send: outlook_email/outlook_password unset",
              file=sys.stderr)
        return 3
    try:
        md = load_digest(sys.stdin)
        (msg, recipients), subject = build_message(md, creds=creds)
    except Exception as e:
        print("stock_digest_send: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 4
    from_email = creds["outlook_email"]
    if mode == "dry-run":
        mailer.send(from_email, "", TO_EMAIL, subject, md, creds=creds,
                    dry_run=True)
        return 0
    ok = mailer.send(from_email, creds["outlook_password"], TO_EMAIL, subject, md,
                     creds=creds, on_error="raise", log=print)
    return 0 if ok else 5


def selftest() -> int:
    """Hermetic: compose/headers/guards only — no network, no creds read."""
    import io
    import tempfile
    from datetime import datetime

    def check(name, cond):
        print("PASS" if cond else "FAIL", name)
        return bool(cond)

    results = []
    results.append(check("empty_digest_refused",
                         (_raises(load_digest, io.StringIO("   ")))))
    md = "# digest\n\nBody row.\n"
    results.append(check("digest_loads", load_digest(io.StringIO(md)) == md))
    results.append(check("too_large_refused",
                         _raises(load_digest, io.StringIO("x" * 120_001))))
    m, subject = build_message(md, now=datetime(2026, 10, 6, 14, 45),
                               creds={"outlook_email": "feedback@cumulustask.com"})
    headers = (subject, m[0]["From"], m[0]["To"])
    results.append(check("subject_has_date",
                         "2026-10-06" in subject and "14:45" in subject))
    results.append(check("from_is_box_address",
                         "feedback@cumulustask.com" in headers[1]))
    results.append(check("to_is_buddy_yahoo", headers[2] == TO_EMAIL))
    results.append(check("body_preserved", md in m[0].as_string()))
    # CLI guards: an unknown flag or an unknown mode must exit non-zero BEFORE
    # any credential read — reorder-proof by construction, so assert it with
    # an unreadable credentials path via monkeypatched state.
    global CREDS_PATH
    real = CREDS_PATH
    CREDS_PATH = Path("/nonexistent/s377/credentials.json")
    import contextlib
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        code = main(["stock_digest_send.py", "--weird"])
    results.append(check("unknown_flag_exits_nonzero", code != 0))
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        code = main(["stock_digest_send.py", "--mode=bogus"])
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