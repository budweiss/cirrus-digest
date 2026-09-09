"""
job_status.py  (S49, 2026-08-01)
===============================================================================
Tiny run-status ledger for CIRRUS scheduled jobs. Each scheduled job calls
record(name, ok, note) when it finishes; the morning brief and jobs_check.py
read summarize() to report which jobs ran and succeeded.

This is the "did it actually run & succeed" half of monitoring. The 30-minute
cirrus_watchdog covers the other half ("is the agent loaded / did it exit
non-zero"). Together they catch both failure modes.

Stdlib only. Never raises to the caller — a monitoring write must not break the
job it is monitoring.
"""
import json
import re
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

STATUS_PATH = Path.home() / "projects/cirrus-digest/logs/jobs-status.json"

# Expected cadence, in hours, with a grace window baked in. A job whose last
# successful run is older than this is "overdue".
CADENCE_H = {
    # S99. Both added the same session they were created, because
    # placement-audit refused them otherwise -- "a job nobody watches fails
    # silently" -- and it was right: neither would have been noticed if it
    # stopped.
    "accesscheck":    2,        # every 30 min on cumulus1 (S101) — watches all
                                # three boxes at BOTH layers and is the only thing
                                # that watches cumulus2 at all
    "ytwatch":       26,        # daily 00:30 (YT-WATCH claim extractor)
    # S141. Added the session Project Immaculate was created, for the reason
    # written at the top of this table. It guards a HARD DEADLINE -- the
    # contest entry locks Sun 2026-09-13 13:00 ET and cannot be entered late --
    # so a silent stop between now and then is the one failure that cannot be
    # recovered from afterwards. Daily 07:15, run by a scheduled task that
    # ssh's to cumulus1; 26h leaves the usual 2h of grace.
    "immaculatecheck": 26,
    "cumulusstatepull": 26,     # daily 01:45 (CIRRUS pulls cumulus1 non-git
                                # state so the backup chain does not start on
                                # Buddy's laptop; must beat Time Machine ~02:30)
    # S102 (S100 finding). intake was NOT in this table at all, which is the
    # whole reason it lost one pass at every boot for at least three boots with
    # nothing noticing. It cycles every ~15 min on both boxes (separate
    # mailboxes), so 2h is eight cycles of grace -- tight enough to see a stall,
    # loose enough not to cry wolf on a single slow cycle.
    "intake":         2,
    "morningbrief":  26,        # daily 07:30
    "modelhealth":   26,        # daily 05:30 (API-model check + self-heal, S56)
    "pedagogy":      26,        # daily 06:00 (runs on CUMULUS since S57)
    "billnewdev":    24 * 8,    # weekly Monday + grace
    "billsnow":      24 * 8,    # weekly Monday + grace
    # S81 CORRECTION: this said `24 * 8` -- weekly + grace -- since S57, when
    # the job WAS weekly. The timer on CUMULUS is `*-*-* 03:00:00` and the unit
    # calls itself "cirrus-hoaleads daily research": it has been DAILY for
    # months. At a 192h tolerance Bill's lead research could have stopped for a
    # full week and this check would have printed a tick every day of it. A
    # cadence looser than the schedule is not a safety margin, it is a blind
    # spot with a checkmark on it.
    "hoaleads":      26,        # daily 03:00 (DE HOA lead monitor, S57; CUMULUS)
    "stratusreview": 24 * 33,   # monthly + grace
    # S67: dropped to BI-WEEKLY (even ISO weeks). The sweep is ~416 Brave
    # queries in one burst -- the single largest line item in the Brave bill,
    # and at weekly cadence it exhausted the $25+$5 monthly cap around the
    # 22nd-25th, degrading search for every other consumer. The gate lives in
    # privacy_monitor.py; this cadence must track it or a normal skipped week
    # reads as "overdue" and trains us to ignore the overdue signal.
    "privacymon":    24 * 15,   # bi-weekly Sunday + grace
    # S67 vendor/account mail watcher (daily 07:20). Nothing watched the
    # operational inboxes for funds/quota/key-expiry/suspension mail before
    # this -- the Brave alert was found by eye.
    "vendormail":    26,        # daily 07:20
    # S66 business-idea pipeline (CIRRUS, daily 07:45 / 07:55 / 08:15).
    # Tracked separately rather than as one entry: during the shakedown week
    # it matters WHICH stage broke -- the report still sends (just thinner)
    # when the scan or ideation fails, so a single combined check would look
    # green while half the pipeline was dead.
    "businessideascan":   26,   # daily 07:45 -- RSS + email + search intake
    "businessideaideate": 26,   # daily 07:55 -- council generation
    "businessideareport": 26,   # daily 08:15 -- the email Buddy actually reads
    "businessideafeeds":  26,   # daily 07:40 -- judge trials (+ discover on Sundays)
    # S75: the two HEAVIEST jobs were absent from this table and never called
    # record(), so jobscheck-report and the morning brief said nothing about
    # them at all -- and a report that omits a job reads exactly like a report
    # where that job is fine (S74 found this; the S74 stall detector exists
    # because "could not check" must never render as "healthy").
    "daily":         26,        # daily 02:00
    "digest":        24 * 8,    # weekly Sunday 02:30 + grace
    # S81: the scout has recorded here since S77 and NOTHING read it, because
    # a ledger entry is only checked if it also appears in this table. On
    # 2026-08-27 it died at 02:00 on a transient DNS outage, wrote
    # ok=False/"FAILED: every provider failed" exactly as designed, and sat
    # unreported for six hours -- the write end was right and the read end had
    # never been told. Daily 02:00 on CUMULUS.
    "opportunityscout": 26,
    # S81: the rest of what placement.py's coverage check found unwatched.
    # Every one of these was a live scheduled job that no monitor looked at,
    # so the only way to learn it had stopped was to notice its output missing.
    "devloop":          26,     # daily 21:30 CIRRUS -- the self-repair loop
                                # itself, which watched everything but itself
    "devreport":        26,     # daily 06:30 CIRRUS -- the morning report; if
                                # THIS stops, the silence looks like a quiet
                                # night, which is the worst possible failure
                                # mode for a reporting job
    "halftimecatalogue": 26,    # daily 06:30 CUMULUS (Justin)
    "cumulusdailybrief": 26,    # daily 20:00 CUMULUS
    "halftimerouting":  24 * 8,  # weekly Sun 22:00 CUMULUS (Justin)
    "entitykbdigest":   24 * 8,  # weekly Mon 05:00 CUMULUS -- Bill, CLIENT-FACING
    # S81 THE FRONT DOOR. Daily 21:15 CIRRUS, fifteen minutes ahead of the
    # builder. Watched from the day it was installed rather than months later,
    # which is the entire point of T44 -- and placement.py's coverage check
    # would have failed the session wrap if this line were missing.
    "devfindings":      26,
    # S82 ALOPECIA P1. Daily 05:45 CUMULUS. Registered in the same change that
    # installed the timer -- T44's rule: a job is watched from the day it
    # exists, not from the day someone notices it stopped.
    "alopeciacollect":  26,
    # S87 THE FITNESS FUNCTION. Daily 00:00 CIRRUS. devloop_yield.py was written
    # in S73 and scheduled NOWHERE for eight days -- no plist, no timer, no
    # runner command, no caller -- while stall-check correctly reported the
    # ledger going stale and nothing acted on it. Armed and watched in the same
    # change (T44), and devloop_yield.main() now calls job_status.record: a
    # MAX_AGE row for a job that never records is a permanent false OVERDUE,
    # which is the REMOTE_JOBS trap described a few lines below.
    "devloopyield":     26,
    # S95 ALOPECIA P2. Weekly Fri 07:00 CUMULUS. 24*8 so a single missed Friday
    # shows before the next one is due. Registered in the same change that armed
    # the timer (T44) -- and this project is exactly why that rule exists: the
    # brief was DATE-CONFIRMED for 2026-09-01, nothing sent, and nothing noticed
    # until Buddy asked. main() calls job_status.record on both the sent and the
    # send_guard-blocked path, so this row can never become a permanent false
    # OVERDUE for a job that simply never records.
    "alopeciabrief":    24 * 8,
}

