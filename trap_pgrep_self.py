#!/usr/bin/env python3
"""trap_pgrep_self.py — S75. Mechanical half of T27 in docs/TOOLING-TRAPS.md.

`pgrep -f X` and `pkill -f X` match against the FULL command line of every
process -- including the shell running the command that contains X. In S75:

  * `pkill -f 'hoa_property_location.py'` killed its own remote shell. The
    command produced EMPTY output and the target survived.
  * a `pgrep -f '<name> --live'` "already running?" guard matched the launcher
    itself and reported ALREADY RUNNING forever.

The `[h]ostname` bracket trick is NOT a general fix: it only works when the
bracketed pattern is the ONLY copy of the name on that command line. Flags a
line when the pattern's bare name ALSO appears somewhere else in the same
command -- which is precisely when the bracket stops helping.

Emits:  T27:<path>:<line>:<message>
READ-ONLY. Always exits 0; the caller counts lines.
"""
import re
import sys
from pathlib import Path

CALL = re.compile(r"\b(pgrep|pkill)\s+-[a-zA-Z]*f[a-zA-Z]*\s+(['\"])(.+?)\2")


def bare(pat: str) -> str:
    """Strip the [x] bracket trick to get the name actually being matched."""
    return re.sub(r"\[(.)\]", r"\1", pat).strip()


def scan(path: Path):
    for i, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
        for m in CALL.finditer(line):
            tool, pat = m.group(1), m.group(3)
            name = bare(pat)
            # the distinctive token of the pattern (longest word-ish chunk)
            toks = [t for t in re.split(r"[\s'\"]+", name) if len(t) > 6]
            if not toks:
                continue
            token = max(toks, key=len)
            # An explicit self-pid exclusion is a REAL fix, not a suppression:
            # `pgrep -f X | grep -vw "$$"` drops the invoking shell, which is
            # the only process the pattern spuriously matches. Accept it, so
            # the check does not keep flagging code that already handles this
            # (T9 — a lint that cries wolf gets switched off).
            if re.search(r'grep\s+-[a-zA-Z]*v[a-zA-Z]*\s+"?\$\$"?', line):
                continue
            # does the SAME line carry a second, unbracketed copy of it?
            rest = line[:m.start()] + line[m.end():]
            if token in rest:
                yield (i, tool, token)


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    for sub in ("runner",):
        base = root / sub
        if not base.is_dir():
            continue
        for f in sorted(list(base.rglob("*.sh")) + list(base.rglob("*.py"))):
            if f.name == "trap_pgrep_self.py":
                continue
            for lineno, tool, token in scan(f):
                print(f"T27:{f.relative_to(root)}:{lineno}:"
                      f"`{tool} -f` whose pattern '{token}' ALSO appears literally "
                      f"elsewhere on the same command line — it will match this "
                      f"invocation itself ({tool} kills its own shell / pgrep "
                      f"reports ALREADY RUNNING forever). The [x] bracket trick "
                      f"does not help here. Track the job with a PID file.")


if __name__ == "__main__":
    main()
