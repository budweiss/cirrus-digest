#!/usr/bin/env python3
"""
CIRRUS client mail — send a staged file to a named intake sender, cc Buddy.
==========================================================================
EXTERNAL SEND: only ever invoked via the runner on Buddy's explicit ask
(same policy as bill-update). Safety rails:
- Recipient must be a sender defined in config/intake_senders.json — the
  address itself never appears in Cowork/git/chat.
- Buddy is ALWAYS cc'd.
- Body comes from a file already deployed to CIRRUS (reviewable in git).

Usage:  python3 client_mail.py <sender_name> <body_file> [subject]
  sender_name: key in intake_senders.json (e.g. alyssa)
  body_file:   path relative to ~/projects/cirrus-digest (e.g.
               mail/Alyssa-intro.md). First line "Subject: ..." is used as
               the subject (and stripped) unless [subject] is given.
"""

import json
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

PROJECT_DIR = Path.home() / "projects/cirrus-digest"
CC_ADDR = "Buddy.Weiss@outlook.com"


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: client_mail.py <sender_name> <body_file> [subject]")
        return 2

    name, body_rel = sys.argv[1].strip().lower(), sys.argv[2]
    subject = sys.argv[3] if len(sys.argv) > 3 else ""

    senders = json.loads((PROJECT_DIR / "config/intake_senders.json").read_text())
    entry = senders.get(name)
    if not isinstance(entry, dict) or not entry.get("emails"):
        print(f"ERROR: '{name}' not found in intake_senders.json — refusing to send")
        return 1
    to_addr = entry["emails"][0]

    body_path = (PROJECT_DIR / body_rel).resolve()
    if not str(body_path).startswith(str(PROJECT_DIR.resolve())):
        print("ERROR: body file must be inside the project dir")
        return 1
    body = body_path.read_text()

    if body.lower().startswith("subject:"):
        first, _, rest = body.partition("\n")
        if not subject:
            subject = first.split(":", 1)[1].strip()
        body = rest.lstrip("\n")
    subject = subject or "A note from CIRRUS"

    creds = json.loads((PROJECT_DIR / "config/credentials.json").read_text())
    from_email = creds["outlook_email"]   # legacy-misnamed: the Gmail sender
    password = creds["outlook_password"]

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_addr
    msg["Cc"] = CC_ADDR

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=60) as server:
        server.ehlo(); server.starttls(); server.ehlo()
        server.login(from_email, password)
        server.sendmail(from_email, [to_addr, CC_ADDR], msg.as_string())

    print(f"sent '{subject}' to {name} (cc Buddy)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
