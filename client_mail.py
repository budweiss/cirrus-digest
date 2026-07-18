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
import mimetypes
import smtplib
import sys
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from pathlib import Path

PROJECT_DIR = Path.home() / "projects/cirrus-digest"
CC_ADDR = "Buddy.Weiss@outlook.com"


def _safe_in_project(rel: str) -> Path:
    p = (PROJECT_DIR / rel).resolve()
    if not str(p).startswith(str(PROJECT_DIR.resolve())):
        raise SystemExit("ERROR: file must be inside the project dir")
    if not p.exists():
        raise SystemExit(f"ERROR: file not found: {rel}")
    return p


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: client_mail.py <sender_name> <body_file> [attachment]")
        return 2

    name, body_rel = sys.argv[1].strip().lower(), sys.argv[2]
    attach_rel = sys.argv[3].strip() if len(sys.argv) > 3 and sys.argv[3].strip() else ""

    senders = json.loads((PROJECT_DIR / "config/intake_senders.json").read_text())
    entry = senders.get(name)
    if not isinstance(entry, dict) or not entry.get("emails"):
        print(f"ERROR: '{name}' not found in intake_senders.json — refusing to send")
        return 1
    to_addr = entry["emails"][0]

    body = _safe_in_project(body_rel).read_text()
    subject = ""
    if body.lower().startswith("subject:"):
        first, _, rest = body.partition("\n")
        subject = first.split(":", 1)[1].strip()
        body = rest.lstrip("\n")
    subject = subject or "A note from CIRRUS"

    creds = json.loads((PROJECT_DIR / "config/credentials.json").read_text())
    from_email = creds["outlook_email"]   # legacy-misnamed: the Gmail sender
    password = creds["outlook_password"]

    if attach_rel:
        msg = MIMEMultipart("mixed")
        msg.attach(MIMEText(body))
        apath = _safe_in_project(attach_rel)
        ctype, _ = mimetypes.guess_type(apath.name)
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        part = MIMEBase(maintype, subtype or "octet-stream")
        part.set_payload(apath.read_bytes())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=apath.name)
        msg.attach(part)
    else:
        msg = MIMEText(body)

    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_addr
    msg["Cc"] = CC_ADDR

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=60) as server:
        server.ehlo(); server.starttls(); server.ehlo()
        server.login(from_email, password)
        server.sendmail(from_email, [to_addr, CC_ADDR], msg.as_string())

    print(f"sent '{subject}' to {name} (cc Buddy)"
          + (f" with attachment {Path(attach_rel).name}" if attach_rel else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
