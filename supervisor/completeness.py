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

    def __init__(self, name, produced_patterns, max_zero_runs, why,
                 zero_phrases=(), produced_phrases=()):
        self.name = name
        self.produced_patterns = [re.compile(p, re.I) for p in produced_patterns]
        # S81, the mirror image of zero_phrases and found the same way -- by
        # running this against the LIVE CUMULUS ledger instead of a fixture.
        # billsnow writes "sent material update", billnewdev writes "sent":
        # both mean the job did its whole job, and neither carries a digit, so
        # every count regex missed and all three were reported "unreadable"
        # FOREVER. unreadable flips ok=False, so Skywarden's heartbeat had been
        # permanently unhealthy on three jobs that were working perfectly --
        # a check that fires on correct behaviour is one you teach yourself to
        # ignore, which costs more than having no check.
        self.produced_phrases = tuple(pp.lower() for pp in produced_phrases)
        # Literal phrases that MEAN zero without carrying a number. Jobs write
        # prose, not just counters -- hoaleads' real note is "no genuine leads",
        # which no count regex can match. Without this the check reports
        # "unreadable" forever and the alert never fires. (Found on the very
        # first live run against CUMULUS's ledger, S67.)
        self.zero_phrases = tuple(z.lower() for z in zero_phrases)
        self.max_zero_runs = max_zero_runs
        self.why = why

    def productivity(self, note: str):
        """(produced_total, matched_any). matched_any=False means unreadable."""
        n = (note or "").lower()
        # zero_phrases win over produced_phrases: "no material change" and
        # "nothing to send" are the explicit statements of a quiet run, and a
        # note can contain both ("nothing to send, so nothing sent").
        for phrase in self.zero_phrases:
            if phrase in n:
                return 0, True
        total, matched = 0, False
        for phrase in self.produced_phrases:
            if phrase in n:
                total, matched = total + 1, True
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
        [r"(\d+)\s+new", r"(\d+)\s+updated", r"(\d+)\s+lead",
         r"found_new_info[\"'\s:]+(\d+)"],
        zero_phrases=("no genuine leads", "no new leads", "no leads"),
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
    # Bill's other two client jobs — weekly, and they SEND him email, so a
    # stalled one is directly client-visible. Weekly cadence means the
    # threshold is in runs, not days: 2 runs = two weeks of silence.
    "billsnow": Rule(
        "billsnow", [r"(\d+)\s+sent"], max_zero_runs=2,
        zero_phrases=("no material change", "nothing to send", "no change"),
        produced_phrases=("sent",),          # S81: the real note is "sent material update"
        why="Bill's snow outlook has sent nothing for two weeks — confirm the "
            "weather research still returns data before assuming a quiet season.",
    ),
    "billnewdev": Rule(
        "billnewdev", [r"(\d+)\s+new", r"(\d+)\s+lead"], max_zero_runs=2,
        zero_phrases=("no new leads", "nothing new"),
        produced_phrases=("sent",),          # S81: the real note is just "sent"
        why="Bill's new-dev lead check found nothing for two weeks — confirm "
            "the DE PLUS/parcel sources still respond.",
    ),
    # S81: the real note is "sent: 2 art, 0 pod, 0 topic" -- none of the old
    # patterns matched it, so Alyssa's digest read as unreadable every day.
    # Counting the PIECES rather than the send keeps a genuinely empty digest
    # ("sent: 0 art, 0 pod, 0 topic") reading as zero, which is the case the
    # threshold exists for.
    "pedagogy": Rule(
        "pedagogy", [r"(\d+)\s+art", r"(\d+)\s+pod", r"(\d+)\s+topic",
                     r"(\d+)\s+sent", r"(\d+)\s+item"], max_zero_runs=5,
        zero_phrases=("quiet day", "nothing to send"),
        why="Alyssa's pedagogy digest has been quiet for five runs — check the "
            "literacy feeds and podcast transcription still produce.",
    ),
    # modelhealth reports "N ok, ... needs-funding". Zero providers OK is a
    # real emergency (every paid model unreachable), so the threshold is 1.
    "modelhealth": Rule(
        "modelhealth", [r"(\d+)\s+ok"], max_zero_runs=1,
        why="ZERO LLM providers healthy — every paid model is unreachable. "
            "Check credentials and provider funding immediately.",
    ),
    # Vendor/account mail watcher (S67). Genuinely quiet most days — zero new
    # items is the NORMAL case, so this is only about the scan itself dying.
    # Its own ok=False already covers a failed scan, so the threshold is high
    # and this is a backstop, not the primary signal.
    # S81. Nightly divergent brainstorm (S77). Added the day it failed at 02:00
    # on a transient DNS outage and nobody was told. Its note is
    # "N model answer(s), M rate card(s)", or "FAILED: ..." when it dies --
    # and a FAILED run never reaches here, because check() leaves ok=False to
    # heartbeat. This rule is for the other shape: it runs, exits 0, and every
    # model returned nothing.
    "opportunityscout": Rule(
        "opportunityscout",
        [r"(\d+)\s+model answer", r"(\d+)\s+rate card"],
        max_zero_runs=2,
        why="The scout ran but no model answered twice running — check "
            "provider reachability (llm-ping) and the rate-card fetches before "
            "assuming the prompts went stale.",
    ),
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


