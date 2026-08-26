#!/usr/bin/env python3
"""Read-only folds over what the system SAID to clients, and what it changed.

S78 (2026-08-25) — steps 3 and 4 of docs/CLIENT-PROMISE-WATCHDOG-PROPOSAL.md.
`client_promises.py` covers the promise itself; this file covers the three
other ways a client conversation goes wrong while every service stays green:

  duplicate_answers()      we sent the same answer twice on one thread
  stalled_threads()        a client wrote and nothing substantive went back
  high_value_overwrites()  a researched fact on a good lead was overwritten

Every function here READS. Nothing in this file writes, sends, or repairs
anything — Skywarden imports it, and Skywarden's contract puts client work
firmly in the NEVER tier. Detection and escalation only.

Two design notes worth keeping:

* **An unreadable source is not a clean result.** Each fold raises on a source
  it cannot read rather than returning []. Every silent-outage this project has
  paid for had the same shape: a check that could not run, reporting nothing,
  indistinguishable from a check that ran and found nothing. The caller turns
  the exception into a loud "did not run".
* **The ledger holds hashes, not the client's words.** duplicate_answers works
  on the fingerprints task_solver stamps at send time. That is enough to know
  two replies were the same, and it keeps client prose out of an audit log the
  supervisor user can read.
"""
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
LEDGER = APP_DIR / "logs" / "self-changes" / "ledger.jsonl"

TS_FMT = "%Y-%m-%d %H:%M:%S"

# Fields that cost a human (or a research pass) real effort to establish, and
# whose silent replacement sends a client to the wrong person. Deliberately
# narrow: a churn alert on every field would be ignored within a week.
HIGH_VALUE_FIELDS = (
    "board_contact", "board_email", "board_phone",
    "current_mgmt_co", "mgmt_status", "legal_name", "website",
)

# Leads worth protecting. A change on a cold lead nobody has contacted is
# ordinary enrichment; the same change on a lead the client is actively
# working is the Hunters Ridge failure.
PROTECTED_LEAD_STATES = ("warm", "hot", "engaged", "client", "won")


def _parse_ts(s: str):
    try:
        return datetime.strptime(str(s)[:19], TS_FMT)
    except Exception:
        return None


def read_ledger(path=None, hours: int = 168) -> list:
    """Ledger rows inside the window, newest last. Raises if unreadable."""
    path = Path(path or LEDGER)
    if not path.exists():
        raise FileNotFoundError(f"self-changes ledger not found at {path}")
    cutoff = datetime.now() - timedelta(hours=hours)
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue          # one corrupt line must not blind the fold
            ts = _parse_ts(row.get("ts"))
            if ts and ts >= cutoff:
                row["_ts"] = ts
                rows.append(row)
    return rows


# ── Check 3a: did we send the same answer twice? ─────────────────────────────

def duplicate_answers(path=None, hours: int = 168) -> list:
    """Outbound answers repeated on one thread to one client.

    This is Bill's bug, stated exactly: he asked a follow-up, and the system
    re-sent the recap it had already sent him. The subject-line defect that
    CAUSED it is fixed, but a duplicate is the symptom of a whole class of
    answer-path faults, not just that one — so it is worth watching for its own
    sake rather than trusting that the known cause is the only cause.

    Matches on two fingerprints. `answer_sha` is the normalised text, so
    whitespace and case drift do not hide a repeat. `answer_skeleton_sha` also
    strips digits, so a recap regenerated with today's date still collides with
    yesterday's — which is what a re-sent KB recap actually looks like.
    """
    answered = [r for r in read_ledger(path, hours)
                if r.get("event") == "auto-answered" and r.get("thread")]
    groups = {}
    for r in answered:
        for kind, sha in (("identical", r.get("answer_sha")),
                          ("near-identical", r.get("answer_skeleton_sha"))):
            if not sha:
                continue
            groups.setdefault((r.get("requester"), r.get("thread"), sha),
                              {"kind": kind, "rows": []})["rows"].append(r)

    hits, seen_exact = [], set()
    for (requester, thread, sha), g in groups.items():
        if len(g["rows"]) < 2:
            continue
        rows = sorted(g["rows"], key=lambda r: r["_ts"])
        # A group of byte-identical answers also collides on the skeleton hash.
        # Report it once, as the stronger finding.
        key = (requester, thread, tuple(r["_ts"] for r in rows))
        if g["kind"] == "identical":
            seen_exact.add(key)
        elif key in seen_exact:
            continue
        hits.append({
            "kind": g["kind"],
            "requester": requester,
            "thread": thread,
            "count": len(rows),
            "title": rows[-1].get("title"),
            "first": rows[0]["_ts"].strftime(TS_FMT),
            "last": rows[-1]["_ts"].strftime(TS_FMT),
            "gap_hours": round(
                (rows[-1]["_ts"] - rows[0]["_ts"]).total_seconds() / 3600, 1),
        })
    return sorted(hits, key=lambda h: h["last"], reverse=True)


