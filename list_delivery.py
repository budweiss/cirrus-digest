#!/usr/bin/env python3
"""list_delivery.py — deliver a reviewed contact list when Buddy replies SEND (S266).

The route, Buddy's decision of 2026-09-23:
  client asks for a list with contact details (intake: task_solver.wants_deliverable,
  narrowed here by wants_contact_list) -> honest ack -> contact_list.py runs on
  this box -> its review email to Buddy carries a [CL:<run id>] tag and says
  "reply SEND" -> Buddy's SEND reply reaches intake -> handle_send_reply()
  delivers the workbook to the client, from this box, cc Buddy.

What this module guarantees, because a client email cannot be unsent:
  * ONE delivery per run id. The ledger is checked before sending and written
    after; a second SEND gets a note to Buddy, not a second email.
  * Anything unclear is HELD, and Buddy is told why: a reply that is not a
    plain SEND, a missing run, no client on record, an unknown client, a
    missing workbook. Holding is never silent.
  * The recipient comes from intake_senders.json by client key, never from
    anything in the reply.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
RUNS_DIR = PROJECT_DIR / "logs/contact_lists"
SENDERS_PATH = PROJECT_DIR / "config/intake_senders.json"
CC_ADDR = "Buddy.Weiss@outlook.com"
REVIEW_SUBJECT = "Contact list ready for review"

TAG_RX = re.compile(r"\[CL:([A-Za-z0-9][A-Za-z0-9._-]{0,63})\]")
_NO = {"not", "dont", "don", "no", "hold", "wait", "stop", "cancel", "never", "later",
       "before", "after", "if", "unless", "change", "fix", "remove", "add", "but"}


def run_tag(run_id: str) -> str:
    return "[CL:%s]" % run_id


def _n(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split())


def wants_contact_list(subject: str, body: str) -> bool:
    """Narrower than task_solver.wants_deliverable (which matches any list or
    file ask): the research job only knows how to find ORGANIZATIONS and their
    contact details, so it launches only when the ask names both a list and a
    contact field. Bill 2026-09-23: 'the name, address and point of contact of
    every new home builder ... excel' -> yes. 'Resend last week's workbook' -> no."""
    t = " %s %s " % (_n(subject), _n(body))
    listy = re.search(r" (every|all the|list of|a list|directory|mailing labels?) ", t)
    contact = re.search(r" (address|addresses|point of contact|contact info|contact information|"
                        r"contacts|phone|phone numbers?|emails?|e mail|mailing labels?) ", t)
    return bool(listy and contact)


def parse_send(subject: str, own_words: str):
    """The run id when this is a plain SEND to a review email, else None.

    `own_words` is the reply with our quoted email already cut off
    (task_solver.strip_quoted_reply). Deliberately strict: the reply must START
    with 'send', be short, and carry no word that could mean 'not yet'. 'Send it
    after you fix row 4' is a conversation, not an approval, and is held."""
    m = TAG_RX.search(subject or "")
    if not m:
        return None
    words = _n(own_words).split()
    if not words or words[0] != "send" or len(words) > 8:
        return None
    if any(w in _NO for w in words):
        return None
    return m.group(1)


def _ledger(runs_dir: Path) -> Path:
    return runs_dir / "deliveries.jsonl"


def delivered(run_id: str, runs_dir: Path = RUNS_DIR):
    """The ledger row if this run was already delivered, else None."""
    p = _ledger(runs_dir)
    if not p.exists():
        return None
    for line in p.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("run_id") == run_id and row.get("event") == "delivered":
            return row
    return None


def plural(noun: str) -> str:
    """The planner names the kind in the singular ('marina', 'new home
    builder'); the note reads 'marinas'. S267 E2E: 'the list you asked for:
    marina in Sussex County' went out to Buddy's test inbox."""
    n = (noun or "").strip()
    if not n or n.lower().endswith("s"):
        return n
    if n.endswith("y") and n[-2:-1].lower() not in "aeiou":
        return n[:-1] + "ies"
    return n + "s"


def cover_note(client: str, meta: dict, summary: dict, box: str) -> tuple:
    """(subject, body) of the delivery email. New client-facing wording (S266):
    shown to Buddy with this build."""
    subj = meta.get("thread_subject") or "Your list"
    subj = subj if subj.lower().startswith("re:") else "Re: " + subj
    lines = ["Hi %s," % client.capitalize(), "",
             "Here's the list you asked for: %s in %s. It's attached as an Excel "
             "workbook." % (plural(summary.get("entity", "organization")), summary.get("region", "your area")),
             ""]
    groups = summary.get("groups") or []
    if groups:
        lines.append("It's split into " + " and ".join(
            "%s (%d)" % (g, n) for g, n in groups) + ".")
    lines += ["Each row has the company, a contact where one is published, and a "
              "mailing address, with the web page each came from. The last tab "
              "explains how to print mailing labels from it.", "",
              "— %s (Buddy's assistant)" % box]
    return subj, "\n".join(lines)