_CADENCE_CACHE = None


def _cadence_table():
    """Load job_status.CADENCE_H BY FILE PATH. Returns None if unreachable.

    `from job_status import CADENCE_H` cannot work here and it is worth saying
    why, because it looks like it should: the supervisor runs with
    WorkingDirectory=/opt/cumulus-supervisor/app, a different tree from the app
    it watches. A plain import would have failed in production while passing
    anywhere it was tested from the repo -- the check would have reported BLIND
    forever and nobody would have known it was never really running.

    APP_DIR is the same anchor the ledger itself is read through, so if the
    ledger is reachable the schedule is too. The second candidate is for running
    from a checkout (selftests, a dev box), where job_status.py sits beside the
    supervisor/ directory.
    """
    global _CADENCE_CACHE
    if _CADENCE_CACHE is not None:
        return _CADENCE_CACHE
    import importlib.util
    here = Path(__file__).resolve().parent
    for cand in (APP_DIR / "job_status.py", here.parent / "job_status.py"):
        try:
            if not cand.exists():
                continue
            spec = importlib.util.spec_from_file_location("_js_cadence", cand)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            table = getattr(m, "CADENCE_H", None)
            if isinstance(table, dict) and table:
                _CADENCE_CACHE = table
                return table
        except Exception:
            continue
    return None


