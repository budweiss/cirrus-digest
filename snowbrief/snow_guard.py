#!/usr/bin/env python3
"""snow_guard.py (S537, 2026-10-29) -- keep Bill's Monday SNOW report from
silently missing a week.

Twice the 04:00 run was refused by admission (09-28: no email that week;
10-12: sent at 09:46 only because someone noticed). bill_snow_weekly records a
failed ledger row but exits 0, so systemd and Skywarden saw success and nothing
re-ran it. Buddy, 10-29: "Yes, build both".

  preflight  Sun 18:00 -- replay Monday 04:00 admission for the draft and judge
             roles with real current provider health and NO model call.
             Telegram Buddy if Monday would be refused.
  catchup    Mon 11:00 -- after the 10:00 auto-renewal: if nothing was sent
             today and today's run errored (or never ran), rerun the job once.
             send_guard still blocks a second send. Telegram Buddy on the outcome.

A quiet week ("no material change", ledger ok) is not a failure and is left alone.
"""
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
TASK = "billsnow"


def next_monday_0400(now=None):
    now = now or datetime.now()
    days = (7 - now.weekday()) % 7 or 7
    return (now + timedelta(days=days)).replace(hour=4, minute=0, second=0, microsecond=0)


def admission_problems(route, health, candidates, *, system, judge_system, contract, when):
    """Problems Monday's run would hit, [] when both roles admit. Pure: the
    caller supplies the route, real health rows and the admission function."""
    fresh = [dict(h, checked_at=when) for h in health]
    need = int(route.get("reviewers") or 1)
    cap = route.get("capability")
    problems = []
    try:
        drafts = candidates(route.get("evaluations", []), fresh, task=TASK, capability=cap,
                            system=system, contract_sha256=contract, now=when)
    except Exception as exc:
        drafts = []
        problems.append(f"draft admission raised {type(exc).__name__}")
    if len(drafts) < need:
        problems.append(f"{len(drafts)} of {need} draft models admitted")
    if need == 2 and route.get("panel_only") is not True:
        try:
            judges = candidates(route.get("judge_evaluations", []), fresh, task=TASK,
                                capability=f"{cap}:synthesis", system=judge_system,
                                contract_sha256=contract, now=when)
        except Exception as exc:
            judges = []
            problems.append(f"judge admission raised {type(exc).__name__}")
        if not judges:
            problems.append("no judge model admitted")
    unhealthy = sorted(h.get("provider") or h.get("id") or "?" for h in health if not h.get("healthy"))
    if unhealthy:
        problems.append("provider unhealthy now: " + ", ".join(map(str, unhealthy)))
    return problems


def preflight(notify, when=None):
    when = when or next_monday_0400()
    try:
        import capability_registry as CR
        import ensemble
        from capability_admission import candidates
        from capability_health import observe_cloud
        sys.path.insert(0, str(HERE))
        import bill_snow_weekly as BS
        route = CR.foundation_route(TASK, ROOT)
        contract = CR.contract_digest(route, ROOT)
        creds = json.loads((ROOT / "config/credentials.json").read_text())
        ids = sorted({e["id"] for e in route.get("evaluations", []) + route.get("judge_evaluations", [])})
        health = [observe_cloud(creds, p) for p in ids]
        problems = admission_problems(route, health, candidates, system=BS.SYSTEM,
                                      judge_system=ensemble.judge_system(TASK),
                                      contract=contract, when=when.timestamp())
    except Exception as exc:
        problems = [f"pre-flight could not replay admission ({type(exc).__name__})"]
    if problems:
        notify(f"SNOW pre-flight: Monday {when:%m-%d %H:%M} report would be refused -- "
               + "; ".join(problems) + ". Fix before 04:00 (snowbrief/snow_guard.py).")
    print("preflight", "OK" if not problems else "PROBLEMS: " + "; ".join(problems))
    return problems


def catchup_needed(sent_stamp, ledger_row, today):
    """(rerun?, why). Reruns only a run that errored or never happened today."""
    if sent_stamp:
        return False, "already sent today"
    if not ledger_row or not str(ledger_row.get("last_run", "")).startswith(today):
        return True, "no run recorded today"
    if ledger_row.get("ok"):
        return False, "today's run completed (quiet week)"
    return True, "today's run failed: " + str(ledger_row.get("note", ""))[:120]


def catchup(notify, *, sent_today, ledger_row, rerun, today=None, weekday=None):
    weekday = datetime.now().weekday() if weekday is None else weekday
    if weekday != 0:
        # A hand-run on any other day would see "no run today" and email Bill.
        print("catchup: not Monday; nothing to catch up")
        return "skipped"
    today = today or datetime.now().strftime("%Y-%m-%d")
    need, why = catchup_needed(sent_today(), ledger_row(), today)
    print("catchup:", why)
    if not need:
        return "skipped"
    rc = rerun()
    if sent_today():
        notify(f"SNOW catch-up: the 04:00 report did not go out ({why}); the 11:00 rerun SENT it to Bill.")
        return "sent"
    row = ledger_row() or {}
    if rc == 0 and row.get("ok") and str(row.get("last_run", "")).startswith(today):
        notify(f"SNOW catch-up: 04:00 failed ({why}); the rerun completed with no material change, so nothing was sent.")
        return "quiet"
    notify(f"SNOW catch-up FAILED: {why}; rerun exit {rc}, note: {str(row.get('note', ''))[:120]}. "
           "Bill has no report this week -- needs a look.")
    return "failed"


