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
import time
from datetime import datetime
from pathlib import Path

STATUS_PATH = Path.home() / "projects/cirrus-digest/logs/jobs-status.json"

# Expected cadence, in hours, with a grace window baked in. A job whose last
# successful run is older than this is "overdue".
CADENCE_H = {
    "morningbrief":  26,        # daily 07:30
    "billnewdev":    24 * 8,    # weekly Monday + grace
    "billsnow":      24 * 8,    # weekly Monday + grace
    "stratusreview": 24 * 33,   # monthly + grace
}


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


def summarize():
    """Return (lines, all_ok).

    all_ok is False only if a KNOWN job is overdue or its last run failed.
    A job with no record yet (freshly deployed, not due) is shown neutrally and
    does NOT flip all_ok — the watchdog covers a never-loaded agent separately.
    """
    try:
        data = json.loads(STATUS_PATH.read_text())
    except Exception:
        data = {}
    now = int(time.time())
    lines, all_ok = [], True
    for name, cad_h in CADENCE_H.items():
        rec = data.get(name)
        if not rec:
            lines.append(f"• {name}: no run recorded yet")
            continue
        age_h = (now - rec.get("epoch", 0)) / 3600.0
        overdue = age_h > cad_h
        good = bool(rec.get("ok")) and not overdue
        all_ok = all_ok and good
        mark = "✅" if good else "⚠️"
        state = ""
        if overdue:
            state = " OVERDUE"
        elif not rec.get("ok"):
            state = " FAILED"
        note = f" — {rec['note']}" if (rec.get("note") and not good) else ""
        lines.append(f"{mark} {name}: {rec.get('last_run', '?')[:16]}{state}{note}")
    return lines, all_ok
