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
                 "alopeciacollect"}                        # S82, runs on CUMULUS
REMOTE_HOST   = "buddy@192.168.0.204"                     # cumulus1 over LAN (CIRRUS read-only key)
REMOTE_STATUS = "cirrus-digest/logs/jobs-status.json"     # ~ on cumulus1


def record(name, ok, note=""):
    """Append/update this job's last-run status. Best-effort; never raises."""
    try:
        data = json.loads(STATUS_PATH.read_text()) if STATUS_PATH.exists() else {}
    except Exception:
        data = {}
    data[name] = {
        "last_run": datetime.now().isoformat(timespec="seconds"),
        "epoch": int(time.time()),
        "ok": bool(ok),
        "note": (note or "")[:200],
    }
    try:
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATUS_PATH.write_text(json.dumps(data, indent=2) + "\n")
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


def _fetch_remote():
    """Read CUMULUS's jobs-status.json over the read-only SSH link (S57). Returns
    the parsed dict, or None if the box is unreachable (never raises)."""
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
             REMOTE_HOST, f"cat {REMOTE_STATUS}"],
            capture_output=True, text=True, timeout=15)
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
    note = f" — {rec['note']}" if (rec.get("note") and not good) else ""
    return f"{mark} {name}{tag}: {rec.get('last_run', '?')[:16]}{state}{note}", good


def summarize():
    """Return (lines, all_ok).

    Node-aware (S57): jobs in REMOTE_JOBS now run on CUMULUS, so when this runs on
    CIRRUS their status is read from CUMULUS's ledger over the SSH link and tagged
    "(CUMULUS)". If CUMULUS is unreachable, those jobs are reported neutrally
    ("can't confirm") rather than falsely OVERDUE. On CUMULUS itself, everything is
    read locally. all_ok is False only if a KNOWN job is overdue or last-run failed;
    a not-yet-recorded or unconfirmable job is neutral.
    """
    local = _load_local()
    node = _here()
    use_remote = node == "CIRRUS"
    remote = _fetch_remote() if use_remote else None
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


def selftest():
    """Offline: verify _row's overdue/failed/ok/neutral evaluation."""
    now = 1_000_000
    hr = 3600
    fails = 0

    def ck(label, cond):
        nonlocal fails
        print(f"  [{'OK ' if cond else 'FAIL'}] {label}")
        fails += 0 if cond else 1

    _, g = _row("j", 26, {"epoch": now - 2 * hr, "ok": True, "last_run": "x"}, now)
    ck("fresh + ok -> good", g is True)
    _, g = _row("j", 26, {"epoch": now - 48 * hr, "ok": True, "last_run": "x"}, now)
    ck("stale beyond cadence -> overdue (not good)", g is False)
    _, g = _row("j", 26, {"epoch": now - 2 * hr, "ok": False, "last_run": "x"}, now)
    ck("recent but failed -> not good", g is False)
    line, g = _row("j", 26, None, now)
    ck("no record -> neutral (None)", g is None and "no run recorded" in line)
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
    for f in here.rglob("*.py"):
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
    ck(f"every watched job has a record() call somewhere (missing: {orphaned})",
       not orphaned)

    # And a daily job that stopped yesterday must actually trip.
    _, g = _row("hoaleads", CADENCE_H["hoaleads"],
                {"epoch": now - 30 * hr, "ok": True, "last_run": "x"}, now)
    ck("hoaleads silent for 30h reads as overdue", g is False)

    print("PASS" if not fails else f"{fails} FAILURE(S)")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    if "selftest" in sys.argv:
        sys.exit(selftest())
