#!/usr/bin/env python3
"""trap_silent_truncate.py — S79. Mechanical half of T40 in docs/TOOLING-TRAPS.md.

A length limit that cuts data and says nothing about it is a check that reports
cleanly while being wrong. `gmail_inbox_peek.py` clipped a client email at 3000
characters mid-sentence, exit 0, no marker — and the discarded tail held the
largest requirement in the message.

Flags a `return`/`yield` of a slice whose bound is a limit-ish NAME (limit, max,
head, chars, cap, ...) when the enclosing FILE never mentions truncation. A
literal bound like `[:3]` is left alone: those are usually "top three", not a
data cut, and flagging them would drown the signal.

Emits:  T40:<path>:<line>:<message>
READ-ONLY. Always exits 0; the caller counts lines.
"""
import re
import sys
from pathlib import Path

# `return <expr>[:NAME]` / `yield <expr>[:NAME]` — capture both halves.
SLICE = re.compile(
    r"\b(?:return|yield)\b\s*(.*?)"
    r"\[\s*:\s*([A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)?)\s*\]")
LIMITISH = re.compile(r"limit|max|head|chars|trunc|size|width|bytes", re.I)

# Only STRING truncation loses information invisibly. `results[:max_results]`
# and `scored[:limit]` are top-N SELECTION — the caller asked for N and got N,
# nothing is hidden. The first cut of this lint flagged five of those and three
# real ones; an 8-finding lint where 5 are noise gets muted, so it must tell
# the two apart. Signal: the sliced expression is text, or the product of a
# string operation. `.replace(` is deliberately absent — slug builders use it.
TEXTISH = re.compile(
    r"\b(?:text|body|content|html|plain|article|summary|snippet|prompt|"
    r"message|msg|excerpt|transcript|raw)\b|re\.sub\(|\.join\(|\.strip\(\)",
    re.I)

# If the file already announces the cut, the author has handled it.
ANNOUNCED = re.compile(r"TRUNCAT|truncat|\bELIDED\b|elided|…|"
                       r"chars total|more not shown", re.M)

# An explicit, reviewed waiver. A deliberate cut that a human has looked at
# says so on the line or the line above:  # T40-OK: <why>
# Without this the lint goes permanently red on intentional limits, and a
# permanently-red check is one nobody reads (S79 fixed exactly that in
# business_idea_scan's suite the same morning this trap was written).
WAIVER = re.compile(r"#\s*T40-OK\b")


def scan(path: Path):
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return
    if ANNOUNCED.search(text):
        return
    lines = text.splitlines()
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        m = SLICE.search(line)
        if not m:
            continue
        expr, bound = m.group(1), m.group(2)
        if not LIMITISH.search(bound) or not TEXTISH.search(expr):
            continue
        if WAIVER.search(line) or (i >= 2 and WAIVER.search(lines[i - 2])):
            continue
        yield i, stripped[:70]


def selftest():
    """S79: verified by planting a file when written, which proved it once and
    then left nothing behind. These pin the three behaviours that matter."""
    failures = []

    def check(label, ok):
        print(("  PASS  " if ok else "  FAIL  ") + label)
        if not ok:
            failures.append(label)

    def scan_text(text):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "m.py"
            f.write_text(text)
            return list(scan(f))

    check("a string truncation with a limit-ish bound is FLAGGED",
          len(scan_text("def f(body, limit):\n    return body[:limit]\n")) == 1)
    check("top-N list selection is NOT flagged (it hides nothing)",
          scan_text("def f(items, limit):\n    return items[:limit]\n") == [])
    check("a literal bound is not flagged",
          scan_text("def f(x):\n    return x[:3]\n") == [])
    check("an inline # T40-OK waiver is honoured",
          scan_text("def f(body, limit):\n"
                    "    return body[:limit]  # T40-OK: reviewed\n") == [])
    check("a waiver on the LINE ABOVE is honoured",
          scan_text("def f(body, limit):\n    # T40-OK: reviewed\n"
                    "    return body[:limit]\n") == [])
    check("a file that already announces truncation is left alone",
          scan_text("def f(body, limit):\n"
                    "    # appends TRUNCATED when it cuts\n"
                    "    return body[:limit]\n") == [])
    check("a commented-out line is not flagged",
          scan_text("def f(body, limit):\n    # return body[:limit]\n"
                    "    return body\n") == [])
    print()
    if failures:
        print("FAILURES: %d" % len(failures))
        return 1
    print("ALL PASS")
    return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        raise SystemExit(selftest())
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    # Scan what actually RUNS. cirrus-repo/ is the deployed source; runner/ is
    # the runner. `cirrus/bot/` is a STALE MIRROR of cirrus-repo — 14 of its 20
    # files diverged, last touched 2026-08-04 — so linting it would report
    # findings that no box would ever execute, and fixing them would edit the
    # wrong copy. Its one genuinely-deployed file is named explicitly.
    files = []
    # S85: these checkers now LIVE in cirrus-repo so they deploy to CIRRUS,
    # where dev_findings runs them and PROJECT_DIR *is* the repo. Given the
    # Cowork root they still look inside cirrus-repo/; given the repo root
    # they scan it directly. Without this the collector on CIRRUS would look
    # for cirrus-repo/cirrus-repo/ and report zero findings forever -- a dead
    # check is worse than none, because it reads as "nothing is wrong".
    bases = ([root / "runner", root / "cirrus-repo"]
             if (root / "cirrus-repo").is_dir() else [root])
    for base in bases:
        if base.is_dir():
            files += sorted(base.rglob("*.py"))
    deployed_from_bot = root / "cirrus/bot/gmail_inbox_peek.py"
    if deployed_from_bot.is_file():
        files.append(deployed_from_bot)
    for f in files:
        if f.name.startswith("trap_"):
            continue              # this file describes the pattern it hunts
        if ".venv" in f.parts or "site-packages" in f.parts:
            continue
        for lineno, snippet in scan(f):
            rel = f.relative_to(root)
            print(f"T40:{rel}:{lineno}:truncates without a marker — "
                  f"a caller cannot tell a cut from a short value: {snippet}")


if __name__ == "__main__":
    main()
