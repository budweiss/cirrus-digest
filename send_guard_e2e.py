"""Prove the one-send-per-day guard short-circuits main() BEFORE the send (S81).

send_guard.py's own selftest checks the DECISION. This checks the WIRING -- that
each job actually consults it, early enough, and that a suppressed run still
reports itself. The two are different failures: a perfect guard nobody calls is
worth nothing.

SAFE BY CONSTRUCTION, and that is the whole design of this file. Testing a
client mailer for real would mean risking a real email to Bill if the guard were
broken -- precisely the outcome under test. So the first thing past the guard
(`decide` for billsnow, `_run` for billnewdev) is replaced with something that
RAISES. If the guard works it is never reached; if the guard is broken this
test explodes. Neither path can send.

The stamp directory is redirected to a temp dir, so the real stamps under
logs/send-stamps are never read or written.

  python3 send_guard_e2e.py [repo-dir]      # exit 0 = wired correctly
"""
import importlib.util, json, sys, tempfile, types
from datetime import datetime
from pathlib import Path

REPO = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(REPO))
import send_guard

FAILS = []
def ck(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond: FAILS.append(name)

class Boom(Exception): pass

def load(relpath, name):
    spec = importlib.util.spec_from_file_location(name, REPO / relpath)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m

tmp = Path(tempfile.mkdtemp())

for relpath, modname, job, tripwire in (
    ("snowbrief/bill_snow_weekly.py", "bsw", "billsnow", "decide"),
    ("newdev/bill_newdev_weekly.py",  "bnw", "billnewdev", "_run"),
):
    print(f"== {job} ==")
    try:
        m = load(relpath, modname)
    except Exception as e:
        ck(f"{job}: module imports", False); print("    ", e); continue

    # Everything past the guard is a landmine.
    setattr(m, tripwire, lambda *a, **k: (_ for _ in ()).throw(Boom("PAST THE GUARD")))
    recorded = []
    m._rec = lambda dry, ok, note="": recorded.append((ok, note))
    # Point send_guard at a scratch dir so we never touch the real stamps.
    m.send_guard = send_guard
    real_ast = send_guard.already_sent_today
    send_guard.already_sent_today = lambda j, now=None, root=None: real_ast(j, now, tmp)

    # 1. No stamp -> the guard must NOT block, so we should hit the tripwire.
    sys.argv = ["x"]
    try:
        m.main(); hit = False
    except Boom:
        hit = True
    except Exception:
        hit = True          # some other early failure; still past the guard
    ck(f"{job}: with NO stamp the run proceeds (guard does not block a real send)", hit)

    # 2. Stamp present -> main() must return cleanly, never reaching the tripwire.
    send_guard.mark_sent(job, "test", None, tmp)
    recorded.clear()
    try:
        m.main(); blocked = True
    except Boom:
        blocked = False
    except Exception as e:
        blocked = False; print("    unexpected:", type(e).__name__, e)
    ck(f"{job}: with today's stamp main() STOPS before the send path", blocked)
    ck(f"{job}: the suppressed run still records to job_status",
       len(recorded) == 1 and recorded[0][0] is True)
    ck(f"{job}: ...and the note says why",
       bool(recorded) and "suppress" in recorded[0][1].lower())

    # 3. --force-send must override.
    sys.argv = ["x", "--force-send"]
    try:
        m.main(); forced = False
    except Boom:
        forced = True
    except Exception:
        forced = True
    ck(f"{job}: --force-send overrides the stamp", forced)

    send_guard.already_sent_today = real_ast
    print()

print("all guard end-to-end checks passed" if not FAILS else f"{len(FAILS)} FAILED")
sys.exit(1 if FAILS else 0)
