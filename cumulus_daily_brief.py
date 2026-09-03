#!/usr/bin/env python3
"""
CUMULUS Daily Brief  (S66)
===============================================================================
Buddy's ask: a daily recap, to Buddy, of what CUMULUS's projects actually did
today -- Bill's HOA research + CRM updates, Alyssa's pedagogy send, Skywarden's
own ops (heartbeat checks, anomalies, guidance/opus requests). Distinct from
`entity_kb_weekly_digest.py` (Bill's own Monday recap, client-facing, only his
properties) -- this is Buddy-facing, covers everything CUMULUS touched today,
and is deliberately deterministic: reads local logs/ledgers only, no LLM call,
gives Skywarden no new write authority. Same pattern as CIRRUS's
morning_brief.py.

Runs at the end of the day (after pedagogy/billsnow/billnewdev/hoaleads have
all had a chance to run) -- see cumulus-daily-brief.timer.

Usage:
  python3 cumulus_daily_brief.py            # compose + SEND (email + telegram)
  python3 cumulus_daily_brief.py --dry-run  # compose + PRINT, send nothing
"""
import json
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path.home() / "cirrus-digest"
CREDS_PATH = PROJECT_DIR / "config/credentials.json"
JOBS_STATUS_PATH = PROJECT_DIR / "logs/jobs-status.json"
SKY_STATE_DIR = Path("/opt/cumulus-supervisor/state")

sys.path.insert(0, str(PROJECT_DIR))

TODAY = datetime.now().strftime("%Y-%m-%d")
DAY_NAME = datetime.now().strftime("%A, %B %d")

# project -> entity_kb project(s) to recap today's events for. Extend this,
# not the logic, when a new client gets an entity_kb project (mirrors
# entity_kb_weekly_digest.py's WEEKLY_DIGEST_RECIPIENTS convention).
CLIENT_KB_PROJECTS = {
    "Bill (property/HOA)": ["hoa_leads_bill"],
}

# which entries in jobs-status.json belong to which client, for the "today's
# jobs" section (skip cadence/overdue math here -- that's job_status.py's
# job on the CIRRUS side; this is just "what ran today, locally").
CLIENT_JOBS = {
    "Bill (property/HOA)": ["hoaleads", "billsnow", "billnewdev"],
    "Alyssa (pedagogy)": ["pedagogy"],
}


