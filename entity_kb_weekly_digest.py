"""entity_kb_weekly_digest.py — Monday weekly digest of a week's entity_kb
changes for a client (S65). Buddy's ask: on Mondays, Bill should get
everything the CRM's daily research found that week, regardless of lead
status — separate from any event-triggered hot-lead alert. Generic across
projects, not HOA-specific: extend WEEKLY_DIGEST_RECIPIENTS below for a new
client using entity_kb.py on their own project(s), no logic change needed.

Usage:
  python3 entity_kb_weekly_digest.py --client bill [--dry-run]
  python3 entity_kb_weekly_digest.py selftest
"""
import argparse
import json
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import entity_kb

PROJECT_DIR = Path.home() / "projects/cirrus-digest"
CREDS_PATH = PROJECT_DIR / "config/credentials.json"
CC_ADDR = "Buddy.Weiss@outlook.com"

# Per-client digest config: which entity_kb project(s) to summarize + who
# gets the email. Extend this dict, not the digest logic, for a new client.
WEEKLY_DIGEST_RECIPIENTS = {
    "bill": {
        "to": "whutchins@knightpropertysvs.com",
        "kb_projects": ["hoa_leads_bill"],
        "label": "Delaware HOA research",
    },
    # S66: Buddy's own new-project shortlist (business_idea_scan.py), not a
    # client -- "to" is Buddy himself, so CC_ADDR below just double-lists him
    # (harmless, same address in To and Cc).
    "buddy-business": {
        "to": "Buddy.Weiss@outlook.com",
        "kb_projects": ["business_ideas"],
        "label": "AI-buildable business ideas",
    },
}


def compose_digest(kb_projects: list, since: str, db_path: str = None) -> str:
    """Builds the plain-text digest body from entity_kb events across one
    or more projects since a given timestamp. Returns '' if there's
    genuinely nothing to report -- caller decides whether to send at all
    (a client should never get an empty "nothing happened" email)."""
    lines = []
    total_events = 0
    for kb_project in kb_projects:
        events = entity_kb.get_events(kb_project, since=since, db_path=db_path)
        if not events:
            continue
        total_events += len(events)
        by_entity = {}
        for ev in events:
            by_entity.setdefault(ev["slug"], {"name": ev["name"], "events": []})
            by_entity[ev["slug"]]["events"].append(ev)
        for _slug, group in sorted(by_entity.items(), key=lambda kv: kv[1]["name"]):
            lines.append(group["name"])
            for ev in sorted(group["events"], key=lambda e: e["occurred_at"]):
                date = (ev.get("occurred_at") or "")[:10]
                if ev["event_type"] == "signal":
                    lines.append(f"  - {date}: {ev.get('summary', '')}")
                else:
                    lines.append(f"  - {date}: {ev['field']} updated")
            lines.append("")

    if total_events == 0:
        return ""
    plural = "s" if total_events != 1 else ""
    header = f"This week's research findings ({total_events} update{plural}):\n\n"
    return header + "\n".join(lines).strip()


def _send_mail(from_email: str, password: str, to_addr: str, cc_addr: str,
               subject: str, body: str) -> bool:
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = from_email
        msg["To"] = to_addr
        if cc_addr:
            msg["Cc"] = cc_addr
        recipients = [to_addr] + ([cc_addr] if cc_addr else [])
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=60) as server:
            server.ehlo(); server.starttls(); server.ehlo()
            server.login(from_email, password)
            server.sendmail(from_email, recipients, msg.as_string())
        return True
    except Exception:
        return False


def run(client: str, dry_run: bool = False, db_path: str = None) -> dict:
    cfg = WEEKLY_DIGEST_RECIPIENTS.get(client)
    if not cfg:
        return {"sent": False, "reason": f"no digest config for client '{client}'"}

    since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00")
    body = compose_digest(cfg["kb_projects"], since, db_path=db_path)
    if not body:
        return {"sent": False, "reason": "nothing to report this week"}

    subject = f"Weekly {cfg['label']} update — {datetime.now():%Y-%m-%d}"
    if dry_run:
        print(f"SUBJECT: {subject}\n\n{body}")
        return {"sent": False, "reason": "dry-run"}

    try:
        creds = json.loads(CREDS_PATH.read_text())
    except Exception as e:
        return {"sent": False, "reason": f"no creds: {e}"}

    ok = _send_mail(creds.get("outlook_email", ""), creds.get("outlook_password", ""),
                    cfg["to"], CC_ADDR, subject, body)
    return {"sent": ok, "reason": "" if ok else "send failed"}


def selftest() -> bool:
    import os
    import tempfile

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)
    checks = []
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        old = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

        # baseline create (no event), then a field change dated 30 days ago,
        # then a signal dated today
        entity_kb.upsert_entity("hoa_leads_bill", "test-hoa", "Test HOA",
                                fields={"county": "Sussex"}, db_path=db_path)
        entity_kb.upsert_entity("hoa_leads_bill", "test-hoa", "Test HOA",
                                fields={"county": "Kent"}, occurred_at=old, db_path=db_path)
        entity_kb.add_signal("hoa_leads_bill", "test-hoa", "distress",
                             "Pool closed this week", occurred_at=now, db_path=db_path)

        since_week = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00")
        body_week = compose_digest(["hoa_leads_bill"], since_week, db_path=db_path)
        checks.append(("this week's digest includes the recent signal",
                       "Pool closed" in body_week))
        checks.append(("this week's digest excludes the 30-day-old field change",
                       "county updated" not in body_week))

        since_far_past = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d 00:00:00")
        body_wide = compose_digest(["hoa_leads_bill"], since_far_past, db_path=db_path)
        checks.append(("a wider window includes the older field change too",
                       "county updated" in body_wide and "Pool closed" in body_wide))

        empty_body = compose_digest(["nonexistent_project"], since_week, db_path=db_path)
        checks.append(("no events in window -> empty digest, not a garbage send",
                       empty_body == ""))

        result = run("nobody-configured", dry_run=True, db_path=db_path)
        checks.append(("unconfigured client is refused, not silently sent",
                       result["sent"] is False and "no digest config" in result["reason"]))
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)

    all_ok = all(ok for _, ok in checks)
    for desc, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
    return all_ok


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(0 if selftest() else 1)

    parser = argparse.ArgumentParser()
    parser.add_argument("--client", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    outcome = run(args.client, dry_run=args.dry_run)
    print(outcome)
    sys.exit(0 if outcome.get("sent") or args.dry_run else 1)
