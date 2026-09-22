#!/usr/bin/env python3
"""Recap email for Project Immaculate — Buddy's ask, S242. Since S253 it is
sent right after each game by immaculate_tick.py (`--window --game LABEL`),
not on a Wednesday timer: "When the game is over, we should look up the
results and compare how we did." (Buddy, 2026-09-22.) Original S242 text:

Weekly Wednesday recap for Project Immaculate — Buddy's ask, S242
(2026-09-21): "on Wednesday can you... email how well we did."

Reads whatever is already recorded in the CRM (both stores) and sends ONE
consolidated email. Deliberately does NO research or judgment of its own —
that is `immaculate-wednesday-resolve`'s job (a Claude scheduled task,
Wednesday mornings, same pattern as `immaculate-daily-check`). This script
only composes and sends, the same split `immaculate_email.py` already uses
for the Saturday recap, for the reason S169 already paid for: "a must-happen
send does not belong at the end of a session prompt." If the resolve step
hangs or runs long, this still sends whatever was already recorded rather
than silently skipping the week.

Sections: season tally (correct/wrong/outstanding for Q1-Q24, including the
Q18-Q24 informational leader snapshots), the most recently seeded weekly
mini-contest compared to reality, and a week-ahead status line.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import immaculate_store as store                  # noqa: E402
import immaculate_weekly_store as weekly           # noqa: E402

TO = "Buddy.Weiss@outlook.com"

SENT_MARKER = HERE / "logs/immaculate-wednesday-sent.json"
WINDOW = timedelta(hours=6)   # Wed 09:05 fire; window just guards re-fires

# S250, Buddy 2026-09-21: "keep the Wednesday email sending for the football
# season. normally ends in Feb sometime." September through February; the timer
# keeps firing all year and records a quiet no-op off-season, so the weekly
# cadence check stays green and the send resumes by itself each September.
SEASON_MONTHS = {9, 10, 11, 12, 1, 2}
OFF_SEASON_NOTE = "off-season — no Wednesday recap (sends September through February)"


def _in_season(now=None):
    now = now or datetime.now(timezone.utc)
    return now.month in SEASON_MONTHS


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
    """Report to the job ledger so a failed send shows up like any other
    job — see immaculate_email.py's identical rationale."""
    try:
        import job_status
        job_status.record("immaculatewednesdayreport", ok, note[:200])
    except Exception:
        pass


def season_section(rows, t):
    """Season tally: correct / wrong / outstanding, Q1-17 game-by-game plus
    the Q18-24 leader snapshots (informational, not final)."""
    L = ["SEASON ENTRY (24 questions, locked) — RUNNING SCORE", ""]
    outstanding = t["total"] - t["resolved"]
    L.append(f"  {t['correct']} correct, {len(t['missed'])} wrong, "
             f"{outstanding} still outstanding, out of {t['total']}.")
    L.append(f"  Perfect score: {'still ALIVE' if t['perfect_alive'] else 'GONE'}"
             + (f" (missed Q{t['missed']})" if t["missed"] else ""))
    L.append("")

    by_num = {int(r["number"]): r for r in rows}
    resolved_lines, outstanding_lines = [], []
    for n in range(1, 18):
        r = by_num.get(n)
        if not r:
            continue
        if r.get("status") == "resolved":
            mark = "OK" if r.get("actual") == r.get("answer") else "MISS"
            resolved_lines.append(f"    Q{n:>2} [{mark}] picked {r['answer']}, "
                                   f"actual {r.get('actual')}")
        else:
            outstanding_lines.append(f"    Q{n:>2} pending — {r['when']}: "
                                      f"{r['question'][:50]}")
    if resolved_lines:
        L.append("  Resolved so far:")
        L.extend(resolved_lines)
        L.append("")
    if outstanding_lines:
        L.append("  Still to be played:")
        L.extend(outstanding_lines)
        L.append("")

    leader_lines = []
    for n in range(18, 25):
        r = by_num.get(n)
        if not r:
            continue
        snap = r.get("leader_snapshot")
        if snap:
            val = f" ({r['leader_value']})" if r.get("leader_value") else ""
            leader_lines.append(f"    Q{n} {r['question'][:40]}: "
                                 f"currently {snap}{val} -- our pick {r['answer']}")
        else:
            leader_lines.append(f"    Q{n} {r['question'][:40]}: "
                                 f"no snapshot yet -- our pick {r['answer']}")
    if leader_lines:
        L.append("  Season-long questions (informational — these only "
                  "resolve at season end):")
        L.extend(leader_lines)
        L.append("")
    return L


def weekly_section():
    """Most recently seeded weekly mini-contest vs. reality."""
    weeks = weekly.weeks_seeded()
    if not weeks:
        return ["WEEKLY CONTEST — none tracked yet.", ""]
    week = weeks[-1]
    rows = weekly.week_answers(week)
    t = weekly.week_tally(week)
    L = [f"WEEKLY CONTEST — Week {week} ({t['resolved']}/{t['total']} graded)", ""]
    if t["resolved"] == 0:
        L.append("  Not graded yet — the resolve step hasn't checked this "
                 "week's results against reality yet.")
    else:
        L.append(f"  {t['correct']}/{t['resolved']} correct so far.")
    L.append("")
    for r in rows:
        if r.get("status") == "resolved":
            mark = "OK" if r.get("actual") == r.get("answer") else "MISS"
            L.append(f"    Q{r['number']:>2} [{mark}] picked {r['answer']}, "
                     f"actual {r.get('actual')} — {r['question'][:44]}")
        else:
            L.append(f"    Q{r['number']:>2} [pending] picked {r['answer']} — "
                     f"{r['question'][:44]}")
    L.append("")
    return L