# S57 cutover: these client jobs now RUN ON CUMULUS. When summarize() runs on
# CIRRUS (dev), it reads their status from CUMULUS's ledger over the read-only SSH
# link instead of the (now-stale) local ledger — so a moved job is reported from
# where it actually runs, not falsely flagged OVERDUE here.
REMOTE_JOBS   = {"billsnow", "billnewdev", "pedagogy", "hoaleads",
                 # S81 -- all of these run on CUMULUS, so when summarize()
                 # runs on CIRRUS their status must be read from CUMULUS's
                 # ledger. Omitting one here does not merely mis-attribute it:
                 # CIRRUS's own ledger has no entry, so it reports OVERDUE
                 # forever and trains us to ignore the overdue signal.
                 "opportunityscout", "halftimecatalogue", "halftimerouting",
                 "cumulusdailybrief", "entitykbdigest",
                 "alopeciacollect",                       # S82, runs on CUMULUS
                 "alopeciabrief",                         # S95, runs on CUMULUS
                 "immaculatecheck",                       # S141, runs on CUMULUS
                 # S102: accesscheck was added in S101 and NOT listed here, so
                 # CIRRUS looked for it locally, never found it, and printed
                 # "no run recorded yet" every time -- neutrally, so it never
                 # failed anything. That state is INDISTINGUISHABLE from the
                 # monitor being dead, and this is the only monitor that watches
                 # cumulus2 at all. Exactly the omission the comment above warns
                 # about, made four rows below the warning.
                 "accesscheck"}                           # S101, runs on cumulus1
