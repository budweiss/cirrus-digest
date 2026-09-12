#!/usr/bin/env python3
"""Email Buddy the current 24 answers.

Buddy, 2026-09-09: send today's answers; then each day up to Saturday send a
COMPLETE recap whenever anything has changed; then he enters in the app.

So the rule this implements is: **a full 24-line recap every time, never a
diff-only note.** He is going to type these into an app from whatever the most
recent email is, and a message that says only "Q14 changed" is a message he
cannot enter from. Changes are called out at the top AND the full sheet follows.

Reads the answers from the CRM, not from the markdown, so anything edited
during the week is what gets sent.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import immaculate_store as store   # noqa: E402
import entity_kb                   # noqa: E402

TO = "Buddy.Weiss@outlook.com"
LOCK = datetime(2026, 9, 13, 17, 0, tzinfo=timezone.utc)

# ── S169: the deterministic Saturday sender (systemd timer) ──────────────────
# On 2026-09-12 the Saturday recap was a desktop session task — a five-step
# prompt whose EMAIL was the last step. It fired at 09:00, ran the check
# (the ledger has it at 09:00:56), and stopped short of the send, silently.
# Buddy found out at 11:30 because there was nothing to find. The send now
# has a deterministic path (`immaculate-saturday-final.timer` on CUMULUS
# runs this script with --window every Saturday 09:05), and the guards below
# make a weekly timer safe: outside contest week it records a quiet no-op,
# inside it a re-fire is suppressed like billsnow's duplicate guard.
SENT_MARKER = HERE / "logs/immaculate-final-sent.json"
WINDOW = timedelta(hours=36)   # opens Sat 01:00 ET for a Sunday 1pm lock


def _send_window_open(now=None):
    """True only in sight of the lock — an email after it is worthless and
    one a month early is noise."""
    now = now or datetime.now(timezone.utc)
    return (LOCK - WINDOW) <= now <= LOCK


def _already_sent_today(now=None):
    now = now or datetime.now(timezone.utc)
    try:
        d = json.loads(SENT_MARKER.read_text())
        return str(d.get("sent_utc", ""))[:10] == now.strftime("%Y-%m-%d")
    except Exception:
        return False


def _stamp_sent(now=None):
    now = now or datetime.now(timezone.utc)
    try:
        SENT_MARKER.parent.mkdir(parents=True, exist_ok=True)
        SENT_MARKER.write_text(json.dumps({"sent_utc": now.isoformat()}))
    except Exception:
        pass          # the marker is a courtesy, never a reason to fail


def _record(ok, note):
    """Report to the job ledger so Skywarden sees this job like any other —
    a FAILED send exits nonzero, becomes a failed unit, and is the
    heartbeat's business. try/except: monitoring must never break the send."""
    try:
        import job_status
        job_status.record("immaculatesaturdayfinal", ok, note[:200])
    except Exception:
        pass


def recent_changes(hours=36, db_path=None):
    """Answer changes ledgered in the window — what to lead the email with."""
    out = []
    for ev in entity_kb.get_events(store.PROJECT, db_path=db_path):
        if (ev.get("event_type") or "") != "field_change":
            continue
        if (ev.get("field") or "") != "answer":
            continue
        def _v(x):
            try: return json.loads(x)
            except Exception: return x
        out.append({"slug": ev.get("slug"), "at": ev.get("occurred_at"),
                    "old": _v(ev.get("old_value")), "new": _v(ev.get("new_value"))})
    return out


