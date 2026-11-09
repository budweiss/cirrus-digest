#!/usr/bin/env python3
"""list_delivery.py — deliver a client's contact list (S266; unattended since S592).

The route, Buddy's decisions of 2026-09-23 and 2026-11-08:
  client asks for a list with contact details (intake: task_solver.wants_deliverable,
  narrowed here by wants_contact_list, or a "same list for <place>" reply to a
  delivered list: same_list_request) -> honest ack -> contact_list.py runs on
  this box -> auto_deliver() sends the workbook to the client, from this box,
  cc Buddy, asking "Is this what you were looking for?" -> Buddy's copy carries
  a [CL:<run id>] tag and says it was delivered. A run that is held (not an
  intake request, nothing confirmed, the send failed) still waits for Buddy's
  SEND reply -> handle_send_reply() -> deliver().

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
DELIVERED_SUBJECT = "Contact list delivered"
FIT_CHECK = "Is this what you were looking for?"

TAG_RX = re.compile(r"\[CL:([A-Za-z0-9][A-Za-z0-9._-]{0,63})\]")
_NO = {"not", "dont", "don", "no", "hold", "wait", "stop", "cancel", "never", "later",
       "before", "after", "if", "unless", "change", "fix", "remove", "add", "but"}


_MOBILE_SIG = re.compile(r"\s*(get outlook for (ios|android)|sent from my (iphone|ipad|android phone))"
                         r"\s*(<[^>\s]*>)?\s*$", re.I)


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
    # Owner names and phone numbers describe supported contact research even
    # without "list". Require request language and organization/name scope.
    owner_contacts = (re.search(r" (looking for|need|provide|identify|compile|want) ", t)
                      and re.search(r" (company|companies|business|businesses) ", t)
                      and re.search(r" (owner|owners) ", t)
                      and re.search(r" names? ", t))
    return bool((listy or owner_contacts) and contact)


def parse_send(subject: str, own_words: str):
    """The run id when this is a plain SEND to a review email, else None.

    `own_words` is the reply with our quoted email already cut off
    (task_solver.strip_quoted_reply). Deliberately strict: the reply must START
    with 'send', be short, and carry no word that could mean 'not yet'. 'Send it
    after you fix row 4' is a conversation, not an approval, and is held."""
    m = TAG_RX.search(subject or "")
    if not m:
        return None
    # S592: Buddy's 11-08 "Send" from his phone ended with "Get Outlook for
    # iOS<https://aka.ms/o0ukef>", which pushed it past the word cap and held a
    # real approval. Drop a stock mobile signature only when it ends the reply.
    own_words = _MOBILE_SIG.sub("", own_words or "")
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
              FIT_CHECK, "",
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


