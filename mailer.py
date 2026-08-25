#!/usr/bin/env python3
"""
mailer — the one place this app sends mail from.
================================================
Before S77 the same SMTP block was written out eight times across seven files.
Each copy drifted: two had no socket timeout (a hang with no deadline), three
defaulted the From display name to the literal "CIRRUS" (so a CUMULUS box
signed its client mail as the wrong machine), and two swallowed every failure
with a bare `except: return False`, so a client email that never arrived looked
exactly like one that did.

Consolidating them buys three things a wrapper library would not:
  * ONE place that decides the From identity — see sender_name(), which
    implements Buddy's 2026-08-25 rule that a reply is signed by the box the
    client wrote to, never by a hardcoded default.
  * ONE place that logs a failed send, so "it didn't arrive" is answerable.
  * ONE dry-run path, so an external send can be inspected before it goes.

Deliberately NOT a general mail library: it does what these seven callers do
and nothing more.
"""

import mimetypes
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_TIMEOUT = 60          # every caller gets a deadline; two used to have none

_BOXES = ("cumulus", "stratus", "cirrus")


def sender_name(from_email: str, creds: dict = None) -> str:
    """Display name for the From header.

    Buddy, 2026-08-25: a client reply is signed by the BOX the client wrote to,
    never as Buddy and never as the other box. The sending address IS that
    identity -- cumulus@cumulustask.com can only be CUMULUS -- so it is the
    ground truth here, not a config value that can go stale. An explicit
    mail_from_name is used only when the address names no known box.

    The old code was `creds.get("mail_from_name", "CIRRUS")`: a hardcoded
    default that signed CUMULUS's mail as CIRRUS wherever the key was unset.
    """
    creds = creds or {}
    configured = str(creds.get("mail_from_name") or "").strip()
    addr = (from_email or "").lower()
    for box in _BOXES:
        if box in addr:
            return box.upper()
    return configured or "ASSISTANT"


def _as_list(v) -> list:
    if not v:
        return []
    return [v] if isinstance(v, str) else [x for x in v if x]


def _attach_file(msg, path):
    p = Path(path)
    ctype, _ = mimetypes.guess_type(p.name)
    maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
    part = MIMEBase(maintype, subtype or "octet-stream")
    part.set_payload(p.read_bytes())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=p.name)
    msg.attach(part)


def build(from_email, to, subject, body, cc=None, html=None,
          attachments=None, from_name=True, creds=None):
    """Compose the message. Separated from the send so a caller (and the
    selftest) can inspect exactly what would go out without a network."""
    to_list, cc_list = _as_list(to), _as_list(cc)
    attachments = _as_list(attachments)

    if attachments:
        msg = MIMEMultipart("mixed")
        msg.attach(MIMEText(body))
        for a in attachments:
            _attach_file(msg, a)
    elif html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain"))
        msg.attach(MIMEText(html, "html"))
    else:
        msg = MIMEText(body)

    msg["Subject"] = subject
    # from_name=False preserves the bare-address From that the intake ack and
    # the two answer paths have always used -- consolidation must not quietly
    # restyle a header a client already recognises.
    msg["From"] = (f"{sender_name(from_email, creds)} <{from_email}>"
                   if from_name else from_email)
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    return msg, to_list + cc_list


def send(from_email, password, to, subject, body, cc=None, html=None,
         attachments=None, from_name=True, creds=None, dry_run=False,
         on_error="raise", log=print):
    """Send one message. Returns True on success.

    on_error="raise"  -- propagate (callers that must fail loudly: the daily
                         digest, the pedagogy digest, staged client mail).
    on_error="false"  -- log and return False (callers that have their own
                         fallback: the intake ack, the answer paths). The log
                         line is the point: the old bare `except: return False`
                         made a failed client send indistinguishable from a
                         successful one.
    """
    msg, recipients = build(from_email, to, subject, body, cc=cc, html=html,
                            attachments=attachments, from_name=from_name,
                            creds=creds)
    if dry_run:
        if log:
            log("DRY RUN — nothing sent")
            for h in ("From", "To", "Cc", "Subject"):
                if msg[h]:
                    log(f"  {h}: {msg[h]}")
            for a in _as_list(attachments):
                log(f"  Attachment: {Path(a).name}")
        return True
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=SMTP_TIMEOUT) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(from_email, password)
            s.sendmail(from_email, recipients, msg.as_string())
        return True
    except Exception as e:
        if log:
            log(f"mail send FAILED to {', '.join(recipients)}: "
                f"{type(e).__name__}: {e}")
        if on_error == "raise":
            raise
        return False


def selftest() -> int:
    failures = 0

    def check(name, cond):
        nonlocal failures
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            failures += 1

    check("cumulus address signs as CUMULUS",
          sender_name("cumulus@cumulustask.com") == "CUMULUS")
    check("cirrus address signs as CIRRUS",
          sender_name("cirrustask@gmail.com") == "CIRRUS")
    check("a stale mail_from_name never overrides the address",
          sender_name("cumulus@cumulustask.com",
                      {"mail_from_name": "CIRRUS"}) == "CUMULUS")
    check("an unrecognised address falls back to the configured name",
          sender_name("someone@example.com",
                      {"mail_from_name": "OFFER"}) == "OFFER")

    msg, rcpt = build("cumulus@cumulustask.com", "a@b.com", "Subj", "hello",
                      cc="c@d.com")
    check("From carries the derived box name",
          msg["From"] == "CUMULUS <cumulus@cumulustask.com>")
    check("cc is addressed AND put on the envelope",
          msg["Cc"] == "c@d.com" and rcpt == ["a@b.com", "c@d.com"])

    msg2, _ = build("cirrustask@gmail.com", "a@b.com", "S", "b", from_name=False)
    check("from_name=False keeps the bare address the ack has always used",
          msg2["From"] == "cirrustask@gmail.com")

    msg3, _ = build("cirrustask@gmail.com", ["a@b.com", "e@f.com"], "S", "plain",
                    html="<p>rich</p>")
    parts = msg3.get_payload()
    check("html builds multipart/alternative, plain part first",
          msg3.get_content_subtype() == "alternative" and len(parts) == 2
          and parts[0].get_content_type() == "text/plain"
          and parts[1].get_content_type() == "text/html")
    check("multiple To addresses are joined and enveloped",
          msg3["To"] == "a@b.com, e@f.com")

    import tempfile, os
    fd, p = tempfile.mkstemp(suffix=".csv")
    os.write(fd, b"a,b\n1,2\n")
    os.close(fd)
    try:
        msg4, _ = build("cirrustask@gmail.com", "a@b.com", "S", "body",
                        attachments=[p])
        names = [x.get_filename() for x in msg4.get_payload()]
        check("attachment rides along with its real filename",
              msg4.get_content_subtype() == "mixed" and Path(p).name in names)
    finally:
        os.unlink(p)

    sent = []
    ok = send("cumulus@cumulustask.com", "pw", "a@b.com", "S", "body",
              dry_run=True, log=sent.append)
    check("dry run sends nothing and shows the From line",
          ok and any("CUMULUS <cumulus@cumulustask.com>" in l for l in sent)
          and any("DRY RUN" in l for l in sent))

    logged = []
    ok = send("bad@nowhere.invalid", "pw", "a@b.com", "S", "b",
              on_error="false", log=logged.append)
    check("a failed send returns False AND says so in the log",
          ok is False and any("FAILED" in l for l in logged))

    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    import sys
    sys.exit(selftest())