def overdue_jobs(status=None, now=None):
    """Which jobs have not recorded a NEW run inside their expected window?

    S96, 2026-09-02. Buddy: "any reason it wasn't caught by skywarden?"

    THE HOLE THIS CLOSES
    --------------------
    On 2026-09-02 `pedagogy_daily` hung at 06:00 and was still hung at 09:00.
    Both halves of Skywarden were blind to it, for two different reasons:

      * `heartbeat` asks `systemctl is-failed`. A hung oneshot sits in
        **activating** -- not failed, not inactive. And with the pre-S96
        `TimeoutStartSec=infinity`, the unit was GUARANTEED never to reach
        `failed`, so the one state heartbeat watches was unreachable.
      * `check()` above reads the ledger, but skips any run it has already
        counted:

              if run_id and run_id == js.get("last_run_id"): continue

        The entry still said YESTERDAY, so the run_id never changed, so it was
        skipped on every tick indefinitely. The guard that correctly stops
        double-counting a run also silently swallows "there has not been a new
        run in three days."

    Both are right by their own definitions. Neither asks the simplest question
    there is: **did this job run at all?** That question had no owner anywhere in
    the supervisor -- there was no cadence or age check in it of any kind.

    WHY THE CADENCE TABLE IS IMPORTED, NOT RESTATED
    -----------------------------------------------
    `job_status.CADENCE_H` already holds every job's expected interval with a
    grace window baked in, and it is already maintained -- S81 corrected a
    192h entry for a job that had been daily for months, precisely because a
    cadence looser than the schedule is "a blind spot with a checkmark on it."
    A second copy here would drift from it, and the first symptom of that drift
    would be this check going quiet. One table, one place to fix.

    SCOPE, AND WHY IT CANNOT FALSE-POSITIVE ON THE OTHER BOX
    -------------------------------------------------------
    Only jobs that have an entry in THIS box's ledger are checked. A CIRRUS-only
    job has no entry here, so it is never judged -- otherwise every CIRRUS job
    would read as permanently overdue on CUMULUS and the check would be muted
    within a day (T9). "Never recorded at all" is deliberately NOT treated as
    overdue: from the ledger alone it is indistinguishable from a job that
    simply does not run on this box, and it is already covered on the CIRRUS
    side by job_status.summarize() and placement-audit.

    Returns [] when everything is inside its window. Never raises.
    """
    now = now or datetime.now()
    status = status if status is not None else _load(STATUS_PATH, {})
    CADENCE_H = _cadence_table()
    if CADENCE_H is None:
        # Cannot read the schedule => cannot judge. Say so; do not return [],
        # which reads identically to "everything is on time".
        return [{"job": "(cadence table)", "hours": 0, "age_h": 0,
                 "why": "could not load job_status.CADENCE_H — this check is "
                        "BLIND, which is not the same as nothing being overdue"}]

    out = []
    for job, entry in sorted(status.items()):
        cad = CADENCE_H.get(job)
        if not cad:
            continue                       # no expected cadence => nothing to judge
        epoch = entry.get("epoch")
        if not epoch:
            continue                       # pre-epoch entry; summarize() covers it
        try:
            age_h = (now.timestamp() - float(epoch)) / 3600.0
        except (TypeError, ValueError):
            continue
        if age_h > cad:
            out.append({
                "job": job,
                "hours": cad,
                "age_h": round(age_h, 1),
                "why": (f"last recorded run was {round(age_h, 1)}h ago, expected "
                        f"every {cad}h — the job is not running, or it starts and "
                        f"never finishes (a hung job never reaches record())"),
            })
    return out


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
    # S96: "did it run at all?" -- the question neither half of Skywarden asked.
    overdue = overdue_jobs(status, now)
    parts = []
    for o in overdue:
        parts.append(f"{o['job']}: {o['why']}")
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
        # S96: `overdue` DOES flip it. A job that has not run is a harder
        # failure than one that ran and produced nothing, and it is the case
        # that went unseen for three hours on 2026-09-02.
        "ok": not stalled and not unreadable and not overdue,
        "stalled": stalled,
        "unreadable": unreadable,
        "unmonitored": unmon,
        "overdue": overdue,
        "detail": "; ".join(parts) if parts else "all jobs producing",
    }