REMOTE_HOST   = "buddy@192.168.0.204"                     # cumulus1 over LAN (CIRRUS read-only key)
REMOTE_STATUS = "cirrus-digest/logs/jobs-status.json"     # ~ on cumulus1


# ── S141: record() used to destroy this file ─────────────────────────────────
# Every job on a box writes its row into ONE shared file, and record() was a
# plain read-modify-write:
#
#     try:    data = json.loads(STATUS_PATH.read_text())
#     except: data = {}                    # <-- a failed read became "empty"
#     data[name] = {...}
#     STATUS_PATH.write_text(...)          # <-- then wrote THAT
#
# `write_text` truncates before it writes. Any job reading inside that window
# got an empty file, `except` turned that into `{}`, and the next write
# contained ONE row -- silently deleting every other job's status. Measured:
# one simulated torn read took a 3-job file down to 1.
#
# What it cost: jobs whose rows had been wiped looked to Skywarden's
# completeness check like jobs that had not run. `accesscheck` was reported
# missing for 28 hours while its journal shows 120 start/finish lines in that
# window -- it never missed a run. Skywarden then spent $0.45 per heartbeat
# reasoning about the phantom: $11.64 of September's $16.38, 71% of its budget,
# on stalls that never happened. The JSONDecodeError it logged at 05:27 today
# is a reader catching the same truncation window from the outside.
#
# Two independent fixes, because either alone leaves a hole:
#   * WRITE atomically (tmp file + os.replace). A reader now sees the old
#     complete file or the new complete file, never a half-written one. This
#     removes the window that creates the torn read in the first place.
#   * NEVER let a failed read mean "empty". A file that exists and does not
#     parse is CORRUPTION, and overwriting it takes every other job's history
#     with it. Retry first (an atomic writer's window is microseconds, so a
#     retry lands on a complete file), and if it still will not parse, keep a
#     copy before starting fresh.
#
# And take a lock across the read-modify-write, or two jobs whose timers
# coincide still lose one another's update -- a quieter version of the same
# bug, and the reason a row can go stale while the job runs fine.
_LOCK_SUFFIX = ".lock"


def _read_status():
    """(data, problem). data is None when the file exists but cannot be trusted.

    The distinction is the whole point: "no file yet" is legitimately {}, while
    "a file that will not parse" must never be silently treated as {}.
    """
    try:
        if not STATUS_PATH.exists():
            return {}, ""
        raw = STATUS_PATH.read_text()
    except Exception as e:  # noqa: BLE001
        return None, f"unreadable ({type(e).__name__})"
    if not raw.strip():
        return None, "empty — a truncated write, not an empty ledger"
    try:
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        return None, f"unparseable ({type(e).__name__})"
    return (data, "") if isinstance(data, dict) else (None, "not a JSON object")


def _write_status_atomic(data):
    """Replace the file in one step. Never raises. -> True on success."""
    try:
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_PATH.with_name(f"{STATUS_PATH.name}.tmp-{os.getpid()}")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, STATUS_PATH)      # atomic on POSIX (both boxes)
        return True
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
        return False


