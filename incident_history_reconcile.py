"""Merge resolved incident history back from a verified checkpoint (S348, O04).

A code rollback to a pre-S340 writer saves `heartbeat-incidents.json` without its
`resolved` history (proven with the actual historical writer in
test_rollback_preservation.py). That history survives only in the protected
checkpoint taken before the rollback (state_checkpoint.py). This produces a
reconciled CANDIDATE file for an operator to review and install deliberately.

What it does, and nothing else:
  * verifies the checkpoint's manifest hashes first; a tampered export is refused;
  * copies the CURRENT active incidents unchanged -- never the export's, which
    may describe incidents that were resolved or changed since;
  * appends export `resolved` entries the current file lacks. An entry already
    present is skipped; a same-key entry with different content keeps the
    CURRENT one and is reported as a conflict;
  * only REPORTS decisions, receipts and the Telegram offset. Restoring any of
    them could replay a consumed decision or a delivered message;
  * writes to a NEW output path that must not exist and must lie outside the
    state directory. It never writes into the live state.

Installing the candidate stays an operator step: stop the supervisor, capture a
fresh checkpoint, review this report, move the candidate into place, start, check.

  python3 incident_history_reconcile.py --checkpoint DIR --state DIR --output FILE [--dry-run]
"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

# Kept OUT of supervisor/: every file there is compared with Skywarden's /opt copy
# (deploy_drift.sh), and this operator tool is not part of the supervisor runtime.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from supervisor import state_checkpoint  # noqa: E402

INCIDENTS = "heartbeat-incidents.json"
WRITER_CAP = 256          # alert_policy.IncidentPolicy.save keeps resolved[-256:]


def _key(entry):
    """Identity of one resolved record: the incident and when it resolved."""
    if entry.get("resolved_at") is not None:
        return (entry.get("incident"), "at", entry["resolved_at"])
    return (entry.get("incident"), "row", json.dumps(entry, sort_keys=True))


def _load(path, what):
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise ValueError("%s is missing" % what)
    except ValueError:
        raise ValueError("%s is not valid JSON" % what)
    if (not isinstance(data, dict) or not isinstance(data.get("incidents"), dict)
            or not isinstance(data.get("resolved", []), list)
            or any(not isinstance(e, dict) for e in data.get("resolved", []))):
        raise ValueError("%s does not have the incident-state shape" % what)
    return data


def reconcile(checkpoint, state):
    """Return (candidate, report). Pure apart from reads; raises ValueError to refuse."""
    checkpoint, state = Path(checkpoint), Path(state)
    manifest = state_checkpoint.verify(checkpoint)          # refuses tampering
    if INCIDENTS not in manifest:
        raise ValueError("checkpoint holds no %s" % INCIDENTS)
    old = _load(checkpoint / INCIDENTS, "checkpoint incident state")
    cur = _load(state / INCIDENTS, "current incident state")
    have = {_key(e): e for e in cur.get("resolved", [])}
    added, duplicates, conflicts = [], 0, []
    for entry in old.get("resolved", []):
        k = _key(entry)
        if k not in have:
            added.append(entry)
            have[k] = entry
        elif have[k] == entry:
            duplicates += 1
        else:
            conflicts.append(entry.get("incident"))
    merged = sorted(added + list(cur.get("resolved", [])),
                    # legacy entries without resolved_at are the oldest
                    key=lambda e: (e.get("resolved_at") is not None, e.get("resolved_at") or 0))
    candidate = {"incidents": cur["incidents"], "resolved": merged}
    # Report-only comparisons: these are never merged.
    exported_queue = {n for n in manifest if n.startswith("pending-requests/")}
    current_queue = {"pending-requests/" + p.name for p in (state / "pending-requests").glob("*.json")}
    report = {
        "active_incidents_kept": len(cur["incidents"]),
        "export_only_active_not_restored": sorted(set(old["incidents"]) - set(cur["incidents"])),
        "resolved_current": len(cur.get("resolved", [])),
        "resolved_added_from_export": len(added),
        "resolved_already_present": duplicates,
        "conflicts_kept_current": conflicts,
        "resolved_total": len(merged),
        "writer_will_truncate": max(0, len(merged) - WRITER_CAP),
        "queued_decisions_in_export_not_current_not_restored": sorted(exported_queue - current_queue),
        "decisions_receipts_offsets": "reported only; never restored",
    }
    return candidate, report


def write_candidate(candidate, output, state):
    output, state = Path(output).resolve(), Path(state).resolve()
    if output.exists():
        raise ValueError("output already exists; refusing to overwrite")
    if state == output.parent or state in output.parents:
        raise ValueError("output must be outside the live state directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".reconciled-", dir=str(output.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(candidate, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.link(tmp, output)              # fails rather than replace a file that appeared meanwhile
    finally:
        os.unlink(tmp)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Merge resolved incident history back from a verified checkpoint.")
    ap.add_argument("--checkpoint", required=True, type=Path)
    ap.add_argument("--state", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    args = ap.parse_args(argv)
    try:
        candidate, report = reconcile(args.checkpoint, args.state)
        if not args.dry_run:
            write_candidate(candidate, args.output, args.state)
    except (ValueError, OSError) as exc:
        print("REFUSED: %s" % exc)
        return 2
    print(json.dumps(dict(report, output=None if args.dry_run else str(args.output)), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
