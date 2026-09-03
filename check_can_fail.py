#!/usr/bin/env python3
"""check_can_fail.py — prove a check can actually FAIL (S97, 2026-09-03).

Buddy, after a run of sessions that each spent 20-30% of their time repairing
the daily run: "if we plan better our implementation won't have so many issues."
Measured against the last two sessions, the defects were not planning failures.
Seven of nine were ONE failure mode:

    a check that reported green because it could not see.

    completeness.check()  read an empty dict for eleven weeks -> "all producing"
    heartbeat             asks `is-failed`; a hung oneshot is `activating`
    job-durations         prints Result=success for a unit hung RIGHT NOW
    ps -o etimes          rejected by macOS ps, which exits 0 and prints the
                          other columns -> "nothing stuck", forever
    gather_stalls         no output from a CRASHED checker -> "nothing stalled"
    selftest | tail       exit status of tail -> PASS for a red suite
    a test reading a LIVE file  -> passes vacuously until real data shows up

S96 named the discipline that catches all of these -- *make the check fail
first* -- and deliberately did NOT build a tool for it, on the grounds that a
bad version (one that GUESSES whether a test is meaningful) would be noise
inside a week. That objection is correct and this tool is shaped by it.

    This tool does not guess. It MEASURES.

It is mutation testing, scoped. For each target it damages the module in one
specific way, re-runs that module's own selftest, and requires the suite to go
RED. A mutation the suite does not notice is called a SURVIVOR, and a survivor
is not an opinion about test quality -- it is a demonstration that the suite
cannot distinguish the real code from a broken version of it. That is precisely
the property every check above lacked.

WHY IT DOES NOT BECOME NOISE (the S96 objection, answered concretely)

  * It reports what it MEASURED, never what it suspects. No heuristics about
    naming, coverage or "looks untested".
  * `--gate --since <rev>` enforces on CHANGED files only -- the ratchet. We do
    not have to fix the world to stop adding to it. (test_coverage_check.py
    already takes --since; this follows it deliberately.)
  * Plain mode is a report. A gate that fires on a 5-year-old survivor on day
    one gets muted in a week (T9).
  * It never mutates a module's own selftest. Damaging the test and watching the
    test fail proves nothing.
  * A mutant that HANGS is reported as INCONCLUSIVE, never as "detected". A
    timeout is not a passing grade (T8).

WHAT A SURVIVOR MEANS, IN ONE LINE
    "I replaced this branch condition with a constant, the whole suite still
     passed, so nothing you have written can tell the two apart."

SAFETY. Mutants are written into the real file and restored in a `finally`,
and the restoration is then VERIFIED BY HASH rather than assumed from the fact
that the write did not raise. A sidecar `.check_can_fail.bak` holds the original
for the duration, so a crash mid-run is recoverable without git (and therefore
for untracked files too); a backup still present at startup means the previous
run died and the file on disk may be a MUTANT, which is refused loudly.

An earlier draft refused to run on any file git called dirty. That sounded
prudent and defeated the whole tool: `--since` selects the file you JUST
EDITED, so the gate could never measure the change it exists to measure.
"""

import argparse
import ast
import hashlib
import os
import subprocess
import sys
from pathlib import Path

# The tree being probed. This file lives in cirrus-repo BECAUSE that is how code
# reaches CIRRUS and CUMULUS -- the checks that burned us worst (completeness,
# stall_check, morning_brief) only run on a box, so the prober has to get there
# too. One copy, deployed like everything else; a second copy in runner/ would
# be the run-command.sh.STALE-DUPLICATE hazard again.
#
# Hence --repo is explicit for on-box runs: this file's own location tells you
# nothing useful once it is deployed.
COWORK = Path(os.environ.get("COWORK", Path(__file__).resolve().parent.parent))

