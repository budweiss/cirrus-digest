#!/usr/bin/env python3
"""client_contact_report.py — merge per-box contact probes into one answer.

S145. Called by runner `client-contact` only after EVERY box has answered; if
any box failed, the caller has already refused and this never runs. That order
matters: this file must never be reachable with a partial set, because a report
that silently covers fewer boxes than it claims is the bug it exists to prevent.
"""
import json
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path


# How long may each client go without hearing from us before that is worth
# saying? Absence of a client here means the default. An EXEMPTION is explicit
# and carries its reason -- the completeness module's idiom, for the same reason:
# "silently unmonitored" and "deliberately not monitored" must not look alike.
# The mailboxes a complete answer MUST include. Hardcoded on purpose: this is
# the assertion, and an assertion derived from whatever happened to answer is
# not an assertion.
EXPECTED_MAILBOXES = {"cirrustask@gmail.com", "cumulus@cumulustask.com"}

DEFAULT_QUIET_DAYS = 14
QUIET_DAYS = {
    "alyssa": 3,     # daily literacy digest -- three quiet days is already wrong
    "bill":   10,    # weekly HOA update + the dev-leads feed when it has news
    "aggie":  30,    # project-driven, no standing cadence
}
EXEMPT = {
    "justin": ("the Halftime dashboard IS the delivery channel (Buddy, S79) -- "
               "he is not emailed per update, so email silence is correct here "
               "and flagging it would train us to ignore this whole check"),
    "buddy":  ("not a client -- his own briefs land here because he is cc'd on "
               "everything"),
}


def assess(d):
    """Structured verdict for a directory of probe outputs. No printing.

    S148: added so the scheduled job can ALERT on a finding rather than
    scraping main()'s stdout. main() renders this; the two cannot disagree
    because there is only one of them.

    {"ok", "refusal": str|None, "boxes": [...], "quiet": [{client, days, limit}],
     "silent": [client], "seen": {client: [(when, send)]}}
    """
    from datetime import datetime, timezone
    boxes, sends, known = [], [], set()
    for f in sorted(Path(d).glob("*.json")):
        try:
            o = json.loads(f.read_text())
        except Exception:
            return {"ok": False, "refusal": f"{f.stem} produced unparseable output"}
        if not o.get("ok"):
            return {"ok": False,
                    "refusal": f"{o.get('box', f.stem)}: {o.get('error')}"}
        boxes.append((f.stem, o.get("box", "?"), o.get("mailbox", "?")))
        known |= set(o.get("recipients") or [])
        for sd in o.get("sends", []):
            sd = dict(sd)
            sd["box"] = (o.get("mailbox") or "?").split("@")[-1]
            sends.append(sd)

    mailboxes = {m for _, _, m in boxes}
    if mailboxes != EXPECTED_MAILBOXES:
        return {"ok": False, "boxes": boxes,
                "refusal": ("expected mailboxes "
                            f"{', '.join(sorted(EXPECTED_MAILBOXES))} but got "
                            f"{', '.join(sorted(mailboxes)) or '(none)'}")}
    if len(mailboxes) < len(boxes):
        return {"ok": False, "boxes": boxes,
                "refusal": "the probes did not cover distinct mailboxes"}

    now = datetime.now(timezone.utc)
    seen = {}
    for sd in sends:
        try:
            when = parsedate_to_datetime(sd["date"])
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        seen.setdefault(sd["client"], []).append((when, sd))

    quiet = []
    for client, rows in seen.items():
        if client in EXEMPT:
            continue
        age_d = (now - max(r[0] for r in rows)).total_seconds() / 86400.0
        limit = QUIET_DAYS.get(client, DEFAULT_QUIET_DAYS)
        if age_d > limit:
            quiet.append({"client": client, "days": round(age_d, 1), "limit": limit})
    silent = sorted(n for n in known if n not in seen and n not in EXEMPT)
    return {"ok": True, "refusal": None, "boxes": boxes, "seen": seen,
            "quiet": sorted(quiet, key=lambda q: -q["days"]), "silent": silent,
            "known": sorted(known), "n_sends": len(sends)}