# ── Check 3b: did a client write and get nothing back? ───────────────────────

def stalled_threads(path=None, hours: int = 48) -> list:
    """Inbound client messages with no substantive outbound reply since.

    "Substantive" means an auto-answered row on the same thread. An ack does
    not count and is not recorded here — that is the point. Bill's go-ahead was
    acked within seconds and the workbook never came; an ack proves the mail
    was received, never that anything happened.

    The default window is deliberately generous. A check that fires on a
    four-hour-old email will be muted inside a week, and a muted check is worse
    than none: it reports clean because nobody reads it.
    """
    rows = read_ledger(path, hours=max(hours * 4, 168))
    inbound, outbound = {}, {}
    for r in rows:
        thread = r.get("thread")
        if not thread:
            continue
        if r.get("event") == "user-intake":
            cur = inbound.get(thread)
            if not cur or r["_ts"] > cur["_ts"]:
                inbound[thread] = r
        elif r.get("event") == "auto-answered":
            cur = outbound.get(thread)
            if not cur or r["_ts"] > cur["_ts"]:
                outbound[thread] = r

    now, stalls = datetime.now(), []
    for thread, r in inbound.items():
        reply = outbound.get(thread)
        if reply and reply["_ts"] >= r["_ts"]:
            continue
        age = (now - r["_ts"]).total_seconds() / 3600
        if age < hours:
            continue
        stalls.append({
            "requester": r.get("requester"),
            "thread": thread,
            "title": r.get("title"),
            "kind": r.get("kind"),
            "received": r["_ts"].strftime(TS_FMT),
            "age_hours": round(age, 1),
            # A build or research request is SUPPOSED to become queued work
            # rather than an instant reply, so say which it was and let the
            # reader judge. Reporting it as a fault would be wrong; hiding it
            # would recreate the blind spot this whole file exists to close.
            "expected_reply": r.get("kind") in ("answer", "confirmation", "resend"),
        })
    return sorted(stalls, key=lambda s: s["age_hours"], reverse=True)


# ── Check 5: is intake actually WORKING, not merely running? ─────────────────

INTAKE_LOG = APP_DIR / "logs" / "intake.log"
INTAKE_ERR_LOG = APP_DIR / "logs" / "intake-launchd.log"
_TS_RX = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")