# ── selftest ────────────────────────────────────────────────────────────────
def selftest() -> bool:
    import time as _t
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))

    # The ACTUAL note hoaleads writes, found on the first live run -- prose,
    # not counters. The count-regex version reported "unreadable" forever,
    # which would have meant the alert never fired.
    real = "no genuine leads"

    # S96. These fixtures used bare 1/2/3/99 as opaque run-id tokens, which the
    # zero-run rule only ever compared for equality. `epoch` is a REAL unix
    # timestamp in production (job_status.record writes int(time.time())), so
    # once the cadence check started reading it, "epoch: 1" meant 1970 and every
    # one of these jobs read as ~500,000 hours overdue. Anchor the same distinct
    # offsets to now: the run ids stay distinct, and each fixture now means "ran
    # just now", which is what the test was always asserting.
    _NOW_E = int(_t.time())

    ck("the real prose note parses as zero, not unreadable",
       RULES["hoaleads"].productivity(real) == (0, True))
    ck("a counted note still parses",
       RULES["hoaleads"].productivity("2 new, 1 updated")[0] == 3)
    st = {"hoaleads": {"ok": True, "epoch": _NOW_E + 1, "note": real}}
    r1 = check(st, {})
    ck("one zero day does NOT alert", r1["ok"] is True)

    # Same job, three consecutive distinct runs -> must fire.
    state = {}
    for epoch in (_NOW_E + 1, _NOW_E + 2, _NOW_E + 3):
        r = check({"hoaleads": {"ok": True, "epoch": epoch, "note": real}}, state)
    ck("three zero runs DO alert", r["ok"] is False)
    ck("alert names the job", any(s["job"] == "hoaleads" for s in r["stalled"]))
    ck("alert carries actionable why", "kentcountyde.gov" in r["stalled"][0]["why"])

    # Productivity resets the counter.
    state = {}
    for epoch in (_NOW_E + 1, _NOW_E + 2):
        check({"hoaleads": {"ok": True, "epoch": epoch, "note": real}}, state)
    r = check({"hoaleads": {"ok": True, "epoch": _NOW_E + 3, "note": "2 new, 1 updated"}}, state)
    ck("a productive run resets the streak", r["ok"] is True)
    ck("streak actually zeroed", state["hoaleads"]["zero_runs"] == 0)

    # The same run seen twice on consecutive 60s ticks must count once.
    state = {}
    for _ in range(5):
        r = check({"hoaleads": {"ok": True, "epoch": _NOW_E + 99, "note": real}}, state)
    ck("re-reading one run does not inflate the streak",
       state["hoaleads"]["zero_runs"] == 1 and r["ok"] is True)

    # A FAILED run is heartbeat's job -- don't double-report or count it.
    state = {}
    r = check({"hoaleads": {"ok": False, "epoch": _NOW_E + 1, "note": "crashed"}}, state)
    ck("failed runs are left to heartbeat", r["ok"] is True and not r["stalled"])

    # An unparseable note must NOT read as healthy.
    state = {}
    r = check({"hoaleads": {"ok": True, "epoch": _NOW_E + 1, "note": "finished"}}, state)
    ck("unreadable note flips ok=False", r["ok"] is False)
    ck("unreadable is reported separately from stalled",
       r["unreadable"] and not r["stalled"])

    # ---- S81: the REAL notes these jobs write, taken off the live CUMULUS
    # ledger. Every one of these read as "unreadable" -- i.e. ok=False, i.e.
    # Skywarden permanently unhealthy -- while the job was working perfectly.
    # Fixtures invented alongside the rule agreed with the rule; only the live
    # ledger disagreed.
    for job, note, want in (
        ("billsnow",   "sent material update",        True),
        ("billnewdev", "sent",                        True),
        ("pedagogy",   "sent: 2 art, 0 pod, 0 topic", True),
    ):
        produced, matched = RULES[job].productivity(note)
        ck(f"{job}'s REAL note parses at all ({note!r})", matched)
        ck(f"{job}'s real note counts as productive", produced > 0 if want else True)
        r = check({job: {"ok": True, "epoch": _NOW_E + 1, "note": note}}, {})
        ck(f"{job} working normally does NOT flip ok=False", r["ok"] is True)

    # ...and a genuinely quiet run must still read as zero, or the fix above
    # would have bought a green light instead of a working check.
    ck("billsnow's quiet note still reads as zero",
       RULES["billsnow"].productivity("no material change") == (0, True))
    ck("an EMPTY pedagogy digest still reads as zero",
       RULES["pedagogy"].productivity("sent: 0 art, 0 pod, 0 topic") == (0, True))
    ck("a zero phrase beats a produced phrase in the same note",
       RULES["billsnow"].productivity("nothing to send, so nothing sent")
       == (0, True))

    # S81 send guard: a run that SUPPRESSED a duplicate must not read as a
    # stalled/unproductive one -- the week's mail did go out, so the job did
    # its work. Pinned here because the note is written in bill_snow_weekly.py
    # and read by rules in this file, and nothing else connects the two.
    # Read the note out of the JOB ITSELF rather than retyping it. The first
    # version of this test used a hyphen where the real string has an em dash,
    # so it was testing a string that never occurs -- the classic way a
    # regression test passes forever while guarding nothing.
    supp = "already sent today \u2014 duplicate send suppressed"
    for src in (Path(__file__).resolve().parent.parent / "snowbrief/bill_snow_weekly.py",):
        try:
            m = re.search(r"already sent today[^\"']*", src.read_text())
            if m:
                supp = m.group(0)
        except OSError:
            pass                      # not readable from here; use the literal
    ck("the suppression note under test is the one the job writes",
       supp.startswith("already sent today") and "suppressed" in supp)
    for job in ("billsnow", "billnewdev"):
        produced, matched = RULES[job].productivity(supp)
        ck(f"{job}: a suppressed-duplicate run parses", matched)
        ck(f"{job}: ...and counts as productive, not a zero week", produced > 0)

    # The scout, added the day it failed silently.
    ck("opportunityscout has a rule", "opportunityscout" in RULES)
    ck("the scout's real success note parses as productive",
       RULES["opportunityscout"].productivity("5 model answer(s), 7 rate card(s)")[0] > 0)
    st = {"opportunityscout": {"ok": True, "epoch": _NOW_E + 1,
                               "note": "0 model answer(s), 0 rate card(s)"}}
    ck("a scout run where nothing answered reads as zero",
       RULES["opportunityscout"].productivity(st["opportunityscout"]["note"])
       == (0, True))

    # A job with no rule is surfaced, not silently ignored -- but is not an
    # incident on its own.
    r = check({"somenewjob": {"ok": True, "epoch": _NOW_E + 1, "note": "done"}}, {})
    ck("unmonitored job is reported", "somenewjob" in r["unmonitored"])
    ck("unmonitored alone does not flip ok", r["ok"] is True)

    bad = 0

    # ── S96 cadence check: "did it run at all?" ──────────────────────────────
    # Case 1 IS 2026-09-02, replayed: pedagogy's ledger entry frozen at the
    # PREVIOUS day's run while the job hung. Every field is what the real entry
    # held; only the clock moves. This is the exact state both halves of
    # Skywarden looked straight at and called healthy.
    import time as _t
    _now = datetime(2026, 9, 2, 9, 0, 0)
    _yesterday = _now.timestamp() - (27 * 3600)      # 27h => past pedagogy's 26h

    _hung = {"pedagogy": {"last_run": "2026-09-01T06:01:25",
                          "epoch": _yesterday, "ok": True,
                          "note": "sent: 1 art, 0 pod, 0 topic"}}
    _od = overdue_jobs(_hung, _now)
    ck("the 2026-09-02 hang is caught: a ledger entry older than the cadence",
       len(_od) == 1 and _od[0]["job"] == "pedagogy")
    ck("...and it flips ok=False, so the escalation path actually runs",
       check(status=_hung, state={}, now=_now)["ok"] is False)

    # ok=True on the stale entry must NOT rescue it. A hung job's last entry is
    # a SUCCESS -- that is the whole trap. If this passed, the check would be
    # blind to exactly the case it exists for.
    ck("a stale entry that says ok=True is still overdue",
       len(overdue_jobs({"pedagogy": {"epoch": _yesterday, "ok": True,
                                      "note": "sent"}}, _now)) == 1)

    # A job that ran on time is silent -- the direction that stops it being
    # muted (T9). 1h old against a 26h cadence.
    ck("a job that ran an hour ago is NOT reported",
       overdue_jobs({"pedagogy": {"epoch": _now.timestamp() - 3600,
                                  "ok": True, "note": "sent"}}, _now) == [])

    # Scope: a job with no cadence entry is not judged, or every unknown job
    # reads as overdue forever.
    ck("a job with no CADENCE_H entry is not judged",
       overdue_jobs({"not_a_real_job": {"epoch": _yesterday, "ok": True,
                                        "note": "x"}}, _now) == [])

    # A weekly job at 27h must stay silent -- proves the check reads the REAL
    # per-job cadence and is not applying one blanket number to everything.
    ck("a WEEKLY job 27h old is not overdue (per-job cadence, not a blanket)",
       overdue_jobs({"billsnow": {"epoch": _yesterday, "ok": True,
                                  "note": "sent"}}, _now) == [])

    # Malformed epochs must be skipped, not crash the supervisor's 60s tick.
    ck("a junk epoch is skipped rather than raising",
       overdue_jobs({"pedagogy": {"epoch": "not-a-number", "ok": True,
                                  "note": "x"}}, _now) == [])
    ck("a missing epoch is skipped rather than raising",
       overdue_jobs({"pedagogy": {"ok": True, "note": "x"}}, _now) == [])

    # An empty ledger is not a clean bill of health, but it is also not an
    # incident -- nothing to judge, so nothing reported.
    ck("an empty ledger reports nothing", overdue_jobs({}, _now) == [])

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
