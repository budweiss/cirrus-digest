#!/usr/bin/env python3
"""job_runner.py — run a long job on THIS box and record what happened (S83).

Why this exists
---------------
Long jobs were launched from the MacBook as a plain `ssh ... python job.py`,
which ties up the session for the whole run, and — with no nohup — kills the
job if the connection drops. On 2026-08-28 that happened: the ssh died, the
pipeline still exited 0, and the run was lost with no output. Twelve runner
commands already did it correctly with `setsid nohup`; nineteen did not. The
pattern was hand-copied per job, so which form a job got depended on who
wrote it.

Three things this fixes that `setsid nohup` alone does not:

1. AN EXIT CODE. Every existing `-status` command infers state from `pgrep`
   plus a log tail, so "no process running" means finished-fine, crashed, or
   never-started — indistinguishable without a human reading the tail. That is
   the same "could not check reads as healthy" shape this project keeps paying
   for. A job here writes a ledger entry with its real exit code.

2. UNBUFFERED OUTPUT. Python buffers stdout when it is not a tty, so a
   redirected log stays EMPTY for the whole run: progress and hang look
   identical while you are waiting. Children run under `-u`.

3. A NAMESPACE. The script name reaches an exec, so it is validated against an
   allowlist rather than trusted (T11).

Stdlib only. Never raises to the caller: a job runner that dies while starting
a job is worse than no job runner.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
JOBS_DIR = APP_DIR / "logs" / "jobs"
VENV_PY = APP_DIR / ".venv/bin/python"

# The namespace gate. A script not named here cannot be launched, so a bad or
# hostile `args.script` from the runner reaches nothing. Keep this to jobs that
# are genuinely long — short ones should stay synchronous, where their output
# is read immediately.
ALLOWED_SCRIPTS = {
    "halftime_catalogue.py",
    "halftime_routing.py",
    "halftime_dashboard.py",
    "cirrus_daily.py",
    "model_bench.py",
    "opportunity_scout.py",
    "alopecia_collect.py",
    "alopecia_brief.py",
    "hoa_daily_research.py",
    "entity_kb.py",
}

_ID_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Arguments are passed as a LIST to subprocess (never a shell string), so the
# shell is out of the picture entirely. This still bounds what can be sent.
_ARG_RX = re.compile(r"^[A-Za-z0-9 ._:,=/@+-]{0,200}$")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def meta_path(job_id: str, jobs_dir: Path = None) -> Path:
    return (jobs_dir or JOBS_DIR) / (job_id + ".json")


def log_path(job_id: str, jobs_dir: Path = None) -> Path:
    return (jobs_dir or JOBS_DIR) / (job_id + ".log")


def _write_meta(job_id: str, data: dict, jobs_dir: Path = None) -> None:
    d = jobs_dir or JOBS_DIR
    d.mkdir(parents=True, exist_ok=True)
    tmp = meta_path(job_id, d).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n")
    tmp.replace(meta_path(job_id, d))     # atomic: a reader never sees a half file


def read_meta(job_id: str, jobs_dir: Path = None) -> dict:
    try:
        return json.loads(meta_path(job_id, jobs_dir).read_text())
    except Exception:
        return {}


def validate(job_id: str, script: str, args: list) -> str:
    """-> "" if the request is runnable, else why not."""
    if not _ID_RX.match(job_id or ""):
        return "bad job id"
    if script not in ALLOWED_SCRIPTS:
        return "script %r is not in the allowlist" % script
    for a in args or []:
        if not _ARG_RX.match(str(a)):
            return "argument %r has characters that are not allowed" % a
    return ""


def run_job(job_id: str, script: str, args: list = None,
            jobs_dir: Path = None, app_dir: Path = None,
            python: str = None) -> int:
    """Run one job to completion, recording start and finish. Returns exit code."""
    args = [str(a) for a in (args or [])]
    d = jobs_dir or JOBS_DIR
    app = app_dir or APP_DIR
    why = validate(job_id, script, args)
    if why:
        _write_meta(job_id, {"id": job_id, "script": script, "args": args,
                             "state": "refused", "reason": why,
                             "started": _now(), "finished": _now(),
                             "exit_code": 2}, d)
        return 2

    py = python or (str(VENV_PY) if Path(VENV_PY).exists() else sys.executable)
    base = {"id": job_id, "script": script, "args": args,
            "node": os.uname().nodename, "python": py,
            "started": _now(), "state": "running",
            "exit_code": None, "finished": None, "pid": os.getpid()}
    _write_meta(job_id, base, d)

    lp = log_path(job_id, d)
    lp.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(lp, "w") as out:
            # -u: without it the log stays EMPTY until the job ends, and a
            # status check cannot tell progress from a hang.
            rc = subprocess.call([py, "-u", str(Path(app) / script)] + args,
                                 cwd=str(app), stdout=out,
                                 stderr=subprocess.STDOUT)
    except Exception as e:
        base.update(state="failed", exit_code=127, finished=_now(),
                    error=str(e)[:300])
        _write_meta(job_id, base, d)
        return 127

    base.update(state="done" if rc == 0 else "failed",
                exit_code=rc, finished=_now())
    _write_meta(job_id, base, d)
    return rc


def status(job_id: str = None, jobs_dir: Path = None, limit: int = 8) -> str:
    """Human-readable status. With no id, the most recent jobs."""
    d = jobs_dir or JOBS_DIR
    if not d.exists():
        return "no jobs have been run on this box yet"
    metas = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if job_id:
        metas = [p for p in metas if p.stem == job_id] or []
        if not metas:
            # a job we cannot find is NOT a job that succeeded
            return "no such job: %s" % job_id
    lines = []
    for p in metas[:limit]:
        try:
            m = json.loads(p.read_text())
        except Exception:
            lines.append("  %-28s UNREADABLE ledger entry" % p.stem)
            continue
        rc = m.get("exit_code")
        lines.append("  %-28s %-8s exit=%-5s %s -> %s  %s %s" % (
            m.get("id", p.stem), m.get("state", "?"),
            "-" if rc is None else rc,
            m.get("started", "?"), m.get("finished") or "(running)",
            m.get("script", "?"), " ".join(m.get("args") or [])))
        if m.get("reason"):
            lines.append("      refused: %s" % m["reason"])
    if job_id:
        lp = log_path(job_id, d)
        if lp.exists():
            tail = lp.read_text(errors="replace").splitlines()[-25:]
            lines.append("  -- last %d log line(s) --" % len(tail))
            lines += ["    " + t[:200] for t in tail]
        else:
            lines.append("  (no log file yet)")
    return "\n".join(lines) or "no jobs recorded"


def selftest() -> int:
    import tempfile
    bad = 0

    def ck(label, cond):
        nonlocal bad
        print("  %s  %s" % ("PASS" if cond else "FAIL", label))
        if not cond:
            bad += 1

    ck("a script outside the allowlist is refused",
       validate("j1", "rm_everything.py", []) != "")
    ck("an allowlisted script is accepted", validate("j1", "cirrus_daily.py", []) == "")
    ck("a bad job id is refused", validate("../etc", "cirrus_daily.py", []) != "")
    ck("an argument with a shell metacharacter is refused",
       validate("j1", "cirrus_daily.py", ["; rm -rf /"]) != "")
    ck("ordinary flags are allowed",
       validate("j1", "cirrus_daily.py", ["--dry-run", "--angles", "8"]) == "")

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        (tdp / "cirrus_daily.py").write_text(
            "import sys\nprint('hello')\nsys.exit(0)\n")
        rc = run_job("ok-job", "cirrus_daily.py", [], jobs_dir=tdp / "jobs",
                     app_dir=tdp, python=sys.executable)
        m = read_meta("ok-job", tdp / "jobs")
        ck("a successful job records exit 0", rc == 0 and m.get("exit_code") == 0)
        ck("...and is marked done", m.get("state") == "done")
        ck("...and its output is captured",
           "hello" in log_path("ok-job", tdp / "jobs").read_text())
        ck("...and it records which node ran it", bool(m.get("node")))

        # THE POINT OF THE LEDGER: a crash must not look like a success
        (tdp / "model_bench.py").write_text("import sys\nsys.exit(3)\n")
        rc = run_job("bad-job", "model_bench.py", [], jobs_dir=tdp / "jobs",
                     app_dir=tdp, python=sys.executable)
        m = read_meta("bad-job", tdp / "jobs")
        ck("a crashing job records its REAL exit code",
           rc == 3 and m.get("exit_code") == 3)
        ck("...and is marked failed, not done", m.get("state") == "failed")

        rc = run_job("nope", "not_allowed.py", [], jobs_dir=tdp / "jobs",
                     app_dir=tdp, python=sys.executable)
        ck("a refused job is RECORDED as refused, not silently skipped",
           rc == 2 and read_meta("nope", tdp / "jobs").get("state") == "refused")

        s = status(jobs_dir=tdp / "jobs")
        ck("status lists every job", "ok-job" in s and "bad-job" in s)
        ck("status shows the exit code, not just the state", "exit=3" in s)
        ck("asking about a job that does not exist says so, it does not pass",
           "no such job" in status("ghost-job", jobs_dir=tdp / "jobs"))
        ck("status of one job includes its log tail",
           "hello" in status("ok-job", jobs_dir=tdp / "jobs"))

    print("\n%s" % ("ALL PASS" if not bad else "%d FAILED" % bad))
    return 1 if bad else 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if "selftest" in a or "--selftest" in a:
        sys.exit(selftest())
    if a and a[0] == "status":
        print(status(a[1] if len(a) > 1 else None))
        sys.exit(0)
    if len(a) < 2:
        print("usage: job_runner.py <job_id> <script.py> [args...]"
              "   |   job_runner.py status [job_id]"
              "   |   job_runner.py selftest")
        sys.exit(2)
    sys.exit(run_job(a[0], a[1], a[2:]))