def intake_health(stale_minutes: int = 40, log_path=None, err_path=None) -> dict:
    """When did intake last COMPLETE a poll, and has it been failing?

    S78, and this is the check whose absence cost a real outage. `intake` is a
    **KeepAlive loop**, not a scheduled job: launchd keeps the wrapper alive and
    reports a live PID and exit 0 whether the python inside it works or not.
    During the S78 break `launchctl list` showed `336  0  com.cirrus.intake` for
    the whole hour while every 15-minute iteration died on an import error.

    So service status answers "is it up", and that question was never the one
    worth asking. This answers **"is it working"**, from the log the loop
    already writes — no new heartbeat file, nothing extra to keep in sync.

    The loop even logged `intake.py failed (exit 1), will retry next cycle` on
    every failure. The signal existed on disk the entire time and nothing read
    it. The same line appears for a 2026-08-22 credential failure, which also
    went unnoticed — so this is a repeat, not a one-off.
    """
    log_path = Path(log_path or INTAKE_LOG)
    err_path = Path(err_path or INTAKE_ERR_LOG)
    out = {"stale_minutes_threshold": stale_minutes}

    if not log_path.exists():
        raise FileNotFoundError(f"intake log not found at {log_path}")

    last = None
    for line in log_path.read_text(errors="replace").splitlines():
        m = _TS_RX.match(line.strip())
        if m:
            last = m.group(1)
    if not last:
        raise ValueError(f"no timestamped line in {log_path} — cannot tell when "
                         f"intake last ran")

    age = (datetime.now() - datetime.strptime(last, TS_FMT)).total_seconds() / 60
    out["last_poll"] = last
    out["age_minutes"] = round(age, 1)
    out["stale"] = age > stale_minutes

    # Recent failures. Counted from the tail only: this file is append-only and
    # years long, and a failure from August last year is not news today.
    fails = 0
    if err_path.exists():
        tail = err_path.read_text(errors="replace").splitlines()[-400:]
        fails = sum(1 for l in tail if "intake.py failed" in l)
    out["recent_failures"] = fails
    return out


# ── Check 4: was a researched fact on a good lead overwritten? ───────────────

def _kb_path(project: str) -> Path:
    """Ask entity_kb where its own database lives — never restate the path.

    The first draft of this guessed logs/entity-kb/<project>.sqlite3, which
    does not exist; the real one is data/entity_kb/<project>.db. A wrong path
    here fails OPEN in the worst way available: FileNotFoundError, which the
    caller reports as "check did not run" -- so it would have been caught, but
    only after shipping a check that never once ran. Derive, don't duplicate.
    """
    import entity_kb
    return entity_kb._db_path(project)


def high_value_overwrites(project: str = "hoa_leads_bill", hours: int = 168,
                          db_path=None) -> list:
    """A known value on a protected lead, replaced with a different one.

    The bug this comes from: a bulk directory job overwrote the researched
    board president of Kent's Hunters Ridge -- a warm, tier-A lead -- with a
    same-named association's officer from another county, and the dry run
    reported "0 ambiguous". Mailing that board would have reached a stranger.

    **On the rule, honestly.** The proposal framed this as "a BULK job changed
    a hand-researched field", but entity_events records no actor, so the fold
    cannot tell a nightly job from a session. Rather than guess, it watches the
    shape that is dangerous regardless of who did it: a non-empty value being
    replaced by a DIFFERENT non-empty value, on a lead the client is actually
    working. Filling a blank is enrichment and is ignored. That rule catches
    the Hunters Ridge case on its own terms and does not depend on data we do
    not have. Adding an actor column later would let this narrow further.

    Reports; never reverts. Which of two values is right is a judgment call,
    and Skywarden does not make judgment calls about client data.
    """
    db = Path(db_path or _kb_path(project))
    if not db.exists():
        raise FileNotFoundError(f"entity KB not found at {db}")
    since = (datetime.now() - timedelta(hours=hours)).strftime(TS_FMT)

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT e.field, e.old_value, e.new_value, e.occurred_at, "
            "       en.slug, en.name, en.lead_state "
            "FROM entity_events e JOIN entities en ON en.id = e.entity_id "
            "WHERE e.project=? AND e.event_type='field_change' "
            "  AND e.occurred_at >= ? "
            "ORDER BY e.occurred_at DESC",
            (project, since)).fetchall()
    finally:
        conn.close()

    hits = []
    for r in rows:
        if r["field"] not in HIGH_VALUE_FIELDS:
            continue
        if (r["lead_state"] or "").lower() not in PROTECTED_LEAD_STATES:
            continue
        try:
            old = json.loads(r["old_value"]) if r["old_value"] else None
            new = json.loads(r["new_value"]) if r["new_value"] else None
        except Exception:
            old, new = r["old_value"], r["new_value"]
        if not old or not new or old == new:
            continue          # filling a blank is enrichment, not an overwrite
        hits.append({
            "project": project,
            "slug": r["slug"],
            "name": r["name"],
            "lead_state": r["lead_state"],
            "field": r["field"],
            "old": str(old)[:120],
            "new": str(new)[:120],
            "occurred_at": r["occurred_at"],
        })
    return hits