def _record_reply(client: str, thread_subject: str, run_id: str, project_dir: Path = PROJECT_DIR):
    """The self-changes ledger row client_watch.stalled_threads counts as a
    reply (S268). Keyed on the client's OWN subject, the same key intake wrote
    for the request, so the thread closes."""
    import client_promises
    import dev_loop
    dev_loop.ledger_append({"event": "list-delivered", "requester": client,
                            "thread": client_promises.thread_key(thread_subject or ""),
                            "run_id": run_id}, project_dir)


def deliver(run_id: str, creds: dict, senders: dict, runs_dir: Path = RUNS_DIR,
            send=None, now=None, record=None) -> dict:
    """Send run `run_id`'s client workbook to its client. Returns
    {"sent": bool, "reason": str, "client": str}. Never raises for a
    hold-worthy condition; those come back as sent=False with the reason."""
    run_dir = runs_dir / run_id
    res = {"sent": False, "reason": "", "client": ""}
    prior = delivered(run_id, runs_dir)
    if prior:
        res["reason"] = "already delivered %s to %s" % (prior.get("ts", "?"), prior.get("client", "?"))
        return res
    try:
        meta = json.loads((run_dir / "meta.json").read_text())
    except (OSError, ValueError):
        res["reason"] = "no client on record for run %s (meta.json missing)" % run_id
        return res
    client = (meta.get("client") or "").strip().lower()
    entry = senders.get(client) if client else None
    res["client"] = client
    if not isinstance(entry, dict) or not entry.get("emails"):
        res["reason"] = "client %r is not in intake_senders.json" % client
        return res
    xlsx = run_dir / ("contact-list-%s-client.xlsx" % run_id)
    if not xlsx.exists():
        res["reason"] = "client workbook missing: %s" % xlsx.name
        return res
    try:
        summary = json.loads((run_dir / "summary.json").read_text())
    except (OSError, ValueError):
        summary = {}
    import mailer
    send = send or mailer.send
    from_email = creds.get("outlook_email", "")
    box = mailer.sender_name(from_email, creds)
    subj, body = cover_note(client, meta, summary, box)
    send(from_email, creds.get("outlook_password", ""), entry["emails"][0], subj, body,
         cc=CC_ADDR, attachments=[str(xlsx)], creds=creds, client=client,
         project=(entry.get("projects") or ["general"])[0])
    with _ledger(runs_dir).open("a") as f:
        f.write(json.dumps({"event": "delivered", "run_id": run_id, "client": client,
                            "subject": subj, "file": xlsx.name,
                            "ts": (now or datetime.now()).isoformat(timespec="seconds")}) + "\n")
    try:
        (record or _record_reply)(client, meta.get("thread_subject"), run_id)
    except Exception as e:
        # After the send, so it can never block a delivery; but loud, because a
        # missing row makes the client_watch stall check call this unanswered.
        res["record_error"] = str(e)
    res.update(sent=True, reason="delivered to %s" % client)
    return res


def handle_send_reply(subject: str, own_words: str, creds: dict, senders: dict = None,
                      runs_dir: Path = RUNS_DIR, send=None, log=print, record=None):
    """Intake's entry point for a message from BUDDY. Returns None when the
    message is not a reply to a review email (intake carries on as normal),
    else the deliver() result. A reply to a review email that is not a plain
    SEND is held, and Buddy is told, so a hesitant reply is never read as a yes."""
    m = TAG_RX.search(subject or "")
    if not m:
        return None
    import mailer
    send = send or mailer.send
    senders = senders if senders is not None else json.loads(SENDERS_PATH.read_text())
    run_id = parse_send(subject, own_words)
    if run_id is None:
        res = {"sent": False, "client": "",
               "reason": "reply to %s was not a plain SEND, so nothing was sent" % run_tag(m.group(1))}
    else:
        res = deliver(run_id, creds, senders, runs_dir, send=send, record=record)
    log("  list_delivery %s: %s" % (m.group(1), res["reason"]))
    if res.get("record_error"):
        log("  ⚠️ list_delivery: delivered, but the ledger row failed (%s); "
            "client_watch will call this thread unanswered" % res["record_error"])
    if not res["sent"]:
        send(creds.get("outlook_email", ""), creds.get("outlook_password", ""), CC_ADDR,
             "Held: %s %s" % (REVIEW_SUBJECT.lower(), run_tag(m.group(1))),
             "Nothing was sent to the client.\n\nWhy: %s.\n\nTo deliver it, reply to the "
             "review email with just: SEND" % res["reason"],
             creds=creds, on_error="false", watch_promises=False)
    return res


