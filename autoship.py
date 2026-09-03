#!/usr/bin/env python3
"""autoship.py — which builds may ship unattended, decided mechanically (S97).

Buddy, 2026-09-03: "I like to have this automated without me at night. We need
to make this happen."

The measured bottleneck: dev_agent logs `[waiting-on-buddy]` on 4 of the last 7
nights. 24 findings collected, `room: 1`, `find_buildable` returns 0. The
builder was never short of work or capability — every path terminates at a tap.

There is also an asymmetry worth naming. CLAUDE.md rule 3a gives the
INTERACTIVE agent standing approval to fix a bug and deploy it without asking.
dev_agent had no equivalent: it must wait even for a patch that adds test cases
and cannot change what the program does. This closes that gap, and only that
gap.

THE RULE, AND WHY IT IS SAFE

A build may ship unattended only if, for EVERY file it touches, the change is
provably confined to test code. Provably, not plausibly:

    parse before and after; delete every selftest/test function and the
    `if __name__ == "__main__":` dispatch block from both; compare what is
    left, as an AST.

If the remaining trees are identical, no production code path can have changed.
Not "looks like tests" -- a structural equality check on everything that is not
a test. Whitespace, comments and formatting are invisible to it, which is right;
a renamed variable in a live function is not, which is also right.

Deliberately conservative in three ways:
  * a new module-level import counts as a production change, even when only the
    selftest uses it. Import side effects are real.
  * anything unparseable is NOT auto-shippable.
  * a NEW file is not auto-shippable, however test-shaped. Adding a file adds
    something the tree did not have.

What still needs Buddy, unchanged:
  * anything touching a production code path
  * a council REJECT (council_hold) -- an auto-hold outranks any auto-ship
  * a build whose gates did not all run
  * external sends and client-facing behaviour, which never come near this path
"""

import ast
import sys

TEST_FN_PREFIXES = ("selftest", "_selftest", "test_", "_test")
TEST_FN_NAMES = {"selftest", "_selftest", "coverage_selftest"}


def _is_test_fn(node):
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    n = node.name
    return n in TEST_FN_NAMES or n.startswith(TEST_FN_PREFIXES)


def _is_main_guard(node):
    """`if __name__ == "__main__":` — the dispatch block, not program logic."""
    if not isinstance(node, ast.If):
        return False
    return any(isinstance(x, ast.Name) and x.id == "__name__"
               for x in ast.walk(node.test))


def production_ast(src):
    """The module with all test code removed, as a comparable string.

    Returns None when the source cannot be parsed -- unparseable is never
    auto-shippable, because a thing we could not read is a thing we did not
    check (the failure family this whole session has been closing).
    """
    try:
        tree = ast.parse(src)
    except Exception:
        return None
    # Test FUNCTIONS are stripped. The `if __name__ == "__main__":` guard is
    # NOT, and that distinction is load-bearing.
    #
    # It looks like dispatch, so the first version treated it as test code. But
    # cirrus_bot.py's main guard is what stops a second LIVE Telegram bot
    # starting: on 2026-08-24 an argument it ignored started one that
    # 409-Conflicted the real bot for four days. A build waiting right now
    # (prop-2026-08-29-562734) edits exactly that guard, and under the first
    # version it would have qualified as "test-only" and shipped itself
    # unattended tonight.
    #
    # The cost is real and accepted: adding a `--selftest` dispatch to a module
    # that lacks one no longer auto-ships. That is the right trade. Adding
    # assertions to an EXISTING selftest -- which is what the blind_check
    # findings generate, and the bulk of this queue -- still does.
    tree.body = [n for n in tree.body if not _is_test_fn(n)]
    for node in ast.walk(tree):
        # Strip docstrings so a reworded comment-as-docstring cannot look like
        # a production change. Behaviour is what matters here.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Module)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    try:
        return ast.dump(tree)
    except Exception:
        return None


def is_test_only_change(before_src, after_src):
    """-> (bool, reason). True only if no production code path can have moved."""
    if before_src is None:
        return False, "new file — adding a file adds more than tests"
    a = production_ast(before_src)
    b = production_ast(after_src)
    if a is None:
        return False, "the ORIGINAL does not parse — cannot establish a baseline"
    if b is None:
        return False, "the PATCHED file does not parse"
    if a != b:
        return False, "production code changed outside the test regions"
    if before_src == after_src:
        return False, "no change at all"
    return True, "only test code changed (production AST identical)"