def compose(rows, t, now=None, game=None):
    """S253: `game` (e.g. "Week 3 CIN @ PIT (final PIT 24, CIN 17)") is set
    when immaculate_tick.py sends this right after a game, which is when it
    runs now; without it the old Wednesday wording is kept."""
    now = now or datetime.now(timezone.utc)
    if game:
        subject = f"Immaculate Prediction — how we did: {game}"
        L = [f"IMMACULATE PREDICTION — POST-GAME RECAP: {game}", ""]
    else:
        subject = f"Immaculate Prediction — Wednesday recap ({now:%Y-%m-%d})"
        L = ["IMMACULATE PREDICTION — WEDNESDAY RECAP", ""]
    L.extend(season_section(rows, t))
    L.extend(weekly_section())
    L.append("Full working: ~/Documents/Cowork/immaculate/")
    L.append("")
    L.append("— CUMULUS, Project Immaculate")
    return subject, "\n".join(L)


def main():
    dry = "--dry-run" in sys.argv
    windowed = "--window" in sys.argv
    if windowed and not _in_season():
        print(OFF_SEASON_NOTE)
        _record(True, OFF_SEASON_NOTE)
        return 0
    if windowed and _already_sent_today():
        print("already sent today — duplicate send suppressed")
        _record(True, "already sent today — duplicate send suppressed")
        return 0

    rows = store.answers()
    t = store.tally()
    game = sys.argv[sys.argv.index("--game") + 1] if "--game" in sys.argv[:-1] else None
    subject, body = compose(rows, t, game=game)

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
             "question": "q", "status": "pending"} for i in range(1, 18)]
    rows += [{"number": str(i), "when": "season", "answer": "PIT",
              "question": "q"} for i in range(18, 25)]
    rows[0] = dict(rows[0], status="resolved", actual="PIT")   # Q1 correct
    rows[1] = dict(rows[1], status="resolved", actual="ATL")   # Q2 wrong
    t = {"resolved": 2, "correct": 1, "total": 24, "perfect_alive": False, "missed": [2]}

    sec = season_section(rows, t)
    text = "\n".join(sec)
    ck("season section reports the correct/wrong/outstanding split",
       "1 correct, 1 wrong, 22 still outstanding, out of 24" in text)
    ck("...and flags the perfect score as gone", "GONE" in text)
    ck("...and lists a resolved question with its mark", "Q 1 [OK]" in text)
    ck("...and lists the miss", "Q 2 [MISS]" in text)

    subj, body = compose(rows, t)
    ck("subject carries a date", "Wednesday recap" in subj)
    gs, gb = compose(rows, t, game="Week 3 CIN @ PIT (final PIT 24, CIN 17)")
    ck("post-game: subject and header name the game",
       "CIN @ PIT" in gs and "POST-GAME RECAP: Week 3" in gb)
    ck("body includes both major sections",
       "SEASON ENTRY" in body and "WEEKLY CONTEST" in body)

    def _d(y, m, d):
        return datetime(y, m, d, 13, 5, tzinfo=timezone.utc)
    ck("in season on the first of September", _in_season(_d(2026, 9, 1)) is True)
    ck("...and through the end of February", _in_season(_d(2027, 2, 28)) is True)
    ck("...and over New Year", _in_season(_d(2027, 1, 6)) is True)
    ck("off-season from the first of March", _in_season(_d(2027, 3, 1)) is False)
    ck("...through the end of August", _in_season(_d(2027, 8, 31)) is False)

    import tempfile, os
    _saved = (globals()["SENT_MARKER"], globals()["_record"],
              globals()["_in_season"], store.answers)
    try:
        with tempfile.TemporaryDirectory() as td:
            globals()["SENT_MARKER"] = Path(td) / "sent.json"
            ck("a fresh marker path means not sent today",
               _already_sent_today() is False)
            _stamp_sent()
            ck("...and after stamping it means sent today",
               _already_sent_today() is True)

            # Guard ORDER: off-season, main() must return before the dedup
            # check and before touching the CRM, and must record a quiet
            # success -- the weekly cadence check reads a missing row as a
            # dead job.
            recorded = []
            globals()["_record"] = lambda ok, note: recorded.append((ok, note))
            globals()["_in_season"] = lambda now=None: False
            store.answers = lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("CRM must not be read off-season"))
            _argv, sys.argv = sys.argv, ["x", "--window"]
            try:
                rc = main()
            finally:
                sys.argv = _argv
            ck("off-season timer run exits 0 without touching the CRM", rc == 0)
            ck("...and records a quiet no-op, not a failure",
               recorded == [(True, OFF_SEASON_NOTE)])

            # In season, the gate lets the run through to the next guard:
            # today's send is already stamped, so it is suppressed as a dup.
            recorded.clear()
            globals()["_in_season"] = lambda now=None: True
            _argv, sys.argv = sys.argv, ["x", "--window"]
            try:
                rc = main()
            finally:
                sys.argv = _argv
            ck("in season, the timer run passes the gate to the dedup guard",
               rc == 0 and recorded ==
               [(True, "already sent today — duplicate send suppressed")])
    finally:
        (globals()["SENT_MARKER"], globals()["_record"],
         globals()["_in_season"], store.answers) = _saved

    print(f"\n{'ALL PASS' if not fails else f'{fails} FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(selftest() if "selftest" in sys.argv else main())
