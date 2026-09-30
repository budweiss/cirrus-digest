#!/usr/bin/env python3
"""cumulus_inbox_peek.py — READ-ONLY raw peek at the CUMULUS mailbox.

Companion to cirrus-repo/gmail_inbox_peek.py (which reads cirrustask@gmail.com
on CIRRUS). Runs ON the CUMULUS box via the runner command `cumulus-gmail-inbox`,
because a client's thread lives on the box they wrote to (S77 rule) and intake's
--peek only sees a 3-day UID window — anything older (or from a sender who typed
their address differently) is invisible to it.

What gmail-inbox does for CIRRUS, this does for the CUMULUS client mailbox:
  list  recent headers (any sender)
  from  <substring>              headers + bodies of that sender's messages
Nothing is marked read (readonly select + BODY.PEEK). Credentials are loaded
server-side (sources.json account + credentials.json credential_key, honoring
INTAKE_ACCOUNT_LABEL) and are never printed.

Usage:
  python3 cumulus_inbox_peek.py list [count]
  python3 cumulus_inbox_peek.py from <substring> [body_chars]
"""
import email
import email.utils
import imaplib
import json
import os
import re
import sys
from email.header import decode_header
from pathlib import Path

REPO = Path(__file__).resolve().parent
CONFIG_PATH = REPO / "config" / "sources.json"
CREDS_PATH = REPO / "config" / "credentials.json"
ACCOUNT_LABEL = os.environ.get("INTAKE_ACCOUNT_LABEL", "cumulus-research")


def dec(s):
    out = ""
    for t, enc in decode_header(s or ""):
        out += t.decode(enc or "utf-8", "ignore") if isinstance(t, bytes) else t
    return re.sub(r"\s+", " ", out).strip()


def _clip(text, limit):
    """Truncate loudly — S79 lesson: a silent clip hides the interesting part."""
    if len(text) <= limit:
        return text
    return (text[:limit] +
            f"\n\n[TRUNCATED — {len(text)} chars total, showing {limit}. "
            f"Re-run with a larger limit: cumulus-gmail-inbox args limit]")


def body_text(msg, limit):
    """Best-effort plain-text body, clipped to `limit` with a visible marker."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and \
               "attachment" not in str(part.get("Content-Disposition", "")):
                try:
                    return _clip(part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "ignore"), limit)
                except Exception:
                    pass
        return "(no text/plain part)"
    try:
        return _clip(msg.get_payload(decode=True).decode(
            msg.get_content_charset() or "utf-8", "ignore"), limit)
    except Exception:
        return "(could not decode body)"


def load_creds():
    # S182: the effective config is the tracked file PLUS the box's untracked
    # overlays — intake.py reads it through runtime_config.load_sources, and so
    # must this (raw JSON misses the real account entirely).
    sys.path.insert(0, str(REPO))
    try:
        from runtime_config import load_sources
        config = load_sources(CONFIG_PATH)
    except Exception:
        config = json.loads(CONFIG_PATH.read_text())
    creds = json.loads(CREDS_PATH.read_text())
    account = next((a for a in config.get("email", {}).get("accounts", [])
                    if a.get("label") == ACCOUNT_LABEL), None)
    if not account:
        sys.exit(f"ERROR: no '{ACCOUNT_LABEL}' account in {CONFIG_PATH}")
    password = creds.get(account.get("credential_key", ""), "")
    if not password:
        sys.exit("ERROR: no credential for the intake account")
    return account, password


def connect():
    account, password = load_creds()
    mail = imaplib.IMAP4_SSL(account["imap_server"],
                             account.get("imap_port", 993), timeout=60)
    mail.login(account["address"], password)
    typ, data = mail.select("inbox", readonly=True)
    if typ != "OK":
        sys.exit("ERROR: could not select inbox")
    total = data[0].decode()
    print(f"INBOX {account['address']}: {total} total message(s)")
    return mail


def list_recent(mail, count=25):
    _, data = mail.search(None, "ALL")
    ids = data[0].split()
    count = max(1, min(int(count), 100))
    print(f"Most recent (newest first):\n")
    for i in reversed(ids[-count:]):
        _, md = mail.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM DATE SUBJECT)])")
        raw = b"".join(p[1] for p in md if isinstance(p, tuple))
        msg = email.message_from_bytes(raw)
        print(f"  {dec(msg.get('Date',''))} | {dec(msg.get('From',''))[:44]:44s} | "
              f"{dec(msg.get('Subject',''))[:60]}")


def show_from(mail, from_sub, body_chars=3000):
    typ, data = mail.search(None, "FROM", f'"{from_sub}"')
    ids = data[0].split()
    if not ids:
        print(f"No messages from '{from_sub}'.")
        return
    print(f"{len(ids)} message(s) from '{from_sub}' — showing the last 3 with bodies:\n")
    for i in reversed(ids[-3:]):
        _, md = mail.fetch(i, "(BODY.PEEK[])")
        raw = b"".join(p[1] for p in md if isinstance(p, tuple))
        msg = email.message_from_bytes(raw)
        print("=" * 70)
        print("From:   ", dec(msg.get("From", "")))
        print("Date:   ", dec(msg.get("Date", "")))
        print("Subject:", dec(msg.get("Subject", "")))
        print("-" * 70)
        print(body_text(msg, body_chars).strip())
        print()


def selftest():
    failures = []

    def check(label, ok):
        print(("  PASS  " if ok else "  FAIL  ") + label)
        if not ok:
            failures.append(label)

    check("clip short passes through", _clip("hello", 100) == "hello")
    clipped = _clip("x" * 5000, 3000)
    check("clip long truncates with marker", clipped.startswith("x" * 3000)
          and "TRUNCATED" in clipped)
    check("dec collapses newlines", dec("a\nb\r\nc") == "a b c")
    check("dec None-safe", dec(None) == "")
    return 1 if failures else 0


def main(argv):
    mode = argv[1] if len(argv) > 1 else "list"
    if mode == "selftest":
        return selftest()
    if not (CONFIG_PATH.exists() and CREDS_PATH.exists()):
        sys.exit(f"ERROR: missing config under {REPO}/config/ (untracked on the box)")
    mail = connect()
    try:
        if mode == "list":
            list_recent(mail, argv[2] if len(argv) > 2 and argv[2].isdigit() else 25)
        elif mode == "from":
            if len(argv) < 3:
                sys.exit("usage: cumulus_inbox_peek.py from <substring> [body_chars]")
            body_chars = 3000
            if len(argv) > 3 and argv[3].isdigit():
                body_chars = int(argv[3])
            show_from(mail, argv[2], body_chars)
        else:
            sys.exit(f"unknown mode '{mode}' (list | from | selftest)")
    finally:
        try:
            mail.logout()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    if not re.fullmatch(r"cumulus_inbox_peek\.py", Path(__file__).name):
        sys.exit("unexpected rename")
    sys.exit(main(sys.argv))