def record(name, ok, note=""):
    """Append/update this job's last-run status. Best-effort; never raises."""
    lock = None
    try:
        import fcntl
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        lock = open(str(STATUS_PATH) + _LOCK_SUFFIX, "w")
        fcntl.flock(lock, fcntl.LOCK_EX)
    except Exception:
        lock = None                        # no lock is worse, but not fatal
    try:
        data, problem = _read_status()
        if data is None:
            for _ in range(3):             # the truncation window is tiny
                time.sleep(0.05)
                data, problem = _read_status()
                if data is not None:
                    break
        if data is None:
            # Still unreadable. Keep it: an overwrite here is exactly the bug.
            try:
                import shutil
                shutil.copy2(STATUS_PATH,
                             f"{STATUS_PATH}.corrupt-{int(time.time())}")
            except Exception:
                pass
            data = {}
        data[name] = {
            "last_run": datetime.now().isoformat(timespec="seconds"),
            "epoch": int(time.time()),
            "ok": bool(ok),
            "note": (note or "")[:200],
        }
        _write_status_atomic(data)
    finally:
        if lock is not None:
            try:
                lock.close()
            except Exception:
                pass


def _here():
    """Display name of the node this is running on (CIRRUS/CUMULUS/STRATUS)."""
    try:
        env = os.environ.get("TARGET_ENV", "dev")
        prof = json.loads((Path.home() / "projects/cirrus-digest/config/node_profiles.json").read_text())
        return prof.get(env, {}).get("node", "CIRRUS")
    except Exception:
        return "CIRRUS"


def _load_local():
    try:
        return json.loads(STATUS_PATH.read_text())
    except Exception:
        return {}


def _fetch_remote(_run=None):
    """Read CUMULUS's jobs-status.json over the read-only SSH link (S57). Returns
    the parsed dict, or None if the box is unreachable (never raises).

    S111: `_run` is a test seam. This branch survived every mutation because
    nothing could reach it without an ssh — and it is the branch that decides
    whether a whole box reads as "unreachable" or as garbage. Returning None is
    load-bearing: summarize() turns it into "can't confirm", NOT into overdue.
    """
    runner = _run or (lambda argv: subprocess.run(
        argv, capture_output=True, text=True, timeout=15))
    try:
        r = runner(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
             REMOTE_HOST, f"cat {REMOTE_STATUS}"])
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception:
        pass
    return None


def _row(name, cad_h, rec, now, tag=""):
    """Pure evaluation of one job's record -> (line, good). Testable offline."""
    if not rec:
        return f"• {name}{tag}: no run recorded yet", None
    age_h = (now - rec.get("epoch", 0)) / 3600.0
    overdue = age_h > cad_h
    good = bool(rec.get("ok")) and not overdue
    mark = "✅" if good else "⚠️"
    state = " OVERDUE" if overdue else (" FAILED" if not rec.get("ok") else "")
    # S83: the note used to render ONLY when the row was already bad, so a run
    # that succeeded WHILE degraded printed a bare tick. On 2026-08-28 the
    # alopecia collector recorded "100 found, 0 new, 1 source error(s)" and
    # jobscheck showed "✅ alopeciacollect" — medRxiv had refused the
    # connection and the report could not say so. The notes are one-liners the
    # jobs already write; showing them costs a few characters and is the
    # difference between "it ran" and "it ran, and here is what it did".
    raw = (rec.get("note") or "").strip()
    note = f" — {raw if not good else raw[:70]}" if raw else ""
    return f"{mark} {name}{tag}: {rec.get('last_run', '?')[:16]}{state}{note}", good


def summarize(_local=None, _node=None, _fetch=None):
    """Return (lines, all_ok).

    S111: the three underscore parameters are TEST SEAMS and default to the
    live sources, so every existing caller is unchanged. Before them this
    function could not be called offline at all, and its two branches — "remote
    box unreachable" and "one bad job makes the run not-ok" — survived every
    mutation. Those are the two the whole ledger rests on.

    Node-aware (S57): jobs in REMOTE_JOBS now run on CUMULUS, so when this runs on
    CIRRUS their status is read from CUMULUS's ledger over the SSH link and tagged
    "(CUMULUS)". If CUMULUS is unreachable, those jobs are reported neutrally
    ("can't confirm") rather than falsely OVERDUE. On CUMULUS itself, everything is
    read locally. all_ok is False only if a KNOWN job is overdue or last-run failed;
    a not-yet-recorded or unconfirmable job is neutral.
    """
    local = _load_local() if _local is None else _local
    node = _here() if _node is None else _node
    use_remote = node == "CIRRUS"
    remote = (_fetch or _fetch_remote)() if use_remote else None
    now = int(time.time())
    lines, all_ok = [], True
    for name, cad_h in CADENCE_H.items():
        is_remote = use_remote and name in REMOTE_JOBS
        if is_remote and remote is None:
            lines.append(f"• {name} (CUMULUS): unreachable — can't confirm")
            continue
        src = remote if is_remote else local
        line, good = _row(name, cad_h, (src or {}).get(name), now,
                          tag=" (CUMULUS)" if is_remote else "")
        if good is False:
            all_ok = False
        lines.append(line)
    return lines, all_ok