# Explicit rather than discovered, and grouped by WHERE the module runs. A
# target whose selftest cannot run in the given tree fails at the BASELINE step
# and is reported as unmeasurable -- honest, but useless noise on every run, so
# it is kept out of the profile instead.
#
# NOTE the argv form per target. job_status dispatches on the BARE word and
# exits 0 IN SILENCE on the flag form; the baseline guard in probe() refuses
# that rather than scoring it, but the profile should still be right.
PROFILES = {
    # Cowork-side tooling, probed on this Mac. Paths are relative to the Cowork
    # root, which is why these carry a cirrus-repo/ or runner/ prefix.
    "cowork": [
        ("cirrus-repo/job_status.py", ["selftest"]),
        ("cirrus-repo/api_backoff.py", ["--selftest"]),
        ("cirrus-repo/placement.py", ["--selftest"]),
        ("runner/wait_state_check.py", ["--selftest"]),
        ("runner/deploy_verify.py", ["--selftest"]),
        ("runner/test_coverage_check.py", ["selftest"]),
    ],
    # On CIRRUS. Paths are relative to the cirrus-digest checkout root.
    # morning_brief is here and not in "cowork" because it loads
    # config/sources.json AT IMPORT and cannot even be imported off-box.
    "cirrus": [
        ("stall_check.py", ["--selftest"]),
        ("morning_brief.py", ["--selftest"]),
        ("job_status.py", ["selftest"]),
    ],
    # On CUMULUS. completeness.check() is the eleven-week blind spot itself:
    # it read an empty dict from a path it could not traverse and reported
    # "all jobs producing" from S67 to S96.
    "cumulus": [
        ("supervisor/completeness.py", ["--selftest"]),
        ("supervisor/heartbeat.py", ["--selftest"]),
        ("cumulus_daily_brief.py", ["--selftest"]),
        ("job_status.py", ["selftest"]),
    ],
}
TARGETS = PROFILES["cowork"]

SELFTEST_NAMES = {"selftest", "_selftest", "main", "_main"}
RUN_TIMEOUT = 120


# ── mutation model ────────────────────────────────────────────────────────────

class _Mutation:
    """One surgical edit: replace a source span with a constant."""

    def __init__(self, kind, lineno, col, end_lineno, end_col, text, ctx):
        self.kind, self.text, self.ctx = kind, text, ctx
        self.lineno, self.col = lineno, col
        self.end_lineno, self.end_col = end_lineno, end_col

    def label(self):
        return f"L{self.lineno} {self.kind} -> {self.text}  (in {self.ctx})"


def _enclosing_selftest(tree):
    """Line ranges of the module's own test code, which must not be mutated."""
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in SELFTEST_NAMES or node.name.startswith("_test"):
                spans.append((node.lineno, getattr(node, "end_lineno", node.lineno)))
    return spans


def _in_spans(line, spans):
    return any(a <= line <= b for a, b in spans)


def _references_dunder_name(test):
    """Is this the module-dispatch guard (`__name__ == "__main__"`)?"""
    return any(isinstance(n, ast.Name) and n.id == "__name__"
               for n in ast.walk(test))


def plan_mutations(src, max_mutants=None):
    """Every mutation we can make to this source, as surgical span replacements.

    Two operators, both chosen because they model the real defect: a branch that
    reports a problem is never taken (`if False`), or is always taken (`if
    True`). A suite that cannot tell either from the original has never
    exercised the problem path.
    """
    tree = ast.parse(src)
    skip = _enclosing_selftest(tree)
    muts = []
    for node in ast.walk(tree):
        # `while True:` would hang; only ever weaken a loop guard.
        if isinstance(node, ast.While):
            variants = [("WHILE", "False")]
        elif isinstance(node, ast.If):
            variants = [("IF", "True"), ("IF", "False")]
        else:
            continue
        t = node.test
        if _in_spans(t.lineno, skip):
            continue
        # A test that is already a constant is its own equivalent mutant.
        if isinstance(t, ast.Constant):
            continue
        # NEVER mutate `if __name__ == "__main__":`. Setting it False stops the
        # suite from RUNNING AT ALL, and a suite that never ran exits 0 and
        # scores as a survivor -- the tool would report its loudest finding
        # against every well-tested file on earth. Caught by this file's own
        # selftest case 3, which is the whole argument for having one.
        if _references_dunder_name(t):
            continue
        ctx = _context_of(tree, t.lineno)
        for kind, text in variants:
            muts.append(_Mutation(kind, t.lineno, t.col_offset,
                                  t.end_lineno, t.end_col_offset, text, ctx))
    muts.sort(key=lambda m: (m.lineno, m.text))
    if max_mutants and len(muts) > max_mutants:
        # Sample ACROSS the file, not the first N by line. The first version
        # sliced [:max], so a capped run only ever probed the top of the module
        # -- imports and early helpers -- and reported "0/8 detected" for
        # stall_check.py, whose real checks live further down. A cap is for
        # bounding runtime; it must not quietly change WHICH code is measured
        # into an unrepresentative sample that reads as a damning score.
        step = len(muts) / float(max_mutants)
        muts = [muts[int(i * step)] for i in range(max_mutants)]
    return muts