# ── starting the research job (intake calls this for a contact-list ask) ──

REQUESTS_REL = "logs/contact_lists/requests"
# Each run spends $1-3 of Brave searches from a $25/month cap that every other
# job shares; a client's tenth list of the day would starve the digests.
MAX_JOBS_PER_CLIENT_PER_DAY = 2


class DailyCap(Exception):
    pass


def new_run_id(client: str, now=None) -> str:
    who = re.sub(r"[^a-z0-9]", "", (client or "").lower())[:12] or "client"
    return "cl-%s-%s" % (who, (now or datetime.now()).strftime("%Y%m%d-%H%M%S"))


def start_job(client: str, request_text: str, thread_subject: str, message_id: str,
              project_dir: Path = PROJECT_DIR, popen=None, now=None, extra_args=()) -> str:
    """Write the request and its client/thread record, then launch
    contact_list.py through job_runner, detached, so intake's poll returns at
    once. Returns the run id. Raises DailyCap past the per-client cap."""
    import subprocess
    import sys
    now = now or datetime.now()
    run_id = new_run_id(client, now)
    req_dir = project_dir / REQUESTS_REL
    req_dir.mkdir(parents=True, exist_ok=True)
    who = run_id.rsplit("-", 2)[0] + "-" + now.strftime("%Y%m%d")
    if len(list(req_dir.glob(who + "-*.txt"))) >= MAX_JOBS_PER_CLIENT_PER_DAY:
        raise DailyCap("%s already has %d list job(s) today" % (client, MAX_JOBS_PER_CLIENT_PER_DAY))
    (req_dir / (run_id + ".txt")).write_text(request_text.strip() + "\n")
    (req_dir / (run_id + ".json")).write_text(json.dumps({
        "client": client, "thread_subject": thread_subject, "message_id": message_id,
        "requested": now.isoformat(timespec="seconds")}, indent=1))
    venv = project_dir / ".venv/bin/python"
    py = str(venv) if venv.exists() else sys.executable
    (popen or subprocess.Popen)(
        [py, "-u", "job_runner.py", run_id, "contact_list.py",
         "--request", "%s/%s.txt" % (REQUESTS_REL, run_id), "--id", run_id,
         "--meta", "%s/%s.json" % (REQUESTS_REL, run_id)] + list(extra_args),
        cwd=str(project_dir), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)
    return run_id


# ── selftest (offline: temp dirs, a fake send, no network, no live files) ──

