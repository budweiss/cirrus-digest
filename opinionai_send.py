#!/usr/bin/env python3
"""opinionai_send — deliver the weekly Opinion AI triage email to Buddy.

Runs ON CUMULUS (where the SMTP credentials live), composes via mailer — the
consolidated send path — and sends. The triage markdown is produced by the
desktop opinionai-review scheduled sessions and piped on stdin by the
opinionai-send runner command (added 2026-10-11).

Buddy authorized this lane (2026-10-08, Phase 1 of the OpinionAI review loop).
Unlike shore_digest_send (yahoo only), the charter requires BOTH of Buddy's
addresses: weiss_buddy@yahoo.com AND buddy.weiss@outlook.com — one message,
both in To, same content.

The subject comes from the document's own H1 (the runner file, not a code
path, owns the subject) and is guarded: it MUST start with "Opinion AI
weekly — ". Everything else mirrors shore_digest_send: non-empty, <=120k
chars, dry-run default, live only explicit, on_error="raise" so "it didn't
arrive" can never look like "it did". No calendar guard here — the lane's
subject carries a plain date, not weekday labels.

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
TO_EMAILS = ["weiss_buddy@yahoo.com", "buddy.weiss@outlook.com"]
SUBJECT_PREFIX = "Opinion AI weekly — "
MODES = ("dry-run", "live")

# Same small renderer the shore digests use — plain text body is always the
# source of truth, HTML is a courtesy copy.
from stock_digest_send import report_html  # noqa: E402


def extract_subject(md: str) -> str:
    """Subject = the document's H1, guarded to the lane's subject prefix."""
    first = md.split("\n", 1)[0].strip()
    if not first.startswith("# "):
        raise ValueError("subject_missing_h1")
    subject = first[2:].strip()
    if not subject.startswith(SUBJECT_PREFIX):
        raise ValueError("subject_prefix_mismatch: must start with "
                         "%r to leave this lane" % SUBJECT_PREFIX)
    if len(subject) > 200:
        raise ValueError("subject_too_long")
    return subject


def load_digest(stream) -> str:
    text = stream.read()
    if not text.strip():
        raise ValueError("empty_digest")
    if len(text) > 120_000:
        raise ValueError("digest_too_large")
    return text


def build_message(md: str, creds: dict = None):
    """Compose the message. Returns (mailer-message tuple, subject)."""
    creds = creds or {}
    subject = extract_subject(md)
    to = ", ".join(TO_EMAILS)
    return mailer.build(creds["outlook_email"], to, subject, md,
                        html=report_html(md), creds=creds), subject, to


def send_live(md, creds, subject, to):
    """Send with on_error='raise' — a silent miss must never look like a send."""
    result = mailer.send(creds["outlook_email"], creds["outlook_password"],
                         to, subject, md, html=report_html(md),
                         creds=creds, on_error="raise", log=print)
    if not result:
        raise ValueError("send_unconfirmed_receipt_requires_review")
    return result


def main(argv) -> int:
    args = argv[1:]
    if [a for a in args if a.startswith("--") and
            a.split("=", 1)[0] not in ("--mode",)]:
        print("opinionai_send: unknown argument(s). Use --mode=dry-run|live; "
              "triage markdown arrives on stdin.", file=sys.stderr)
        return 2
    mode = "dry-run"
    for a in args:
        if a.startswith("--mode"):
            mode = a.split("=", 1)[1] if "=" in a else "dry-run"
    if mode not in MODES:
        print("opinionai_send: need --mode with one of %s" % ", ".join(MODES),
              file=sys.stderr)
        return 2
    try:
        creds = json.loads(CREDS_PATH.read_text())
    except Exception as e:
        print("opinionai_send: credentials unreadable (%s)" % type(e).__name__,
              file=sys.stderr)
        return 3
    if not creds.get("outlook_email") or not creds.get("outlook_password"):
        print("opinionai_send: outlook_email/outlook_password unset",
              file=sys.stderr)
        return 3
    try:
        md = load_digest(sys.stdin)
        message, subject, to = build_message(md, creds=creds)
    except Exception as e:
        print("opinionai_send: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 4
    from_email = creds["outlook_email"]
    if mode == "dry-run":
        mailer.send(from_email, "", to, subject, md, creds=creds,
                    html=report_html(md), dry_run=True)
        return 0
    try:
        ok = send_live(md, creds, subject, to)
    except Exception as e:
        print("opinionai_send: live send FAILED: %s: %s" %
              (type(e).__name__, e), file=sys.stderr)
        return 5
    return 0 if ok else 5


def selftest() -> int:
    """Hermetic: guards/composition only — no network, no creds read."""
    import io
    import tempfile

    def check(name, cond):
        print("PASS" if cond else "FAIL", name)
        return bool(cond)

    results = []
    results.append(check("empty_digest_refused",
                         (_raises(load_digest, io.StringIO("   ")))))
    good = "# Opinion AI weekly — 3 candidates (2026-10-11)\n\nBody.\n"
    results.append(check("digest_loads", load_digest(io.StringIO(good)) == good))
    results.append(check("too_large_refused",
                         _raises(load_digest, io.StringIO("x" * 120_001))))
    results.append(check("subject_prefix_guarded", _raises(
        extract_subject, "# Jersey Shore Deal Digest — 2026-10-11\nbody")))
    results.append(check("no_h1_refused", _raises(
        extract_subject, "Opinion AI weekly — no H1")))
    m, subject, to = build_message(
        good, creds={"outlook_email": "feedback@cumulustask.com"})
    results.append(check("subject_from_h1",
                         subject == "Opinion AI weekly — 3 candidates (2026-10-11)"))
    results.append(check("to_has_both_addresses",
                         "weiss_buddy@yahoo.com" in m[0]["To"] and
                         "buddy.weiss@outlook.com" in m[0]["To"]))
    results.append(check("from_is_box_address",
                         "feedback@cumulustask.com" in m[0]["From"]))
    # MIME-encodes non-ASCII (the em dash in the lane subject), so compare the
    # DECODED plain-text part, not the serialized string.
    plain = next(p.get_payload(decode=True).decode("utf-8")
                 for p in m[0].walk()
                 if p.get_content_type() == "text/plain")
    results.append(check("body_preserved", plain == good))
    # CLI guards exit non-zero BEFORE any send path is touched.
    global CREDS_PATH
    real = CREDS_PATH
    CREDS_PATH = Path("/nonexistent/opinionai/credentials.json")
    import contextlib
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        code = main(["opinionai_send.py", "--weird"])
    results.append(check("unknown_flag_exits_nonzero", code != 0))
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        code = main(["opinionai_send.py", "--mode=bogus"])
    results.append(check("bad_mode_exits_nonzero", code == 2))
    CREDS_PATH = real
    _ = tempfile
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
    sys.exit(main(sys.argv))