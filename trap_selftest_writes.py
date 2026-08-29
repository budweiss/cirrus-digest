#!/usr/bin/env python3
"""T32 — a selftest that writes to a REAL file (S78, 2026-08-25).

Buddy: *"I am thinking we have done this many times ... is there any way to
prevent this from occurring?"* Yes, and this is it.

A selftest exists to prove the code works. The moment it writes to a live path
it can also BREAK the thing it is testing around, and it does so in the worst
possible way: silently, at the moment everyone is most convinced the system is
healthy, because a test suite just passed.

The instance that prompted this: `mailer.selftest()` needed an intake allowlist
to resolve a client from a recipient, so its first draft wrote the real
`config/intake_senders.json` and restored it in a `finally`. That file gates
which clients may reach intake at all, is deliberately absent from git, and is
hand-maintained on the box. A crash between write and restore would have left
intake admitting the wrong senders — or none — with no copy to restore from.

**The codebase already has the right pattern.** `entity_kb`, `task_solver` and
`client_watch` all take a `db_path=` / path argument precisely so their tests
point somewhere harmless. The failure is not ignorance, it is that nothing
noticed when a new test skipped it. So: a check, not a reminder.

THE RULE: inside a selftest (or any test_* function), every write or delete
must target a path derived from `tempfile`. Anything else is flagged. Pass a
path in as a parameter, or build one under `TemporaryDirectory()`.

Prints `path:line:reason` per hit. READ-ONLY.
"""
import ast
import sys
from pathlib import Path

# Calls that create, modify or destroy something on disk.
WRITE_METHODS = {"write_text", "write_bytes", "unlink", "mkdir", "touch",
                 "rename", "rmdir", "chmod", "symlink_to"}
WRITE_FUNCS = {"remove", "unlink", "rmtree", "rename", "makedirs",
               "mkdir", "copy", "copy2", "copyfile", "move", "chmod"}
# `replace` is handled separately, by ARITY. Path.replace(target) and
# os.replace(src, dst) move a file; str.replace(old, new) is everywhere and
# means nothing here. Flagging both made the check fire on
# `.replace("credentials.json", "")` in a perfectly safe test -- and a lint that
# cries wolf on the honest code gets muted, which is worse than no lint at all.
# One positional argument is a Path move; two is almost always a string.
ONE_ARG_WRITE = {"replace"}
TEMP_MARKERS = ("tempfile", "TemporaryDirectory", "NamedTemporaryFile",
                "mkdtemp", "mkstemp", "gettempdir")


def _is_test_func(node) -> bool:
    n = node.name
    return n == "selftest" or n.startswith("test_") or n.endswith("_selftest")


def _safe_names(fn, seeds=()) -> set:
    """Variables in this function that hold a tempfile-derived path.

    Deliberately generous: it follows one hop (td = TemporaryDirectory(); p =
    Path(td) / "x") because that is how every honest test in this tree is
    written. Being generous here is the right trade -- a false NEGATIVE costs a
    missed lint, a false POSITIVE costs trust in the whole check, and a lint
    nobody trusts gets muted.
    """
    names, changed = set(seeds), True
    while changed:
        changed = False
        for node in ast.walk(fn):
            if not isinstance(node, (ast.Assign, ast.withitem)):
                continue
            if isinstance(node, ast.withitem):
                targets = [node.optional_vars] if node.optional_vars else []
                value = node.context_expr
            else:
                targets, value = node.targets, node.value
            src = ast.dump(value) if value is not None else ""
            tainted = (any(m in src for m in TEMP_MARKERS)
                       or any(f"id='{n}'" in src or f"attr='{n}'" in src
                              for n in names))
            if not tainted:
                continue
            for t in targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Name) and sub.id not in names:
                        names.add(sub.id)
                        changed = True
    return names


def _target_src(call) -> str:
    if isinstance(call.func, ast.Attribute):
        return ast.dump(call.func.value)
    return ast.dump(call)


def check_file(path: Path) -> list:
    try:
        tree = ast.parse(path.read_text())
    except Exception:
        return []
    hits = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_test_func(fn):
            continue
        # A path handed in as a PARAMETER is the caller's problem, not a live
        # write -- `db_path=` injection is exactly the pattern we want people
        # using, so it seeds the safe set and propagates through assignments
        # (p = Path(db_path)) the same way a tempfile-derived name does.
        # Without that propagation this check flagged the very habit it exists
        # to encourage, which would have made it worse than nothing.
        safe = _safe_names(fn, {a.arg for a in fn.args.args + fn.args.kwonlyargs})
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            name = (node.func.attr if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", ""))
            is_write = False
            if isinstance(node.func, ast.Attribute) and name in WRITE_METHODS:
                is_write = True
            elif isinstance(node.func, ast.Attribute) and name in WRITE_FUNCS:
                is_write = True
            elif (name in ONE_ARG_WRITE and isinstance(node.func, ast.Attribute)
                  and len(node.args) == 1 and not node.keywords):
                is_write = True
            elif name == "open" and len(node.args) > 1:
                mode = node.args[1]
                if isinstance(mode, ast.Constant) and isinstance(mode.value, str) \
                        and any(c in mode.value for c in "wax+"):
                    is_write = True
            if not is_write:
                continue
            src = _target_src(node) + ast.dump(node)
            if any(m in src for m in TEMP_MARKERS):
                continue
            if any(f"id='{n}'" in src for n in safe):
                continue
            hits.append((node.lineno,
                         f"{name}() inside {fn.name}() writes a path that is not "
                         f"tempfile-derived and was not passed in"))
    return hits


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else Path.home() / "Documents/Cowork")
    for py in sorted(root.rglob("*.py")):
        p = str(py)
        if any(skip in p for skip in ("/.git/", "__pycache__", "/venv/",
                                      "/.venv/", "site-packages",
                                      "trap_selftest_writes.py")):
            continue
        for line, why in check_file(py):
            print(f"{py.relative_to(root)}:{line}:{why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
