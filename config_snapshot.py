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
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path.home() / "projects/cirrus-digest"
CONFIG_DIR  = PROJECT_DIR / "config"
SNAP_DIR    = CONFIG_DIR / "snapshots"
RETAIN_DAYS = 60   # S80: was 14, but _prune had been inert for months (the
                   # timestamp slice bug below), so nothing was ever deleted and
                   # 14 was never a number anyone had actually lived with. Fixing
                   # the mechanism at 14 would have destroyed 46 snapshots — 46
                   # days of config rollback — in one unattended run. Raised to 60
                   # so retention starts working WITHOUT a bulk delete; it now
                   # trims one day at a time. Lowering it is a deliberate, separate
                   # decision to make once the mechanism has been seen working.

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
    captured = []
    missing = []
    for name in FILES:
        src = CONFIG_DIR / name
        if src.exists():
            shutil.copy2(src, dest / name)
            n += 1
            captured.append(name)
        else:
            missing.append(name)
    (dest / "_manifest.json").write_text(json.dumps(
        {"created": datetime.now().isoformat(timespec="seconds"),
         "files": n, "captured": captured, "missing": missing,
         "tag": tag}, indent=2))
    _prune()
    _log(f"took snapshot {stamp} ({n} files)")
    return dest


# The timestamp prefix of a snapshot directory name, e.g. 2026-08-26_213922.
# Matched, not sliced: the old code took d.name[:19] while the stamp is 17
# characters, so a TAGGED directory (2026-07-11_084746_daily -> the slice
# "2026-07-11_084746_d") raised in strptime, hit `except ValueError: continue`,
# and was skipped forever. All three callers pass a tag -- self_review passes
# `kind`, dev_agent passes "dev-loop", restore passes "pre-restore" -- so in
# practice every snapshot was immune and RETAIN_DAYS had never once applied.
# CIRRUS was holding 64, the oldest 46 days past a 14-day policy. (S80)
_STAMP_RX = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{6})")


def stamp_of(name: str):
    """Parse a snapshot directory name -> datetime, or None if it is not one.

    None is the honest answer for a directory that is not a snapshot; the
    caller skips it. That is different from the old failure, which skipped
    directories that WERE snapshots because the slice was the wrong length.
    """
    m = _STAMP_RX.match(name or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d_%H%M%S")
    except ValueError:
        return None


def prunable(snap_dir=None, now=None):
    """Snapshot dirs older than RETAIN_DAYS. Pure: reads, never deletes.

    Split out from _prune so the retention rule can be tested without a real
    directory and inspected before anything is removed -- this policy had been
    silently inert for months, and its first working run deletes in bulk.
    """
    snap_dir = Path(snap_dir) if snap_dir else SNAP_DIR
    if not snap_dir.exists():
        return []
    cutoff = (now or datetime.now()) - timedelta(days=RETAIN_DAYS)
    out = []
    for d in sorted(snap_dir.iterdir()):
        if not d.is_dir():
            continue
        when = stamp_of(d.name)
        if when is not None and when < cutoff:
            out.append(d)
    return out


def _prune(snap_dir=None):
    """Delete what prunable() names. One rule, in one place."""
    for d in prunable(snap_dir):
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


def selftest() -> int:
    """Offline unit tests: python3 config_snapshot.py selftest

    The retention rule had been inert for months and nobody knew, because this
    module had no tests and dev_agent could therefore only ever py_compile it.
    Everything below runs against a tempfile directory -- never CONFIG_DIR,
    never the real snapshots (T32).
    """
    import tempfile
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))
        print("  [%s] %s" % ("OK " if cond else "FAIL", name))

    # Ages are derived FROM RETAIN_DAYS, never written as literals: a fixture
    # pinned to a fixed date stops testing the rule the moment the constant
    # moves past it, and reports ALL PASS while checking nothing (S80 raised
    # RETAIN_DAYS 14 -> 60, which is exactly the move that would have done it).
    NOW = datetime(2026, 8, 26, 21, 0, 0)
    OLD = (NOW - timedelta(days=RETAIN_DAYS + 10)).strftime("%Y-%m-%d_%H%M%S")
    NEW = (NOW - timedelta(days=1)).strftime("%Y-%m-%d_%H%M%S")

    WANT = datetime.strptime(NEW, "%Y-%m-%d_%H%M%S")
    ck("stamp_of parses an UNTAGGED name", stamp_of(NEW) == WANT)
    # The whole bug: the stamp is 17 chars and the old code sliced 19, so any
    # tag made strptime raise and the directory was skipped forever.
    ck("stamp_of parses a TAGGED name (the S80 bug)", stamp_of(NEW + "_daily") == WANT)
    ck("stamp_of parses a tag containing digits", stamp_of(NEW + "_run2") == WANT)
    ck("stamp_of rejects a non-snapshot directory name", stamp_of("notes") is None)
    ck("stamp_of rejects an impossible date", stamp_of("2026-13-45_999999") is None)
    ck("stamp_of rejects an empty name", stamp_of("") is None)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for n in (OLD, OLD + "_daily", OLD + "_dev-loop",
                  NEW, NEW + "_daily", "not-a-snapshot"):
            (root / n).mkdir()
        (root / "loose-file.txt").write_text("x")

        names = sorted(d.name for d in prunable(root, now=NOW))
        ck("prunable takes the old UNTAGGED snapshot", OLD in names)
        ck("prunable takes the old TAGGED snapshots — the ones that never died",
           OLD + "_daily" in names and OLD + "_dev-loop" in names)
        ck("prunable spares a snapshot inside the retention window",
           NEW not in names and NEW + "_daily" not in names)
        # Guard the fixture itself: if OLD ever drifts inside the window the
        # suite would pass while testing nothing (T42).
        ck("  ...and the OLD fixture really is outside it",
           stamp_of(OLD) < NOW - timedelta(days=RETAIN_DAYS))
        ck("prunable ignores a directory that is not a snapshot",
           "not-a-snapshot" not in names)
        ck("prunable ignores a loose file", "loose-file.txt" not in names)
        ck("prunable is READ-ONLY — nothing is gone until _prune runs",
           (root / OLD).exists() and len(list(root.iterdir())) == 7)

        _prune(root)
        left = sorted(d.name for d in root.iterdir())
        ck("_prune removed exactly what prunable named",
           left == [NEW, NEW + "_daily", "loose-file.txt", "not-a-snapshot"])

    with tempfile.TemporaryDirectory() as td:
        empty = Path(td) / "nope"
        ck("prunable on a missing directory is empty, not an error",
           prunable(empty, now=NOW) == [])
        _prune(empty)
        ck("_prune on a missing directory does not raise", True)

    print()
    bad = [n for n, ok in checks if not ok]
    if bad:
        print("FAILURES: %d" % len(bad))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    if cmd == "selftest":
        sys.exit(selftest())
    if cmd == "snapshot":
        take_snapshot(sys.argv[2] if len(sys.argv) > 2 else "")
    elif cmd == "list":
        for name, files in list_snapshots():
            print(f"{name}  [{', '.join(files)}]")
    elif cmd == "restore" and len(sys.argv) > 2:
        restore(sys.argv[2])
    else:
        print("usage: config_snapshot.py snapshot|list|restore <name>")