def selftest() -> bool:
    import tempfile
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  PASS  " if cond else "  FAIL  ") + name)
        ok = ok and bool(cond)

    bill = ("I need the name, address and point of contact of every new home builder in "
            "the state of Delaware. Separate the list by builders currently building "
            "communities and builder that are not. Format the list in an excel spread "
            "sheet so it can be used to print mailing labels.")
    check("Bill's real 2026-09-23 request is a contact list", wants_contact_list("Re: Delaware development leads", bill))
    check("a resend of a workbook is not", not wants_contact_list("workbook", "Can you resend last week's workbook?"))
    check("a spreadsheet of snow totals is not", not wants_contact_list("", "Put the snow totals in a spreadsheet"))

    subj = "Re: %s %s: new home builder in Delaware" % (REVIEW_SUBJECT, run_tag("cl-20260923-1"))
    check("a plain SEND reply yields the run id", parse_send(subj, "SEND") == "cl-20260923-1")
    check("'Send it' counts", parse_send(subj, "Send it.") == "cl-20260923-1")
    check("'send after you fix row 4' is held", parse_send(subj, "send after you fix row 4") is None)
    check("'don't send yet' is held", parse_send(subj, "Don't send yet") is None)
    check("'Looks good' (no SEND) is held", parse_send(subj, "Looks good") is None)
    check("no tag in the subject -> not ours", parse_send("Re: something else", "SEND") is None)

    sent = []

    def fake_send(frm, pw, to, subject, body, **kw):
        sent.append({"to": to, "subject": subject, "body": body, "kw": kw})
        return True

    creds = {"outlook_email": "cumulus@cumulustask.com", "outlook_password": "x"}
    senders = {"bill": {"emails": ["bill@example.com"], "projects": ["property-management"]}}
    with tempfile.TemporaryDirectory() as td:
        runs = Path(td)
        rd = runs / "cl-20260923-1"
        rd.mkdir()
        (rd / "meta.json").write_text(json.dumps({"client": "bill",
                                                  "thread_subject": "Delaware development leads"}))
        (rd / "summary.json").write_text(json.dumps({"entity": "new home builder", "region": "Delaware",
                                                     "groups": [["Building Communities", 30],
                                                                ["Not Building Communities", 29]]}))
        (rd / "contact-list-cl-20260923-1-client.xlsx").write_bytes(b"x")
        recorded = []
        rec = lambda c, t, r: recorded.append((c, t, r))
        r = handle_send_reply(subj, "SEND", creds, senders, runs, send=fake_send, log=lambda m: None,
                              record=rec)
        check("a delivery writes the reply row client_watch counts, keyed on the client's subject",
              recorded == [("bill", "Delaware development leads", "cl-20260923-1")])
        check("SEND delivers to the client on record, cc Buddy, with the workbook",
              r["sent"] and sent[-1]["to"] == "bill@example.com" and sent[-1]["kw"]["cc"] == CC_ADDR
              and sent[-1]["kw"]["attachments"][0].endswith("-client.xlsx"))
        check("the note says 'new home builders', not 'new home builder'",
              "list you asked for: new home builders in Delaware" in sent[-1]["body"])
        check("plural(): marina -> marinas, property company -> property companies, HOAs stays",
              plural("marina") == "marinas" and plural("property company") == "property companies"
              and plural("HOAs") == "HOAs" and plural("survey") == "surveys")
        check("the delivery threads on the client's subject and names the split",
              sent[-1]["subject"] == "Re: Delaware development leads"
              and "Building Communities (30)" in sent[-1]["body"] and "CUMULUS" in sent[-1]["body"])
        n = len(sent)
        r2 = handle_send_reply(subj, "SEND", creds, senders, runs, send=fake_send, log=lambda m: None,
                               record=rec)
        check("a second SEND does not send twice (or record twice); Buddy is told instead",
              not r2["sent"] and "already delivered" in r2["reason"] and len(recorded) == 1
              and len(sent) == n + 1 and sent[-1]["to"] == CC_ADDR)
        r3 = handle_send_reply(subj.replace("cl-20260923-1", "cl-missing"), "SEND", creds, senders,
                               runs, send=fake_send, log=lambda m: None)
        check("an unknown run is held and Buddy is told",
              not r3["sent"] and "meta.json missing" in r3["reason"] and sent[-1]["to"] == CC_ADDR)
        r4 = handle_send_reply(subj, "hold on", creds, senders, runs, send=fake_send, log=lambda m: None)
        check("a hesitant reply is held and Buddy is told",
              r4 is not None and not r4["sent"] and "not a plain SEND" in r4["reason"])
        check("a message that is not a reply to a review email is left to intake",
              handle_send_reply("Re: Delaware development leads", "SEND", creds, senders, runs,
                                send=fake_send, log=lambda m: None) is None)
        rd2 = runs / "cl-x"
        rd2.mkdir()
        (rd2 / "meta.json").write_text(json.dumps({"client": "mallory"}))
        (rd2 / "contact-list-cl-x-client.xlsx").write_bytes(b"x")
        r5 = deliver("cl-x", creds, senders, runs, send=fake_send, record=rec)
        check("a client missing from the allowlist is held", not r5["sent"] and "not in intake_senders" in r5["reason"])
        launched = []

        def fake_popen(args, **kw):
            launched.append((args, kw))

        proj = Path(td) / "app"
        proj.mkdir()
        t0 = datetime(2026, 9, 23, 17, 30, 0)
        rid = start_job("bill", bill, "Re: Delaware development leads", "<m1@x>", proj, fake_popen, t0)
        args, kw = launched[-1]
        rq = json.loads((proj / REQUESTS_REL / (rid + ".json")).read_text())
        check("start_job writes the request + client record and launches contact_list via job_runner",
              rid == "cl-bill-20260923-173000" and TAG_RX.search(run_tag(rid))
              and args[2:5] == ["job_runner.py", rid, "contact_list.py"]
              and "--meta" in args and kw["start_new_session"] and rq["client"] == "bill"
              and rq["message_id"] == "<m1@x>")
        start_job("bill", bill, "s", "<m2@x>", proj, fake_popen, datetime(2026, 9, 23, 17, 31, 0))
        try:
            start_job("bill", bill, "s", "<m3@x>", proj, fake_popen, datetime(2026, 9, 23, 17, 32, 0))
            capped = False
        except DailyCap:
            capped = True
        check("a third list job for one client in a day is refused (search budget)", capped and len(launched) == 2)
        start_job("bill", bill, "s", "<m4@x>", proj, fake_popen, datetime(2026, 9, 24, 8, 0, 0))
        check("...and the cap resets the next day", len(launched) == 3)
    print("selftest: %s" % ("OK" if ok else "FAILED"))
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
