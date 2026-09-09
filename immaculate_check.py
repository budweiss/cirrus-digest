#!/usr/bin/env python3
"""Daily double-check of the Immaculate entries — Buddy's ask.

WHAT IT ACTUALLY CHECKS, and what it deliberately does not
----------------------------------------------------------
Re-deriving 24 researched answers every day would be expensive and would mostly
re-confirm base rates that cannot move (Q13's odd/even split does not change
because a running back tweaked a hamstring). So this checks the things that CAN
move an answer before the Sunday lock, and says plainly when it cannot check
something:

  * the CONTEST ITSELF -- the questions PDF is re-fetched and hashed. If the
    Steelers quietly change a question or an option list, that is the single
    most dangerous thing that could happen to this entry and nothing else
    would catch it.
  * VOLATILE answers -- the ones whose `volatile` flag is set: injuries, depth
    chart and market moves. Reported for a human to judge, not auto-changed.
  * The deadline, counted down in hours.

Writes nothing to the answers. If something needs changing, that is Buddy's
call, and `immaculate_store` records it with a dated field_change when he makes
it.
"""
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import immaculate_store as store   # noqa: E402
import entity_kb                   # noqa: E402

QUESTIONS_URL = ("https://static.clubs.nfl.com/image/upload/steelers/"
                 "wkvr9j26n5nxzpbxhbha")
KNOWN_SHA = "9add2d0c686f56fb17801ba4901db5bfb8fab3de9f526a2c26a8ddb0680f5f63"
LOCK = datetime(2026, 9, 13, 17, 0, tzinfo=timezone.utc)   # 1:00 PM ET


def fetch_sha(url=QUESTIONS_URL):
    """sha256 of the official questions PDF, or None if unreachable.

    None is NOT 'unchanged' -- an unreachable source is an unknown, and the
    caller must say so rather than print a clean bill (T76)."""
    r = subprocess.run(["curl", "-sS", "-m", "45", "-L", url],
                       capture_output=True)
    if r.returncode != 0 or not r.stdout:
        return None
    return hashlib.sha256(r.stdout).hexdigest()


def check_source():
    sha = fetch_sha()
    if sha is None:
        return ("UNCHECKED", "could not fetch the official questions PDF — "
                             "this is NOT 'unchanged'")
    if sha != KNOWN_SHA:
        return ("CHANGED", f"⚠ THE OFFICIAL QUESTIONS PDF HAS CHANGED. "
                           f"was {KNOWN_SHA[:12]}, now {sha[:12]}. "
                           f"Re-read it before entering — an option list may "
                           f"have moved under us.")
    return ("OK", f"official questions unchanged ({sha[:12]})")


def hours_left(now=None):
    now = now or datetime.now(timezone.utc)
    return (LOCK - now).total_seconds() / 3600.0


def report(now=None):
    lines = []
    state, detail = check_source()
    lines.append(f"[{state}] source: {detail}")

    h = hours_left(now)
    if h > 0:
        lines.append(f"[OK] {h:.0f} hours until the entry locks "
                     f"(Sun 13 Sep, 1:00 PM ET)")
    else:
        lines.append(f"[LOCKED] the entry closed {-h:.0f} hours ago — "
                     f"answers are final; from here this job tracks results")

    try:
        rows = store.answers()
    except Exception as e:
        lines.append(f"[UNCHECKED] could not read the CRM: {e}")
        return lines, state

    vol = [r for r in rows if r.get("volatile") == "yes"]
    lines.append(f"[OK] {len(rows)} answers on file, {len(vol)} of them "
                 f"volatile (injury/depth-chart/market sensitive)")

    weak = [r for r in rows if float(r.get("confidence", 1)) < 0.45]
    if weak:
        lines.append("[NOTE] weakest answers, worth a human eye before lock: "
                     + ", ".join(f"Q{r['number']} {r['answer']} "
                                 f"({float(r['confidence'])*100:.0f}%)"
                                 for r in weak))
    return lines, state


def selftest() -> int:
    fails = 0
    def ck(n, c):
        nonlocal fails
        print(f"  [{'OK ' if c else 'FAIL'}] {n}")
        fails += 0 if c else 1

    from datetime import timedelta
    ck("hours_left counts down before the lock",
       hours_left(LOCK - timedelta(hours=10)) > 9.9)
    ck("...and goes negative after it",
       hours_left(LOCK + timedelta(hours=5)) < 0)

    # the source check must never treat 'unreachable' as 'unchanged'
    g = globals()
    saved = g["fetch_sha"]
    try:
        g["fetch_sha"] = lambda url=None: None
        st, d = check_source()
        ck("an unreachable questions PDF is UNCHECKED, never OK", st == "UNCHECKED")
        ck("...and says so in words", "NOT 'unchanged'" in d)
        g["fetch_sha"] = lambda url=None: "deadbeef" * 8
        st, d = check_source()
        ck("a CHANGED questions PDF is flagged loudly", st == "CHANGED"
           and "HAS CHANGED" in d)
        g["fetch_sha"] = lambda url=None: KNOWN_SHA
        st, d = check_source()
        ck("the known-good hash reports OK", st == "OK")
    finally:
        g["fetch_sha"] = saved
    print(f"\n{'ALL PASS' if not fails else f'{fails} FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    if "selftest" in sys.argv:
        sys.exit(selftest())
    lines, state = report()
    print(f"=== Immaculate daily check — {datetime.now():%Y-%m-%d %H:%M} ===")
    for l in lines:
        print("  " + l)
    try:
        import job_status
        job_status.record("immaculatecheck", state != "CHANGED",
                          "; ".join(l for l in lines if not l.startswith("[OK]"))[:200]
                          or "all clear")
    except Exception:
        pass
    sys.exit(1 if state == "CHANGED" else 0)