def _live_catchup(notify):
    import job_status
    import send_guard

    def ledger_row():
        try:
            return json.loads(job_status.STATUS_PATH.read_text()).get(TASK)
        except Exception:
            return None

    def rerun():
        return subprocess.run([sys.executable, str(HERE / "bill_snow_weekly.py")],
                              cwd=str(ROOT), timeout=1700).returncode

    return catchup(notify, sent_today=lambda: send_guard.already_sent_today(TASK),
                   ledger_row=ledger_row, rerun=rerun)


def selftest():
    ok = True

    def ck(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(("  PASS  " if cond else "  FAIL  ") + name)

    sun = datetime(2026, 11, 1, 18, 0)
    ck("Sunday 18:00 targets Monday 04:00", next_monday_0400(sun) == datetime(2026, 11, 2, 4, 0))
    ck("a Monday targets the NEXT Monday", next_monday_0400(datetime(2026, 11, 2, 11)) == datetime(2026, 11, 9, 4, 0))

    route = {"reviewers": 2, "capability": "billsnow:x",
             "evaluations": [{"id": "anthropic"}, {"id": "kimi"}], "judge_evaluations": [{"id": "anthropic"}]}
    health = [{"provider": "anthropic", "healthy": True, "checked_at": 1}, {"provider": "kimi", "healthy": True, "checked_at": 1}]
    seen = {}

    def admit_all(evs, h, **kw):
        seen.setdefault("now", kw["now"]); seen.setdefault("checked", [x["checked_at"] for x in h])
        return list(evs)
    kw = dict(system="s", judge_system="j", contract="c", when=500.0)
    ck("all roles admitted -> no problems", admission_problems(route, health, admit_all, **kw) == [])
    ck("admission is replayed AT Monday's time with health stamped then",
       seen["now"] == 500.0 and seen["checked"] == [500.0, 500.0])
    one = admission_problems(route, health, lambda evs, h, **k: evs[:1], **kw)
    ck("one draft admitted of two is a problem", any("1 of 2 draft" in p for p in one))
    nojudge = admission_problems(route, health, lambda evs, h, **k: [] if k["capability"].endswith(":synthesis") else evs, **kw)
    ck("no judge admitted is a problem", "no judge model admitted" in nojudge)

    def boom(*a, **k):
        raise ValueError("expired")
    ck("admission raising is a problem, not a crash", admission_problems(route, health, boom, **kw))
    sick = [dict(health[0]), dict(health[1], healthy=False)]
    ck("an unhealthy provider is reported", any("kimi" in p for p in admission_problems(route, sick, admit_all, **kw)))

    today = "2026-11-02"
    ck("already sent -> no rerun", catchup_needed({"x": 1}, None, today)[0] is False)
    ck("quiet week (ok row today) -> no rerun", catchup_needed(None, {"last_run": today + "T04:02", "ok": True}, today)[0] is False)
    ck("failed row today -> rerun", catchup_needed(None, {"last_run": today + "T04:00", "ok": False, "note": "x"}, today)[0])
    ck("no row today -> rerun", catchup_needed(None, {"last_run": "2026-10-26T04:02", "ok": True}, today)[0])

    msgs = []
    state = {"sent": None, "row": {"last_run": today + "T04:00", "ok": False, "note": "refused"}, "runs": 0}

    def rerun_sends():
        state["runs"] += 1; state["sent"] = {"stamp": 1}; return 0
    r = catchup(msgs.append, sent_today=lambda: state["sent"], ledger_row=lambda: state["row"],
                rerun=rerun_sends, today=today, weekday=0)
    ck("a failed morning is rerun once and reported sent", r == "sent" and state["runs"] == 1 and "SENT" in msgs[-1])

    def rerun_fails():
        state["runs"] += 1; return 0
    state.update(sent=None, runs=0)
    r = catchup(msgs.append, sent_today=lambda: state["sent"], ledger_row=lambda: state["row"],
                rerun=rerun_fails, today=today, weekday=0)
    ck("a rerun that still fails is reported FAILED", r == "failed" and "FAILED" in msgs[-1])
    state.update(row={"last_run": today + "T04:02", "ok": True}, runs=0)
    r = catchup(msgs.append, sent_today=lambda: None, ledger_row=lambda: state["row"],
                rerun=rerun_fails, today=today, weekday=0)
    ck("a quiet week is left alone, no rerun", r == "skipped" and state["runs"] == 0)
    state.update(row=None, runs=0)
    r = catchup(msgs.append, sent_today=lambda: None, ledger_row=lambda: None,
                rerun=rerun_fails, today="2026-10-29", weekday=3)
    ck("a hand-run on a Thursday never reruns the job", r == "skipped" and state["runs"] == 0)

    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "selftest":
        return selftest()
    from immaculate_agent import send_telegram
    import job_status
    if mode == "preflight":
        problems = preflight(send_telegram)
        job_status.record("snowguard", not problems,
                          "monday admits" if not problems else "monday would be refused")
        return 0
    if mode == "catchup":
        out = _live_catchup(send_telegram)
        job_status.record("snowguard", out != "failed", "catchup " + out)
        return 0
    print("usage: snow_guard.py preflight|catchup|selftest")
    return 2


if __name__ == "__main__":
    sys.exit(main())
