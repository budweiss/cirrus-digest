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
"""
import json
import sys
import urllib.request
from pathlib import Path

DIGEST_DIR = Path.home() / "projects/cirrus-digest"
sys.path.insert(0, str(DIGEST_DIR))
import job_status  # noqa: E402

CREDS = json.loads((DIGEST_DIR / "config/credentials.json").read_text())


def telegram(text):
    try:
        tok = CREDS["telegram_bot_token"]
        chat = CREDS["telegram_user_id"]
        data = json.dumps({"chat_id": int(chat), "text": text,
                           "parse_mode": "Markdown"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage", data=data,
            headers={"Content-Type": "application/json",
                     "User-Agent": "CirrusJobsCheck/1.0"})
        urllib.request.urlopen(req, timeout=30).read()
        return True
    except Exception as e:
        print("telegram send failed:", e)
        return False


def main():
    report = "--report" in sys.argv
    lines, all_ok = job_status.summarize()
    body = "\n".join(lines) if lines else "(no jobs recorded yet)"
    print(body)
    if all_ok and not report:
        print("all scheduled jobs healthy — no alert sent.")
        return
    header = "✅ *CIRRUS jobs* — all ran clean" if all_ok else "⚠️ *CIRRUS jobs need attention*"
    telegram(header + "\n" + body)


if __name__ == "__main__":
    main()
