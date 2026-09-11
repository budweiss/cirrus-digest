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
from email.utils import getaddresses
from pathlib import Path

HERE = Path(__file__).resolve().parent
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 45
SENT_BOXES = ('"[Gmail]/Sent Mail"', '"Sent Items"', "Sent")


def _identity():
    """Who is this box, derived from the box -- not from ambient config.

    S145. The first version used node_info.node_name(), which reads TARGET_ENV
    and defaults to "dev" -> "CIRRUS". Services set that variable; an ssh
    invocation does not. So the CUMULUS probe cheerfully reported "CIRRUS", the
    aggregator printed "boxes answered: CIRRUS, CIRRUS", and every CUMULUS-sent
    email was labelled as coming from CIRRUS.

    That is the same mistake as the incident this command exists for: trusting a
    label instead of the thing it names. hostname is a property of the machine,
    and the MAILBOX is what actually matters -- the bug was never "which host",
    it was "which mailbox did you look in".
    """
    import socket
    return socket.gethostname()


def main():
    out = {"box": _identity(), "ok": False, "error": None, "sends": []}
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

        # S146. Every client name we KNOW about travels with the result, not just
        # the ones that happen to have mail in the window. The report needs the
        # difference to say "no send in window" -- which is the one finding this
        # whole command exists to surface, and the first version could not make
        # it: a client with zero sends simply did not appear.
        out["recipients"] = sorted({n for n, _ in recipients})
        if ids:
            st, chunk = M.fetch(",".join(i.decode() for i in ids),
                                "(BODY.PEEK[HEADER.FIELDS (DATE SUBJECT TO CC)])")
            for part in chunk or []:
                if not isinstance(part, tuple):
                    continue
                msg = email.message_from_bytes(part[1])
                # S146. Match on parsed ADDRESSES, and take EVERY match.
                #
                # The first version did `next(a for a in by_addr if a in dests)`
                # over a raw header substring. Two bugs in one line: it stopped at
                # the first match in dict order, and Buddy is cc'd on every client
                # email -- so if his key preceded a client's in intake_senders.json,
                # every one of that client's emails was attributed to Buddy and the
                # client read as having heard nothing. That is this command
                # reporting the exact false "abandoned" signal it was built to
                # prevent. Substring also let bill@x.com match notbill@x.com.
                dests = [a.lower() for _, a in
                         getaddresses([msg.get("To") or "", msg.get("Cc") or ""])]
                hits = [a for a in by_addr if a in dests]
                # Buddy is on everything; he is never the reason an email exists.
                clients = [a for a in hits if by_addr[a] != "buddy"]
                hits = clients or hits
                if not hits:
                    continue
                try:
                    subj = str(make_header(decode_header(msg.get("Subject") or "")))
                except Exception:
                    subj = (msg.get("Subject") or "")[:120]
                for hit in hits:
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
