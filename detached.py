#!/usr/bin/env python3
"""detached.py — launch a job that must outlive the session, and be able to
say afterwards whether it finished. S141, watcher audit item 5.

WHY (2026-09-08, S139/S140). A gate harness was launched over ssh, the ssh
dropped, `tee` died, the harness took SIGPIPE at its final echo -- and its
summary file was left ZERO BYTES while the python child underneath had actually
completed fine. Nothing said so. The file is still there:
`~/tp2fp8/gate-effort-low-1.summary`, 0 bytes, 2026-09-08 15:50. The result had
to be reconstructed from a later run.

The class is wider than that one accident: any job a session starts and then
stops watching can die -- OOM, a reboot, a dropped connection, a `kill` from
something else -- and leave behind exactly what a *slow* job leaves behind,
which is nothing.

THE CONTRACT. A detached job writes a marker when it STARTS and updates it when
it ENDS. Then "started and never ended, well past when it should have" is a
question anyone can answer without having watched. That is the whole idea: not
to prevent the death, but to make the silence impossible.

  detached.py start --name gate-low-2 --expect-minutes 20 -- ./harness.sh
  detached.py sweep                     # JSON of every marker's state
  detached.py sweep --brief             # one line per problem

`model_health` runs the sweep daily and alerts, so a marker nobody looks at is
still read by something.

NEVER RAISES on the marker path: instrumentation must not be what kills the job
it is watching.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

MARKER_DIR = Path(os.environ.get("COWORK_DETACHED_DIR",
                                 Path.home() / ".cowork-detached"))
# How long after its own estimate a job is presumed dead rather than slow. A
# job that says 20 minutes and is still going at 20 is fine; at 20 + this, the
# honest answer is "nobody knows", which is what gets reported.
GRACE_MINUTES = 30


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def write_marker(name, **fields):
    """Create or update one marker. Never raises."""
    try:
        MARKER_DIR.mkdir(parents=True, exist_ok=True)
        p = MARKER_DIR / f"{name}.json"
        cur = {}
        if p.exists():
            try:
                cur = json.loads(p.read_text())
            except Exception:
                cur = {}
        cur.setdefault("name", name)
        cur.update(fields)
        p.write_text(json.dumps(cur, indent=2) + "\n")
        return p
    except Exception:
        return None


def start(name, cmd, expect_minutes=60, cwd=None):
    """Launch `cmd` detached, marked. Returns the child pid, or 0."""
    log = MARKER_DIR / f"{name}.log"
    write_marker(name, cmd=" ".join(cmd), started=_now(),
                 expect_minutes=int(expect_minutes), finished=None,
                 exit_code=None, log=str(log))
    try:
        MARKER_DIR.mkdir(parents=True, exist_ok=True)
        # Its own log FILE, never a pipe: the S140 accident was a `tee` on the
        # far side of an ssh that went away, and SIGPIPE at the harness's final
        # echo. A file cannot hang up.
        #
        # `start_new_session=True` is the detach, and it is deliberately NOT a
        # `setsid nohup` prefix: CIRRUS is a Mac and macOS ships no `setsid`
        # binary, so that prefix raises FileNotFoundError there and the marker
        # records a job that never started -- on the one box where the whole
        # point is to survive a dropped session. Popen's own flag calls
        # setsid(2) in the child and is portable. (Same family as the
        # `ps -o etimes` trap in stall_check: a Linux-ism that a Mac rejects.)
        with open(log, "ab", buffering=0) as fh:
            proc = subprocess.Popen(
                list(cmd),
                stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                cwd=cwd or os.getcwd(), start_new_session=True)
        write_marker(name, pid=proc.pid)
        return proc.pid
    except Exception as e:
        write_marker(name, finished=_now(), exit_code=-1,
                     error=f"{type(e).__name__}: {e}")
        return 0


def finish(name, exit_code):
    write_marker(name, finished=_now(), exit_code=int(exit_code))


def _alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def load_markers(d=None):
    d = Path(d or MARKER_DIR)
    out = []
    try:
        for p in sorted(d.glob("*.json")):
            try:
                out.append(json.loads(p.read_text()))
            except Exception:
                out.append({"name": p.stem, "unreadable": True})
    except Exception:
        return []
    return out


def sweep_verdict(markers, now=None, alive=None, grace=GRACE_MINUTES):
    """(problems, ok_count) from markers. PURE -- `alive` is injectable so the
    selftest never depends on real pids."""
    now = now or datetime.now()
    alive = alive if alive is not None else _alive
    problems, ok = [], 0
    for m in markers:
        name = m.get("name", "?")
        if m.get("unreadable"):
            problems.append(f"{name}: marker unreadable — cannot say whether it ran")
            continue
        if m.get("finished"):
            rc = m.get("exit_code")
            if rc not in (0, None):
                problems.append(f"{name}: finished with exit {rc}")
            else:
                ok += 1
            continue
        # started and not finished: slow, or dead?
        try:
            started = datetime.strptime(m.get("started", ""), "%Y-%m-%d %H:%M:%S")
        except Exception:
            problems.append(f"{name}: started with no readable timestamp")
            continue
        due = started + timedelta(minutes=int(m.get("expect_minutes") or 60))
        if now <= due + timedelta(minutes=grace):
            ok += 1                      # still inside its own estimate
            continue
        pid = m.get("pid")
        late = int((now - due).total_seconds() // 60)
        if pid and alive(pid):
            problems.append(f"{name}: still running {late} min past its estimate "
                            f"(pid {pid}) — slow, or wedged")
        else:
            problems.append(f"{name}: NEVER FINISHED — started {m.get('started')}, "
                            f"expected {m.get('expect_minutes')} min, process is "
                            f"gone and no exit code was ever written")
    return problems, ok


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("start")
    s.add_argument("--name", required=True)
    s.add_argument("--expect-minutes", type=int, default=60)
    s.add_argument("rest", nargs=argparse.REMAINDER)
    f = sub.add_parser("finish")
    f.add_argument("--name", required=True)
    f.add_argument("--exit-code", type=int, required=True)
    sw = sub.add_parser("sweep")
    sw.add_argument("--brief", action="store_true")
    sub.add_parser("selftest")
    a = ap.parse_args()

    if a.cmd == "selftest":
        return selftest()
    if a.cmd == "start":
        cmd = [x for x in a.rest if x != "--"]
        if not cmd:
            print("nothing to run", file=sys.stderr)
            return 2
        pid = start(a.name, cmd, a.expect_minutes)
        print(f"{a.name}: pid {pid}, marker {MARKER_DIR}/{a.name}.json")
        return 0 if pid else 1
    if a.cmd == "finish":
        finish(a.name, a.exit_code)
        return 0
    problems, ok = sweep_verdict(load_markers())
    if a.brief:
        for p in problems:
            print(f"- {p}")
        if not problems:
            print(f"- all clear ({ok} marker(s))")
    else:
        print(json.dumps({"problems": problems, "ok": ok}, indent=2))
    return 1 if problems else 0


def selftest():
    fails = 0

    def ck(name, cond):
        nonlocal fails
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        fails += 0 if cond else 1

    now = datetime(2026, 9, 9, 12, 0, 0)

    def m(**kw):
        base = {"name": "j", "started": "2026-09-09 10:00:00",
                "expect_minutes": 20, "finished": None, "exit_code": None,
                "pid": 999}
        base.update(kw)
        return base

    # THE CASE THIS EXISTS FOR: the S140 harness. Launched, its ssh dropped, the
    # process is gone, and no exit code was ever written.
    probs, _ = sweep_verdict([m()], now=now, alive=lambda p: False)
    ck("detached: started, long overdue, process gone -> NEVER FINISHED",
       len(probs) == 1 and "NEVER FINISHED" in probs[0])

    probs, ok = sweep_verdict([m(finished="2026-09-09 10:15:00", exit_code=0)],
                              now=now, alive=lambda p: False)
    ck("detached: a clean finish is quiet", not probs and ok == 1)

    probs, _ = sweep_verdict([m(finished="2026-09-09 10:15:00", exit_code=2)],
                             now=now, alive=lambda p: False)
    ck("detached: a non-zero exit is reported", len(probs) == 1
       and "exit 2" in probs[0])

    # Slow is not dead. A job inside its own estimate must not be alarmed about
    # (T9) -- and one still running past it is called slow, not lost.
    probs, ok = sweep_verdict([m(started="2026-09-09 11:55:00")], now=now,
                              alive=lambda p: True)
    ck("detached: a job inside its own estimate is quiet", not probs and ok == 1)
    probs, _ = sweep_verdict([m()], now=now, alive=lambda p: True)
    ck("detached: still-running past its estimate is 'slow or wedged', not lost",
       len(probs) == 1 and "still running" in probs[0])

    probs, _ = sweep_verdict([{"name": "x", "unreadable": True}], now=now)
    ck("detached: an unreadable marker ALERTS, never counts as fine (T76)",
       len(probs) == 1 and "unreadable" in probs[0])

    probs, ok = sweep_verdict([], now=now)
    ck("detached: no markers at all is quiet, not a failure", not probs and ok == 0)

    # start() must not write to the real marker dir from a test (T32/T80).
    import tempfile
    d = Path(tempfile.mkdtemp())
    g = globals()
    saved = g["MARKER_DIR"]
    try:
        g["MARKER_DIR"] = d
        pid = start("selftest-echo", ["/bin/echo", "hi"], expect_minutes=1)
        time.sleep(0.4)
        rec = json.loads((d / "selftest-echo.json").read_text())
        ck("detached: start() writes a marker with the command and a pid",
           rec.get("cmd", "").endswith("hi") and rec.get("pid"))
        ck("detached: ...and the child's output goes to a FILE, not a pipe that "
           "can hang up", (d / "selftest-echo.log").read_text().strip() == "hi")
        ck("detached: the live marker dir was untouched (T32)",
           not (saved / "selftest-echo.json").exists())
        ck("detached: the launch is portable — no setsid binary is invoked, "
           "because macOS (CIRRUS) has none",
           "setsid" not in Path(__file__).read_text().split("subprocess.Popen(")[1][:200])
    finally:
        g["MARKER_DIR"] = saved

    print(f"\n{'ALL PASS' if fails == 0 else f'{fails} FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