def _read_json(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default if default is not None else {}


def gather_client_events():
    """Today's entity_kb events, grouped by client, entity name."""
    import entity_kb
    out = {}
    for client, projects in CLIENT_KB_PROJECTS.items():
        lines = []
        for proj in projects:
            events = entity_kb.get_events(proj, since=f"{TODAY} 00:00:00")
            if not events:
                continue
            by_entity = {}
            for ev in events:
                by_entity.setdefault(ev["slug"], {"name": ev["name"], "events": []})
                by_entity[ev["slug"]]["events"].append(ev)
            for _slug, group in sorted(by_entity.items(), key=lambda kv: kv[1]["name"]):
                for ev in group["events"]:
                    if ev["event_type"] == "signal":
                        lines.append(f"  - {group['name']}: {ev.get('summary', '')}")
                    else:
                        lines.append(f"  - {group['name']}: {ev['field']} updated")
        if lines:
            out[client] = lines
    return out


def gather_job_lines():
    """Today's job status, one line per job, grouped by client."""
    status = _read_json(JOBS_STATUS_PATH)
    out = {}
    for client, job_names in CLIENT_JOBS.items():
        lines = []
        for name in job_names:
            rec = status.get(name)
            if not rec:
                continue
            ran_today = str(rec.get("last_run", "")).startswith(TODAY)
            if not ran_today:
                continue
            mark = "✅" if rec.get("ok") else "⚠️"
            note = f" — {rec['note']}" if rec.get("note") else ""
            lines.append(f"  {mark} {name}: {rec.get('last_run', '')[11:16]}{note}")
        if lines:
            out[client] = lines
    return out


def _sudo_cat(path):
    """Skywarden's state dir (/opt/cumulus-supervisor/state) is owned by the
    cumulus-supervisor service account, unreadable to buddy directly -- read
    it the same way the cumulus-supervisor-* runner commands do (`sudo -n
    cat`). Never raises; returns "" on any failure (missing sudo rights,
    missing file, etc.) so a permissions hiccup degrades this one section,
    not the whole brief."""
    try:
        r = subprocess.run(["sudo", "-n", "cat", str(path)],
                           capture_output=True, text=True, timeout=15)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


# S96. How far back to look when asking "is this anomaly a blip or a habit?"
# Two weeks covers a weekly job twice and is short enough that a fixed fault
# ages out of the count once it is actually fixed.
RECURRENCE_DAYS = 14
REPAIR_TOOLS = ("restart_service", "reset_failed")


def _unit_in(detail, unit):
    """Does this issue line refer to this unit? Deliberately a substring test.

    The two sides are written by different code paths — an issue reads
    "heartbeat found an issue: failed units: cirrus-modelhealth.service" and the
    repair records just "cirrus-modelhealth.service" — so there is no shared id
    to join on. A substring match on the unit name is exact enough (unit names
    are distinctive) and, being one-directional, cannot pair a repair with an
    unrelated issue that merely mentions a number.
    """
    return bool(unit) and unit in detail


def gather_skywarden():
    """Skywarden's own day: ledger rows (what it did/checked) + today's spend.

    S96. Buddy, reading the 2026-09-02 brief: "if skywarden fixes the issue, the
    report doesn't show this failure if it was corrected" — and the answer this
    grew into is the opposite of hiding it.

    A healed failure is still a failure, and healing is exactly what makes it
    silent: Skywarden repairs a unit within a tick, so a job that breaks and
    self-fixes EVERY NIGHT leaves the same trace as one that has never broken.
    Measured the same day, from Skywarden's own ledger:

        cirrus-modelhealth.service  repaired on 4 separate days (08-25, 08-27,
                                    08-31, 09-01) — 6 restarts + 2 reset-failed
        cirrus-hoaleads.service     58 issue flags, and NO repair, because
                                    Skywarden correctly judged it non-transient

    Nobody saw the modelhealth pattern, because each night it looked like an
    isolated blip. Suppressing healed rows would have made that permanent.

    So: mark them healed, and make REPETITION the loud part. A row that healed
    once is informational; the same row on four of the last fourteen days is the
    signal, and it is the one that was missing.
    """
    rows, all_rows = [], []
    cutoff = (datetime.now() - timedelta(days=RECURRENCE_DAYS)).strftime("%Y-%m-%d")
    for line in _sudo_cat(SKY_STATE_DIR / "ledger.jsonl").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        ts = str(r.get("ts", ""))
        if ts[:10] >= cutoff:
            all_rows.append(r)
        if ts.startswith(TODAY):
            rows.append(r)

    issues = [r for r in rows if "issue" in str(r.get("event", "")).lower()
              or "issue" in str(r.get("detail", "")).lower()]
    guidance = [r for r in rows if str(r.get("event", "")) in
                ("guidance-request", "opus-request", "guidance", "opus_upgrade")]
    checks = [r for r in rows if r not in issues and r not in guidance]

    lines = [f"  {len(checks)} routine check(s), {len(issues)} anomaly/issue "
             f"flag(s), {len(guidance)} escalation(s) to you"]
    # S96: STAMP THE TIME. These rows are today's LEDGER — a record of what
    # happened during the day, not a reading of current state. Without a clock
    # the reader cannot tell a failure that is happening NOW from one that was
    # healed minutes after it appeared, and Skywarden heals a failed unit within
    # a tick (measured 2026-09-02: failed 09:05:39, healed 09:06:45), so most
    # of these are already over by the time the brief goes out at 20:00. On
    # 2026-09-02 this line read "failed units: cirrus-pedagogy.service" twelve
    # hours after the unit had gone back to status=0/SUCCESS.
    # Today's repairs, so an anomaly can say whether it was actually fixed.
    repairs = [r for r in rows if str(r.get("event", "")) == "action"
               and str(r.get("tool", "")) in REPAIR_TOOLS]
    # How many DISTINCT DAYS in the window each repaired unit needed repair.
    repair_days = {}
    for r in all_rows:
        if str(r.get("event", "")) == "action" and str(r.get("tool", "")) in REPAIR_TOOLS:
            repair_days.setdefault(str(r.get("detail", "")), set()).add(
                str(r.get("ts", ""))[:10])

    for r in issues[:5]:
        when = str(r.get("ts", ""))[11:16] or "??:??"
        detail = str(r.get("detail", ""))
        lines.append(f"  ⚠️ [{when}] {detail[:100]}")
        # Was it repaired today, and is this a habit?
        fixed = [x for x in repairs if _unit_in(detail, str(x.get("detail", "")))]
        if fixed:
            unit = str(fixed[0].get("detail", ""))
            at = str(fixed[-1].get("ts", ""))[11:16] or "??:??"
            days = len(repair_days.get(unit, ()))
            if days >= 3:
                lines.append(f"       ↳ healed {at} — but repaired on {days} of "
                             f"the last {RECURRENCE_DAYS} days. RECURRING: the "
                             f"restart is holding a real fault together.")
            else:
                lines.append(f"       ↳ healed {at} by Skywarden"
                             + (f" ({days}d in {RECURRENCE_DAYS})" if days > 1 else ""))
        else:
            # No repair. Either Skywarden judged it non-transient (hoaleads,
            # 58 flags and no restart) or it is outside what it may touch.
            lines.append("       ↳ NOT repaired — still open, or outside "
                         "Skywarden's restart allowlist.")
    for r in guidance[:5]:
        lines.append(f"  🆘 {str(r.get('detail', r.get('result', '')))[:100]}")

    spend_today = 0.0
    for line in _sudo_cat(SKY_STATE_DIR / "spend-ledger.jsonl").splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        if str(row.get("ts", "")).startswith(TODAY):
            spend_today += float(row.get("cost_usd", 0))
    lines.append(f"  ${spend_today:.2f} spent today")
    return lines


def compose():
    events = gather_client_events()
    jobs = gather_job_lines()
    sky = gather_skywarden()

    clients = sorted(set(events) | set(jobs))
    lines = [f"# 🌙 CUMULUS Daily Brief — {DAY_NAME}", ""]

    if not clients:
        lines.append("**Quiet day** — no client jobs ran and no CRM activity recorded today.")
        lines.append("")
    for client in clients:
        lines.append(f"**{client}**")
        if client in jobs:
            lines += jobs[client]
        if client in events:
            lines.append("  Today's CRM updates:")
            lines += events[client]
        lines.append("")

    lines.append("**Skywarden (CUMULUS supervisor)**")
    lines += sky
    lines.append("")
    lines.append("*Composed by CUMULUS on-box (cumulus_daily_brief.py) — deterministic, no LLM call.*")

    subject = f"🌙 CUMULUS Daily Brief — {DAY_NAME}"
    return subject, "\n".join(lines)


def send_telegram(text, creds):
    token = creds.get("telegram_bot_token", "")
    user = str(creds.get("telegram_user_id", "")).strip()
    if not token or not user:
        return "telegram: no token/user configured — skipped"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunk = text if len(text) <= 3900 else text[:3900] + "\n…(truncated)"
    data = urllib.parse.urlencode({"chat_id": user, "text": chunk, "parse_mode": "Markdown"}).encode()

    # S66 fix: Telegram returns HTTP 400 (not a 200-with-ok:false) for
    # malformed Markdown entities -- e.g. this brief's raw "board_contact"/
    # "current_mgmt_co" field names read as unmatched italic underscores.
    # urlopen() RAISES on a non-2xx status, so that used to jump straight to
    # the outer except and skip the plain-text retry below entirely -- it
    # only ever ran for the rarer 200-but-ok:false case. Catch the markdown
    # attempt's own exception so the plain-text retry actually runs on both
    # failure shapes.
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
            ok = json.loads(r.read()).get("ok")
        if ok:
            return "telegram: sent"
    except Exception:
        pass

    try:
        data = urllib.parse.urlencode({"chat_id": user, "text": chunk}).encode()
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
            ok = json.loads(r.read()).get("ok")
        return "telegram: sent (plain)" if ok else "telegram: failed"
    except Exception as e:
        return f"telegram: error {e}"


def main():
    dry = "--dry-run" in sys.argv or "--dry" in sys.argv
    subject, body = compose()
    if dry:
        print("=== DRY RUN — nothing sent ===")
        print("SUBJECT:", subject)
        print("-" * 70)
        print(body)
        return

    from entity_kb_weekly_digest import _send_mail
    creds = _read_json(CREDS_PATH)
    ok = _send_mail(creds.get("outlook_email", ""), creds.get("outlook_password", ""),
                    "Buddy.Weiss@outlook.com", "", subject, body)
    print("email:", "sent" if ok else "failed")
    tg = send_telegram(body, creds)
    print(tg)

# S81: record into the job_status ledger so an overdue/failed run is actually
# SEEN. Until today this job ran unwatched -- opportunity_scout wrote its
# status correctly and nothing read it, and these jobs did not even write one.
# Best-effort and never allowed to change the exit status: monitoring must not
# break the thing it monitors.
    try:
        import job_status
        job_status.record("cumulusdailybrief", bool(ok),
                          "sent" if ok else "email FAILED to send")
    except Exception as e:
        print(f"job_status.record failed: {e}")


def selftest() -> int:
    """S96. Pins the three states an anomaly line can be in.

    Written because the 2026-09-02 brief showed a failure with no clock and no
    outcome, and answering "is this live?" took three separate checks against the
    box. The fixtures below are the REAL ledger shapes from that day, taken from
    `runner cumulus-heal-history`, not invented.

    The load-bearing case is the middle one. Skywarden repairs a unit within a
    tick, so a job that breaks and self-fixes every night looks exactly like one
    that has never broken — cirrus-modelhealth was repaired on 4 separate days in
    two weeks and nobody saw a pattern. Hiding healed rows, which is the obvious
    reading of "don't show it if it was corrected", would have made that
    permanent. Repetition has to be the loud part.
    """
    import json as _j
    from datetime import datetime as _dt, timedelta as _td
    global _sudo_cat, TODAY
    ok = fail = 0

    def ck(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1; print("  ok   " + name)
        else:
            fail += 1; print("  FAIL " + name)

    today = _dt.now().strftime("%Y-%m-%d")

    def ago(n):
        return (_dt.now() - _td(days=n)).strftime("%Y-%m-%d")

    ledger = [
        # A one-off blip, healed. This is the 2026-09-02 pedagogy case.
        {"ts": f"{today} 09:06:12", "event": "check", "tool": "heartbeat",
         "detail": "heartbeat found an issue: failed units: cirrus-pedagogy.service"},
        {"ts": f"{today} 09:06:45", "event": "action", "tool": "restart_service",
         "detail": "cirrus-pedagogy.service", "result": "restarted"},
        # Healed, but a habit: 4 distinct days inside the window.
        {"ts": f"{today} 05:30:40", "event": "check", "tool": "heartbeat",
         "detail": "heartbeat found an issue: failed units: cirrus-modelhealth.service"},
        {"ts": f"{today} 05:31:02", "event": "action", "tool": "restart_service",
         "detail": "cirrus-modelhealth.service", "result": "restarted"},
        {"ts": f"{ago(2)} 05:31:00", "event": "action", "tool": "restart_service",
         "detail": "cirrus-modelhealth.service", "result": "restarted"},
        {"ts": f"{ago(6)} 05:31:00", "event": "action", "tool": "reset_failed",
         "detail": "cirrus-modelhealth.service", "result": "ok"},
        {"ts": f"{ago(8)} 05:31:00", "event": "action", "tool": "restart_service",
         "detail": "cirrus-modelhealth.service", "result": "restarted"},
        # Flagged and deliberately NOT repaired — Skywarden judged it
        # non-transient. 58 of these in the real ledger, zero restarts.
        {"ts": f"{today} 11:19:00", "event": "check", "tool": "heartbeat",
         "detail": "cirrus-hoaleads.service: FAILED (exit code 2), not a transient issue"},
        # Outside the window: must NOT count toward recurrence.
        {"ts": f"{ago(40)} 05:31:00", "event": "action", "tool": "restart_service",
         "detail": "cirrus-modelhealth.service", "result": "restarted"},
    ]
    saved_cat, saved_today = _sudo_cat, TODAY
    try:
        _sudo_cat = lambda p: ("\n".join(_j.dumps(r) for r in ledger)
                               if "ledger" in str(p) else "")
        TODAY = today
        out = "\n".join(gather_skywarden())
    finally:
        _sudo_cat, TODAY = saved_cat, saved_today

    ck("a healed one-off says so, with a time", "healed 09:06 by Skywarden" in out)
    ck("...and is NOT hidden (a healed failure is still a failure)",
       "cirrus-pedagogy.service" in out)
    ck("...and is not shouted about — no RECURRING on a one-off",
       out.count("RECURRING") == 1)
    # THE ONE THAT MATTERS.
    ck("a healed-but-REPEATING fault is called out as recurring",
       "repaired on 4 of the last 14 days" in out and "RECURRING" in out)
    ck("an unrepaired issue is marked still-open, not healed",
       "NOT repaired" in out)
    ck("every anomaly line carries a clock", out.count("⚠️ [") == 3)
    ck("a repair older than the window does not inflate the count",
       "5 of the last 14" not in out)
    print()
    print("all daily-brief selftests passed" if not fail else f"{fail} FAILED")
    return 1 if fail else 0


if __name__ == "__main__":
    # T57: the subcommand is argv[0], never `"--selftest" in sys.argv`.
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        raise SystemExit(selftest())
    main()