def _context_of(tree, line):
    best = "<module>"
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.lineno <= line <= getattr(node, "end_lineno", node.lineno):
                best = node.name
    return best


def apply_mutation(src, m):
    """Replace the span with the constant, preserving every other byte."""
    lines = src.splitlines(keepends=True)
    starts, pos = [], 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln)
    a = starts[m.lineno - 1] + m.col
    b = starts[m.end_lineno - 1] + m.end_col
    return src[:a] + m.text + src[b:]


# ── running a target's own suite ──────────────────────────────────────────────

def run_suite(path, argv):
    """-> (state, detail, output). state in {'green','red','inconclusive'}."""
    try:
        r = subprocess.run([sys.executable, str(path)] + list(argv),
                           capture_output=True, text=True, timeout=RUN_TIMEOUT,
                           cwd=str(path.parent))
    except subprocess.TimeoutExpired:
        # A hang is NOT a detection. Saying otherwise would credit the suite for
        # a mutant it never judged (T8: missing output must not read as clean).
        return "inconclusive", f"timed out after {RUN_TIMEOUT}s", ""
    except Exception as e:
        return "inconclusive", f"{type(e).__name__}: {e}", ""
    out = (r.stdout or "") + (r.stderr or "")
    return ("green" if r.returncode == 0 else "red"), f"exit {r.returncode}", out


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()



def probe(rel, argv, max_mutants=None, verbose=False):
    """Mutate one module repeatedly; report which mutations its suite missed."""
    path = (COWORK / rel).resolve()
    res = {"target": rel, "survivors": [], "killed": 0,
           "inconclusive": [], "error": None, "total": 0}
    if not path.exists():
        res["error"] = "file not found"
        return res
    # Crash safety. The first version refused any file git reported as dirty,
    # which sounded prudent and defeated the entire point: `--since` selects the
    # file you JUST EDITED, so the gate could never measure the change it exists
    # to measure. The real protection was never git -- it is the byte-exact
    # restore plus the hash check below, which also covers untracked files that
    # git would have said nothing about.
    #
    # A sidecar backup makes a crash recoverable without git at all. A backup
    # left behind at startup means a previous run died mid-mutation, and that is
    # the one case worth refusing, because the file on disk is a MUTANT.
    bak = path.with_suffix(path.suffix + ".check_can_fail.bak")
    if bak.exists():
        res["error"] = (f"a previous run left {bak.name} — the file on disk may "
                        f"be a MUTANT. Restore it (mv {bak.name} {path.name}) "
                        f"before probing again.")
        return res

    original = path.read_text()
    before = _sha(path)

    state, detail, out = run_suite(path, argv)
    if state != "green":
        # Cannot measure detection against a suite that is not passing.
        res["error"] = f"baseline suite is not green ({state}, {detail})"
        return res
    if not out.strip():
        # THE TRAP THIS WHOLE FILE IS ABOUT, and it bit this file first.
        # `job_status.py --selftest` dispatches on `"selftest" in sys.argv`, so
        # the flag form matched nothing, fell off the end of __main__ and exited
        # 0 IN SILENCE. The probe scored that as a green baseline and then
        # reported 0/10 mutations detected -- a perfect score for a suite that
        # never ran. Exit 0 from a suite that produced no output is not a pass;
        # it is an argv mistake (T57). Refuse to measure against it.
        res["error"] = ("baseline exited 0 but printed NOTHING — the suite did "
                        "not run (check the argv form, e.g. `selftest` vs "
                        "`--selftest`)")
        return res

    try:
        muts = plan_mutations(original, max_mutants)
    except SyntaxError as e:
        res["error"] = f"cannot parse: {e}"
        return res
    res["total"] = len(muts)

    bak.write_text(original)
    try:
        for m in muts:
            path.write_text(apply_mutation(original, m))
            st, det, _ = run_suite(path, argv)
            if st == "red":
                res["killed"] += 1
            elif st == "green":
                res["survivors"].append(m.label())
            else:
                res["inconclusive"].append(f"{m.label()} [{det}]")
            if verbose:
                print(f"    {st:12} {m.label()}")
    finally:
        path.write_text(original)

    # Verify the restore rather than trust the write (the whole point of this
    # file is not trusting an operation because it did not raise).
    if _sha(path) != before:
        res["error"] = (f"RESTORE FAILED — the original is in {bak.name}; "
                        f"run: mv {bak.name} {path.name}")
        return res
    bak.unlink(missing_ok=True)
    return res


