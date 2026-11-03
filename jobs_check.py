#!/usr/bin/env python3
"""
jobs_check.py  (S49, 2026-08-01)
===============================================================================
Second daily watch for CIRRUS scheduled jobs. Reads the job_status ledger and
Telegrams Buddy ONLY when something is overdue or failed (quiet when all good),
so the afternoon check is signal, not noise. The morning brief already surfaces
the same summary at 07:30, giving the once/twice-a-day coverage.

Usage:
  python3 jobs_check.py            # alert only if a job is overdue/failed
  python3 jobs_check.py --report   # always Telegram the full status (manual check)
  python3 jobs_check.py --selftest # offline checks of the message shape

Exit 1 when Telegram refused any part, so com.cirrus.jobscheck shows a failed
last exit and the watchdog alerts (S560).
"""
import json
import sys
import urllib.request
from pathlib import Path

DIGEST_DIR = Path.home() / "projects/cirrus-digest"
sys.path.insert(0, str(DIGEST_DIR))
import job_status  # noqa: E402

# Telegram refuses a sendMessage over 4096 chars; keep room for the header.
LIMIT = 3900


def compose(lines, all_ok, report):
    """Message parts for one alert. PURE.

    S560: the alert used to carry every row. At 63 rows it was 5,444 chars, so
    Telegram refused 15 of the 16:30 alerts from 10-17 on with HTTP 400, and
    the job still exited 0. An alert now names only the rows that are not green,
    plus a count; --report keeps every row. Either way the text is split on
    line boundaries, never cut."""
    header = "✅ *CIRRUS jobs* — all ran clean" if all_ok else "⚠️ *CIRRUS jobs need attention*"
    shown = list(lines) if report else [l for l in lines if not l.startswith("✅")]
    if len(shown) < len(lines):
        shown.append(f"✅ {len(lines) - len(shown)} other job(s) healthy")
    parts, cur = [], header
    for line in shown or ["(no jobs recorded yet)"]:
        line = line[:LIMIT]
        if len(cur) + 1 + len(line) > LIMIT:
            parts.append(cur)
            cur = line
        else:
            cur += "\n" + line
    return parts + [cur]


def _refusal(e):
    """Telegram's own reason for an HTTP error ("message is too long"), or ''."""
    try:
        return json.loads(e.read()).get("description", "")[:140]
    except Exception:
        return ""


def telegram(text):
    try:
        creds = json.loads((DIGEST_DIR / "config/credentials.json").read_text())
        tok = creds["telegram_bot_token"]
        chat = creds["telegram_user_id"]
        data = json.dumps({"chat_id": int(chat), "text": text,
                           "parse_mode": "Markdown"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage", data=data,
            headers={"Content-Type": "application/json",
                     "User-Agent": "CirrusJobsCheck/1.0"})
        urllib.request.urlopen(req, timeout=30).read()
        return True
    except Exception as e:
        # S161: job output breaks Markdown — a square-bracketed error line
        # ("Command '['/Library...") 400'd the 16:30 alert on 2026-09-11 and
        # it was LOST, the one day the alert mattered. Retry as plain text:
        # the alert matters more than the formatting. cirrus_bot.send_message
        # has carried this fallback for its own sends all along.
        try:
            data = json.dumps({"chat_id": int(chat), "text": text}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{tok}/sendMessage", data=data,
                headers={"Content-Type": "application/json",
                         "User-Agent": "CirrusJobsCheck/1.0"})
            urllib.request.urlopen(req, timeout=30).read()
            print("telegram: Markdown 400 — sent as plain text instead")
            return True
        except Exception as e2:
            why = _refusal(e2)
            print("telegram send failed:", e2, ("— " + why) if why else "")
            return False


def main():
    report = "--report" in sys.argv
    lines, all_ok = job_status.summarize()
    print("\n".join(lines) if lines else "(no jobs recorded yet)")
    if all_ok and not report:
        print("all scheduled jobs healthy — no alert sent.")
        return 0
    parts = compose(lines, all_ok, report)
    sent = sum(telegram(p) for p in parts)
    print(f"telegram: {sent} of {len(parts)} part(s) sent")
    return 0 if sent == len(parts) else 1


def selftest():
    fail = 0

    def ck(label, cond):
        nonlocal fail
        fail += not cond
        print(("PASS " if cond else "FAIL ") + label)

    green = [f"✅ job{i}: 2026-11-03T04:00 — " + "x" * 70 for i in range(60)]
    bad = ["⚠️ personalopinion: 2026-11-02T14:31 FAILED — status=failed",
           "☑️ stockdiscovery: 2026-11-01T09:33 failed, handled — gate unchanged",
           "• billsam (CUMULUS): no run recorded yet"]
    lines = green[:30] + bad + green[30:]
    ck("the live-sized ledger is over Telegram's cap as one message", len("\n".join(lines)) > 4096)
    alert = compose(lines, False, False)
    ck("an alert fits one message", len(alert) == 1 and len(alert[0]) <= LIMIT)
    ck("an alert keeps every row that is not green", all(b in alert[0] for b in bad))
    ck("an alert drops green rows and counts them",
       "job0:" not in alert[0] and "60 other job(s) healthy" in alert[0])
    report = compose(lines, False, True)
    ck("a full report splits into parts under the cap",
       len(report) > 1 and all(len(p) <= LIMIT for p in report))
    ck("a full report keeps every row exactly once, uncut",
       "\n".join(report).split("\n")[1:] == lines)
    ck("all-green --report says so", compose(green[:2], True, True)[0].startswith("✅ *CIRRUS jobs*"))
    print(f"{'ok' if not fail else str(fail) + ' failed'}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(selftest() if sys.argv[1:] == ["--selftest"] else main())
