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
  attachment:  optional path(s) relative to the project dir (e.g.
               mail/Guide.docx), comma-separated for more than one, to
               attach to the message.
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


def _record_sent(name: str, subject: str, body_rel: str, project_dir: Path = None) -> None:
    """S268: the self-changes ledger row client_watch.stalled_threads counts as
    a substantive reply. Before this, a reply a session staged and sent by hand
    left no row, so the thread read as unanswered (Bill's 2026-09-23 builder
    list would have been flagged REPLY EXPECTED two days after he got it).
    Keyed by thread_key(subject), so a staged reply closes the client's thread
    only when it keeps the client's own subject."""
    import client_promises
    import dev_loop
    dev_loop.ledger_append({"event": "client-mail-sent", "requester": name,
                            "thread": client_promises.thread_key(subject),
                            "file": body_rel}, project_dir or PROJECT_DIR)


def _parse_attach_list(attach_rel: str) -> list:
    """Comma-separated attachment paths -> a clean list (empty if none).
    Pulled out of main() (S232) so the multi-attachment parsing has
    something to test without a network or real credentials."""
    return [a.strip() for a in (attach_rel or "").split(",") if a.strip()]


def main() -> int:
    if len([a for a in sys.argv[1:] if a != "--dry-run"]) < 2:
        print("usage: client_mail.py <sender_name> <body_file> [attachment] [--dry-run]")
        return 2

    argv = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry_run = "--dry-run" in sys.argv
    name, body_rel = argv[0].strip().lower(), argv[1]
    attach_rel = argv[2].strip() if len(argv) > 2 and argv[2].strip() else ""
    # S232: comma-separated so a single send can carry more than one
    # attachment (e.g. two alternative diagrams) -- mailer.send() already
    # accepts a list, this CLI just never exposed more than one slot.
    attach_rels = _parse_attach_list(attach_rel)

    senders = json.loads((PROJECT_DIR / "config/intake_senders.json").read_text())
    entry = senders.get(name)
    if not isinstance(entry, dict) or not entry.get("emails"):
        print(f"ERROR: '{name}' not found in intake_senders.json — refusing to send")
        return 1
    to_addr = entry["emails"][0]

    attach = [_safe_in_project(a) for a in attach_rels] if attach_rels else None
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
                    cc=CC_ADDR, attachments=attach,
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
                cc=CC_ADDR, attachments=attach,
                creds=creds, client=name,
                project=(entry.get("projects") or ["general"])[0])

    try:
        _record_sent(name, subject, body_rel)
    except Exception as e:
        # After the send: never blocks it, but said out loud, because a missing
        # row makes the stall check call this thread unanswered.
        print(f"WARNING: sent, but the reply row was not recorded ({e}); "
              f"client_watch will call this thread unanswered")
    print(f"sent '{subject}' to {name} (cc Buddy)"
          + (f" with attachment(s) {', '.join(Path(a).name for a in attach_rels)}"
             if attach_rels else ""))
    return 0


def selftest() -> int:
    """Offline: no network, no credentials, no real send. Covers the S232
    multi-attachment parsing and the path-traversal guard it now runs
    per-segment instead of once."""
    import tempfile
    fails = 0

    def check(name, cond):
        nonlocal fails
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        fails += 0 if cond else 1

    with tempfile.TemporaryDirectory() as td:
        _record_sent("bill", "Re: Delaware development leads - nothing new", "mail/x.md", Path(td))
        row = json.loads((Path(td) / "logs/self-changes/ledger.jsonl").read_text().splitlines()[-1])
        check("_record_sent: a hand-sent reply writes the row client_watch counts, on the client's thread",
              row["event"] == "client-mail-sent" and row["requester"] == "bill"
              and row["thread"] == "delaware development leads nothing new")
    check("_parse_attach_list: a single path",
          _parse_attach_list("mail/a.png") == ["mail/a.png"])
    check("_parse_attach_list: two paths, comma-separated",
          _parse_attach_list("mail/a.png,mail/b.png") == ["mail/a.png", "mail/b.png"])
    check("_parse_attach_list: whitespace around commas is stripped",
          _parse_attach_list("mail/a.png, mail/b.png , mail/c.png")
          == ["mail/a.png", "mail/b.png", "mail/c.png"])
    check("_parse_attach_list: empty string -> [] (not ['']), so "
          "attach=None downstream, not attach=[Path('')]",
          _parse_attach_list("") == [])
    check("_parse_attach_list: None -> []", _parse_attach_list(None) == [])
    check("_parse_attach_list: a trailing comma adds no blank entry",
          _parse_attach_list("mail/a.png,") == ["mail/a.png"])

    # _safe_in_project must still refuse an escape attempt and a missing
    # file even now that it's called once per comma-separated segment
    # instead of once total (S232's actual new risk: a list comprehension
    # silently dropping a bad segment instead of refusing the whole send).
    global PROJECT_DIR
    _orig_project_dir = PROJECT_DIR
    tmp = Path(tempfile.mkdtemp())
    (tmp / "mail").mkdir()
    (tmp / "mail" / "ok.png").write_bytes(b"fake-image-bytes")
    try:
        PROJECT_DIR = tmp
        check("_safe_in_project: a real in-project file resolves",
              _safe_in_project("mail/ok.png").exists())
        try:
            _safe_in_project("../../etc/passwd")
            escaped = True
        except SystemExit:
            escaped = False
        check("_safe_in_project: a path that escapes the project dir is refused",
              not escaped)
        try:
            _safe_in_project("mail/does-not-exist.png")
            missing_ok = True
        except SystemExit:
            missing_ok = False
        check("_safe_in_project: a missing file is refused, not silently skipped",
              not missing_ok)
    finally:
        PROJECT_DIR = _orig_project_dir

    return 0 if fails == 0 else 1


if __name__ == "__main__":
    if "selftest" in sys.argv[1:]:
        sys.exit(selftest())
    sys.exit(main())