def compose(rows, changes, now=None):
    now = now or datetime.now(timezone.utc)
    h = (LOCK - now).total_seconds() / 3600.0
    when = "LOCKED" if h <= 0 else f"{h:.0f} hours left"

    subject = (f"Immaculate Prediction — all 24 answers "
               f"({'UPDATED, ' if changes else ''}{when})")

    L = []
    L.append("IMMACULATE PREDICTION 2026 — CURRENT ENTRY")
    L.append("")
    if h > 0:
        L.append(f"** LOCKS Sunday 13 September, 1:00 PM ET — {h:.0f} hours from now. **")
        L.append("   That is kickoff, not end of day. The Steelers' clock is official.")
    else:
        L.append("** The entry window has closed. These answers are final. **")
    L.append("")

    if changes:
        L.append("WHAT CHANGED SINCE THE LAST EMAIL")
        for c in changes:
            L.append(f"  {c['slug'].upper()}: {c['old']}  ->  {c['new']}   ({c['at']})")
        L.append("")
    else:
        L.append("No answer has changed since the last email.")
        L.append("")

    L.append("THE 24 ANSWERS — enter these in the Steelers app")
    L.append("")
    L.append("  #   Game / scope        Answer            Conf   Question")
    L.append("  " + "-" * 84)
    for f in rows:
        n = int(f["number"])
        L.append(f"  {n:>2}  {f['when']:<18s} {f['answer']:<16s} "
                 f"{float(f['confidence'])*100:>4.0f}%  {f['question'][:38]}")
        if n == 17:
            L.append("  " + "-" * 84 + "   (season-long questions below)")
    L.append("")

    against = [f for f in rows if f["answer"] in ("CIN", "BAL", "Ravens")]
    if against:
        L.append("FOUR ANSWERS GO AGAINST THE STEELERS — your call to override")
        for f in against:
            L.append(f"  Q{f['number']}: {f['answer']}  ({f['question'][:44]})")
        L.append("  Q14 is cheap to flip (55/45). Q17 and Q24 are the expensive ones.")
        L.append("")

    L.append("WHILE YOU ARE IN THE APP, PLEASE GRAB")
    L.append("  1. The TIE-BREAKER question(s). Not published anywhere. They decide")
    L.append("     the money if more than one entry is perfect.")
    L.append("  2. Whether a separate weekly/in-season game is running.")
    L.append("  3. What the asterisk means on Q18, Q19 and Q20.")
    L.append("")
    L.append("WHAT IT IS WORTH")
    L.append("  A perfect 24/24 is roughly 1 in 50 million. Q23 (the exact win")
    L.append("  total) costs a factor of six on its own and no research fixes it.")
    L.append("  The rules say NO GUARANTEED WINNER. Entry is free — it is a")
    L.append("  lottery ticket with better-than-random numbers.")
    L.append("")
    L.append("Full working: ~/Documents/Cowork/immaculate/ (ENTRY-SHEET.md,")
    L.append("the two RESEARCH files, MODEL-POLL.md).")
    L.append("")
    L.append("— CUMULUS, Project Immaculate")
    return subject, "\n".join(L)


def main():
    dry = "--dry-run" in sys.argv
    windowed = "--window" in sys.argv
    # S169: --window is the deterministic (systemd-timer) path. ORDER MATTERS:
    # the contest-state guards come FIRST — off-season the CRM legitimately
    # holds anything, and the 24-answer refusal must not fire weekly for months.
    if windowed and not _send_window_open():
        print("no active contest — the send window is closed")
        _record(True, "no active contest — the send window is closed")
        return 0
    if windowed and _already_sent_today():
        # the billsnow/alopeciabrief idiom: a re-fire is the guard doing its
        # job, not a failure — record a good run, send nothing.
        print("already sent today — duplicate send suppressed")
        _record(True, "already sent today — duplicate send suppressed")
        return 0
    rows = store.answers()
    if len(rows) != 24:
        print(f"REFUSING TO SEND: the CRM holds {len(rows)} answers, not 24.")
        if windowed:
            _record(False, f"REFUSED: CRM holds {len(rows)} answers, not 24")
        return 1
    changes = recent_changes()
    subject, body = compose(rows, changes)

    if dry:
        print("=== DRY RUN — nothing sent ===")
        print("TO:", TO)
        print("SUBJECT:", subject)
        print("-" * 70)
        print(body)
        return 0

    from entity_kb_weekly_digest import _send_mail
    creds = json.loads((HERE / "config/credentials.json").read_text())
    ok = _send_mail(creds.get("outlook_email", ""), creds.get("outlook_password", ""),
                    TO, "", subject, body)
    print("email:", "sent" if ok else "FAILED")
    if windowed:
        if ok:
            _stamp_sent()
        _record(ok, "sent" if ok else "email send FAILED")
    return 0 if ok else 1


