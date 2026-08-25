#!/usr/bin/env python3
"""Client promise ledger — what we told a client we would do, and whether we did.

S78 (2026-08-25), built on Buddy's ask after this failure:

    CUMULUS offered Bill a 224-row workbook. Bill replied "yes -- clean it up
    and send it". CUMULUS answered his go-ahead with a recap he already had,
    and the workbook was never built. Every service was healthy the whole time,
    and every existing check passed, because nothing in the system had any
    record that a promise had been made.

That is the gap this file closes. We already record that mail ARRIVED
(intake.jsonl) and that a reply LEFT (dev_loop "auto-answered"). We recorded
nothing about whether the thing we said we would do got DONE.

Design notes worth keeping:

* **Append-only.** The file is a log of events, and current state is folded from
  it. A promise's history is the point -- "when did we say this, when did he
  confirm, when did it ship" is exactly the question being asked -- so nothing
  is ever rewritten in place.
* **Never raises on the client path.** task_solver calls this immediately after
  a client's reply has already gone out. A bookkeeping failure must not turn
  into a client-visible error, so every public writer swallows and returns a
  falsy value. Same discipline as task_solver._record_question_attempt.
* **Thread identity is the normalised subject.** "Re: X", "RE: X" and "X" are
  one thread. This is deliberately the same notion of "whose words are these"
  that task_solver.is_reply_subject uses.
"""
import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

LEDGER = Path(__file__).parent / "logs" / "client_promises.jsonl"

DEFAULT_SLA_HOURS = 48

# open      -- we said we would do it; client has not answered
# confirmed -- client said go ahead; we owe them the thing
# closed    -- delivered (or explicitly cancelled)
OPEN_STATES = ("open", "confirmed")

_RE_PREFIX_RX = re.compile(r"^\s*(re|fwd?|aw|antw|res)\s*(\[\d+\])?\s*:\s*",
                           re.IGNORECASE)


def thread_key(subject: str) -> str:
    """One key for a subject and all its replies."""
    s = subject or ""
    prev = None
    while s != prev:                      # "Re: Re: Fwd: X" -> "X"
        prev = s
        s = _RE_PREFIX_RX.sub("", s, count=1)
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _append(event: dict) -> bool:
    """Never raises. Returns False if the write failed."""
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with open(LEDGER, "a") as f:
            f.write(json.dumps(event) + "\n")
        return True
    except Exception:
        return False


def _events(path: Path = None) -> list:
    p = path or LEDGER
    if not p.exists():
        return []
    out = []
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue          # one corrupt line must not blind the whole check
    except Exception:
        return []
    return out


def _fold(path: Path = None) -> dict:
    """Current state of every promise, by id."""
    promises = {}
    for e in _events(path):
        pid = e.get("id")
        if not pid:
            continue
        if e.get("event") == "opened":
            promises[pid] = dict(e, state="open", history=[e])
        elif pid in promises:
            p = promises[pid]
            p["history"].append(e)
            if e.get("event") == "confirmed":
                p["state"] = "confirmed"
                p["confirmed_at"] = e.get("at")
            elif e.get("event") == "closed":
                p["state"] = "closed"
                p["closed_at"] = e.get("at")
                p["closed_by"] = e.get("by")
                p["close_note"] = e.get("note")
    return promises


def make_id(client: str, subject: str, promise: str) -> str:
    raw = f"{client}|{thread_key(subject)}|{promise}|{_now()}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def open_promise(client: str, project: str, subject: str, promise: str,
                 message_id: str = "", sla_hours: int = DEFAULT_SLA_HOURS,
                 detected_by: str = "", escalated: bool = False,
                 path: Path = None) -> str:
    """Record that we told `client` we would do something. Returns the id, or
    "" if the write failed (never raises -- see module docstring)."""
    promise = (promise or "").strip()
    if not client or not promise:
        return ""
    pid = make_id(client, subject, promise)
    ev = {"at": _now(), "event": "opened", "id": pid, "client": client,
          "project": project, "subject": subject or "",
          "thread": thread_key(subject), "promise": promise[:600],
          "message_id": message_id, "sla_hours": int(sla_hours),
          "detected_by": detected_by, "escalated": bool(escalated)}
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as f:
                f.write(json.dumps(ev) + "\n")
            return pid
        except Exception:
            return ""
    return pid if _append(ev) else ""


def _transition(pid: str, event: str, path: Path = None, **kw) -> bool:
    ev = {"at": _now(), "event": event, "id": pid}
    ev.update(kw)
    if path is not None:
        try:
            with open(path, "a") as f:
                f.write(json.dumps(ev) + "\n")
            return True
        except Exception:
            return False
    return _append(ev)


def confirm_promise(pid: str, note: str = "", path: Path = None) -> bool:
    """The client said go ahead. We now OWE them the thing."""
    return _transition(pid, "confirmed", path=path, note=(note or "")[:400])


def close_promise(pid: str, by: str = "", note: str = "",
                  path: Path = None) -> bool:
    """Delivered, or explicitly cancelled."""
    return _transition(pid, "closed", path=path, by=by, note=(note or "")[:400])


def list_promises(state: str = None, client: str = None,
                  path: Path = None) -> list:
    out = []
    for p in _fold(path).values():
        if state and p.get("state") != state:
            continue
        if client and p.get("client") != client:
            continue
        out.append(p)
    out.sort(key=lambda p: p.get("at", ""))
    return out


