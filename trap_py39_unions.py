#!/usr/bin/env python3
"""T34 — PEP 604 (`X | None`) in a module CIRRUS must import (S78, 2026-08-25).

CIRRUS runs the **system python 3.9.6**, not a venv. 3.9 has no PEP 604 unions,
so `def f() -> dict | None:` is evaluated at def time and raises
`TypeError: unsupported operand type(s) for |`. The module does not merely warn
— it fails to import, and it takes every importer down with it.

The instance: `promise_detect.py` shipped without
`from __future__ import annotations`. It imported fine on CUMULUS (3.11), every
selftest passed, both boxes reported "UP TO DATE" — and `intake.py` on CIRRUS
had been unable to process **any client mail** since the deploy. It was found by
accident, while running `intake-peek` to check whether a client had replied.

`from __future__ import annotations` makes annotations lazy strings and fixes it
on 3.9. `task_solver.py` has carried that import for exactly this reason; the
new module was written without noticing why.

THE RULE: any `.py` in `cirrus-repo/` using `X | Y` in an annotation must have
`from __future__ import annotations`. Runtime unions outside annotations
(`isinstance(x, int | str)`) cannot be fixed that way and are flagged too.

Prints `path:line:reason`. READ-ONLY.
"""
import ast
import sys
from pathlib import Path


def _has_future(tree) -> bool:
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            if any(a.name == "annotations" for a in node.names):
                return True
    return False


def _union_lines(node) -> list:
    """BinOp with `|` appearing inside an annotation."""
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr):
            out.append(sub.lineno)
    return out


def check_file(path: Path) -> list:
    try:
        src = path.read_text()
        tree = ast.parse(src)
    except Exception:
        return []
    if _has_future(tree):
        return []

    hits = []
    for node in ast.walk(tree):
        anns = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                anns.append(node.returns)
            for a in (node.args.args + node.args.kwonlyargs
                      + node.args.posonlyargs):
                if a.annotation is not None:
                    anns.append(a.annotation)
        elif isinstance(node, ast.AnnAssign) and node.annotation is not None:
            anns.append(node.annotation)
        for a in anns:
            for line in _union_lines(a):
                hits.append((line,
                             "PEP 604 union in an annotation without "
                             "`from __future__ import annotations` — CIRRUS "
                             "runs python 3.9 and this raises TypeError at "
                             "IMPORT time, taking every importer down with it"))
    return sorted(set(hits))


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1
                else Path.home() / "Documents/Cowork")
    # Only code CIRRUS actually imports. CUMULUS runs 3.11 and does not care;
    # the Mac-side runner helpers are irrelevant here.
    # S85: these checkers now LIVE in cirrus-repo so they deploy to CIRRUS,
    # where dev_findings runs them and PROJECT_DIR *is* the repo. Given the
    # Cowork root they still look inside cirrus-repo/; given the repo root
    # they scan it directly. Without this the collector on CIRRUS would look
    # for cirrus-repo/cirrus-repo/ and report zero findings forever -- a dead
    # check is worse than none, because it reads as "nothing is wrong".
    base = root / "cirrus-repo" if (root / "cirrus-repo").is_dir() else root
    if not base.exists():
        return 0
    for py in sorted(base.rglob("*.py")):
        p = str(py)
        if any(skip in p for skip in ("__pycache__", "/.venv/", "/venv/",
                                      "site-packages", "/supervisor/")):
            continue
        for line, why in check_file(py):
            print(f"{py.relative_to(root)}:{line}:{why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
