#!/usr/bin/env python3
"""trap_ignored_send.py -- S292. Mechanical half of T113 in docs/TOOLING-TRAPS.md.

A sender that never raises reports failure only by RETURNING it ("FAILED: ...").
Called as a bare statement, that return is dropped, and the code after it
carries on as if the message went out. immaculate_tick.py did exactly this:
a failed contest alert still marked the week found and stopped the checks.

A "status sender" is any function that returns a string starting "FAILED"
(directly, or via a variable assigned one). Flags a statement that calls one
and drops the result -- by its own name, as `module.name(...)`, or through a
local alias (`tell = ... send_telegram`). Callers that assign, test, append,
or print the result are not flagged.

Emits:  T113:<path>:<line>:<message>
READ-ONLY. Always exits 0; the caller counts lines.
"""
import ast
import sys
from pathlib import Path

SKIP = {".venv", "venv", "node_modules", "__pycache__", ".git"}


def _failed_str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.startswith("FAILED")
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        return isinstance(first, ast.Constant) and str(first.value).startswith("FAILED")
    return False


def senders_in(tree):
    """Names of functions that report failure by returning a FAILED string."""
    out = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        failed_vars = {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign)
                       and _failed_str(n.value) for t in n.targets if isinstance(t, ast.Name)}
        for n in ast.walk(fn):
            if isinstance(n, ast.Return) and n.value is not None and (
                    _failed_str(n.value)
                    or (isinstance(n.value, ast.Name) and n.value.id in failed_vars)):
                out.add(fn.name)
                break
    return out


def _refs(expr, names):
    """Does `expr` name a sender WITHOUT calling it (an alias, not a result)?"""
    called = {id(n.func) for n in ast.walk(expr) if isinstance(n, ast.Call)}
    return any(id(n) not in called and (
        (isinstance(n, ast.Name) and n.id in names)
        or (isinstance(n, ast.Attribute) and n.attr in names)) for n in ast.walk(expr))


def scan(tree, senders):
    aliases = set(senders)
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            aliases |= {a.asname for a in n.names if a.asname and a.name in senders}
        elif isinstance(n, ast.Assign) and _refs(n.value, senders):
            aliases |= {t.id for t in n.targets if isinstance(t, ast.Name)}
    for n in ast.walk(tree):
        if not isinstance(n, ast.Expr):
            continue
        call = n.value.value if isinstance(n.value, ast.Await) else n.value
        if not isinstance(call, ast.Call):
            continue
        f = call.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
        if (isinstance(f, ast.Name) and name in aliases) or (
                isinstance(f, ast.Attribute) and name in senders):
            yield n.lineno, name


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    files, trees = [], {}
    for sub in ("cirrus-repo", "runner"):
        base = root / sub
        if base.is_dir():
            files += [f for f in sorted(base.rglob("*.py")) if not SKIP & set(f.parts)]
    for f in files:
        try:
            trees[f] = ast.parse(f.read_text(errors="replace"))
        except (SyntaxError, ValueError):
            pass
    senders = set().union(*(senders_in(t) for t in trees.values())) if trees else set()
    for f, tree in trees.items():
        for lineno, name in scan(tree, senders):
            print(f"T113:{f.relative_to(root)}:{lineno}:`{name}(...)` as a bare statement -- "
                  f"it never raises and reports failure only by RETURNING \"FAILED: ...\", "
                  f"so this drops the failure and the next line runs as if it was sent. "
                  f"Check the result (== \"sent\") before recording anything as done.")


if __name__ == "__main__":
    main()
