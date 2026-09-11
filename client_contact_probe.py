#!/usr/bin/env python3
"""client_contact_probe.py — when did each client last hear from THIS box?

S144, 2026-09-10. Written the evening a session concluded a client had gone
five weeks without contact, sent him an apology saying so, and was wrong: it had
searched the cirrustask mailbox only, while the emails in question had gone from
cumulus@cumulustask.com. Bill had in fact been mailed four days earlier, and the
"missing" deliverable had been sent on 2026-08-25 with the same attachment.

The lesson is NOT "remember there are two mailboxes." It is that asking one of
them looks exactly like asking all of them: you get a clean, plausible, complete
-looking answer with no indication that half the evidence was never consulted.

So this prints ONE BOX'S view and says which box it is. The aggregator
(runner `client-contact`) refuses to report at all unless every box answered --
see its comment. A partial contact history is not a contact history.

Reads config/credentials.json on the box it runs on; prints dates, recipients
and subjects only. No credential ever leaves this process.
"""
import imaplib
import email
import json
import sys
from email.header import decode_header, make_header
from pathlib import Path

HERE = Path(__file__).resolve().parent
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 45
SENT_BOXES = ('"[Gmail]/Sent Mail"', '"Sent Items"', "Sent")


def _node():
    try:
        sys.path.insert(0, str(HERE))
        import node_info
        return node_info.node_name()
    except Exception:
        return "UNKNOWN"


def main():
    out = {"box": _node(), "ok": False, "error": None, "sends": []}
    try:
        creds = json.loads((HERE / "config/credentials.json").read_text())
        user, pw = creds["outlook_email"], creds["outlook_password"]
        out["mailbox"] = user                      # the address, never the password
        recipients = []
        try:
            senders = json.loads((HERE / "config/intake_senders.json").read_text())
            for name, e in senders.items():
                for addr in (e.get("emails") or []) if isinstance(e, dict) else []:
                    recipients.append((name, addr))
        except Exception as exc:
            out["error"] = f"intake_senders unreadable: {type(exc).__name__}"
            print(json.dumps(out)); return 1

        M = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=60)
        M.login(user, pw)
        selected = None
        for b in SENT_BOXES:
            if M.select(b, readonly=True)[0] == "OK":
                selected = b
                break
        if not selected:
            out["error"] = "no Sent folder found"
            print(json.dumps(out)); return 1

        from datetime import datetime, timedelta
        since = (datetime.now() - timedelta(days=DAYS)).strftime("%d-%b-%Y")

        # ONE search for the window, then match recipients locally. The first
        # version issued a separate IMAP SEARCH per recipient, which is a
        # round-trip each and took long enough that the runner timed out before
        # the command returned.
        st, data = M.search(None, f"SINCE {since}")
        ids = data[0].split() if data and data[0] else []
        by_addr = {a.lower(): n for n, a in recipients}
        if ids:
            st, chunk = M.fetch(",".join(i.decode() for i in ids),
                                "(BODY.PEEK[HEADER.FIELDS (DATE SUBJECT TO CC)])")
            for part in chunk or []:
                if not isinstance(part, tuple):
                    continue
                msg = email.message_from_bytes(part[1])
                dests = ((msg.get("To") or "") + "," + (msg.get("Cc") or "")).lower()
                hit = next((a for a in by_addr if a and a in dests), None)
                if not hit:
                    continue
                try:
                    subj = str(make_header(decode_header(msg.get("Subject") or "")))
                except Exception:
                    subj = (msg.get("Subject") or "")[:120]
                out["sends"].append({
                    "client": by_addr[hit], "to": hit,
                    "date": (msg.get("Date") or "").strip(),
                    "subject": subj[:120],
                })
        M.logout()
        out["ok"] = True
    except Exception as exc:
        # Name the failure. An empty list from a box that could not be reached
        # is the exact shape of the bug this file exists to prevent.
        out["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    print(json.dumps(out))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