def selftest() -> int:
    fails = 0
    def ck(n, c):
        nonlocal fails
        print(f"  [{'OK ' if c else 'FAIL'}] {n}")
        fails += 0 if c else 1

    rows = [{"number": str(i), "when": "wk", "answer": "PIT",
             "confidence": "0.5", "question": "q"} for i in range(1, 25)]
    s, b = compose(rows, [])
    ck("a no-change email still carries all 24 lines",
       all(f"\n  {i:>2}  " in b for i in range(1, 25)))
    ck("...and says so rather than looking broken", "No answer has changed" in b)
    ck("subject carries the countdown", "hours left" in s or "LOCKED" in s)

    ch = [{"slug": "q14", "at": "2026-09-11 08:00", "old": "BAL", "new": "PIT"}]
    s2, b2 = compose(rows, ch)
    ck("a changed answer leads the email", "WHAT CHANGED" in b2
       and "BAL  ->  PIT" in b2)
    ck("...and the subject flags it", "UPDATED" in s2)
    ck("the full 24 are STILL included after a change — he types from this one",
       all(f"\n  {i:>2}  " in b2 for i in range(1, 25)))

    from datetime import timedelta
    _, b3 = compose(rows, [], now=LOCK + timedelta(hours=1))
    ck("after the lock it stops telling him to hurry",
       "final" in b3 and "hours from now" not in b3)

    # ── S169: the deterministic Saturday sender's guards ─────────────────
    ck("the window is open in sight of the lock",
       _send_window_open(LOCK - timedelta(hours=12)) is True)
    ck("...closed 48h out", _send_window_open(LOCK - timedelta(hours=48)) is False)
    ck("...and closed once the lock passes",
       _send_window_open(LOCK + timedelta(hours=1)) is False)

    import tempfile as _tf
    _saved = (globals()["SENT_MARKER"], globals()["_record"],
              globals()["_send_window_open"], store.answers)
    try:
        with _tf.TemporaryDirectory() as _td:
            globals()["SENT_MARKER"] = Path(_td) / "sent.json"
            ck("a fresh marker path means not sent today",
               _already_sent_today() is False)
            _stamp_sent()
            ck("...and after stamping it means sent today",
               _already_sent_today() is True)

            # The guard ORDER is the whole point: off-window, main() must
            # return BEFORE touching the CRM, which legitimately holds
            # anything off-season.
            recorded = []
            globals()["_record"] = lambda ok, note: recorded.append((ok, note))
            globals()["_send_window_open"] = lambda now=None: False
            store.answers = lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("CRM must not be read off-window"))
            _argv, sys.argv = sys.argv, ["x", "--window"]
            try:
                rc = main()
            finally:
                sys.argv = _argv
            ck("off-window exits 0 without touching the CRM", rc == 0)
            ck("...and records a quiet no-op, not a failure",
               recorded == [(True, "no active contest — the send window is closed")])

            # In-window with today's send already done: suppressed, good run.
            recorded.clear()
            globals()["_send_window_open"] = lambda now=None: True
            _argv, sys.argv = sys.argv, ["x", "--window"]
            try:
                rc = main()
            finally:
                sys.argv = _argv
            ck("an in-window re-fire is suppressed, not duplicated", rc == 0)
            ck("...and recorded as the guard working, ok=True",
               recorded and recorded[0][0] is True
               and "suppressed" in recorded[0][1])

            # In-window with the wrong answer count: refuses, records FAILURE.
            recorded.clear()
            globals()["SENT_MARKER"] = Path(_td) / "never.json"
            store.answers = lambda *a, **k: [{}] * 23
            _argv, sys.argv = sys.argv, ["x", "--window"]
            try:
                rc = main()
            finally:
                sys.argv = _argv
            ck("in-window with != 24 answers refuses", rc == 1)
            ck("...and records ok=False so the heartbeat sees it",
               recorded and recorded[0][0] is False)
    finally:
        (globals()["SENT_MARKER"], globals()["_record"],
         globals()["_send_window_open"], store.answers) = _saved
    print(f"\n{'ALL PASS' if not fails else f'{fails} FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(selftest() if "selftest" in sys.argv else main())
