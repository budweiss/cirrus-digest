#!/usr/bin/env python3
"""trap_mac_timeout.py — S75. Mechanical half of T28 in docs/TOOLING-TRAPS.md.

There is no `timeout` binary on macOS (it is `gtimeout`, from coreutils). The
RUNNER HOST is a Mac and so is CIRRUS, so `timeout N ...` there fails with
"command not found" (exit 127) — the guard silently is not a guard.

It IS valid against CUMULUS, which is Linux. So this flags only the macOS-bound
uses: a bare local `timeout`, or one inside an `ssh cirrus` command. Lines
mentioning CUMULUS_HOST are left alone.

Emits:  T28:<path>:<line>:<message>
READ-ONLY. Always exits 0; the caller counts lines.
"""
import re
import sys
from pathlib import Path

# NOTE the `^\s*`: the first version anchored on bare `^` and therefore
# matched NOTHING in a real script, where every command is indented
# inside a case body. It passed clean on the very file that had the bug.
TIMEOUT = re.compile(r"(?:^\s*|[;&|]\s*|\$\(\s*)timeout\s+\d")
CUMULUS = re.compile(r"CUMULUS_HOST|cumulus1|buddy@cumulus")


def scan(path: Path):
    for i, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
        if not TIMEOUT.search(line):
            continue
        if CUMULUS.search(line):
            continue          # Linux box — timeout exists there
        yield i, line.strip()[:70]


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    base = root / "runner"
    if not base.is_dir():
        return
    for f in sorted(list(base.rglob("*.sh")) + [root / "session_wrap.sh"]):
        if not f.exists() or f.name == "trap_mac_timeout.py":
            continue
        for lineno, snippet in scan(f):
            print(f"T28:{f.relative_to(root)}:{lineno}:"
                  f"`timeout` on a macOS target — no such binary on the Mac "
                  f"(it is gtimeout); the command fails 127 and the guard does "
                  f"NOT apply. Use signal.alarm inside Python, or target "
                  f"CUMULUS. Line: {snippet}")


if __name__ == "__main__":
    main()