# ── Selftests ────────────────────────────────────────────────────────────────

def selftest() -> int:
    """Offline, no ledger, no DB, no network — fixtures in a temp dir.

    These folds decide whether a client-facing fault is seen or missed, so they
    are tested rather than trusted. The cases that matter are the NEGATIVE ones:
    a check that fires on everything gets muted, and a muted check reports clean
    forever.
    """
    import tempfile
    bad = 0

    def check(label, ok):
        nonlocal bad
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            bad += 1

    def ts(hours_ago):
        return (datetime.now() - timedelta(hours=hours_ago)).strftime(TS_FMT)

    with tempfile.TemporaryDirectory() as td:
        led = Path(td) / "ledger.jsonl"

        def write(rows):
            led.write_text("".join(json.dumps(r) + "\n" for r in rows))

        # ---- duplicate_answers ------------------------------------------
        write([
            {"ts": ts(30), "event": "auto-answered", "requester": "bill",
             "thread": "back creek", "title": "Back Creek", "answer_sha": "aaa",
             "answer_skeleton_sha": "sss"},
            {"ts": ts(2), "event": "auto-answered", "requester": "bill",
             "thread": "back creek", "title": "Back Creek", "answer_sha": "aaa",
             "answer_skeleton_sha": "sss"},
        ])
        d = duplicate_answers(led)
        check("the same answer twice on one thread is caught", len(d) == 1)
        check("it is reported as identical, not near-identical",
              d and d[0]["kind"] == "identical")
        check("an identical pair is not ALSO reported via the skeleton hash",
              len(d) == 1)
        check("the gap is measured", d and 27 < d[0]["gap_hours"] < 29)

        write([
            {"ts": ts(30), "event": "auto-answered", "requester": "bill",
             "thread": "back creek", "answer_sha": "aaa", "answer_skeleton_sha": "sss"},
            {"ts": ts(2), "event": "auto-answered", "requester": "bill",
             "thread": "back creek", "answer_sha": "bbb", "answer_skeleton_sha": "sss"},
        ])
        d = duplicate_answers(led)
        check("a recap regenerated with a new date still collides",
              len(d) == 1 and d[0]["kind"] == "near-identical")

        write([
            {"ts": ts(5), "event": "auto-answered", "requester": "bill",
             "thread": "back creek", "answer_sha": "aaa", "answer_skeleton_sha": "s1"},
            {"ts": ts(4), "event": "auto-answered", "requester": "bill",
             "thread": "hunters ridge", "answer_sha": "aaa", "answer_skeleton_sha": "s1"},
        ])
        check("the same answer on two DIFFERENT threads is not a duplicate",
              duplicate_answers(led) == [])
        write([
            {"ts": ts(5), "event": "auto-answered", "requester": "bill",
             "thread": "t", "answer_sha": "aaa", "answer_skeleton_sha": "s1"},
            {"ts": ts(4), "event": "auto-answered", "requester": "aggie",
             "thread": "t", "answer_sha": "aaa", "answer_skeleton_sha": "s1"},
        ])
        check("the same answer to two DIFFERENT clients is not a duplicate",
              duplicate_answers(led) == [])
        write([
            {"ts": ts(300), "event": "auto-answered", "requester": "bill",
             "thread": "t", "answer_sha": "aaa", "answer_skeleton_sha": "s1"},
            {"ts": ts(2), "event": "auto-answered", "requester": "bill",
             "thread": "t", "answer_sha": "aaa", "answer_skeleton_sha": "s1"},
        ])
        check("a repeat outside the window is not reported",
              duplicate_answers(led, hours=168) == [])
        write([{"ts": ts(2), "event": "auto-answered", "requester": "bill",
                "thread": "t", "answer_sha": "aaa", "answer_skeleton_sha": "s1"}])
        check("a single answer is not a duplicate", duplicate_answers(led) == [])

        # A pre-S78 ledger has no fingerprints at all. It must read as "nothing
        # found", never crash -- the rows predate the field by design.
        write([{"ts": ts(2), "event": "auto-answered", "requester": "bill",
                "thread": "t"},
               {"ts": ts(1), "event": "auto-answered", "requester": "bill",
                "thread": "t"}])
        check("rows written before fingerprints existed do not crash the fold",
              duplicate_answers(led) == [])

        # ---- stalled_threads --------------------------------------------
        write([{"ts": ts(72), "event": "user-intake", "requester": "bill",
                "thread": "back creek", "title": "Back Creek", "kind": "answer"}])
        st = stalled_threads(led, hours=48)
        check("an inbound with no reply at all is a stall", len(st) == 1)
        check("a stall on an answer-kind expects a reply",
              st and st[0]["expected_reply"] is True)

        write([{"ts": ts(72), "event": "user-intake", "requester": "bill",
                "thread": "back creek", "kind": "answer"},
               {"ts": ts(71), "event": "auto-answered", "requester": "bill",
                "thread": "back creek"}])
        check("an inbound answered afterwards is not a stall",
              stalled_threads(led, hours=48) == [])

        write([{"ts": ts(72), "event": "auto-answered", "requester": "bill",
                "thread": "back creek"},
               {"ts": ts(50), "event": "user-intake", "requester": "bill",
                "thread": "back creek", "kind": "answer"}])
        check("a reply that PREDATES the inbound does not clear the stall",
              len(stalled_threads(led, hours=48)) == 1)

        write([{"ts": ts(4), "event": "user-intake", "requester": "bill",
                "thread": "t", "kind": "answer"}])
        check("a fresh inbound is not yet a stall",
              stalled_threads(led, hours=48) == [])

        write([{"ts": ts(72), "event": "user-intake", "requester": "bill",
                "thread": "t", "kind": "build"}])
        st = stalled_threads(led, hours=48)
        check("a build request is still reported", len(st) == 1)
        check("...but is flagged as not expecting an instant reply",
              st and st[0]["expected_reply"] is False)

        # ---- intake_health: the check whose absence cost a real outage ---
        ilog = Path(td) / "intake.log"
        ierr = Path(td) / "intake-launchd.log"
        ilog.write_text(f"[{ts(0.1)}] inbox: 6 in window, 0 new\n")
        ierr.write_text("all quiet\n")
        h = intake_health(log_path=ilog, err_path=ierr)
        check("a recent poll is not stale", h["stale"] is False)
        check("the age is measured", h["age_minutes"] < 15)

        ilog.write_text(f"[{ts(3)}] inbox: 6 in window, 0 new\n")
        h = intake_health(log_path=ilog, err_path=ierr)
        check("a poll 3 hours old IS stale — this is the S78 outage shape",
              h["stale"] is True)

        # The exact bytes the loop wrote during the S78 break.
        ierr.write_text("TypeError: unsupported operand type(s) for |\n"
                        "intake.py failed (exit 1), will retry next cycle\n" * 3)
        h = intake_health(log_path=ilog, err_path=ierr)
        check("repeated loop failures are counted", h["recent_failures"] == 3)

        ilog.write_text("no timestamps here at all\n")
        try:
            intake_health(log_path=ilog, err_path=ierr)
            check("a log with no timestamp raises rather than reading healthy", False)
        except ValueError:
            check("a log with no timestamp raises rather than reading healthy", True)
        try:
            intake_health(log_path=Path(td) / "gone.log")
            check("a missing intake log raises rather than reading healthy", False)
        except FileNotFoundError:
            check("a missing intake log raises rather than reading healthy", True)

        # ---- unreadable sources must be LOUD ----------------------------
        gone = Path(td) / "nope.jsonl"
        try:
            duplicate_answers(gone)
            check("a missing ledger raises rather than reporting clean", False)
        except FileNotFoundError:
            check("a missing ledger raises rather than reporting clean", True)
        try:
            high_value_overwrites(db_path=Path(td) / "nope.sqlite3")
            check("a missing entity KB raises rather than reporting clean", False)
        except FileNotFoundError:
            check("a missing entity KB raises rather than reporting clean", True)

        led.write_text('{"ts": "' + ts(2) + '", "event": "auto-answered", '
                       '"requester": "bill", "thread": "t", "answer_sha": "a", '
                       '"answer_skeleton_sha": "s"}\n'
                       'this is not json\n'
                       '{"ts": "' + ts(1) + '", "event": "auto-answered", '
                       '"requester": "bill", "thread": "t", "answer_sha": "a", '
                       '"answer_skeleton_sha": "s"}\n')
        check("a corrupt line is skipped and the rest still folds",
              len(duplicate_answers(led)) == 1)

        # ---- high_value_overwrites --------------------------------------
        db = Path(td) / "kb.sqlite3"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE entities (id INTEGER PRIMARY KEY, project TEXT, slug TEXT,
                name TEXT, lead_state TEXT);
            CREATE TABLE entity_events (id INTEGER PRIMARY KEY, project TEXT,
                entity_id INTEGER, event_type TEXT, field TEXT, old_value TEXT,
                new_value TEXT, occurred_at TEXT);
        """)
        conn.execute("INSERT INTO entities VALUES (1,'p','hunters-ridge',"
                     "'Hunters Ridge','warm')")
        conn.execute("INSERT INTO entities VALUES (2,'p','cold-one','Cold One','cold')")
        rows = [
            # the real one: a researched president replaced by a stranger
            (1, "board_contact", '"President: Urshla Dowdy"',
             '"Daniel Kennefick, President"'),
            # filling a blank on the same warm lead is enrichment, not a loss
            (1, "board_phone", 'null', '"302-555-0000"'),
            # a low-value field churning is noise
            (1, "property_zip", '"19711"', '"19702"'),
            # the same overwrite on a cold lead is ordinary
            (2, "board_contact", '"Old Name"', '"New Name"'),
        ]
        for eid, field, old, new in rows:
            conn.execute("INSERT INTO entity_events (project, entity_id, event_type,"
                         " field, old_value, new_value, occurred_at) "
                         "VALUES ('p',?,'field_change',?,?,?,?)",
                         (eid, field, old, new, ts(2)))
        conn.commit(); conn.close()

        h = high_value_overwrites("p", db_path=db)
        check("a researched contact overwritten on a WARM lead is caught",
              len(h) == 1 and h[0]["slug"] == "hunters-ridge")
        check("the old value is reported so it can be restored",
              h and "Urshla Dowdy" in h[0]["old"])
        check("filling a blank field is NOT an overwrite",
              all(x["field"] != "board_phone" for x in h))
        check("a low-value field is not watched",
              all(x["field"] != "property_zip" for x in h))
        check("the same change on a COLD lead is not reported",
              all(x["slug"] != "cold-one" for x in h))
        check("an old event outside the window is not reported",
              high_value_overwrites("p", hours=1, db_path=db) == [])

    print("\nALL PASS" if not bad else f"\n{bad} FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv or len(sys.argv) == 1:
        sys.exit(selftest())
