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
    "hoaleads":      24 * 8,    # weekly Monday + grace (DE HOA lead monitor, S57)
    "stratusreview": 24 * 33,   # monthly + grace
    "privacymon":    24 * 8,    # weekly Sunday + grace
    # S66 business-idea pipeline (CIRRUS, daily 07:45 / 07:55 / 08:15).
    # Tracked separately rather than as one entry: during the shakedown week
    # it matters WHICH stage broke -- the report still sends (just thinner)
    # when the scan or ideation fails, so a single combined check would look
    # green while half the pipeline was dead.
    "businessideascan":   26,   # daily 07:45 -- RSS + email + search intake
    "businessideaideate": 26,   # daily 07:55 -- council generation
    "businessideareport": 26,   # daily 08:15 -- the email Buddy actually reads
    "businessideafeeds":  26,   # daily 07:40 -- judge trials (+ discover on Sundays)
}

# S57 cutover: these client jobs now RUN ON CUMULUS. When summarize() runs on
# CIRRUS (dev), it reads their status from CUMULUS's ledger over the read-only SSH
# link instead of the (now-stale) local ledger — so a moved job is reported from
# where it actually runs, not falsely flagged OVERDUE here.
REMOTE_JOBS   = {"billsnow", "billnewdev", "pedagogy", "hoaleads"}
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
    print("PASS" if not fails else f"{fails} FAILURE(S)")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    if "selftest" in sys.argv:
        sys.exit(selftest())