def open_for_thread(client: str, subject: str, path: Path = None) -> list:
    """Every still-owed promise on this client's thread. This is what tells
    intake that an inbound "yes, go ahead" is a CONFIRMATION and not a new
    question."""
    key = thread_key(subject)
    return [p for p in _fold(path).values()
            if p.get("client") == client
            and p.get("thread") == key
            and p.get("state") in OPEN_STATES]


def overdue(now: datetime = None, path: Path = None) -> list:
    """Promises past their SLA and still owed.

    A CONFIRMED promise is the serious one -- the client has said yes and is
    waiting. It is reported even inside its SLA window once the window passes,
    and its age is measured from the confirmation, not from the offer.
    """
    now = now or datetime.now()
    late = []
    for p in _fold(path).values():
        if p.get("state") not in OPEN_STATES:
            continue
        stamp = p.get("confirmed_at") or p.get("at")
        try:
            started = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        age_h = (now - started).total_seconds() / 3600.0
        if age_h > float(p.get("sla_hours") or DEFAULT_SLA_HOURS):
            late.append(dict(p, age_hours=round(age_h, 1)))
    late.sort(key=lambda p: -p["age_hours"])
    return late


def escalation_rate(path: Path = None) -> dict:
    """How often promise-detection had to leave the local model.

    S73's llm_providers._ollama docstring asked for evidence before routing
    anything to the local model. This job produces that evidence as a
    by-product: every opened promise records which model decided it.
    """
    opened = [e for e in _events(path) if e.get("event") == "opened"]
    esc = sum(1 for e in opened if e.get("escalated"))
    by_model = {}
    for e in opened:
        by_model[e.get("detected_by") or "?"] = by_model.get(e.get("detected_by") or "?", 0) + 1
    return {"decided": len(opened), "escalated": esc,
            "rate": round(esc / len(opened), 3) if opened else None,
            "by_model": by_model}


# ── Selftest (offline, no network, temp file) ────────────────────────────────
def selftest() -> int:
    import os
    import tempfile

    failures = 0

    def check(name, cond):
        nonlocal failures
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            failures += 1

    fd, tmp = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    p = Path(tmp)
    try:
        check("'Re: X' and 'X' are one thread",
              thread_key("Re: Back Creek") == thread_key("Back Creek"))
        check("stacked prefixes collapse",
              thread_key("Re: Fwd: RE: Back Creek") == thread_key("Back Creek"))
        check("different subjects are different threads",
              thread_key("Back Creek") != thread_key("Front Creek"))

        pid = open_promise("bill", "property-management",
                           "Back Creek - the president, plus every HOA contact",
                           "send the whole 224 as a workbook", path=p,
                           detected_by="ollama", sla_hours=48)
        check("opening a promise returns an id", bool(pid))
        check("it is open", len(list_promises(state="open", path=p)) == 1)

        # THE case this file exists for: his reply arrives on "Re: <subject>".
        found = open_for_thread("bill", "Re: Back Creek - the president, plus "
                                        "every HOA contact", path=p)
        check("a reply on the thread finds the open promise", len(found) == 1)
        check("another client's thread does not",
              open_for_thread("aggie", "Re: Back Creek - the president, plus "
                                       "every HOA contact", path=p) == [])

        check("confirming works", confirm_promise(pid, "client said go ahead", path=p))
        check("state is now confirmed",
              list_promises(state="confirmed", path=p)[0]["id"] == pid)
        check("a confirmed promise is still owed",
              len(open_for_thread("bill", "Back Creek - the president, plus "
                                          "every HOA contact", path=p)) == 1)

        check("not overdue yet", overdue(path=p) == [])
        check("overdue once the SLA passes",
              len(overdue(now=datetime.now() + timedelta(hours=72), path=p)) == 1)

        check("closing works", close_promise(pid, by="S78", note="workbook sent", path=p))
        check("a closed promise is no longer owed",
              open_for_thread("bill", "Back Creek - the president, plus every "
                                      "HOA contact", path=p) == [])
        check("and never reads as overdue",
              overdue(now=datetime.now() + timedelta(days=30), path=p) == [])

        rate = escalation_rate(path=p)
        check("escalation rate is measured",
              rate["decided"] == 1 and rate["escalated"] == 0
              and rate["by_model"] == {"ollama": 1})

        # A corrupt line must not blind the ledger -- this check is the thing
        # that would go quiet if it did.
        with open(p, "a") as f:
            f.write("{not json at all\n")
        check("a corrupt line is skipped, the rest still folds",
              len(list_promises(path=p)) == 1)

        check("an empty promise is refused",
              open_promise("bill", "x", "subj", "   ", path=p) == "")
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


def report() -> int:
    """Human-readable state of the ledger. Used by the on-box verification and
    handy for a quick 'what do we owe anyone' from a session."""
    offered = list_promises(state="open")
    owed = list_promises(state="confirmed")
    late = overdue()
    print(f"ledger: {LEDGER}")
    print(f"  offered, awaiting the client's answer : {len(offered)}")
    print(f"  confirmed, we owe them the thing      : {len(owed)}")
    print(f"  OVERDUE                               : {len(late)}")
    print(f"  detection escalation                  : {escalation_rate()}")
    for p in late:
        print(f"  OVERDUE {p['client']} [{p['state']}, {p['age_hours']}h]: "
              f"{str(p['promise'])[:100]}")
    for p in owed:
        if p not in late:
            print(f"  owed {p['client']}: {str(p['promise'])[:100]}")
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        sys.exit(report())
    sys.exit(selftest())
