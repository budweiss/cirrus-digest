#!/usr/bin/env python3
"""
config_snapshot.py — point-in-time backups of CIRRUS's mutable config so any
automatic change (esp. self-review source auto-adds) can be rolled back.

The overlay (sources.local.json) and the approval queue (pending_approvals
.json) are intentionally OUTSIDE git, so they have no version history. This
module snapshots them (plus the git-tracked sources.json for good measure)
into config/snapshots/<YYYY-MM-DD_HHMMSS>/ and keeps the last RETAIN_DAYS.

Called automatically at the START of each self-review (before it changes
anything). Also a CLI:
  python3 config_snapshot.py snapshot        # take one now
  python3 config_snapshot.py list            # list available snapshots
  python3 config_snapshot.py restore <name>  # restore a snapshot (backs up
                                             # current state first)
"""

import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path.home() / "projects/cirrus-digest"
CONFIG_DIR  = PROJECT_DIR / "config"
SNAP_DIR    = CONFIG_DIR / "snapshots"
RETAIN_DAYS = 14

# Mutable files worth snapshotting (skip credentials — never copy secrets).
FILES = ["sources.local.json", "pending_approvals.json", "sources.json",
         "email_omit.txt"]


def _log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] snapshot: {msg}",
          flush=True)


def take_snapshot(tag=""):
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S") + (f"_{tag}" if tag else "")
    dest = SNAP_DIR / stamp
    dest.mkdir(exist_ok=True)
    n = 0
    for name in FILES:
        src = CONFIG_DIR / name
        if src.exists():
            shutil.copy2(src, dest / name)
            n += 1
    (dest / "_manifest.json").write_text(json.dumps(
        {"created": datetime.now().isoformat(timespec="seconds"),
         "files": n, "tag": tag}, indent=2))
    _prune()
    _log(f"took snapshot {stamp} ({n} files)")
    return dest


def _prune():
    if not SNAP_DIR.exists():
        return
    cutoff = datetime.now() - timedelta(days=RETAIN_DAYS)
    for d in SNAP_DIR.iterdir():
        if not d.is_dir():
            continue
        try:
            when = datetime.strptime(d.name[:19], "%Y-%m-%d_%H%M%S")
        except ValueError:
            continue
        if when < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            _log(f"pruned old snapshot {d.name}")


def list_snapshots():
    if not SNAP_DIR.exists():
        return []
    out = []
    for d in sorted(SNAP_DIR.iterdir(), reverse=True):
        if d.is_dir():
            files = sorted(p.name for p in d.glob("*.json")
                           if p.name != "_manifest.json") + \
                    sorted(p.name for p in d.glob("*.txt"))
            out.append((d.name, files))
    return out


def restore(name):
    src = SNAP_DIR / name
    if not src.is_dir():
        # allow prefix match (date only)
        cands = [d for d in SNAP_DIR.iterdir()
                 if d.is_dir() and d.name.startswith(name)]
        if len(cands) == 1:
            src = cands[0]
        elif not cands:
            _log(f"no snapshot matching '{name}'")
            return False
        else:
            _log(f"ambiguous '{name}': {[c.name for c in cands]}")
            return False
    # Back up current state first (so restore is itself reversible).
    take_snapshot(tag="pre-restore")
    n = 0
    for f in src.glob("*"):
        if f.name == "_manifest.json":
            continue
        shutil.copy2(f, CONFIG_DIR / f.name)
        n += 1
    _log(f"restored {src.name} ({n} files). Restart the bot if queue changed.")
    return True


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    if cmd == "snapshot":
        take_snapshot(sys.argv[2] if len(sys.argv) > 2 else "")
    elif cmd == "list":
        for name, files in list_snapshots():
            print(f"{name}  [{', '.join(files)}]")
    elif cmd == "restore" and len(sys.argv) > 2:
        restore(sys.argv[2])
    else:
        print("usage: config_snapshot.py snapshot|list|restore <name>")
