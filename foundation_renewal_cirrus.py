#!/usr/bin/env python3
"""CIRRUS entry point for foundation_renewal.py (S316).

CIRRUS tests only its own local (ollama) approvals; cloud approvals are tested
on CUMULUS. This file exists so the CIRRUS run records CIRRUS's own ledger name
-- job_status's static check reads which names each box's entry file records.

    python3 foundation_renewal_cirrus.py            # the daily check (LaunchDaemon 04:50)
    python3 foundation_renewal_cirrus.py --force    # run the local tests now
    python3 foundation_renewal_cirrus.py --plan     # what a run would do; no model calls
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import foundation_renewal as R

if __name__ == "__main__":
    a = sys.argv[1:]
    if a not in ([], ["--force"], ["--plan"]):
        sys.exit("usage: foundation_renewal_cirrus.py [--force|--plan]")   # T107: never fall through
    if R.on_cumulus():
        sys.exit("this is the CIRRUS entry point; on CUMULUS run foundation_renewal.py")
    if a == ["--plan"]:
        R.plan(); sys.exit(0)
    ok, note = R.main(force=a == ["--force"])
    try:
        import job_status
        job_status.record("foundationrenewal", ok, note[:300])
    except Exception as ex:
        R.log("job_status.record failed: %s" % ex)
    sys.exit(0 if ok else 1)