# ── reporting ─────────────────────────────────────────────────────────────────

def report(results, gate=False, summary=False):
    bad = 0
    print("== check-can-fail (mutation probe) ==\n")
    for r in results:
        if r["error"]:
            print(f"  ??  {r['target']}: {r['error']}")
            if "RESTORE FAILED" in (r["error"] or ""):
                bad += 1
            continue
        n_s, n_i = len(r["survivors"]), len(r["inconclusive"])
        mark = "ok " if not n_s else "GAP"
        print(f"  {mark} {r['target']:38} {r['killed']}/{r['total']} mutations "
              f"detected" + (f", {n_s} SURVIVED" if n_s else "")
              + (f", {n_i} inconclusive" if n_i else ""))
        if not summary:
            for s in r["survivors"]:
                print(f"        survivor: {s}")
            for s in r["inconclusive"]:
                print(f"        inconclusive: {s}")
        if n_s:
            bad += 1

    total_s = sum(len(r["survivors"]) for r in results if not r["error"])
    print(f"\n  {len(results)} target(s), {total_s} surviving mutation(s)")
    if total_s:
        print("\n  A SURVIVOR is not a style opinion. It means the suite passed")
        print("  against a deliberately broken version of the code, so nothing")
        print("  written can tell the two apart — which is exactly how a check")
        print("  ends up reporting green because it cannot see.")
    return 1 if (gate and bad) else 0


def _git_lines(args):
    try:
        r = subprocess.run(["git"] + args, capture_output=True, text=True,
                           cwd=str(COWORK))
        return {l.strip() for l in (r.stdout or "").splitlines() if l.strip()}
    except Exception:
        return set()


def changed_files(since):
    """Files touched since `since`, accepting a DATE or a REV, plus uncommitted.

    Mirrors test_coverage_check.changed_py deliberately, so PASS 6 and PASS 9
    of the wrap agree on what "this session" means: `git log --since` first
    (which is what makes `--since midnight` work), falling back to `<rev>..HEAD`
    when the string was a revision rather than a date.

    The uncommitted set is unioned in because the whole point of the ratchet is
    to measure the check you JUST edited -- waiting until it is committed would
    move the gate to after the moment it is useful.
    """
    changed = _git_lines(["log", "--since", since, "--name-only",
                          "--pretty=format:"])
    if not changed:
        changed = _git_lines(["diff", "--name-only", f"{since}..HEAD"])
    changed |= _git_lines(["diff", "--name-only", "HEAD"])       # uncommitted
    changed |= _git_lines(["diff", "--name-only", "--cached"])   # staged
    return changed


def changed_targets(since):
    changed = changed_files(since)
    return [(rel, argv) for rel, argv in TARGETS if rel in changed]


# ── selftest ──────────────────────────────────────────────────────────────────

