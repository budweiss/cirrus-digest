#!/usr/bin/env python3
"""Fixed, root-owned job-status feed for the CUMULUS supervisor. S96.

Deployed to /usr/local/sbin/cumulus_job_status.py, owned root:root mode 755 —
NOT synced as part of supervisor/app/ (which cumulus-supervisor itself can
write). A script cumulus-supervisor could edit and then run via sudo would be a
privilege-escalation hole, so this one lives somewhere it cannot touch. Same
shape and same reason as cumulus_creds_health.py (S63) and
cumulus_client_watch.py (S78).

WHY IT EXISTS
-------------
Measured on cumulus1, 2026-09-02, as the cumulus-supervisor user:

    /home/buddy is drwxr-x--- buddy:buddy   -> cannot traverse
    NO ACCESS  /home/buddy/cirrus-digest/logs/jobs-status.json
    STATUS_PATH: /opt/cumulus-supervisor/cirrus-digest/logs/jobs-status.json
    jobs the supervisor can see: 0

Two faults stacked. `completeness.APP_DIR` defaults to
`Path.home()/"cirrus-digest"`, and Path.home() for the cumulus-supervisor user
is /opt/cumulus-supervisor — so it pointed at a path that does not exist. And
even with the right path it lacks permission to read it.

`completeness._load()` returns {} for a missing file, and an empty ledger has
nothing to complain about, so **check() has reported "all jobs producing" since
S67 without ever reading a single job.** A missing file reading as clean, inside
the module written to stop exactly that (T8).

WHY A SUDO HELPER AND NOT A PUBLISHED COPY OR A GROUP
-----------------------------------------------------
Buddy chose "publish the ledger to the supervisor" (2026-09-02) over widening
access. This is that, in the shape this box already uses and has already
reviewed:

  * A COPY on a timer would be one more moving part, and a stopped copier leaves
    a frozen ledger that reads as real data. This reads live, so there is
    nothing to go stale.
  * Adding cumulus-supervisor to the `buddy` group is worse than it sounds and
    this tree already rejected it in writing (see cumulus-supervisor.sudoers,
    S78): cirrus-digest is 775 and its files 664, so group membership grants
    **WRITE** on the app checkout to a read-only agent.

This grants ONE fixed, root-owned, non-editable script producing ONE fixed JSON
digest of operational metadata — job names, run timestamps, ok flags, and the
short counter notes the completeness rules already parse. It is not a general
read of the checkout and the agent cannot repoint it.

NO ARGUMENTS AT ALL, so the sudoers grant needs no wildcard (wildcard argument
matching is exploitable — this file's header says so). argv is checked here too:
defense in depth, but the grant must not be quietly wider than its comment.

Output: one JSON object on stdout. Exit 0 with ok=true, or non-zero with
ok=false and a reason. Never prints a credential or a client's prose.
"""
import json
import sys
import time
from pathlib import Path

APP = Path("/home/buddy/cirrus-digest")
LEDGER = APP / "logs/jobs-status.json"
JOB_STATUS = APP / "job_status.py"

# Only these keys leave the app tree. A whitelist, not a filter: if job_status
# ever starts recording something richer, it does not silently become readable
# by the supervisor because someone added a field.
FIELDS = ("last_run", "epoch", "ok", "note")


def _fail(msg, code=1):
    print(json.dumps({"ok": False, "error": msg}))
    sys.exit(code)


def main():
    # The sudoers grant is argument-free; refuse anything else rather than
    # relying on sudo alone to enforce it.
    if len(sys.argv) > 1:
        _fail(f"takes no arguments (got {len(sys.argv) - 1})", 2)

    try:
        raw = json.loads(LEDGER.read_text())
    except Exception as e:
        _fail(f"cannot read ledger {LEDGER}: {type(e).__name__}: {e}")

    if not isinstance(raw, dict):
        _fail(f"ledger is {type(raw).__name__}, expected object")

    jobs = {}
    for name, entry in raw.items():
        if isinstance(entry, dict):
            jobs[name] = {k: entry[k] for k in FIELDS if k in entry}

    # The cadence table, from the one place it is maintained. A second copy
    # would drift, and the first symptom of that drift would be the supervisor's
    # cadence check going quiet.
    cadence = {}
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_js", JOB_STATUS)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        t = getattr(m, "CADENCE_H", None)
        if isinstance(t, dict):
            cadence = {str(k): float(v) for k, v in t.items()
                       if isinstance(v, (int, float))}
    except Exception as e:
        # A readable ledger with no schedule is still useful to the zero-run
        # rules, so do not fail the whole feed -- but say the table is missing
        # so the caller reports BLIND rather than "nothing overdue".
        print(json.dumps({"ok": True, "generated_at": int(time.time()),
                          "jobs": jobs, "cadence_h": {},
                          "cadence_error": f"{type(e).__name__}: {e}"}))
        return 0

    print(json.dumps({"ok": True, "generated_at": int(time.time()),
                      "jobs": jobs, "cadence_h": cadence}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
