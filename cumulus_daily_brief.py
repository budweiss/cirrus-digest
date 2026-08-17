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
from datetime import datetime
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


def gather_skywarden():
    """Skywarden's own day: ledger rows (what it did/checked) + today's spend."""
    rows = []
    for line in _sudo_cat(SKY_STATE_DIR / "ledger.jsonl").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if str(r.get("ts", "")).startswith(TODAY):
            rows.append(r)

    issues = [r for r in rows if "issue" in str(r.get("event", "")).lower()
              or "issue" in str(r.get("detail", "")).lower()]
    guidance = [r for r in rows if str(r.get("event", "")) in
                ("guidance-request", "opus-request", "guidance", "opus_upgrade")]
    checks = [r for r in rows if r not in issues and r not in guidance]

    lines = [f"  {len(checks)} routine check(s), {len(issues)} anomaly/issue "
             f"flag(s), {len(guidance)} escalation(s) to you"]
    for r in issues[:5]:
        lines.append(f"  ⚠️ {str(r.get('detail', ''))[:100]}")
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
    print(send_telegram(body, creds))


if __name__ == "__main__":
    main()
