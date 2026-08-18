"""Completeness checks for the CUMULUS supervisor (Skywarden) — S67.

WHY THIS EXISTS
---------------
Buddy, 2026-08-18: "why didn't Skywarden catch this? We need to make sure events
running on CUMULUS are monitored for COMPLETENESS and either corrected or called
to our attention."

On 2026-08-18 `cirrus-hoaleads.service` — Bill's live HOA research — ran, exited
0, and accomplished nothing:

    discovery: 61 candidates -> council_kept: 0 -> 0 new, 0 updated
    refresh:   5 attempted   -> refreshed: 5  -> found_new_info: 0

Skywarden reported "all clear". It was right by its own definition and useless
by ours, because `heartbeat.py` asks exactly two questions: is any unit
`systemctl is-failed`, and are credentials healthy. The service exited cleanly,
so the answer was no. **Skywarden was a LIVENESS monitor. A job that runs, exits
0, and produces nothing was invisible to it by design.**

The root cause of that particular zero was that Kent County migrated
co.kent.de.us -> kentcountyde.gov and the new domain 403s automated fetchers —
so the deep dive's authoritative source silently went away. Nothing in the stack
could tell "the source is gone" from "quiet day". That is the class of failure
this module exists to catch, not the one bug.

THE DESIGN, AND WHY IT IS NOT AN LLM CALL
-----------------------------------------
Deterministic and free, same as `heartbeat.py`: it reads the job_status ledger
every job already writes and applies an explicit per-job rule. It runs on the
60s heartbeat tick, so it cannot cost anything or hang. When it fires, the
EXISTING escalation path takes over — heartbeat returns ok=False, which triggers
the reasoning pass, which can investigate and either correct or notify. We are
adding the missing *signal*, not a second escalation mechanism.

ONE ZERO IS NOT A FAILURE. A research job legitimately has quiet days; alerting
on every one trains Buddy to ignore the alert, which is the same outcome as no
alert. So each rule carries a consecutive-zero threshold, and state persists
across ticks. The threshold is the whole point: `hoaleads` producing nothing for
three straight days is not a quiet week, it is a broken source.

ADDING A JOB: put it in RULES. A job with no rule is NOT silently ignored —
`unmonitored_jobs()` reports it, so the gap is visible rather than assumed.
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

APP_DIR = Path(os.environ.get("CUMULUS_APP_DIR", Path.home() / "cirrus-digest"))
STATUS_PATH = APP_DIR / "logs/jobs-status.json"
STATE_PATH = APP_DIR / "logs/completeness-state.json"


class Rule:
    """How to read 'did this job actually produce anything' from its status note.

    `produced_patterns` are regexes whose FIRST capture group is a count of
    something the job produced. The job is unproductive for a run when every
    pattern that matches yields 0 — a pattern that does not match at all is
    ignored, so a note-format change degrades to "can't tell", never to a false
    all-clear.
    """

    def __init__(self, name, produced_patterns, max_zero_runs, why):
        self.name = name
        self.produced_patterns = [re.compile(p, re.I) for p in produced_patterns]
        self.max_zero_runs = max_zero_runs
        self.why = why

    def productivity(self, note: str):
        """(produced_total, matched_any). matched_any=False means unreadable."""
        total, matched = 0, False
        for pat in self.produced_patterns:
            m = pat.search(note or "")
            if m:
                matched = True
                try:
                    total += int(m.group(1))
                except (ValueError, IndexError):
                    pass
        return total, matched


# Explicit per-job rules. Thresholds reflect how often each job SHOULD produce.
RULES = {
    # Bill's HOA research. Refreshes 5 properties/day on an alphabetical
    # rotation; a day where none of the 5 yields anything is plausible, three in
    # a row means the source is gone (which is exactly what happened when Kent
    # County moved domains and started 403-ing us).
    "hoaleads": Rule(
        "hoaleads",
        [r"(\d+)\s+new", r"(\d+)\s+updated", r"(\d+)\s+found_new_info",
         r"found_new_info[\"'\s:]+(\d+)"],
        max_zero_runs=3,
        why="Bill's HOA deep dive produced nothing — check whether the county "
            "source is reachable (Kent County moved to kentcountyde.gov and "
            "403s automated fetchers; New Castle/Sussex use ArcGIS layers).",
    ),
    # Business-idea pipeline (CIRRUS today, may move to CUMULUS). Generation is
    # adversarially filtered on purpose, so zero KEPT ideas is normal for a day
    # or two; a week of zero means the critique has drifted too harsh — the
    # exact failure S67 caught once already and calibrated against controls.
    "businessideaideate": Rule(
        "businessideaideate",
        [r"(\d+)\s+kept"],
        max_zero_runs=7,
        why="No ideas survived critique for a week — re-measure the critique "
            "against the recorded controls above _CRITIQUE_SYSTEM before "
            "assuming the pipeline is fine.",
    ),
    "businessideascan": Rule(
        "businessideascan",
        [r"(\d+)\s+new"],
        max_zero_runs=5,
        why="Intake admitted nothing for five runs — check the local prefilter "
            "is not over-rejecting and that the email/RSS sources still fetch.",
    ),
    # Vendor/account mail watcher (S67). Genuinely quiet most days — zero new
    # items is the NORMAL case, so this is only about the scan itself dying.
    # Its own ok=False already covers a failed scan, so the threshold is high
    # and this is a backstop, not the primary signal.
    "vendormail": Rule(
        "vendormail",
        [r"(\d+)\s+new", r"(\d+)\s+open"],
        max_zero_runs=30,
        why="Vendor-mail watcher has tracked nothing for a month — confirm the "
            "inboxes are still reachable rather than assuming a quiet month.",
    ),
}


def _load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def _save_state(state):
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass          # monitoring must never break the thing it monitors


def unmonitored_jobs(status=None):
    """Jobs in job_status with no completeness rule.

    Reported rather than ignored: a job nobody wrote a rule for is a monitoring
    gap, and the whole lesson of this module is that unmonitored looks exactly
    like healthy.
    """
    status = status if status is not None else _load(STATUS_PATH, {})
    return sorted(set(status) - set(RULES))


def check(status=None, state=None, now=None):
    """Deterministic completeness pass. Returns a dict; never raises.

    {"ok", "stalled": [...], "unreadable": [...], "unmonitored": [...], "detail"}
    ok=False means: a job has been running clean and producing nothing for
    longer than its rule allows -- escalate.
    """
    now = now or datetime.now()
    status = status if status is not None else _load(STATUS_PATH, {})
    state = state if state is not None else _load(STATE_PATH, {})

    stalled, unreadable = [], []

    for job, rule in RULES.items():
        entry = status.get(job)
        if not entry:
            continue                       # never run / not on this box
        # A job that reported failure is heartbeat's problem, not ours. Don't
        # double-report; and don't count a failed run as a "zero" run either.
        if not entry.get("ok", True):
            continue

        run_id = str(entry.get("epoch") or entry.get("last_run") or "")
        js = state.setdefault(job, {"zero_runs": 0, "last_run_id": ""})
        if run_id and run_id == js.get("last_run_id"):
            continue                       # already counted this run

        produced, matched = rule.productivity(entry.get("note", ""))
        if not matched:
            # Note format changed -- we genuinely cannot tell. Surface it as a
            # separate state rather than silently treating it as productive.
            unreadable.append(f"{job} (note not parseable: {entry.get('note','')[:60]!r})")
            js["zero_runs"] = 0
        elif produced > 0:
            js["zero_runs"] = 0
        else:
            js["zero_runs"] = js.get("zero_runs", 0) + 1
            if js["zero_runs"] >= rule.max_zero_runs:
                stalled.append({
                    "job": job,
                    "zero_runs": js["zero_runs"],
                    "threshold": rule.max_zero_runs,
                    "last_note": entry.get("note", ""),
                    "why": rule.why,
                })
        js["last_run_id"] = run_id
        js["checked_at"] = now.isoformat(timespec="seconds")

    if state is not None:
        _save_state(state)

    unmon = unmonitored_jobs(status)
    parts = []
    for s in stalled:
        parts.append(f"{s['job']}: ran clean but produced nothing "
                     f"{s['zero_runs']}x (threshold {s['threshold']}) — {s['why']}")
    if unreadable:
        parts.append("unreadable status notes: " + ", ".join(unreadable))
    if unmon:
        parts.append("no completeness rule for: " + ", ".join(unmon))

    return {
        # `unmonitored` alone does NOT flip ok -- it is a to-do for us, not an
        # incident. `unreadable` DOES, because a note we cannot parse is a
        # check that has silently stopped working.
        "ok": not stalled and not unreadable,
        "stalled": stalled,
        "unreadable": unreadable,
        "unmonitored": unmon,
        "detail": "; ".join(parts) if parts else "all jobs producing",
    }


# ── selftest ────────────────────────────────────────────────────────────────
def selftest() -> bool:
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))

    # The exact hoaleads note shape from the 2026-08-18 run that Skywarden
    # called "all clear".
    real = "0 new, 0 updated, found_new_info 0"
    st = {"hoaleads": {"ok": True, "epoch": 1, "note": real}}
    r1 = check(st, {})
    ck("one zero day does NOT alert", r1["ok"] is True)

    # Same job, three consecutive distinct runs -> must fire.
    state = {}
    for epoch in (1, 2, 3):
        r = check({"hoaleads": {"ok": True, "epoch": epoch, "note": real}}, state)
    ck("three zero runs DO alert", r["ok"] is False)
    ck("alert names the job", any(s["job"] == "hoaleads" for s in r["stalled"]))
    ck("alert carries actionable why", "kentcountyde.gov" in r["stalled"][0]["why"])

    # Productivity resets the counter.
    state = {}
    for epoch in (1, 2):
        check({"hoaleads": {"ok": True, "epoch": epoch, "note": real}}, state)
    r = check({"hoaleads": {"ok": True, "epoch": 3, "note": "2 new, 1 updated"}}, state)
    ck("a productive run resets the streak", r["ok"] is True)
    ck("streak actually zeroed", state["hoaleads"]["zero_runs"] == 0)

    # The same run seen twice on consecutive 60s ticks must count once.
    state = {}
    for _ in range(5):
        r = check({"hoaleads": {"ok": True, "epoch": 99, "note": real}}, state)
    ck("re-reading one run does not inflate the streak",
       state["hoaleads"]["zero_runs"] == 1 and r["ok"] is True)

    # A FAILED run is heartbeat's job -- don't double-report or count it.
    state = {}
    r = check({"hoaleads": {"ok": False, "epoch": 1, "note": "crashed"}}, state)
    ck("failed runs are left to heartbeat", r["ok"] is True and not r["stalled"])

    # An unparseable note must NOT read as healthy.
    state = {}
    r = check({"hoaleads": {"ok": True, "epoch": 1, "note": "finished"}}, state)
    ck("unreadable note flips ok=False", r["ok"] is False)
    ck("unreadable is reported separately from stalled",
       r["unreadable"] and not r["stalled"])

    # A job with no rule is surfaced, not silently ignored -- but is not an
    # incident on its own.
    r = check({"somenewjob": {"ok": True, "epoch": 1, "note": "done"}}, {})
    ck("unmonitored job is reported", "somenewjob" in r["unmonitored"])
    ck("unmonitored alone does not flip ok", r["ok"] is True)

    bad = 0
    for name, ok in checks:
        print(("  ok   " if ok else "  FAIL ") + name)
        bad += 0 if ok else 1
    print()
    print("all completeness selftests passed" if not bad else f"{bad} FAILED")
    return bad == 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(0 if selftest() else 1)
    print(json.dumps(check(), indent=2))