def may_autoship(rec, file_pairs):
    """Decide for a whole build. -> (bool, reason).

    `file_pairs` is [(path, before_src_or_None, after_src)]. Every file must
    qualify: one production change anywhere makes the whole build Buddy's.
    """
    if not file_pairs:
        return False, "no files — nothing measured, so nothing auto-shipped"
    if (rec or {}).get("council_hold"):
        # An auto-hold outranks an auto-ship, always. The council rejected it.
        return False, "council REJECTED this build (held)"
    verdict = ((rec or {}).get("council") or {}).get("verdict")
    if verdict != "approve":
        return False, f"council verdict is {verdict!r}, not approve"
    ran = " ".join((rec or {}).get("ran") or [])
    if "selftest(" not in ran:
        return False, "the selftest gate did not run — unverified"
    for path, before, after in file_pairs:
        ok, why = is_test_only_change(before, after)
        if not ok:
            return False, f"{path}: {why}"
    return True, ("test-only change across %d file(s); production AST identical"
                  % len(file_pairs))


def selftest():
    ok = fail = 0

    def ck(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  PASS {name}")
        else:
            fail += 1
            print(f"  FAIL {name}")

    base = ('import os\n'
            'def verdict(n):\n'
            '    if n < 0:\n'
            '        return "BAD"\n'
            '    return "OK"\n'
            'def selftest():\n'
            '    assert verdict(1) == "OK"\n'
            '    return 0\n'
            'if __name__ == "__main__":\n'
            '    import sys\n'
            '    sys.exit(selftest())\n')

    # 1. adding assertions to the selftest — the case this exists to allow
    more_tests = base.replace(
        '    assert verdict(1) == "OK"\n',
        '    assert verdict(1) == "OK"\n    assert verdict(-1) == "BAD"\n')
    okc, why = is_test_only_change(base, more_tests)
    ck("adding selftest assertions is test-only", okc)

    # 2. a one-character change to LIVE logic — must never auto-ship
    prod = base.replace("if n < 0:", "if n <= 0:")
    okc, why = is_test_only_change(base, prod)
    ck("changing a production branch is NOT test-only", not okc)
    ck("...and says why", "production code changed" in why)

    # 3. production change disguised alongside a test change
    sneaky = more_tests.replace('return "OK"', 'return "OKAY"')
    ck("a production change bundled WITH test changes is caught",
       not is_test_only_change(base, sneaky)[0])

    # 4. formatting/comments/docstrings are not production changes
    cosmetic = base.replace('def verdict(n):\n',
                            'def verdict(n):\n    """Docstring added."""\n')
    ck("a docstring is not a production change",
       is_test_only_change(base, cosmetic)[0])

    # 5. a new import counts as production — import side effects are real
    imported = base.replace("import os\n", "import os\nimport json\n")
    ck("a new module-level import is NOT test-only",
       not is_test_only_change(base, imported)[0])

    # 6. THE ONE THAT MATTERS. The __main__ guard is not test code, however much
    #    it looks like dispatch. cirrus_bot.py's guard is what stops a second
    #    LIVE Telegram bot starting -- an argument it ignored on 2026-08-24
    #    started one that 409-Conflicted the real bot for four days. A build
    #    waiting right now edits exactly that guard.
    nodispatch = base.split("if __name__")[0]
    ck("adding a __main__ dispatch is NOT auto-shippable",
       not is_test_only_change(nodispatch, base)[0])
    guard_changed = base.replace(
        '    sys.exit(selftest())\n',
        '    if sys.argv[1:]:\n        sys.exit(2)\n    sys.exit(selftest())\n')
    ck("editing the __main__ guard is NOT auto-shippable",
       not is_test_only_change(base, guard_changed)[0])

    # 7. unparseable, either side, is never auto-shippable
    ck("an unparseable patch is not auto-shippable",
       not is_test_only_change(base, "def broken(:\n")[0])
    ck("an unparseable ORIGINAL is not auto-shippable",
       not is_test_only_change("def broken(:\n", base)[0])
    ck("a new file is not auto-shippable", not is_test_only_change(None, base)[0])
    ck("an identical file is not a change", not is_test_only_change(base, base)[0])

    # 8. build-level gating
    good = {"council": {"verdict": "approve"}, "ran": ["compile", "selftest(1/1)"]}
    ck("an approved test-only build may autoship",
       may_autoship(good, [("m.py", base, more_tests)])[0])
    ck("a council REJECT blocks autoship even for test-only",
       not may_autoship({**good, "council_hold": True},
                        [("m.py", base, more_tests)])[0])
    ck("a council verdict that is not approve blocks autoship",
       not may_autoship({"council": {"verdict": "revise"}, "ran": ["selftest(1/1)"]},
                        [("m.py", base, more_tests)])[0])
    ck("a build whose selftest gate never ran is not autoshipped",
       not may_autoship({"council": {"verdict": "approve"}, "ran": ["compile"]},
                        [("m.py", base, more_tests)])[0])
    ck("one production file among many blocks the WHOLE build",
       not may_autoship(good, [("a.py", base, more_tests),
                               ("b.py", base, prod)])[0])
    ck("no files means no autoship", not may_autoship(good, [])[0])

    print(f"\n  {ok} passed, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--selftest", "selftest"):
        raise SystemExit(selftest())
    print(__doc__.splitlines()[0])
