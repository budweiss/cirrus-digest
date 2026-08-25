#!/usr/bin/env python3
"""
Client mail — send a staged file to a named intake sender, cc Buddy.
=====================================================================
Runs on whichever box the thread belongs to. The From address and the
signature come from that box's own credentials.json (mail_from_name), so a
reply always comes back from where the client wrote to (Buddy, 2026-08-25).
EXTERNAL SEND: only ever invoked via the runner on Buddy's explicit ask
(same policy as bill-update). Safety rails:
- Recipient must be a sender defined in config/intake_senders.json — the
  address itself never appears in Cowork/git/chat.
- Buddy is ALWAYS cc'd.
- Body comes from a file already deployed to CIRRUS (reviewable in git).
- Optional attachment must also live inside the project dir (reviewable).

Usage:  python3 client_mail.py <sender_name> <body_file> [attachment]
  sender_name: key in intake_senders.json (e.g. alyssa)
  body_file:   path relative to ~/projects/cirrus-digest (e.g.
               mail/Alyssa-intro.md). First line "Subject: ..." is used as
               the subject (and stripped).
  attachment:  optional path relative to the project dir (e.g.
               mail/Guide.docx) to attach to the message.
"""

import json
import sys
from pathlib import Path

import mailer

# Resolve from THIS file, not a hardcoded home-relative path: the app lives at
# ~/projects/cirrus-digest on CIRRUS and /home/buddy/cirrus-digest on CUMULUS,
# so the old constant made this module unrunnable on CUMULUS — which is where
# a client whose thread arrived there has to be answered from (S77).
PROJECT_DIR = Path(__file__).resolve().parent
CC_ADDR = "Buddy.Weiss@outlook.com"


def _safe_in_project(rel: str) -> Path:
    p = (PROJECT_DIR / rel).resolve()
    if not str(p).startswith(str(PROJECT_DIR.resolve())):
        raise SystemExit("ERROR: file must be inside the project dir")
    if not p.exists():
        raise SystemExit(f"ERROR: file not found: {rel}")
    return p


def main() -> int:
    if len([a for a in sys.argv[1:] if a != "--dry-run"]) < 2:
        print("usage: client_mail.py <sender_name> <body_file> [attachment] [--dry-run]")
        return 2

    argv = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry_run = "--dry-run" in sys.argv
    name, body_rel = argv[0].strip().lower(), argv[1]
    attach_rel = argv[2].strip() if len(argv) > 2 and argv[2].strip() else ""

    senders = json.loads((PROJECT_DIR / "config/intake_senders.json").read_text())
    entry = senders.get(name)
    if not isinstance(entry, dict) or not entry.get("emails"):
        print(f"ERROR: '{name}' not found in intake_senders.json — refusing to send")
        return 1
    to_addr = entry["emails"][0]

    attach = _safe_in_project(attach_rel) if attach_rel else None
    creds = json.loads((PROJECT_DIR / "config/credentials.json").read_text())
    from_email = creds["outlook_email"]   # legacy-misnamed: the Gmail sender
    password = creds["outlook_password"]

    body = _safe_in_project(body_rel).read_text()
    subject = ""
    if body.lower().startswith("subject:"):
        first, _, rest = body.partition("\n")
        subject = first.split(":", 1)[1].strip()
        body = rest.lstrip("\n")
    # Not a hardcoded box name either: the fallback subject follows the sender.
    subject = subject or f"A note from {mailer.sender_name(from_email, creds)}"

    if dry_run:
        # An external send is irreversible, so the FROM LINE gets read before
        # it goes, not inferred from config (S77).
        mailer.send(from_email, password, to_addr, subject, body,
                    cc=CC_ADDR, attachments=[attach] if attach else None,
                    creds=creds, dry_run=True)
        print("  --- body (first 15 lines) ---")
        for line in body.splitlines()[:15]:
            print(f"  {line}")
        return 0

    # S78 — `client=name` is what puts this send in the promise ledger. This is
    # the path a human stages by hand, and it went unwatched until now: the
    # 2026-08-25 workbook email offered Bill a further New Castle backlog and
    # the ledger showed "0 open promises" the whole time it sat in his inbox.
    # A promise made deliberately is no less a promise than one a model wrote.
    mailer.send(from_email, password, to_addr, subject, body,
                cc=CC_ADDR, attachments=[attach] if attach else None,
                creds=creds, client=name,
                project=(entry.get("projects") or ["general"])[0])

    print(f"sent '{subject}' to {name} (cc Buddy)"
          + (f" with attachment {Path(attach_rel).name}" if attach_rel else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
