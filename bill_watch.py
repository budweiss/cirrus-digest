#!/usr/bin/env python3
"""bill_watch.py (S537, 2026-10-29) -- know before Bill does.

Bill told Buddy he kept seeing "no new information". The S537 audit found a
SNOW outlook frozen since July, two Mondays refused by expired approvals, and
an HOA digest repeating August articles -- each visible in a ledger nobody was
asked to read. Buddy: "Yes, start part 1".

  reportcard  Mon 12:00 -- one Telegram to Buddy: for each of Bill's three
              Monday emails, SENT / QUIET / FAILED / NOT RUN, plus whether the
              content moved (SNOW's snowfall estimate and since when).
  approvals   daily 12:00 -- Telegram Buddy when a route's approvals all lapse
              within 24 h (after the 04:40 tests and 10:00 auto-review have had
              their chance) or have already lapsed. Advisory-date tasks
              (billsnow, ideate) are skipped: their date cannot block a run.
              Silent when nothing needs a look.
"""
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

BILL_JOBS = (("billsnow", "SNOW outlook", True),
             ("billnewdev", "Development leads", True),
             ("entitykbdigest", "HOA digest", False))   # (ledger name, label, has send stamp)
ESTIMATE_RX = re.compile(r"Central estimate[^~]*~\s*([0-9]+\s*[-–]\s*[0-9]+)")


def job_line(label, row, stamped, today, extra=""):
    """One report-card line from a ledger row (dict or None) and a send stamp."""
    ran_today = bool(row) and str(row.get("last_run", "")).startswith(today)
    note = str((row or {}).get("note", ""))[:90]
    if not ran_today:
        state = "NOT RUN today"
    elif not row.get("ok"):
        state = "FAILED"
    elif stamped or note.startswith("sent"):
        state = "SENT"
    else:
        state = "QUIET (nothing sent)"
    ok = state.startswith(("SENT", "QUIET"))
    return ok, f"{'OK ' if ok else '!! '}{label}: {state}" + (f" -- {note}" if note else "") + extra


def estimate_history(texts):
    """[(date, estimate)] oldest-first -> (latest, since-date) for the SNOW
    central estimate, or (None, None) when no refresh states one."""
    rows = [(d, m.group(1).replace(" ", "").replace("–", "-"))
            for d, t in texts for m in [ESTIMATE_RX.search(t)] if m]
    if not rows:
        return None, None
    latest = rows[-1][1]
    since = rows[-1][0]
    for d, est in reversed(rows):
        if est != latest:
            break
        since = d
    return latest, since


def lapsing(projects, now, advisory, window=24 * 3600):
    """[(route, when, expired?)] for enabled routes whose approved records all
    end within `window` (or already ended). Advisory-date tasks are skipped."""
    out = []
    for name, rec in sorted(projects.items()):
        if not isinstance(rec, dict) or rec.get("enabled") is False:
            continue
        task = name.split("foundation:", 1)[-1]
        if task in advisory:
            continue
        for kind in ("evaluations", "judge_evaluations"):
            exps = [e.get("expires_at") for e in rec.get(kind) or []
                    if isinstance(e, dict) and e.get("approved") is True
                    and isinstance(e.get("expires_at"), (int, float))]
            if not exps:
                continue
            last = max(exps)
            if last - now < window:
                out.append((name + ("" if kind == "evaluations" else " (judge)"), last, last <= now))
    return out


def reportcard(notify, today=None):
    import job_status
    import send_guard
    today = today or datetime.now().strftime("%Y-%m-%d")
    try:
        ledger = json.loads(job_status.STATUS_PATH.read_text())
    except Exception:
        ledger = {}
    lines, all_ok = [], True
    for name, label, has_stamp in BILL_JOBS:
        extra = ""
        if name == "billsnow":
            out = ROOT / "snowbrief/out"
            texts = [(p.stem[-10:], p.read_text(errors="replace"))
                     for p in sorted(out.glob("SNOW-2026-27-Outlook-Refresh-*.md"))]
            est, since = estimate_history(texts)
            if est:
                extra = f"; snowfall estimate ~{est}\" (unchanged since {since})"
        if name == "entitykbdigest":
            log = subprocess.run(["journalctl", "-u", "entity-kb-weekly-digest", "--since", today,
                                  "--no-pager", "-o", "cat"], capture_output=True, text=True).stdout
            if "'attached': False" in log:
                extra = "; WORKBOOK MISSING from the email"
                all_ok = False
        ok, line = job_line(label, ledger.get(name), has_stamp and send_guard.already_sent_today(name),
                            today, extra)
        all_ok = all_ok and ok
        lines.append(line)
    notify(f"Bill's Monday report card {today}:\n" + "\n".join(lines))
    print("\n".join(lines))
    return all_ok