def main(d):
    boxes, sends, known = [], [], set()
    for f in sorted(Path(d).glob("*.json")):
        try:
            o = json.loads(f.read_text())
        except Exception:
            print(f"UNVERIFIABLE — {f.stem} produced unparseable output")
            return 3
        if not o.get("ok"):
            print(f"UNVERIFIABLE — {o.get('box', f.stem)}: {o.get('error')}")
            return 3
        boxes.append((f.stem, o.get("box", "?"), o.get("mailbox", "?")))
        known |= set(o.get("recipients") or [])
        for s in o.get("sends", []):
            # Label by MAILBOX DOMAIN: that is the fact that was missed, and it
            # is the one a reader needs to trace where a send came from.
            s["box"] = (o.get("mailbox") or "?").split("@")[-1]
            sends.append(s)

    # THE GUARD THAT MATTERS. The original incident was not "a box was down" --
    # every probe succeeded. It was that ONE MAILBOX was consulted and the answer
    # looked complete. So the check is on DISTINCT MAILBOXES, not on how many
    # files came back: two probes that both read cirrustask@ would otherwise sail
    # through as "2 boxes answered".
    mailboxes = {m for _, _, m in boxes}

    # S146. The guard used to be `len(mailboxes) < len(boxes)`, which passes with
    # ZERO files and with ONE -- an empty directory printed "no sends found ...
    # REAL zero, both boxes answered" and exited 0. The refusal is the whole
    # point of this command, so it must assert the EXPECTED set, not merely that
    # the set has no duplicates. Only the runner loop knew how many to expect,
    # which put the guarantee in the wrong layer.
    if mailboxes != EXPECTED_MAILBOXES:
        print("UNVERIFIABLE — expected exactly these mailboxes and did not get them:")
        print(f"    expected: {', '.join(sorted(EXPECTED_MAILBOXES))}")
        print(f"    answered: {', '.join(sorted(mailboxes)) or '(none)'}")
        for stem, box, mbox in boxes:
            print(f"      {stem:<10} host={box:<18} mailbox={mbox}")
        print()
        print("A contact history missing a mailbox is not a contact history —")
        print("that is how 2026-09-10 concluded a client had been ignored for")
        print("five weeks when he had been mailed three days earlier.")
        return 3
    if len(mailboxes) < len(boxes):
        print("UNVERIFIABLE — the probes did not cover distinct mailboxes:")
        for stem, box, mbox in boxes:
            print(f"    {stem:<10} host={box:<18} mailbox={mbox}")
        print()
        print("Two probes reading the SAME mailbox is the exact shape of the")
        print("2026-09-10 error: a complete-looking answer drawn from half the")
        print("evidence. Refusing rather than reporting it.")
        return 3

    print("mailboxes covered: "
          + ", ".join(f"{m} (on {b})" for _, b, m in sorted(boxes))
          + f"  -- {len(sends)} send(s)")
    print()
    now = datetime.now(timezone.utc)
    by_client = {}
    for s in sends:
        try:
            when = parsedate_to_datetime(s["date"])
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        by_client.setdefault(s["client"], []).append((when, s))

    # S146. A client with NO send in the window is the finding this command
    # exists for, and the first version dropped it silently: by_client was built
    # only from sends that existed, so "nothing at all" printed nothing at all.
    silent = sorted(n for n in known if n not in by_client and n not in EXEMPT)
    for name in silent:
        print(f"{name}: NO SEND IN WINDOW  <-- nothing at all, in either mailbox")
    if silent:
        print()
    if not by_client and not known:
        print("UNVERIFIABLE — no recipient list came back from either probe; "
              "cannot tell silence from not having looked")
        return 3

    for client, rows in sorted(by_client.items(),
                               key=lambda kv: max(r[0] for r in kv[1])):
        rows.sort(key=lambda r: r[0], reverse=True)
        last, top = rows[0]
        age_d = (now - last).total_seconds() / 86400.0
        if client in EXEMPT:
            flag = "  (not email-driven — see below)"
        else:
            limit = QUIET_DAYS.get(client, DEFAULT_QUIET_DAYS)
            flag = (f"  <-- QUIET {age_d:.0f}d, expected within {limit}d"
                    if age_d > limit else "")
        print(f"{client}: last heard from us {age_d:.1f} days ago{flag}")
        if client in EXEMPT:
            print(f"    exempt: {EXEMPT[client]}")
        for when, s in rows[:5]:
            print(f"    {when:%Y-%m-%d %H:%M}  [{s['box']}]  {s['subject'][:78]}")
        if len(rows) > 5:
            print(f"    ... and {len(rows) - 5} more in the window")
        print()
    return 0


def selftest() -> int:
    """The REFUSALS, which are the whole point of this command.

    S146. F7: the guard used to be `len(mailboxes) < len(boxes)` -- it passed
    with one probe and with none, so an empty directory printed "no sends found
    ... REAL zero, both boxes answered" and exited 0. F5: a client with no send
    in the window never appeared at all, which is the single finding this command
    exists to surface. Both shipped because the only check was one hand-run of
    the happy path.
    """
    import json as _json
    import tempfile
    bad = 0

    def ck(name, cond):
        nonlocal bad
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        bad += 0 if cond else 1

    def box(d, stem, host, mailbox, sends=(), recipients=("bill", "aggie")):
        (Path(d) / f"{stem}.json").write_text(_json.dumps({
            "box": host, "ok": True, "mailbox": mailbox,
            "recipients": list(recipients), "sends": list(sends)}))

    one = {"client": "bill", "to": "b@x",
           "date": "Wed, 10 Sep 2026 12:00:00 -0400", "subject": "s"}
    A, B = sorted(EXPECTED_MAILBOXES)

    with tempfile.TemporaryDirectory() as d:
        ck("an EMPTY directory refuses (it used to report a clean zero)",
           main(d) == 3)
    with tempfile.TemporaryDirectory() as d:
        box(d, "one", "hostA", A, [one])
        ck("ONE mailbox refuses, however healthy it looks", main(d) == 3)
    with tempfile.TemporaryDirectory() as d:
        box(d, "one", "hostA", A, [one]); box(d, "two", "hostA", A, [one])
        ck("TWO probes on the SAME mailbox refuses", main(d) == 3)
    with tempfile.TemporaryDirectory() as d:
        box(d, "one", "hostA", A, [one]); box(d, "two", "hostB", B, [one])
        ck("both expected mailboxes -> it reports", main(d) == 0)
    with tempfile.TemporaryDirectory() as d:
        box(d, "one", "hostA", A, [], recipients=("bill",))
        box(d, "two", "hostB", B, [], recipients=("bill",))
        ck("a client with NO send is still reported, not dropped", main(d) == 0)
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "bad.json").write_text("{ not json")
        box(d, "two", "hostB", B, [one])
        ck("an unparseable probe refuses", main(d) == 3)
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "a.json").write_text(_json.dumps(
            {"box": "hostA", "ok": False, "error": "sudo denied", "mailbox": A}))
        box(d, "two", "hostB", B, [one])
        ck("a probe reporting ok=false refuses", main(d) == 3)

    print()
    print("all client_contact_report selftests passed" if not bad else f"{bad} FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main(sys.argv[1]))
