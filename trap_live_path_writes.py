#!/usr/bin/env python3
"""T80 — a selftest reaches a live file THROUGH the code it is testing (S141).

T32's lint (`trap_selftest_writes.py`) inspects writes that appear *lexically
inside* a test function. It says so in its own docstring, and that is exactly
the hole this closes: the write can be one call deeper, in the production code
the test is exercising, and then no amount of tempfile discipline in the test
itself helps.

The instance: `task_solver.selftest()` did everything right — `tempfile.mkstemp`
for the DB, `db_path=` injected into every call. But the function under test
also called `_record_question_attempt()`, which appended to the module-level
`_ASK_LEDGER = Path(__file__).parent / "logs" / "kb_question_attempts.jsonl"`.
That constant took no parameter, so the injection could not reach it. Every
selftest run since 2026-08-24 wrote 8 synthetic client questions into the live
ledger — 80 rows on the Mac, 8 on CIRRUS, 120 on CUMULUS.

The cost was not the rows. `stall_check` reads that ledger to decide whether
Bill has ever asked the KB anything, and reported
`STALL outcomes[hoa_leads_bill] — matching is broken` every morning against a
KB with zero events. Matching was fine. Nobody had asked. **A detector fed
fabricated input is worse than no detector**: it is the one that gets ignored.

THE RULE this checks: in a module that has a selftest, a module-level path
constant built from `Path(__file__).parent` must not be written by a function
that offers no way to point it somewhere else. Give the function a path
parameter (or derive the path from one it already takes, the way task_solver
now derives the ledger from `db_path`).

Deliberately narrow, because a lint that cries wolf gets muted (T9). The first
draft flagged 3 modules and only 1 was real; the two false positives
(`client_promises._append`, `newdev/plus_pull.main`) are what set these gates:

  * only modules that actually define a selftest — elsewhere there is no test
    to leak through;
  * only module-level constants anchored at `Path(__file__)` — a path read from
    config or handed in by a caller is already injectable;
  * only writes (`open(..., 'a'/'w')`, `write_text`, `unlink`, …);
  * not flagged when the writing function itself takes a `*path*`/`*dir*`/
    `*file*` parameter — that is the fix, already applied;
  * **and it must be REACHABLE from an injected path**: the writer has to be
    called by a function that does take a path parameter, at a call site that
    parameter does not already guard. `client_promises.open_promise(path=...)`
    returns inside `if path is not None:` long before it reaches `_append`, so
    a test passing a path never gets there — measured, not assumed: running its
    selftest on CIRRUS left the live ledger byte-identical at 1301 bytes.
    `plus_pull.main()` is not called from anywhere injectable at all.

Prints `path:line:reason` per hit. READ-ONLY.
"""
import ast
import sys
from pathlib import Path

WRITE_METHODS = {"write_text", "write_bytes", "unlink", "touch", "mkdir",
                 "rename", "replace", "rmdir", "chmod"}
WRITE_FUNCS = {"remove", "unlink", "rmtree", "rename", "makedirs", "copy",
               "copy2", "copyfile", "move"}
PATH_PARAM_HINTS = ("path", "dir", "file", "dest", "target", "out", "ledger")


def _module_level_file_consts(tree) -> dict:
    """UPPER_CASE = <expr mentioning Path(__file__)> at module level -> lineno."""
    consts = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or node.value is None:
            continue
        src = ast.dump(node.value)
        if "'__file__'" not in src and '"__file__"' not in src:
            continue
        for t in node.targets:
            if isinstance(t, ast.Name):
                consts[t.id] = node.lineno
    return consts


def _path_params(fn) -> list:
    args = fn.args
    names = [a.arg for a in list(args.args) + list(args.kwonlyargs)
             + list(getattr(args, "posonlyargs", []))]
    return [n for n in names if any(h in n.lower() for h in PATH_PARAM_HINTS)]


def _has_injectable_param(fn) -> bool:
    return bool(_path_params(fn))


def _guard_lines(fn, params) -> list:
    """Line numbers after which a path param has already been handled.

    The shape that matters: `if path is not None: <write to path>; return` —
    everything below it only runs when NO path was injected, so a test that
    passes one can never reach it. Returns the end line of each such block.
    """
    ends = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        test = ast.dump(node.test)
        if not any(f"id='{p}'" in test for p in params):
            continue
        if any(isinstance(x, (ast.Return, ast.Raise))
               for x in ast.walk(node)):
            ends.append(max(getattr(x, "lineno", node.lineno)
                            for x in ast.walk(node)))
    return ends


def _reachable_from_injection(tree, writer_name) -> bool:
    """Is `writer_name` called from a path-taking function, unguarded?"""
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = _path_params(fn)
        if not params:
            continue
        guards = _guard_lines(fn, params)
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            called = (node.func.attr if isinstance(node.func, ast.Attribute)
                      else getattr(node.func, "id", ""))
            if called != writer_name:
                continue
            if any(node.lineno > g for g in guards):
                continue      # only runs when nothing was injected
            return True
    return False


def check_file(path: Path) -> list:
    try:
        tree = ast.parse(path.read_text())
    except Exception:
        return []
    fns = [n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if not any(f.name == "selftest" or f.name.endswith("_selftest")
               for f in fns):
        return []
    consts = _module_level_file_consts(tree)
    if not consts:
        return []

    hits = []
    for fn in fns:
        if fn.name == "selftest" or fn.name.endswith("_selftest"):
            continue          # T32's lint already owns writes inside the test
        if _has_injectable_param(fn):
            continue          # the caller can already redirect it
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
            elif name == "open" and len(node.args) > 1:
                mode = node.args[1]
                if isinstance(mode, ast.Constant) and isinstance(mode.value, str) \
                        and any(c in mode.value for c in "wax+"):
                    is_write = True
            if not is_write:
                continue
            src = ast.dump(node)
            for const, _ in consts.items():
                if f"id='{const}'" not in src:
                    continue
                if not _reachable_from_injection(tree, fn.name):
                    break     # nothing injectable can ever reach this write
                hits.append((node.lineno,
                             f"{fn.name}() writes the module-level live path "
                             f"{const} and takes no path parameter, but is "
                             f"reached from a function that DOES take one — a "
                             f"selftest injecting a path still writes the live "
                             f"file (T80)"))
                break
    return hits


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1
                else Path.home() / "Documents/Cowork")
    for py in sorted(root.rglob("*.py")):
        p = str(py)
        if any(skip in p for skip in ("/.git/", "__pycache__", "/venv/",
                                      "/.venv/", "site-packages",
                                      "trap_live_path_writes.py")):
            continue
        for line, why in check_file(py):
            print(f"{py.relative_to(root)}:{line}:{why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