def approvals(notify, now=None):
    from capability_admission import ADVISORY_TASKS
    now = now or time.time()
    data = json.loads((ROOT / "config/model_capabilities.json").read_text())
    hits = lapsing(data.get("projects", data), now, set(ADVISORY_TASKS))
    if hits:
        notify("Approval watch: " + "; ".join(
            f"{n} {'LAPSED' if gone else 'lapses'} {datetime.fromtimestamp(w):%m-%d %H:%M}"
            for n, w, gone in hits) + ". Renewal has not installed -- review the packet.")
    print("approvals:", "nothing lapsing within 24 h" if not hits else hits)
    return hits


def selftest():
    ok = True

    def ck(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(("  PASS  " if cond else "  FAIL  ") + name)

    t = "2026-11-02"
    ck("a sent run is OK", job_line("S", {"last_run": t + "T04:02", "ok": True, "note": "sent material update"}, {"x": 1}, t)[1].startswith("OK S: SENT"))
    ck("a quiet run is OK and says nothing was sent",
       "QUIET" in job_line("N", {"last_run": t + "T04:30", "ok": True, "note": "no new leads"}, None, t)[1])
    ck("a failed run is flagged", job_line("S", {"last_run": t + "T04:00", "ok": False, "note": "refused"}, None, t)[0] is False)
    ck("last week's row is NOT RUN today", "NOT RUN" in job_line("D", {"last_run": "2026-10-26T05:00", "ok": True}, None, t)[1])
    ck("no ledger row at all is NOT RUN", "NOT RUN" in job_line("D", None, None, t)[1])

    refresh = lambda est: f"- **Central estimate:** below-to-near-normal, **~{est}″**, across"
    hist = [("2026-07-27", refresh("12-22")), ("2026-10-19", refresh("10-20")),
            ("2026-10-26", refresh("12–22")), ("2026-10-28", refresh("12-22"))]
    ck("estimate unchanged since the last time it moved", estimate_history(hist) == ("12-22", "2026-10-26"))
    ck("no stated estimate -> None", estimate_history([("2026-10-28", "no table")]) == (None, None))

    now = 1_000_000.0
    projects = {"foundation:hoa-leads": {"evaluations": [{"approved": True, "expires_at": now + 3600}, {"approved": True, "expires_at": now + 7200}]},
                "foundation:pedagogy-topic": {"evaluations": [{"approved": True, "expires_at": now + 5 * 86400}]},
                "foundation:billsnow": {"evaluations": [{"approved": True, "expires_at": now - 10}]},
                "foundation:old": {"enabled": False, "evaluations": [{"approved": True, "expires_at": now - 10}]},
                "foundation:gone": {"evaluations": [{"approved": True, "expires_at": now - 10}, {"approved": False, "expires_at": now + 9e5}]},
                "foundation:split": {"evaluations": [{"approved": True, "expires_at": now + 3600}, {"approved": True, "expires_at": now + 4 * 86400}]}}
    hits = {n: gone for n, _, gone in lapsing(projects, now, {"billsnow"})}
    ck("a route whose approvals all end within 24 h is flagged", hits.get("foundation:hoa-leads") is False)
    ck("an already lapsed route is flagged LAPSED", hits.get("foundation:gone") is True)
    ck("an unapproved record does not count as cover", "foundation:gone" in hits)
    ck("one record still valid for days covers the route", "foundation:split" not in hits)
    ck("advisory billsnow and disabled routes are skipped", "foundation:billsnow" not in hits and "foundation:old" not in hits)
    ck("a healthy route is silent", "foundation:pedagogy-topic" not in hits)

    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "selftest":
        return selftest()
    from immaculate_agent import send_telegram
    import job_status
    if mode == "reportcard":
        good = reportcard(send_telegram)
        job_status.record("billreportcard", True, "all delivered" if good else "needs a look")
        return 0
    if mode == "approvals":
        hits = approvals(send_telegram)
        job_status.record("approvalwatch", True, f"{len(hits)} route(s) lapsing")
        return 0
    print("usage: bill_watch.py reportcard|approvals|selftest")
    return 2


if __name__ == "__main__":
    sys.exit(main())
