#!/usr/bin/env python3
"""remote_verify.py — verify a patch WHERE IT WILL RUN (S97, 2026-09-03).

Buddy, 2026-09-03: "let's build the CUMULUS builder agent."

Investigating first turned up that the builder bridge already exists. S91 wired
Skywarden's `file_repair_ticket` to `dev_agent.promote_tickets`, so a repair the
CUMULUS supervisor diagnoses becomes an ordinary Tier-1 build item on CIRRUS. It
fired on 2026-09-01 for the modelhealth defect. The architecture was never the
gap.

THE ACTUAL GAP: dev_agent builds and verifies on CIRRUS, which is a Mac. Every
gate it runs -- compile, selftest, dependents, dry-run -- proves the patch works
on macOS. A fix for a CUMULUS job touches systemd, journald, Linux paths and
Linux `ps`, and none of that is exercised. The builder could ship a patch to the
box serving Bill, Alyssa and Justin having never once run it there.

That is this session's own theme pointed at the builder: a check that passes
because it could not see. `ps -o etimes` is the standing proof -- Linux-only,
and macOS answers it by printing the other columns and exiting 0.

HOW IT AVOIDS BECOMING NOISE

The hard part is not running a test on another box. It is telling
    "this patch breaks on Linux"                        <- must fail the build
apart from
    "this module was never meant to run on Linux"       <- must not
without a hand-maintained list of which files belong to which box (T44: a name
list standing in for a reachability question, which is how gate 4 got it wrong).

So it MEASURES A BASELINE. Each candidate's selftest runs on CUMULUS against a
pristine export of HEAD first:

    fails at HEAD  -> EXCUSED. Not runnable there; nothing to say about it.
    passes at HEAD -> now apply the patch and re-run. A failure is real.

Exactly the `prebroken` discipline gate 3 already uses for dependents, and the
same shape as every fix that has held today: establish what the answer is before
the change, then measure the change.

SAFETY. It never touches CUMULUS's live checkout -- that box runs client jobs.
`git archive HEAD` exports a throwaway tree into mktemp and the patched files
are copied over that. Tracked files only, so config/credentials.json (gitignored)
cannot ride along. The scratch path is validated before any rm -rf.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# The link dev_agent already uses to read CUMULUS's ticket queue (S91).
CUMULUS_HOST = os.environ.get("CUMULUS_HOST", "buddy@192.168.0.204")
CUMULUS_REPO = "~/cirrus-digest"

SSH_BASE = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
SETUP_TIMEOUT = 120
RUN_TIMEOUT = 300
COPY_TIMEOUT = 120

# A scratch dir we are willing to delete. Anything else is a bug, and rm -rf is
# not the place to find out.
SCRATCH_RX = re.compile(r"^/tmp/rv-[A-Za-z0-9]{6,}$")


# ── the half that runs ON CUMULUS ────────────────────────────────────────────

def selftest_argvs(path):
    """Every spelling this module answers to, or [] if it has no suite.

    Mirrors dev_agent.selftest_argvs deliberately. A module that DEFINES a
    selftest but dispatches to it from nothing is not verifiable: running it
    bare would execute its production path.
    """
    try:
        src = Path(path).read_text(errors="ignore")
    except OSError:
        return []
    if not re.search(r"^\s*def\s+_?selftest\s*\(", src, re.M):
        return []
    args = []
    if '"--selftest"' in src or "'--selftest'" in src:
        args.append(["--selftest"])
    if re.search(r"""["']selftest["']""", src):
        args.append(["selftest"])
    return args


def run_selftests(root, rels):
    """Run each module's selftest inside `root`. -> {rel: {rc, tail}}.

    Runs on CUMULUS, invoked as `remote_verify.py --run-selftests`. A module
    with no invokable suite is reported rc=None -- absent, never "passed",
    because a missing test reading as clean is the whole family of bug this
    session has been chasing (T8).
    """
    root = Path(root)
    out = {}
    for rel in rels:
        fp = root / rel
        if not fp.exists():
            out[rel] = {"rc": None, "tail": "not present in this tree"}
            continue
        argvs = selftest_argvs(fp)
        if not argvs:
            out[rel] = {"rc": None, "tail": "no invokable selftest"}
            continue
        rc, tail = 0, ""
        for a in argvs:
            try:
                r = subprocess.run([sys.executable, str(fp)] + a, cwd=str(root),
                                   capture_output=True, text=True,
                                   timeout=RUN_TIMEOUT)
                rc = r.returncode
                tail = ((r.stdout or "") + (r.stderr or ""))[-1200:]
            except subprocess.TimeoutExpired:
                rc, tail = 124, f"timed out after {RUN_TIMEOUT}s"
            except Exception as e:  # noqa: BLE001
                rc, tail = 125, f"{type(e).__name__}: {e}"
            if rc != 0:
                break
        out[rel] = {"rc": rc, "tail": tail}
    return out


# ── the half that runs ON CIRRUS ─────────────────────────────────────────────

def _ssh(args, timeout=SETUP_TIMEOUT):
    try:
        r = subprocess.run(SSH_BASE + [CUMULUS_HOST] + args,
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or ""), (r.stderr or "")
    except Exception as e:  # noqa: BLE001
        return 255, "", f"{type(e).__name__}: {e}"


def _make_scratch():
    """Export CUMULUS's HEAD into a throwaway tree. -> (path, err).

    git archive is written to a FILE and untarred separately, never piped: a
    pipeline exits with tar's status, so a failed export would yield an empty
    tree that then reports every module as unrunnable (T64, found this morning).
    """
    script = (
        f"T=$(mktemp -d /tmp/rv-XXXXXXXX) || exit 9; "
        f"cd {CUMULUS_REPO} || {{ rm -rf \"$T\"; exit 9; }}; "
        f"git archive --format=tar HEAD > \"$T/x.tar\" || {{ rm -rf \"$T\"; exit 9; }}; "
        f"tar -xf \"$T/x.tar\" -C \"$T\" || {{ rm -rf \"$T\"; exit 9; }}; "
        f"rm -f \"$T/x.tar\"; echo \"$T\"")
    rc, out, err = _ssh([script])
    if rc != 0:
        return None, f"could not export HEAD on CUMULUS (rc={rc}) {err.strip()[:200]}"
    path = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if not SCRATCH_RX.match(path):
        return None, f"refusing an unexpected scratch path: {path!r}"
    return path, ""


def _cleanup(scratch):
    # Validated again at the point of deletion, not merely when it was created.
    if scratch and SCRATCH_RX.match(scratch):
        _ssh([f"rm -rf {scratch}"])


def _remote_run(scratch, rels):
    rc, out, err = _ssh(
        [f"cd {scratch} && python3 remote_verify.py --run-selftests "
         + " ".join(rels)], timeout=RUN_TIMEOUT + 60)
    if rc != 0:
        return None, f"remote selftest runner failed (rc={rc}) {err.strip()[:200]}"
    try:
        return json.loads(out), ""
    except Exception:
        return None, f"unparseable runner output: {out.strip()[:200]}"


def _copy_patched(worktree, rels, scratch):
    for rel in rels:
        src = Path(worktree) / rel
        if not src.exists():
            continue
        parent = str(Path(rel).parent)
        if parent not in (".", ""):
            rc, _, err = _ssh([f"mkdir -p {scratch}/{parent}"])
            if rc != 0:
                return f"mkdir {parent} failed: {err.strip()[:160]}"
        try:
            r = subprocess.run(
                ["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                 str(src), f"{CUMULUS_HOST}:{scratch}/{rel}"],
                capture_output=True, text=True, timeout=COPY_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            return f"scp {rel}: {type(e).__name__}: {e}"
        if r.returncode != 0:
            return f"scp {rel} failed: {(r.stderr or '').strip()[:160]}"
    return ""


def verify_on_cumulus(worktree, changed):
    """Does this patch still work on the box it will run on? -> result dict.

    {"ok", "checked", "excused", "skipped", "detail", "ran"}

    UNREACHABLE IS NOT A PASS, and it is not a failure either. If CUMULUS
    cannot be reached, this returns ok=True with ran="cumulus(unreachable)" --
    a cross-box gate that hard-fails on a network blip would be muted within a
    week, and the same call in dev_agent already treats a CUMULUS read as
    best-effort. But it says so, every time: a silent zero here would look
    exactly like "verified fine", which is the failure shape this file exists
    to close.
    """
    res = {"ok": True, "checked": [], "excused": [], "skipped": [],
           "detail": "", "ran": ""}
    cands = [p for p in changed if p.endswith(".py")
             and (Path(worktree) / p).exists()
             and selftest_argvs(Path(worktree) / p)]
    if not cands:
        res["ran"] = "cumulus(0 candidates)"
        return res

    scratch, err = _make_scratch()
    if not scratch:
        res["ran"] = "cumulus(unreachable)"
        res["detail"] = err
        return res

    try:
        # 1. BASELINE at HEAD. This is what separates "the patch breaks on
        #    Linux" from "this module never ran on Linux".
        before, err = _remote_run(scratch, cands)
        if before is None:
            res["ran"] = "cumulus(unreachable)"
            res["detail"] = err
            return res

        live = [r for r in cands if before.get(r, {}).get("rc") == 0]
        for r in cands:
            if r not in live:
                why = (before.get(r) or {}).get("tail", "")[:120]
                rc = (before.get(r) or {}).get("rc")
                res["excused"].append(f"{r} (rc={rc} at HEAD: {why})")
        if not live:
            res["ran"] = "cumulus(0 runnable, %d excused)" % len(res["excused"])
            return res

        # 2. Apply the patch over the export and re-run only what was green.
        cerr = _copy_patched(worktree, live, scratch)
        if cerr:
            res["ran"] = "cumulus(unreachable)"
            res["detail"] = cerr
            return res

        after, err = _remote_run(scratch, live)
        if after is None:
            res["ran"] = "cumulus(unreachable)"
            res["detail"] = err
            return res

        broke = [r for r in live if (after.get(r) or {}).get("rc") != 0]
        res["checked"] = live
        res["ran"] = "cumulus(%d checked, %d excused)" % (
            len(live), len(res["excused"]))
        if broke:
            res["ok"] = False
            first = broke[0]
            res["detail"] = (
                "%s passes its selftest on CIRRUS and FAILS on CUMULUS.\n"
                "It passed on CUMULUS at HEAD, so this patch is what broke it "
                "there — a macOS/Linux difference the CIRRUS gates cannot see.\n\n"
                "%s" % (first, (after.get(first) or {}).get("tail", "")[-1000:]))
        return res
    finally:
        _cleanup(scratch)


# ── selftest ─────────────────────────────────────────────────────────────────

def selftest():
    """Every case pins the direction that matters: an unverified patch must
    never render as a verified one."""
    import tempfile
    ok = fail = 0

    def ck(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  PASS {name}")
        else:
            fail += 1
            print(f"  FAIL {name}")

    # --- selftest_argvs -----------------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        (t / "both.py").write_text(
            'def selftest():\n    return 0\nif __name__ == "__main__":\n'
            '    import sys\n    if "--selftest" in sys.argv: sys.exit(selftest())\n')
        (t / "none.py").write_text("x = 1\n")
        (t / "defined_only.py").write_text("def selftest():\n    return 0\n")
        ck("a dispatching module reports its argv forms",
           selftest_argvs(t / "both.py") != [])
        ck("a module with no selftest reports none",
           selftest_argvs(t / "none.py") == [])
        ck("a selftest nothing can INVOKE is not verifiable",
           selftest_argvs(t / "defined_only.py") == [])

        # --- run_selftests --------------------------------------------------
        (t / "green.py").write_text(
            'import sys\ndef selftest():\n    print("ok")\n    return 0\n'
            'if __name__ == "__main__":\n'
            '    sys.exit(selftest() if "--selftest" in sys.argv else 0)\n')
        (t / "red.py").write_text(
            'import sys\ndef selftest():\n    print("boom")\n    return 1\n'
            'if __name__ == "__main__":\n'
            '    sys.exit(selftest() if "--selftest" in sys.argv else 0)\n')
        got = run_selftests(t, ["green.py", "red.py", "none.py", "missing.py"])
        ck("a passing suite reports rc=0", got["green.py"]["rc"] == 0)
        ck("a failing suite reports non-zero", got["red.py"]["rc"] == 1)
        ck("a module with no suite is rc=None, never 0",
           got["none.py"]["rc"] is None)
        ck("an absent file is rc=None, never 0", got["missing.py"]["rc"] is None)

    # --- the scratch-path guard, which gates an rm -rf ----------------------
    ck("a well-formed scratch path is accepted",
       bool(SCRATCH_RX.match("/tmp/rv-AbC12345")))
    for bad in ("/tmp", "/", "/home/buddy", "/tmp/rv-", "/tmp/rv-x y",
                "/tmp/../etc", ""):
        if SCRATCH_RX.match(bad):
            fail += 1
            print(f"  FAIL rm -rf guard rejects {bad!r}")
            break
    else:
        ok += 1
        print("  PASS rm -rf guard rejects every unexpected path")

    # --- verify_on_cumulus's decision logic, with the network faked ---------
    real_scratch, real_run, real_copy = _make_scratch, _remote_run, _copy_patched
    g = globals()
    try:
        with tempfile.TemporaryDirectory() as td:
            wt = Path(td)
            (wt / "m.py").write_text(
                'import sys\ndef selftest():\n    return 0\n'
                'if __name__ == "__main__":\n'
                '    sys.exit(selftest() if "--selftest" in sys.argv else 0)\n')

            # unreachable box: NOT a pass, NOT a failure, and always said aloud
            g["_make_scratch"] = lambda: (None, "ssh down")
            r = verify_on_cumulus(wt, ["m.py"])
            ck("an unreachable CUMULUS does not fail the build", r["ok"] is True)
            ck("...and never claims it verified anything",
               r["ran"] == "cumulus(unreachable)" and not r["checked"])

            g["_make_scratch"] = lambda: ("/tmp/rv-Test1234", "")
            g["_copy_patched"] = lambda w, rels, s: ""

            # green at HEAD, red after the patch -> a REAL Linux-only breakage
            seq = [({"m.py": {"rc": 0, "tail": "ok"}}, ""),
                   ({"m.py": {"rc": 1, "tail": "ModuleNotFoundError: fcntl"}}, "")]
            g["_remote_run"] = lambda s, rels: seq.pop(0)
            r = verify_on_cumulus(wt, ["m.py"])
            ck("green at HEAD then red patched FAILS the build", r["ok"] is False)
            ck("...and the detail names the box difference",
               "FAILS on CUMULUS" in r["detail"])

            # red at HEAD -> excused, because it never ran there to begin with
            g["_remote_run"] = lambda s, rels: (
                {"m.py": {"rc": 1, "tail": "no systemd here"}}, "")
            r = verify_on_cumulus(wt, ["m.py"])
            ck("a module already red at HEAD is EXCUSED, not blamed",
               r["ok"] is True and len(r["excused"]) == 1 and not r["checked"])

            # green both sides -> pass, and it says what it checked
            seq2 = [({"m.py": {"rc": 0, "tail": ""}}, ""),
                    ({"m.py": {"rc": 0, "tail": ""}}, "")]
            g["_remote_run"] = lambda s, rels: seq2.pop(0)
            r = verify_on_cumulus(wt, ["m.py"])
            ck("green both sides passes", r["ok"] is True and r["checked"] == ["m.py"])
            ck("...and reports the count actually checked",
               r["ran"].startswith("cumulus(1 checked"))

            # a file with no selftest is not a candidate at all
            (wt / "plain.py").write_text("x = 1\n")
            r = verify_on_cumulus(wt, ["plain.py"])
            ck("a module with no invokable suite is skipped, not 'verified'",
               r["ran"] == "cumulus(0 candidates)")

            # a copy failure must not read as verified
            g["_copy_patched"] = lambda w, rels, s: "scp exploded"
            g["_remote_run"] = lambda s, rels: ({"m.py": {"rc": 0, "tail": ""}}, "")
            r = verify_on_cumulus(wt, ["m.py"])
            ck("a failed copy reports unreachable, never verified",
               r["ok"] is True and r["ran"] == "cumulus(unreachable)"
               and not r["checked"])
    finally:
        g["_make_scratch"], g["_remote_run"], g["_copy_patched"] = (
            real_scratch, real_run, real_copy)

    print(f"\n  {ok} passed, {fail} failed")
    return 1 if fail else 0


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "--run-selftests":
        print(json.dumps(run_selftests(Path.cwd(), argv[1:])))
        return 0
    if argv and argv[0] in ("--selftest", "selftest"):
        return selftest()
    print(__doc__.splitlines()[0])
    print("\n  --selftest              this module's own suite"
          "\n  --run-selftests <f...>  run those modules' suites here (used on CUMULUS)"
          "\n  --check <worktree> <f...>  verify a patch on CUMULUS")
    if argv and argv[0] == "--check" and len(argv) >= 3:
        r = verify_on_cumulus(argv[1], argv[2:])
        print(json.dumps(r, indent=2))
        return 0 if r["ok"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
