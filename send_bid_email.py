#!/usr/bin/env python3
"""
send_bid_email.py — send an email with attachments, reusing CIRRUS's working
SMTP config (the same one send_digest.py uses for the daily digest).

Run FROM ~/projects/cirrus-digest/ so `from send_digest import ...` works.
Usage: python3 send_bid_email.py <to> <subject> <body_file> <attach...>
"""
import os, smtplib, sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from pathlib import Path
from send_digest import SMTP_SERVER, SMTP_PORT, FROM_EMAIL, FROM_PASS, FROM_NAME

def main():
    to, subject, body_file = sys.argv[1], sys.argv[2], sys.argv[3]
    attachments = sys.argv[4:]
    msg = MIMEMultipart()
    msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["To"] = to
    # Optional CC via env (added Session 35): CC_EMAIL=addr python3 send_bid_email.py ...
    cc = os.environ.get("CC_EMAIL", "").strip()
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg.attach(MIMEText(Path(body_file).read_text(), "plain"))
    for p in attachments:
        data = Path(p).read_bytes()
        part = MIMEApplication(data, Name=Path(p).name)
        part["Content-Disposition"] = f'attachment; filename="{Path(p).name}"'
        msg.attach(part)
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as s:
        s.starttls()
        s.login(FROM_EMAIL, FROM_PASS)
        s.send_message(msg)
    print(f"SENT from {FROM_EMAIL} to {to} | subject: {subject} | "
          f"{len(attachments)} attachment(s)")

if __name__ == "__main__":
    main()
