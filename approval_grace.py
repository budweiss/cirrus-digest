#!/usr/bin/env python3
"""P3 (Buddy, S530): a 72-hour grace period for an expired reviewed approval.

An approval whose fresh renewal fixtures all passed the mechanical gates
(shape, identity, finish, error) keeps running for up to 72 hours after it
expires while its review is pending. Any gate failure, a confirmed defect, or a
changed record ends it. This is not an approval: it never edits a registry,
never moves an expiry, and runs out on its own.

Entries are written only by the renewal jobs on the owned hosts and are keyed
by the hash of the exact approval record they cover.

    python3 approval_grace.py list
    python3 approval_grace.py revoke SHA [SHA ...]
"""
import fcntl
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

PATH = Path(__file__).resolve().parent / "config" / "approval-grace.json"
GRACE_S = 72 * 3600   # after the approval's own expiry
# Renewal fixtures re-run daily; CIRRUS adopts CUMULUS's cloud results at 04:50
# and 10:00, so its newest copy can be about 30 hours old before the next one.
FRESH_S = 36 * 3600


def record_sha(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()


def load(path=PATH):
    """The grace store, or an empty one; anything malformed grants nothing."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        data = None
    if (not isinstance(data, dict) or not isinstance(data.get("entries"), dict)
            or not isinstance(data.get("revoked", []), list)):
        return {"entries": {}, "revoked": []}
    return data


def active(record, now, data=None):
    """True only inside the 72 h after expiry, with fresh passing evidence."""
    data = load() if data is None else data
    sha = record_sha(record)
    entry = data["entries"].get(sha)
    if not isinstance(entry, dict) or sha in data.get("revoked", []):
        return False
    expires, tested = record.get("expires_at"), entry.get("tested_at")
    finite = lambda v: type(v) in (int, float) and math.isfinite(v)
    return (finite(expires) and finite(tested) and expires <= now < expires + GRACE_S
            and 0 <= now - tested <= FRESH_S)


def _update(change, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = load(path)
        data.setdefault("revoked", [])
        change(data)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
        os.replace(str(tmp), str(path))


def replace_source(source, entries, revoked=(), path=PATH):
    """Replace one writer's entries; a revoked record is never re-added."""
    def change(data):
        data["revoked"] = sorted(set(data["revoked"]) | set(revoked))
        data["entries"] = {k: v for k, v in data["entries"].items()
                           if not isinstance(v, dict) or v.get("source") != source}
        data["entries"].update({k: dict(v, source=source) for k, v in entries.items()
                                if k not in data["revoked"]})
        data["updated_at"] = time.time()
    _update(change, path)


def revoke(shas, path=PATH):
    def change(data):
        data["revoked"] = sorted(set(data["revoked"]) | set(shas))
        for sha in shas:
            data["entries"].pop(sha, None)
    _update(change, path)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["list"]:
        data, now = load(), time.time()
        for sha, e in sorted(data["entries"].items(), key=lambda kv: str(kv[1].get("label"))):
            print("%s  %-7s  tested %.1f h ago  %s" % (sha[:12], e.get("source"),
                  (now - e.get("tested_at", 0)) / 3600, e.get("label")))
        print("%d entr%s, %d revoked" % (len(data["entries"]), "y" if len(data["entries"]) == 1 else "ies",
                                         len(data["revoked"])))
    elif args[:1] == ["revoke"] and args[1:] and all(
            len(a) == 64 and not set(a) - set("0123456789abcdef") for a in args[1:]):
        revoke(args[1:])
        print("revoked %d" % len(args[1:]))
    else:
        sys.exit("usage: approval_grace.py list | revoke SHA [SHA ...]")