def _selftest_record(ck):
    """S141 — record() must never destroy the rows it did not write.

    Every case below is the real incident: a shared file, one truncating
    writer, and rows that vanished while their jobs kept running.
    """
    import tempfile, json as _j, threading
    from pathlib import Path as _P
    g = globals()
    saved = g["STATUS_PATH"]
    d = _P(tempfile.mkdtemp())
    try:
        g["STATUS_PATH"] = d / "jobs-status.json"     # T32: never the live file
        three = {j: {"last_run": "x", "epoch": 1, "ok": True, "note": ""}
                 for j in ("accesscheck", "alopeciacollect", "hoaleads")}

        # 1. THE BUG, stated honestly. Rows already gone from the disk cannot
        #    be brought back -- once a truncated file is all that exists, the
        #    history is lost. What is fixable is the two things that CAUSED it:
        #    creating the window (check 5), and treating a file we could not
        #    read as one that was legitimately empty. So an empty file must be
        #    handled as CORRUPTION -- kept, and visibly so -- and never
        #    confused with the missing file in check 3, which really is {}.
        g["STATUS_PATH"].write_text(_j.dumps(three))
        g["STATUS_PATH"].write_text("")               # a truncated file
        record("intake", True, "a normal run")
        ck("record: an EMPTY file is corruption, not an empty ledger — kept, "
           "not silently accepted",
           len(list(d.glob("jobs-status.json.corrupt-*"))) == 1
           and "intake" in _j.loads(g["STATUS_PATH"].read_text()))
        for c in d.glob("jobs-status.json.corrupt-*"):
            c.unlink()

        # 2. Corruption is preserved, never silently replaced.
        g["STATUS_PATH"].write_text("{not json at all")
        record("intake", True, "x")
        ck("record: an unparseable file is COPIED ASIDE before starting fresh",
           len(list(d.glob("jobs-status.json.corrupt-*"))) == 1)
        ck("record: ...and the run is still recorded",
           "intake" in _j.loads(g["STATUS_PATH"].read_text()))

        # 3. No file at all is legitimately empty -- that must still work.
        g["STATUS_PATH"].unlink()
        record("first", True, "")
        ck("record: a missing file is created, not treated as corruption",
           list(_j.loads(g["STATUS_PATH"].read_text())) == ["first"])

        # 4. Concurrency: two writers must not lose each other. This is the
        #    quieter half of the same bug -- a row going stale while its job
        #    runs fine.
        g["STATUS_PATH"].write_text(_j.dumps(three))
        names = [f"j{i}" for i in range(12)]
        ts = [threading.Thread(target=record, args=(n, True, "")) for n in names]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        got = _j.loads(g["STATUS_PATH"].read_text())
        ck("record: 12 concurrent writers all survive, and so do the originals",
           set(got) >= set(names) | set(three))

        # 5. A reader must NEVER catch a half-written file -- that window is
        #    what created the torn read in the first place.
        #
        #    The payload is deliberately LARGE. The first version of this check
        #    used a handful of tiny rows and PASSED against a mutant with the
        #    atomic write removed: a small write_text() finishes too fast for a
        #    python reader to land inside it, so the check could not fail and
        #    was therefore not a check. ~300 padded rows widen the window until
        #    a non-atomic write is caught every time.
        big = {f"pad{i}": {"last_run": "x", "epoch": 1, "ok": True,
                           "note": "y" * 180} for i in range(300)}
        g["STATUS_PATH"].write_text(_j.dumps(big, indent=2))
        bad = []
        stop = threading.Event()

        def _reader():
            while not stop.is_set():
                try:
                    raw = g["STATUS_PATH"].read_text()
                    if raw.strip() and not raw.rstrip().endswith("}"):
                        bad.append(raw[-40:])
                except Exception:
                    pass

        r = threading.Thread(target=_reader)
        r.start()
        try:
            for i in range(30):
                record(f"w{i}", True, "z" * 180)
        finally:
            stop.set()
            r.join()
        ck("record: a concurrent reader never sees a partial file", not bad)

        #    ...and the check above cannot PROVE atomicity: in one process the
        #    GIL means the reader is never scheduled inside a C-level write, so
        #    it passes against a non-atomic mutant too. A check that cannot
        #    fail is not a check (T78), so pin the property that actually
        #    distinguishes the two, deterministically: os.replace swaps in a
        #    NEW file, while write_text truncates the existing one in place.
        #    Different inode == the reader could only ever have had the old
        #    complete file or the new complete one.
        ino_before = g["STATUS_PATH"].stat().st_ino
        record("inode-probe", True, "")
        ck("record: the write REPLACES the file (new inode), never truncates "
           "it in place — this is what removes the torn-read window",
           g["STATUS_PATH"].stat().st_ino != ino_before)

        # 6. No temp files left behind.
        ck("record: no .tmp- droppings left in the log dir",
           not list(d.glob("*.tmp-*")))
    finally:
        g["STATUS_PATH"] = saved