def auto_deliver(run_id: str, creds: dict, senders: dict = None, runs_dir: Path = RUNS_DIR,
                 send=None, now=None, record=None) -> dict:
    """S592 (Buddy 11-08: client-request results go back without him): send a
    finished run straight to its client. deliver()'s result, plus "auto" (was
    the run eligible). Held, never raised, and the caller tells Buddy why, when
    the run was not started by intake for an unrefused request (meta
    auto_deliver), nothing was confirmed, or the send failed. deliver() still
    allows one delivery per run id."""
    res = {"sent": False, "reason": "", "client": "", "auto": False}
    try:
        meta = json.loads((runs_dir / run_id / "meta.json").read_text())
    except (OSError, ValueError):
        meta = {}
    if not isinstance(meta, dict) or meta.get("auto_deliver") is not True:
        res["reason"] = "this run was not started by intake for a client request"
        return res
    res["auto"] = True
    try:
        rows = int(json.loads((runs_dir / run_id / "summary.json").read_text()).get("rows") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        rows = 0
    if rows < 1:
        res["reason"] = "no organization was confirmed, so there is nothing to send"
        return res
    calls = []

    def tracked(*a, **k):
        if send is None:
            import mailer
            out = mailer.send(*a, **k)
        else:
            out = send(*a, **k)
        calls.append(1)
        return out

    try:
        senders = senders if senders is not None else json.loads(SENDERS_PATH.read_text())
        res.update(deliver(run_id, creds, senders, runs_dir, send=tracked, now=now, record=record))
    except Exception as e:
        # Class name only: an SMTP error can carry addresses.
        if calls:
            res.update(sent=True, reason="delivered, but the ledger row failed (%s); "
                                         "do not SEND again" % type(e).__name__)
        else:
            res["reason"] = "the send failed (%s), so nothing reached the client" % type(e).__name__
    return res


# ── "same list for <place>" (S592) ──
# Bill 11-08 11:22 replied to a delivered list with "same list for Sussex
# County": no list or contact wording, so both detectors missed it. A reply on a
# thread we delivered a list on, asking for the same list somewhere else, is a
# list request for the prior run's kind of organization in the new place.

# Up to five words between "same" and the place ("same info", "same list of
# snow plow operators"), but not "same time / day / way as ...".
_SAME_RX = re.compile(r"\b(?:the\s+)?(?:same|similar)\s+"
                      r"(?:(?!(?:time|day|days|week|month|year|as|to|that|rate|price)\b)[a-z]+\s+){0,5}?"
                      r"(?:but\s+)?(?:for|in|covering)\s+(?P<place>[^.?!\n;,]{2,80})", re.I)
_PLACE_END = re.compile(r"\s+(?:please|pls|too|also|as well|if|thanks|thank|asap|when|by|"
                        r"(?:in|as)\s+(?:an?\s+)?(?:excel|spreadsheet|pdf|csv|workbook|word)|"
                        r"and\s+(?:include|add|send|also|please|can|could|would|i|we|let))\b.*$", re.I)
_NOT_PLACE = {"me", "us", "him", "her", "them", "my", "our", "your", "his", "their", "now",
              "today", "tomorrow", "tonight", "next", "later", "reference", "review",
              "everyone", "everybody", "all", "that", "this", "it", "free", "sure", "a", "an",
              "excel", "spreadsheet", "pdf", "word", "csv", "email", "format"}
_ASK = {"can", "could", "would", "will", "please", "pls", "need", "want", "like", "do", "run",
        "send", "make", "get", "pull", "build", "create", "prepare", "give", "find", "provide",
        "compile", "repeat", "redo", "produce", "generate", "gather", "research", "search",
        "look", "put", "share", "email"}
_LEAD = {"the", "a", "and", "also", "now", "ok", "okay", "great", "thanks", "thank", "you",
         "hi", "hello", "next", "then", "so", "bill", "buddy"}
_NEG = {"not", "dont", "don", "no", "never", "already"}


def asks_same_list(own_words: str):
    """The place in "same list for <place>" when the client's own words ask for
    it, else None. 'I already got the same list for Kent' is not an ask."""
    text = own_words or ""
    for m in _SAME_RX.finditer(text):
        start = max(text.rfind(c, 0, m.start()) for c in ".!?\n") + 1
        lead = _n(text[start:m.start()]).split()
        # An ask: only filler before "the same", a request word, or one
        # imperative verb ("Compile the same ..."); not "We used the same ...".
        imperative = len(lead) == 1 and not lead[0].endswith("ed")
        if any(w in _NEG for w in lead) or not (set(lead) <= _LEAD or set(lead) & _ASK or imperative):
            continue
        place = " ".join(_PLACE_END.sub("", m.group("place")).split()[:6]).strip(" '\"")
        if _n(place) and _n(place).split()[0] not in _NOT_PLACE:
            return place
    return None


def prior_delivery(client: str, subject: str, body: str = "", runs_dir: Path = RUNS_DIR):
    """The ledgered delivery to `client` on this subject's thread that the reply
    answers: the one whose kind is quoted first in the body (the email replied
    to comes first in a quote chain), else the latest. None if the thread never
    had a list delivered."""
    import client_promises
    key = client_promises.thread_key(subject or "")
    p = _ledger(runs_dir)
    if not key or not p.exists():
        return None
    rows = []
    for line in p.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (isinstance(row, dict) and row.get("event") == "delivered" and row.get("run_id")
                and row.get("client") == client
                and client_promises.thread_key(row.get("subject") or "") == key):
            rows.append(row)
    if not rows:
        return None
    quoted, pos = _n(body), {}
    for row in rows:
        try:
            entity = _n(json.loads((runs_dir / row["run_id"] / "plan.json").read_text())["entity"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        hits = [quoted.find(t) for t in {entity, _n(plural(entity))} if t and t in quoted]
        if hits:
            pos[row["run_id"]] = min(hits)
    if pos:
        first = min(pos.values())
        rows = [r for r in rows if pos.get(r["run_id"]) == first]
    return max(rows, key=lambda r: r.get("ts") or "")


def same_list_request(client: str, subject: str, own_words: str, body: str = "",
                      runs_dir: Path = RUNS_DIR):
    """{"request", "prior_run", "place"} for a "same list for <place>" reply on a
    delivered-list thread, else None. The request names the prior run's kind
    of organization (plan.json entity) and split, in the new place."""
    place = asks_same_list(own_words)
    if not place:
        return None
    prior = prior_delivery(client, subject, body, runs_dir)
    if not prior:
        return None
    try:
        plan = json.loads((runs_dir / prior["run_id"] / "plan.json").read_text())
        entity = plan["entity"].strip()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if not entity:
        return None
    lines = ["The same list as before, for a new place: every %s in %s." % (entity, place),
             "For each one: the company name, a point of contact with their title, "
             "the phone number and the mailing address."]
    if plan.get("group_question") and plan.get("group_names"):
        lines.append("Split the list the same way as before: %s" % plan["group_question"])
    if plan.get("region"):
        # "Kent County" alone could be Michigan; the earlier list says which state.
        lines.append("The earlier list covered %s; this one covers %s only." % (plan["region"], place))
    lines.append('The client wrote: "%s"' % " ".join((own_words or "").split())[:300])
    return {"request": "\n".join(lines), "prior_run": prior["run_id"], "place": place}


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
              project_dir: Path = PROJECT_DIR, popen=None, now=None, extra_args=(),
              auto_deliver: bool = False, extra_meta: dict = None) -> str:
    """One launch per original client message, including manual prior runs.

    auto_deliver (S592): intake passes True for a request its gate did not
    refuse; the finished run then goes straight to the client (auto_deliver()).
    Every other launch keeps the review email and Buddy's SEND.

    Keep the reservation after an uncertain launch; never spend or send twice
    by treating a later poll as a new request. Different subjects are irrelevant.
    """
    import fcntl
    if not client or not message_id:
        raise ValueError('Original client and Message-ID required')
    req_dir = project_dir / REQUESTS_REL
    req_dir.mkdir(parents=True, exist_ok=True)
    with (req_dir / '.launch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        matches = matching_jobs(project_dir, client, message_id)
        if len(matches) > 1:
            raise ValueError('Ambiguous existing jobs; reconcile before execution')
        if matches:
            return next(iter(matches))
        return _start_job(client, request_text, thread_subject, message_id,
                          project_dir, popen, now, extra_args, auto_deliver, extra_meta)


def matching_jobs(project_dir, client, message_id):
    """Read-only exact request identity lookup; never match by subject alone."""
    runs = Path(project_dir) / 'logs/contact_lists'
    found = {}
    identities = {}
    paths = list((runs / 'requests').glob('*.json')) + list(runs.glob('*/meta.json'))
    for path in paths:
        meta = json.loads(path.read_text())
        if not isinstance(meta, dict):
            raise ValueError('Invalid contact job metadata')
        run_id = path.parent.name if path.name == 'meta.json' else path.stem
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', run_id):
            raise ValueError('Invalid contact job identity')
        identities.setdefault(run_id, set()).add((meta.get('client'), meta.get('message_id')))
        if meta.get('client') == client and meta.get('message_id') == message_id:
            found[run_id] = meta
    if any(len(identities[run_id]) != 1 for run_id in found):
        raise ValueError('Conflicting contact job identity; reconcile before execution')
    return found


def _start_job(client: str, request_text: str, thread_subject: str, message_id: str,
               project_dir: Path, popen, now, extra_args, auto_deliver=False,
               extra_meta=None) -> str:
    """Write the request and its client/thread record, then launch
    contact_list.py through job_runner, detached, so intake's poll returns at
    once. Returns the run id. Raises DailyCap past the per-client cap."""
    import subprocess
    import sys
    now = now or datetime.now()
    run_id = new_run_id(client, now)
    req_dir = project_dir / REQUESTS_REL
    req_dir.mkdir(parents=True, exist_ok=True)
    if (req_dir / (run_id + '.json')).exists():
        import hashlib
        run_id += '-' + hashlib.sha256(message_id.encode()).hexdigest()[:8]
    who = run_id.rsplit("-", 2)[0] + "-" + now.strftime("%Y%m%d")
    if len(list(req_dir.glob(who + "-*.txt"))) >= MAX_JOBS_PER_CLIENT_PER_DAY:
        raise DailyCap("%s already has %d list job(s) today" % (client, MAX_JOBS_PER_CLIENT_PER_DAY))
    (req_dir / (run_id + ".txt")).write_text(request_text.strip() + "\n")
    (req_dir / (run_id + ".json")).write_text(json.dumps(dict(extra_meta or {}, **{
        "client": client, "thread_subject": thread_subject, "message_id": message_id,
        "requested": now.isoformat(timespec="seconds"), "auto_deliver": bool(auto_deliver)}),
        indent=1))
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
    ios = "\n\nGet Outlook for iOS<https://aka.ms/o0ukef>"
    check("'Send' + Outlook iOS signature counts (S592)", parse_send(subj, "Send" + ios) == "cl-20260923-1")
    check("'Send' + Sent from my iPhone counts", parse_send(subj, "SEND\n\nSent from my iPhone") == "cl-20260923-1")
    check("signature does not rescue 'don't send yet'", parse_send(subj, "Don't send yet" + ios) is None)
    check("a bare signature is not a SEND", parse_send(subj, ios) is None)
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
        check("a launch without auto_deliver keeps the SEND step (manual runs)",
              json.loads((proj / REQUESTS_REL / (rid + ".json")).read_text())["auto_deliver"] is False)
        rid5 = start_job("bill", bill, "s", "<m5@x>", proj, fake_popen, datetime(2026, 9, 25, 8, 0, 0),
                         auto_deliver=True, extra_meta={"same_list_of": "cl-prior"})
        rq5 = json.loads((proj / REQUESTS_REL / (rid5 + ".json")).read_text())
        check("intake's launch records auto_deliver and the prior run; identity fields win",
              rq5["auto_deliver"] is True and rq5["same_list_of"] == "cl-prior"
              and rq5["client"] == "bill" and rq5["message_id"] == "<m5@x>")

        # ── S592: auto_deliver (Buddy 11-08: results go back without him) ──
        check("the cover note asks the fit question", FIT_CHECK in cover_note("bill", {}, {}, "CUMULUS")[1])
        auto = Path(td) / "auto"
        auto.mkdir()

        def mkrun(run_id, meta, rows=3, plan=None):
            d = auto / run_id
            d.mkdir()
            (d / "meta.json").write_text(json.dumps(meta))
            (d / "summary.json").write_text(json.dumps({"entity": "snow plow operator",
                                                        "region": "Philadelphia", "rows": rows,
                                                        "groups": []}))
            (d / ("contact-list-%s-client.xlsx" % run_id)).write_bytes(b"x")
            (d / "plan.json").write_text(json.dumps(plan or {"entity": "snow plow operator",
                                                             "region": "Philadelphia"}))
        intake_meta = {"client": "bill", "thread_subject": "Research", "auto_deliver": True}
        mkrun("cl-a1", intake_meta)
        n = len(sent)
        a1 = auto_deliver("cl-a1", creds, senders, auto, send=fake_send, record=rec)
        check("an intake run goes straight to the client, cc Buddy, workbook attached, fit question asked",
              a1["sent"] and a1["auto"] and len(sent) == n + 1 and sent[-1]["to"] == "bill@example.com"
              and sent[-1]["kw"]["cc"] == CC_ADDR and sent[-1]["kw"]["attachments"][0].endswith("cl-a1-client.xlsx")
              and FIT_CHECK in sent[-1]["body"] and sent[-1]["subject"] == "Re: Research")
        check("...and is ledgered, so Buddy's SEND on his copy is refused",
              delivered("cl-a1", auto) is not None
              and not handle_send_reply("Re: %s %s: x" % (DELIVERED_SUBJECT, run_tag("cl-a1")), "SEND",
                                        creds, senders, auto, send=fake_send, log=lambda m: None,
                                        record=rec)["sent"])
        n = len(sent)
        a2 = auto_deliver("cl-a1", creds, senders, auto, send=fake_send, record=rec)
        check("one delivery per run id: a second auto_deliver sends nothing",
              not a2["sent"] and "already delivered" in a2["reason"] and len(sent) == n)
        mkrun("cl-man", {"client": "bill", "thread_subject": "Research"})
        a3 = auto_deliver("cl-man", creds, senders, auto, send=fake_send, record=rec)
        check("a run intake did not start for an unrefused request (no auto_deliver) is held",
              not a3["sent"] and not a3["auto"] and len(sent) == n and delivered("cl-man", auto) is None)
        mkrun("cl-str", dict(intake_meta, auto_deliver="yes"))
        check("auto_deliver must be literally true", not auto_deliver("cl-str", creds, senders, auto,
                                                                       send=fake_send)["sent"])
        mkrun("cl-zero", intake_meta, rows=0)
        a4 = auto_deliver("cl-zero", creds, senders, auto, send=fake_send, record=rec)
        check("a run that confirmed nothing is held, not sent",
              not a4["sent"] and a4["auto"] and "nothing to send" in a4["reason"] and len(sent) == n)

        def boom(*a, **k):
            raise ConnectionError("smtp down for bill@example.com")
        mkrun("cl-fail", intake_meta)
        a5 = auto_deliver("cl-fail", creds, senders, auto, send=boom, record=rec)
        check("a failed send is held, ledgers nothing, and names only the error class",
              not a5["sent"] and "send failed (ConnectionError)" in a5["reason"]
              and "bill@" not in a5["reason"] and delivered("cl-fail", auto) is None)
        led = Path(td) / "badledger"
        led.mkdir()

        def send_then_break(*a, **k):
            fake_send(*a, **k)
            (led / "deliveries.jsonl").mkdir()      # so the ledger write after it fails
            return True
        d6 = led / "cl-led"
        d6.mkdir()
        for f in ("meta.json", "summary.json", "contact-list-cl-fail-client.xlsx"):
            (d6 / f.replace("cl-fail", "cl-led")).write_bytes((auto / "cl-fail" / f).read_bytes())
        n = len(sent)
        a6 = auto_deliver("cl-led", creds, senders, led, send=send_then_break, record=rec)
        check("a send that went out but could not be ledgered says so (never 'send failed')",
              a6["sent"] and "do not SEND again" in a6["reason"] and len(sent) == n + 1)

        # ── S592: "same list for <place>" replies to a delivered list ──
        yes = ["same list for Sussex County", "Same list for Sussex County please.",
               "Thanks! Can you do the same for Kent County?", "same thing in New Castle County too",
               "I'd like the same list for Sussex County, DE", "Could you also run the same list "
               "covering Cape May County and include emails",
               # the shape of Bill's real 11-08 reply: one verb, one noun, county + state
               "Get the same info for Sussex county Delaware.",
               "Compile the same list of snow plow operators for Kent County in excel",
               "Assemble the same list for Kent County"]
        got = [asks_same_list(t) for t in yes]
        check("'same list for <place>' asks yield the place: %s" % got,
              got == ["Sussex County", "Sussex County", "Kent County", "New Castle County",
                      "Sussex County", "Cape May County", "Sussex county Delaware", "Kent County",
                      "Kent County"])
        no = ["I already got the same list for Kent County from the county.",
              "We used the same list for Sussex County last year", "Don't do the same for Kent County",
              "Can you send the same list for me", "same list for now is fine",
              "Please put the same list in excel", "Send the list for Sussex County",
              "Plowed the same lots in Kent County", "Send it at the same time in the morning"]
        check("not an ask, or not a place: %s" % [asks_same_list(t) for t in no],
              all(asks_same_list(t) is None for t in no))
        thread = Path(td) / "thread"
        thread.mkdir()
        for rid_, ent, ts in (("cl-ops", "snow plow operator", "2026-11-08T11:19:00"),
                              ("cl-dlr", "snow plow dealer", "2026-11-08T10:38:00"),
                              ("cl-old", "home builder", "2026-09-24T10:00:00")):
            (thread / rid_).mkdir()
            (thread / rid_ / "plan.json").write_text(json.dumps(
                {"entity": ent, "region": "Philadelphia",
                 "group_question": "building now?" if ent == "home builder" else None,
                 "group_names": ["Yes", "No"] if ent == "home builder" else None}))
        with (thread / "deliveries.jsonl").open("w") as f:
            for rid_, subj_, ts in (("cl-ops", "Re: Research", "2026-11-08T11:19:00"),
                                    ("cl-dlr", "Re: Research", "2026-11-08T10:38:00"),
                                    ("cl-old", "Re: Delaware leads", "2026-09-24T10:00:00")):
                f.write(json.dumps({"event": "delivered", "run_id": rid_, "client": "bill",
                                    "subject": subj_, "ts": ts}) + "\n")
        sl = same_list_request("bill", "Re: Research", "same list for Sussex County",
                               "same list for Sussex County", thread)
        check("Bill's 11-08 reply: the latest list on his thread, the new place, his words",
              sl and sl["prior_run"] == "cl-ops" and sl["place"] == "Sussex County"
              and "every snow plow operator in Sussex County" in sl["request"]
              and "The earlier list covered Philadelphia; this one covers Sussex County only." in sl["request"]
              and wants_contact_list("", sl["request"]))
        quoted = ("same list for Sussex County > On Nov 8, CUMULUS wrote: > Here's the list you asked "
                  "for: snow plow dealers in Philadelphia. > > On Nov 8 > snow plow operators")
        check("a reply quoting the dealer list gets the dealers, not the later operator list",
              same_list_request("bill", "RE: Research", "same list for Sussex County", quoted,
                                thread)["prior_run"] == "cl-dlr")
        ob = same_list_request("bill", "Re: Delaware leads", "Same list for Kent County please", "", thread)
        check("the prior run's split carries over", ob and "building now?" in ob["request"])
        check("no delivered list on that thread -> not a same-list request",
              same_list_request("bill", "Re: Snow totals", "same list for Sussex County", "", thread) is None)
        check("another client's delivered list never counts",
              same_list_request("aggie", "Re: Research", "same list for Sussex County", "", thread) is None)
        check("no ask in the reply -> None even on a delivered thread",
              same_list_request("bill", "Re: Research", "Thanks, got it", "", thread) is None)
    print("selftest: %s" % ("OK" if ok else "FAILED"))
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
