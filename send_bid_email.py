#!/usr/bin/env python3
"""
send_bid_email.py — send an email with attachments, reusing CIRRUS's working
SMTP config (the same one send_digest.py uses for the daily digest).

Run FROM ~/projects/cirrus-digest/ so `from send_digest import ...` works.
Usage: python3 send_bid_email.py <to> <subject> <body_file> <attach...>
"""
import os, sys
from pathlib import Path

import mailer
from send_digest import CREDS, FROM_EMAIL, FROM_PASS

def main():
    to, subject, body_file = sys.argv[1], sys.argv[2], sys.argv[3]
    attachments = sys.argv[4:]
    # Optional CC via env (added Session 35): CC_EMAIL=addr python3 send_bid_email.py ...
    cc = os.environ.get("CC_EMAIL", "").strip()
    mailer.send(FROM_EMAIL, FROM_PASS, to, subject,
                Path(body_file).read_text(), cc=cc, attachments=attachments,
                creds=CREDS)
    print(f"SENT from {FROM_EMAIL} to {to} | subject: {subject} | "
          f"{len(attachments)} attachment(s)")

if __name__ == "__main__":
    main()