def selftest():
    """Offline: verify _row's overdue/failed/ok/neutral evaluation."""
    now = 1_000_000
    hr = 3600
    fails = 0

    def ck(label, cond):
        nonlocal fails
        print(f"  [{'OK ' if cond else 'FAIL'}] {label}")
        fails += 0 if cond else 1

    # S102. Two placement assertions, in both directions. A name in CADENCE_H
    # that runs on CUMULUS but is missing from REMOTE_JOBS reads as "never ran"
    # on CIRRUS forever; a name that runs on BOTH boxes must NOT be in it, or
    # CIRRUS would report CUMULUS's copy and go blind to its own.
    ck("accesscheck is read from CUMULUS — it runs on cumulus1 (S101)",
       "accesscheck" in REMOTE_JOBS)
    ck("intake is NOT remote — both boxes run their own, on SEPARATE mailboxes",
       "intake" in CADENCE_H and "intake" not in REMOTE_JOBS)
    ck("every REMOTE_JOBS name has a cadence, or it is never checked at all",
       REMOTE_JOBS <= set(CADENCE_H))

    _, g = _row("j", 26, {"epoch": now - 2 * hr, "ok": True, "last_run": "x"}, now)
    ck("fresh + ok -> good", g is True)
    _, g = _row("j", 26, {"epoch": now - 48 * hr, "ok": True, "last_run": "x"}, now)
    ck("stale beyond cadence -> overdue (not good)", g is False)
    _, g = _row("j", 26, {"epoch": now - 2 * hr, "ok": False, "last_run": "x"}, now)
    ck("recent but failed -> not good", g is False)
    line, g = _row("j", 26, None, now)
    ck("no record -> neutral (None)", g is None and "no run recorded" in line)

    # S83: a healthy row must still carry what the job reported about itself.
    line_ok, g_ok = _row("x", 26, {"epoch": now - hr, "ok": True,
                                   "last_run": "2026-08-28T05:45:09",
                                   "note": "100 found, 0 new, 1 source error(s)"}, now)
    ck("a HEALTHY row still shows its note (the degraded-but-ok case)",
       g_ok is True and "1 source error(s)" in line_ok)
    line, _ = _row("billsnow", 999, {"epoch": now, "ok": True, "last_run": "x"}, now,
                   tag=" (CUMULUS)")
    ck("remote tag renders", "(CUMULUS)" in line)

    # ---- S81: invariants about the TABLE, not just about _row ----------------
    # Every check above passed for years while the table itself was the broken
    # part: hoaleads was watched at a weekly tolerance though it runs daily,
    # and opportunityscout wrote a status nothing read. Mechanics were tested;
    # the contents never were.

    # A job is only watched if it is in BOTH structures. A REMOTE_JOB missing
    # from CADENCE_H is read off CUMULUS and then never looked at.
    orphans = sorted(REMOTE_JOBS - set(CADENCE_H))
    ck(f"every REMOTE_JOB is also in CADENCE_H (orphans: {orphans})", not orphans)

    # The specific regression. 24*8 here would mean a daily job may vanish for
    # a week and still print a tick.
    ck("hoaleads is watched at a DAILY tolerance, not weekly",
       CADENCE_H.get("hoaleads", 0) <= 30)
    ck("opportunityscout is watched at all", "opportunityscout" in CADENCE_H)
    ck("opportunityscout is read from the box it runs on",
       "opportunityscout" in REMOTE_JOBS)

    # No entry may be looser than a month unless it is genuinely monthly --
    # a large number here is how a blind spot hides in plain sight.
    loose = sorted(k for k, v in CADENCE_H.items()
                   if v > 24 * 16 and k != "stratusreview")
    ck(f"no job is watched at a tolerance over ~16d (loose: {loose})", not loose)

    # ---- S81: does every watched job have a WRITER? ----------------------
    # The other half of the loop, and a genuinely silent hole. A key in
    # CADENCE_H whose job never calls record() reads as "no run recorded yet"
    # -> good is None -> NEUTRAL -> all_ok stays True, forever. Adding a job to
    # this table without wiring its record() call therefore looks exactly like
    # a healthy job, which is the same failure the whole table exists to catch.
    # placement.py's coverage check cannot see this: it compares the table to
    # the SCHEDULE, and both sides would be satisfied.
    here = Path(__file__).resolve().parent
    writers = set()
    # RECURSE. The first version globbed the top level plus supervisor/ and
    # reported four false orphans -- billsnow, billnewdev, privacymon and
    # stratusreview all record from subdirectories (snowbrief/, newdev/,
    # privacy/, stratus/). Same shape as T44 itself: the GLOB of a check is its
    # scope, and a scope narrower than reality gives a confident wrong answer.
    _SKIP = {"__pycache__", ".git", ".venv", "venv", "node_modules", "build"}
    # S100: *.sh TOO. cumulus_state_pull.sh records from bash (it shells out to
    # python3 -c "import job_status; job_status.record(...)"), which is a
    # legitimate pattern -- and this check reported it as an orphan because it
    # only globbed *.py. That is the THIRD time this same check has been wrong
    # for the same reason: the first version read line-by-line and cried wolf on
    # eight wrapped calls, the second globbed too few directories and invented
    # four more. The glob of a check is its scope, and a scope narrower than
    # reality gives a confident wrong answer.
    for f in sorted(list(here.rglob("*.py")) + list(here.rglob("*.sh"))):
        if _SKIP & set(f.parts):
            continue
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        # Match across the CALL, not per line: these calls routinely wrap, and
        # the first version of this check read line-by-line and reported eight
        # false orphans -- a check that cries wolf on correct code gets
        # switched off, which is worse than not having it.
        for m in re.finditer(r"(?:job_status\.)?(?:record|_log_job)\s*\(",
                             text):
            window = text[m.end(): m.end() + 160]
            for k in CADENCE_H:
                if f'"{k}"' in window or f"'{k}'" in window:
                    writers.add(k)
    # jobscheck/watchdog-style keys would go here if any were read-only; today
    # every watched job is expected to write its own row.
    orphaned = sorted(set(CADENCE_H) - writers)
    # S101: expose the scanned extensions so the selftest can assert the SCOPE,
    # not just today's happy answer. PASS 6 flagged this file as changed without
    # a test change, and it was right: widening the glob to *.sh was a behaviour
    # change that only showed up as "the orphan list happens to be empty now".
    _scan_scope = sorted({f.suffix for f in here.rglob("*") if f.suffix in (".py", ".sh")})
    ck("the writer scan covers BOTH .py and .sh (a shell recorder is legitimate)",
       _scan_scope == [".py", ".sh"])
    ck("cumulusstatepull specifically is found, and it records from bash",
       "cumulusstatepull" in writers)
    ck(f"every watched job has a record() call somewhere (missing: {orphaned})",
       not orphaned)

    # And a daily job that stopped yesterday must actually trip.
    _, g = _row("hoaleads", CADENCE_H["hoaleads"],
                {"epoch": now - 30 * hr, "ok": True, "last_run": "x"}, now)
    ck("hoaleads silent for 30h reads as overdue", g is False)

    # ── S111 — summarize() and _fetch_remote(), the two branches the ledger
    #    rests on. Both survived EVERY mutation (8 of 10) because nothing here
    #    ever called them: the suite tested _row and the tables only. This is
    #    the TEST_GAP finding dev_findings raised on 2026-09-05.
    _now = int(time.time())
    _fresh = {"last_run": "x", "epoch": _now - 60, "ok": True, "note": "fine"}
    _failed = {"last_run": "x", "epoch": _now - 60, "ok": False, "note": "bad"}
    _rj = sorted(REMOTE_JOBS)[0] if REMOTE_JOBS else None
    _lj = next(n for n in CADENCE_H if n not in REMOTE_JOBS)

    if _rj:
        # The accesscheck shape (S102): an unreachable box must read NEUTRALLY.
        # "can't confirm" and "overdue" are different claims, and reporting the
        # second when you mean the first is how a dead monitor looks like a
        # late one.
        _lines, _ok = summarize(_local={_lj: _fresh}, _node="CIRRUS",
                                _fetch=lambda: None)
        _rline = [l for l in _lines if _rj in l]
        ck("an unreachable CUMULUS renders the remote job as can't-confirm",
           _rline and "unreachable — can't confirm" in _rline[0])
        ck("...and that does NOT make the run not-ok — unconfirmable is "
           "neutral, not failed",
           _ok is True)

        # The inverse: reachable box -> a real row, not the excuse line.
        _lines, _ok = summarize(_local={_lj: _fresh}, _node="CIRRUS",
                                _fetch=lambda: {_rj: _fresh})
        _rline = [l for l in _lines if _rj in l]
        ck("...while a REACHABLE CUMULUS produces a real row instead",
           _rline and "can't confirm" not in _rline[0]
           and "(CUMULUS)" in _rline[0])

        # A remote job that FAILED must still fail the run.
        _lines, _ok = summarize(_local={_lj: _fresh}, _node="CIRRUS",
                                _fetch=lambda: {_rj: _failed})
        ck("a FAILED remote job makes the whole run not-ok", _ok is False)

        # On CUMULUS nothing is fetched remotely at all.
        _called = []
        summarize(_local={_lj: _fresh}, _node="CUMULUS",
                  _fetch=lambda: _called.append(1))
        ck("on CUMULUS the remote fetch is never attempted", _called == [])

    # all_ok aggregation, both directions.
    _lines, _ok = summarize(_local={_lj: _failed}, _node="CUMULUS")
    ck("one failed LOCAL job makes the run not-ok", _ok is False)
    _lines, _ok = summarize(_local={n: _fresh for n in CADENCE_H},
                            _node="CUMULUS")
    ck("...and an all-healthy ledger is ok — or the flag is stuck off",
       _ok is True)

    # _fetch_remote: the branch that decides "unreachable" vs "garbage".
    class _R:
        def __init__(self, rc, out): self.returncode, self.stdout = rc, out
    ck("_fetch_remote parses a good reply",
       _fetch_remote(_run=lambda a: _R(0, '{"j": {"ok": true}}')) == {"j": {"ok": True}})
    ck("...returns None when ssh FAILS, rather than raising",
       _fetch_remote(_run=lambda a: _R(255, "")) is None)
    ck("...returns None on an EMPTY reply — a blank file is not an empty ledger",
       _fetch_remote(_run=lambda a: _R(0, "   ")) is None)
    ck("...returns None on unparseable JSON rather than propagating",
       _fetch_remote(_run=lambda a: _R(0, "not json")) is None)
    ck("...and never raises even if the runner itself explodes",
       _fetch_remote(_run=lambda a: (_ for _ in ()).throw(OSError("boom"))) is None)
    # The case that separates "checked the exit code" from "parsed whatever came
    # back": a FAILED ssh that still printed valid JSON on stdout. Every other
    # bad input is caught by the json.loads exception either way, so without
    # this the returncode test can be deleted and nothing notices. Trusting
    # stdout from a non-zero exit is how a half-open connection becomes a
    # confident, wrong ledger.
    ck("a FAILED ssh is not trusted even when its stdout parses as JSON",
       _fetch_remote(_run=lambda a: _R(255, '{"j": {"ok": true}}')) is None)

    _selftest_record(ck)
    print("PASS" if not fails else f"{fails} FAILURE(S)")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    if "selftest" in sys.argv:
        sys.exit(selftest())