def selftest():
    """Proves THIS tool can fail — the property it exists to demand of others.

    The load-bearing cases are 3 and 4: a suite that genuinely pins its module
    must come back clean (or the tool is noise), and a suite that pins nothing
    must come back with survivors (or the tool is decoration).
    """
    import tempfile
    ok = fail = 0

    def ck(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1; print(f"  PASS {name}")
        else:
            fail += 1; print(f"  FAIL {name}")

    # 1. the mutation planner
    src = ("def f(x):\n"
           "    if x > 3:\n"
           "        return 'big'\n"
           "    return 'small'\n")
    muts = plan_mutations(src)
    ck("an if-test yields both a True and a False mutant", len(muts) == 2)
    ck("mutants are located on the if line", all(m.lineno == 2 for m in muts))
    ck("the enclosing function is named", all(m.ctx == "f" for m in muts))

    mutated = apply_mutation(src, [m for m in muts if m.text == "False"][0])
    ck("applying a mutation replaces only the test",
       "if False:" in mutated and "return 'small'" in mutated
       and "return 'big'" in mutated)
    ck("the rest of the file is byte-identical",
       len(mutated) == len(src) - len("x > 3") + len("False"))

    # 2. a module's own selftest is never mutated
    src2 = ("def check(v):\n"
            "    if v:\n"
            "        return 1\n"
            "    return 0\n"
            "def selftest():\n"
            "    if check(1) != 1:\n"
            "        return 1\n"
            "    return 0\n")
    m2 = plan_mutations(src2)
    ck("mutations skip the module's own selftest",
       all(m.lineno < 5 for m in m2) and len(m2) == 2)

    # a cap must SAMPLE the file, not truncate to its first lines -- a capped
    # run that only ever probes the top reports an unrepresentative score as if
    # it were a verdict (it read "stall_check 0/8" on the first live run).
    wide = "def f(x):\n" + "".join(
        f"    if x == {i}:\n        return {i}\n" for i in range(1, 21))
    capped = plan_mutations(wide, max_mutants=4)
    ck("a cap returns exactly the requested number", len(capped) == 4)
    ck("a capped sample spans the whole file, not just the top",
       max(m.lineno for m in capped) > 20)

    # 3 & 4. end to end, on two real files that differ ONLY in test strength.
    body = ("import sys\n"
            "def verdict(n):\n"
            "    if n < 0:\n"
            "        return 'BAD'\n"
            "    return 'OK'\n")
    # NB the prints: the baseline guard rejects a suite that exits 0 in
    # silence, because that is an argv mistake rather than a pass. A fixture
    # without them would exercise the guard instead of the probe.
    strong = body + ("def selftest():\n"
                     "    print('checking both branches')\n"
                     "    assert verdict(-1) == 'BAD'\n"
                     "    assert verdict(1) == 'OK'\n"
                     "    return 0\n"
                     "if __name__ == '__main__':\n"
                     "    sys.exit(selftest())\n")
    # pins only the happy path -- the exact shape of every defect in the header
    blind = body + ("def selftest():\n"
                    "    print('checking the happy path')\n"
                    "    assert verdict(1) == 'OK'\n"
                    "    return 0\n"
                    "if __name__ == '__main__':\n"
                    "    sys.exit(selftest())\n")

    global COWORK
    real_cowork = COWORK
    try:
        with tempfile.TemporaryDirectory() as td:
            COWORK = Path(td)
            for name, text in (("strong.py", strong), ("blind.py", blind)):
                (Path(td) / name).write_text(text)
            r_strong = probe("strong.py", ["--selftest"])
            r_blind = probe("blind.py", ["--selftest"])

            ck("a suite that pins both branches leaves NO survivor",
               r_strong["error"] is None and not r_strong["survivors"])
            ck("...and it actually ran mutations", r_strong["total"] > 0)
            ck("a suite that pins only the happy path DOES leave a survivor",
               r_blind["error"] is None and len(r_blind["survivors"]) > 0)
            ck("the survivor names the branch it could not distinguish",
               any("verdict" in s for s in r_blind["survivors"]))

            # 5. restoration is verified, not assumed
            ck("the file is byte-identical after probing",
               (Path(td) / "blind.py").read_text() == blind)

            # 6. a suite that is already red cannot be measured, and says so
            (Path(td) / "red.py").write_text(
                body + ("def selftest():\n"
                        "    print('deliberately failing')\n"
                        "    return 1\n"
                        "if __name__ == '__main__':\n"
                        "    sys.exit(selftest())\n"))
            r_red = probe("red.py", ["--selftest"])
            ck("a red baseline is reported as unmeasurable, not as clean",
               r_red["error"] is not None and "baseline" in r_red["error"])

            # 7. a missing target is loud
            ck("a missing file is an error, not an empty pass",
               probe("nope.py", ["--selftest"])["error"] == "file not found")

            # 8. THE ONE THAT CAUGHT THIS TOOL'S OWN BUG. job_status.py
            #    dispatches on the bare word `selftest`, so `--selftest` matched
            #    nothing, fell off the end of __main__ and exited 0 silently.
            #    The probe scored that as a green baseline and reported
            #    "0/10 mutations detected" -- a perfect blindness score for a
            #    suite that had never run. Exit 0 with no output is an argv
            #    mistake, not a pass.
            (Path(td) / "silent.py").write_text(
                body + ("def selftest():\n"
                        "    print('ran')\n"
                        "    return 0\n"
                        "if __name__ == '__main__':\n"
                        "    if 'selftest' in sys.argv:\n"
                        "        sys.exit(selftest())\n"))
            r_wrong = probe("silent.py", ["--selftest"])   # wrong argv form
            r_right = probe("silent.py", ["selftest"])     # right argv form
            ck("a suite that exits 0 in SILENCE is refused, not scored",
               r_wrong["error"] is not None and "printed NOTHING" in r_wrong["error"])
            ck("...and it is never reported as 0-of-N detected",
               not r_wrong["survivors"] and r_wrong["total"] == 0)
            ck("the same file with the RIGHT argv form measures fine",
               r_right["error"] is None and r_right["total"] > 0)
    finally:
        COWORK = real_cowork

    print(f"\n  {ok} passed, {fail} failed")
    return 1 if fail else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--gate", action="store_true",
                    help="exit non-zero when a target has survivors")
    ap.add_argument("--since", metavar="REV",
                    help="only probe targets changed since REV (the ratchet)")
    ap.add_argument("--target", action="append",
                    help="probe only this path (repeatable)")
    ap.add_argument("--profile", default="cowork", choices=sorted(PROFILES),
                    help="which target set: cowork (this Mac), cirrus, cumulus")
    ap.add_argument("--repo", metavar="ROOT",
                    help="tree to probe (REQUIRED on a box — this file's own "
                         "location means nothing once deployed)")
    ap.add_argument("--max", type=int, default=None,
                    help="cap mutations per target")
    ap.add_argument("--verbose", action="store_true")
    # On-box runs page through `tail`, and a module with 100+ survivors pushes
    # the per-target summary out of the window entirely -- the first CIRRUS run
    # showed detail for one module and the totals for none.
    ap.add_argument("--summary", action="store_true",
                    help="per-target lines only, no per-survivor detail")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    global COWORK, TARGETS
    if a.repo:
        COWORK = Path(a.repo).resolve()
    TARGETS = PROFILES[a.profile]

    targets = TARGETS
    if a.since:
        # The ratchet only makes sense against a git checkout we are tracking
        # sessions in. On a box the tree is a throwaway export with no history,
        # so --since is meaningless there and the full profile is probed.
        targets = changed_targets(a.since)
        if not targets:
            print(f"== check-can-fail ==\n\n  no probed target changed since "
                  f"{a.since} — nothing to measure")
            return 0
    if a.target:
        want = set(a.target)
        targets = [t for t in TARGETS if t[0] in want]
        if not targets:
            print(f"== check-can-fail ==\n\n  no target in profile "
                  f"'{a.profile}' matched {sorted(want)} — nothing measured")
            return 1

    results = []
    for rel, argv in targets:
        if a.verbose:
            print(f"  probing {rel} ...")
        results.append(probe(rel, argv, max_mutants=a.max, verbose=a.verbose))
    return report(results, gate=a.gate, summary=a.summary)


if __name__ == "__main__":
    raise SystemExit(main())
