#!/usr/bin/env python3
"""Project Immaculate — the 24 contest answers, in the CRM.

WHY entity_kb RATHER THAN A FILE
--------------------------------
Buddy asked for the entries in "a CRM or something like this", with a daily job
to double-check them. `entity_kb.upsert_entity` already diffs the fields you
hand it against what is stored and ledgers a `field_change` event for anything
that moved. So a daily re-check does not need change-detection code of its own:
re-derive the volatile facts, upsert, and any drift is recorded with a
timestamp and both values. That is the whole reason to use it.

One entity per question, slug q01..q24. When a question resolves during the
season, `entity_kb.record_outcome` marks it — so by January the same store says
how many we actually got right.

Nothing here contacts a model or the web; it is the ledger only.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import entity_kb  # noqa: E402

PROJECT = "immaculate"

# (num, when, question, options, answer, confidence, basis, volatile)
# `volatile` marks answers that a daily check could plausibly overturn before
# lock: injuries, depth chart, line moves. The stable ones are base rates.
QUESTIONS = [
 (1,  "Wk1 vs ATL",  "First points of the game", "PIT / ATL / No Points",
      "PIT", 0.50, "market has ATL -104 / PIT +100 — coin flip", True),
 (2,  "Wk2 @ NE",    "Steelers defense forces a turnover", "Yes / No",
      "Yes", 0.78, "27 takeaways in 17 games, ~1.6/game", False),
 (3,  "Wk3 vs CIN",  "Who wins", "PIT / CIN / Tie",
      "PIT", 0.52, "modelled; flips on schedule adjustment — true toss-up", True),
 (4,  "Wk4 @ CLE",   "Who wins", "PIT / CLE / Tie",
      "PIT", 0.545, "modelled from win totals + measured home field", True),
 (5,  "Wk5 vs IND",  "Steelers total points", "0-10 / 11-20 / 21-30 / 30+",
      "21-30", 0.40, "MEASURED: 34.5% of 444 team-games in 2025", False),
 (6,  "Wk6 @ TB",    "How the first points are scored", "TD / FG / Safety / None",
      "TD", 0.55, "scoring splits ~60/40 toward touchdowns", False),
 (7,  "Wk7 @ NO (Paris)", "Who scores first", "PIT / NO / No Points",
      "PIT", 0.55, "neutral site — New Orleans gets no home edge", True),
 (8,  "Wk8 vs CLE",  "Who wins", "PIT / CLE / Tie",
      "PIT", 0.668, "modelled; best of the six division games", True),
 (9,  "Wk10 @ CIN",  "Who wins", "PIT / CIN / Tie",
      "CIN", 0.598, "modelled; Cincinnati better and at home", True),
 (10, "Wk11 @ PHI",  "Steelers offense scores on the first drive", "Yes / No",
      "No", 0.62, "~1/3 of NFL drives score; wording may not even be our drive", False),
 (11, "Wk12 vs DEN", "Longest FG of the game",
      "No FG / 0-19 / 21-29 / 30-39 / 40-49 / 50-59 / 60+",
      "50-59", 0.42, "MEASURED: max of ~3.2 makes; robust to a cold penalty", True),
 (12, "Wk13 vs HOU", "Steelers total passing TDs", "0-10 (pick a number)",
      "1", 0.34, "MEASURED: Rodgers 1.5/game, Poisson mode is 1 not 2", True),
 (13, "Wk14 @ JAC",  "Total points odd or even", "Odd / Even",
      "Odd", 0.57, "MEASURED: 57% odd across two long samples", False),
 (14, "Wk15 vs BAL", "Who wins", "PIT / BAL / Tie",
      # BUDDY'S CALL, 2026-09-09. The MODEL still says BAL at 54.9% -- Baltimore
      # is three wins better on the season line and that beats a measured
      # 2.36-point home field. Entering PIT is a deliberate override, and the
      # confidence below is the honest other side of that same number: 45.1%,
      # not 54.9%. This was flagged from the start as the cheap one to flip if
      # the entry should feel like a Steelers fan's entry; Q17 and Q24 are the
      # expensive ones and stay as modelled.
      "PIT", 0.451, "Buddy's override; model says BAL 54.9%, so PIT is 45.1%", True),
 (15, "Wk16 vs CAR", "More total yards of offense", "PIT / CAR / Tie",
      "PIT", 0.60, "home vs a weak opponent; yards are less noisy than points", True),
 (16, "Wk17 @ TEN",  "The last score of the game", "TD / FG / Safety / None",
      "TD", 0.581, "MEASURED: 58.1% of 222 games in 2025", False),
 (17, "Wk18 @ BAL",  "Who wins", "PIT / BAL / Tie",
      "BAL", 0.672, "modelled; Week 18 rest risk is the wildcard", True),
 (18, "season", "Most rushing + receiving TDs",
      "J. Warren / R. Dowdle / DK Metcalf / M. Pittman Jr. / P. Freiermuth / D. Washington / Other",
      "DK Metcalf", 0.30, "backfield split 3 ways; Metcalf unambiguous WR1", True),
 (19, "season", "Longest offensive TD of the season",
      "J. Warren / R. Dowdle / DK Metcalf / M. Pittman Jr. / P. Freiermuth / D. Washington / Other",
      "DK Metcalf", 0.40, "14.4 yards per catch — the only deep threat", True),
 (20, "season", "Leads the Steelers in sacks",
      "P. Queen / T.J. Watt / A. Highsmith / P. Wilson / C. Heyward / N. Herbig / J. Brisker / D. Elliott / J. Porter Jr. / Other",
      "T.J. Watt", 0.45, "Watt 11.5 then 7.0; Highsmith led 2025 at a 12.4 pace", True),
 (21, "season", "Steelers interceptions", "0-10 / 11-15 / 16-19 / 20-24 / 25+",
      "16-19", 0.35, "8-year mode AND the 5-year mean (16.2) agree", False),
 (22, "season", "Steelers field goals", "0-20 / 21-25 / 26-30 / 31-35 / 36+",
      "26-30", 0.35, "8-year mode; 17-game era is a 3-way split", False),
 (23, "season", "Regular-season wins", "0-17 (pick a number)",
      # BUDDY'S CALL, 2026-09-09. Was 8 (the market's implied mean is ~8.2 with
      # the under at -140). Changed to 9 on his judgement, backed by 4 of the 5
      # polled models also saying 9. The two are within noise of each other --
      # at a 2.3-win standard deviation, P(8) and P(9) are both ~16-17% -- so
      # this is not a worse answer, it is a differently-argued one, and it is
      # the question where a feel for the team is worth as much as the model.
      "9", 0.17, "Buddy's call; 4 of 5 models agree; P(8) and P(9) are within "
                 "noise at a 2.3-win sd", True),
 (24, "season", "AFC North winner", "Bengals / Browns / Ravens / Steelers",
      "Ravens", 0.50, "BAL +102 favourite vs PIT +500", True),
]

DEADLINE = "2026-09-13 13:00 ET"


def seed(db_path=None):
    """Write all 24 into the CRM. Idempotent — a re-run with the same answers
    changes nothing and ledgers nothing; a CHANGED answer ledgers a
    field_change, which is the audit trail this exists for.

    Never sends `status` for an entity that already exists. Bug found S242
    (2026-09-21): seed() used to send status="pending" unconditionally, so
    ANY later call -- including the bare no-subcommand fallback this file's
    __main__ used to have, which a stray `--help` would fall into -- silently
    un-resolved every already-resolved question. Confirmed live: it wiped
    Q1's resolved status the day after S241 recorded it. status/actual belong
    to record_result() once a question resolves; seed() only owns the
    re-derivable prediction fields (answer/confidence/basis/etc)."""
    created = changed = 0
    for num, when, q, opts, ans, conf, basis, volatile in QUESTIONS:
        slug = f"q{num:02d}"
        exists = entity_kb.get_entity(PROJECT, slug, db_path=db_path) is not None
        fields = {"number": str(num), "when": when, "question": q,
                  "options": opts, "answer": ans,
                  "confidence": f"{conf:.3f}", "basis": basis,
                  "volatile": "yes" if volatile else "no",
                  "deadline": DEADLINE}
        if not exists:
            fields["status"] = "pending"
        res = entity_kb.upsert_entity(
            PROJECT, slug, f"Q{num}: {q}",
            entity_type="contest question",
            fields=fields,
            db_path=db_path)
        if res.get("created"):
            created += 1
            continue          # a new entity reports every field as "changed"
        if res.get("changed_fields"):
            changed += 1
            print(f"  {slug} CHANGED: {res['changed_fields']}")
    return created, changed


def answers(db_path=None):
    """The current entry, straight from the CRM."""
    out = []
    for e in entity_kb.list_entities(PROJECT, db_path=db_path):
        # entity_kb returns the field dict under "state", not "fields"
        f = e.get("state") or {}
        try:
            out.append((int(f.get("number", 0)), f))
        except ValueError:
            pass
    return [f for _, f in sorted(out)]


def record_result(num, actual, note="", db_path=None):
    """Mark question NUM resolved against what actually happened (Buddy,
    S241, 2026-09-20: "can we monitor our picks after each week").

    Ledgers a `record_outcome` event (append-only, so re-recording the same
    question a second time adds a second event rather than silently
    overwriting -- callers that want to avoid duplicates should check
    `answers()`'s status first) and flips the entity's `status` field to
    "resolved" via `upsert_entity`, so `answers()`/`tally()` see it without
    having to replay the event log.

    Returns {slug, answer, actual, correct, status}, or None if NUM is not
    one of the 24 questions.
    """
    slug = f"q{num:02d}"
    e = entity_kb.get_entity(PROJECT, slug, db_path=db_path)
    if not e:
        return None
    answer = e.get("state", {}).get("answer")
    correct = (answer == actual)
    entity_kb.record_outcome(PROJECT, slug, "correct" if correct else "incorrect",
                              note=note or f"actual={actual}", db_path=db_path)
    entity_kb.upsert_entity(PROJECT, slug, e.get("name", f"Q{num}"),
                            fields={"status": "resolved", "actual": actual},
                            db_path=db_path)
    return {"slug": slug, "answer": answer, "actual": actual,
            "correct": correct, "status": "resolved"}


def snapshot_leader(num, current_leader, current_value="", note="", db_path=None):
    """Record a NON-authoritative running snapshot for a season-long question
    (Q18-Q24) that cannot resolve until the season ends. Buddy, S242,
    2026-09-21: "we can keep track of leaders for these questions." Writes
    `leader_snapshot`/`leader_value`/`leader_snapshot_at` -- deliberately
    separate fields from `status`/`actual`, which stay reserved for the real
    end-of-season result via record_result(). Safe to call every week; each
    change is still ledgered by upsert_entity's normal diff, so the history
    of who was "currently leading" over the season is preserved too.

    Returns {slug, leader, value} or None if NUM is not one of the 24
    questions.
    """
    slug = f"q{num:02d}"
    e = entity_kb.get_entity(PROJECT, slug, db_path=db_path)
    if not e:
        return None
    entity_kb.upsert_entity(
        PROJECT, slug, e.get("name", f"Q{num}"),
        fields={"leader_snapshot": current_leader,
                "leader_value": str(current_value),
                "leader_snapshot_note": note},
        db_path=db_path)
    return {"slug": slug, "leader": current_leader, "value": current_value}


def tally(db_path=None):
    """Running score: how many resolved questions we got right, out of how
    many total, and whether the perfect-24 is still alive. Unresolved
    questions count toward neither -- this reports what has actually
    happened, not a projection of what has not."""
    rows = answers(db_path=db_path)
    resolved = [r for r in rows if r.get("status") == "resolved"]
    correct = [r for r in resolved if r.get("actual") == r.get("answer")]
    missed = [int(r["number"]) for r in resolved if r.get("actual") != r.get("answer")]
    return {"resolved": len(resolved), "correct": len(correct), "total": len(rows),
            "perfect_alive": len(missed) == 0, "missed": sorted(missed)}


def selftest() -> int:
    import tempfile
    fails = 0

    def ck(name, cond):
        nonlocal fails
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        fails += 0 if cond else 1

    fd, tmp = tempfile.mkstemp(suffix=".db")
    import os
    os.close(fd); os.unlink(tmp)          # T32: never the live CRM
    try:
        ck("there are exactly 24 questions", len(QUESTIONS) == 24)
        ck("they are numbered 1..24 with no gaps",
           [q[0] for q in QUESTIONS] == list(range(1, 25)))
        ck("every answer is one of that question's own options",
           all(a.split(" (")[0] in [o.strip() for o in opts.replace("/", " / ").split(" / ")]
               or "pick a number" in opts
               for _, _, _, opts, a, _, _, _ in QUESTIONS))
        ck("every confidence is a real probability",
           all(0.0 < c <= 1.0 for *_, c, _, _ in
               [(q[0], q[1], q[2], q[3], q[4], q[5], q[6], q[7]) for q in QUESTIONS]))

        c, ch = seed(db_path=tmp)
        # A newly created entity reports every field as changed, which is true
        # but not interesting; seed() counts it as a creation only.
        ck("seeding creates 24 entities and reports no CHANGES",
           c == 24 and ch == 0)
        c2, ch2 = seed(db_path=tmp)
        ck("re-seeding the SAME answers changes nothing (idempotent)",
           c2 == 0 and ch2 == 0)

        # The point of using a CRM: a changed answer leaves a trail.
        #
        # The fixture is derived, NOT hard-coded. The first version wrote
        # `fields={"answer": "PIT"}` against q14 because q14's answer happened
        # to be BAL at the time -- so when Buddy legitimately changed q14 TO
        # PIT, the "change" became a no-op and both checks failed. The test
        # broke because the DATA moved, which is a test coupled to live values
        # and the same family as T32. Flip to a sentinel that cannot collide
        # with any real answer.
        before = (entity_kb.get_entity(PROJECT, "q14", db_path=tmp)
                  or {}).get("state", {}).get("answer")
        entity_kb.upsert_entity(PROJECT, "q14", "Q14: Who wins",
                                fields={"answer": "__test_sentinel__"},
                                db_path=tmp)
        evs = entity_kb.get_events(PROJECT, slug="q14", db_path=tmp)
        ck("changing an answer ledgers a field_change event",
           any((e.get("event_type") or "") == "field_change" for e in evs))
        # entity_kb JSON-encodes the values, so "BAL" is stored as '"BAL"'
        def _v(x):
            try: return json.loads(x)
            except Exception: return x
        ck("...and the event records BOTH the old and the new value",
           any(_v(e.get("old_value")) == before
               and _v(e.get("new_value")) == "__test_sentinel__"
               for e in evs))
        ck("...and the fixture is derived from the data, so a future answer "
           "change cannot break this test",
           before is not None and before != "__test_sentinel__")

        got = answers(db_path=tmp)
        ck("answers() returns all 24 in question order",
           len(got) == 24 and [int(f["number"]) for f in got] == list(range(1, 25)))

        # q02's seeded answer is "Yes"; q03's is "PIT" -- record one hit, one miss.
        r_hit = record_result(2, "Yes", note="test", db_path=tmp)
        ck("record_result reports a correct pick", r_hit is not None and r_hit["correct"] is True)
        q2 = next(f for f in answers(db_path=tmp) if int(f["number"]) == 2)
        ck("...and flips status to resolved", q2.get("status") == "resolved")
        ck("...and stores the actual value", q2.get("actual") == "Yes")

        r_miss = record_result(3, "CIN", note="test", db_path=tmp)
        ck("record_result reports an incorrect pick", r_miss is not None and r_miss["correct"] is False)

        ck("record_result on a question that does not exist returns None",
           record_result(99, "x", db_path=tmp) is None)

        # S242 regression: re-seeding after a question resolves must NOT
        # touch its status/actual. This is the exact bug that silently wiped
        # Q1's resolved status live on 2026-09-21.
        seed(db_path=tmp)
        q2_after_reseed = next(f for f in answers(db_path=tmp) if int(f["number"]) == 2)
        ck("S242: re-seeding after resolve leaves status alone",
           q2_after_reseed.get("status") == "resolved")
        ck("S242: re-seeding after resolve leaves actual alone",
           q2_after_reseed.get("actual") == "Yes")

        t = tally(db_path=tmp)
        ck("tally counts exactly the resolved questions", t["resolved"] == 2)
        ck("tally counts exactly the correct ones", t["correct"] == 1)
        ck("tally flags the perfect score as gone once one miss is resolved",
           t["perfect_alive"] is False and t["missed"] == [3])

        snap = snapshot_leader(20, "T.J. Watt", 8.5, note="test", db_path=tmp)
        ck("snapshot_leader reports the slug and leader",
           snap is not None and snap["slug"] == "q20" and snap["leader"] == "T.J. Watt")
        q20 = next(f for f in answers(db_path=tmp) if int(f["number"]) == 20)
        ck("...and it lands in the CRM as leader_snapshot",
           q20.get("leader_snapshot") == "T.J. Watt" and q20.get("leader_value") == "8.5")
        ck("...without touching status (still pending -- not a real resolution)",
           q20.get("status") == "pending")
        ck("snapshot_leader on a question that does not exist returns None",
           snapshot_leader(99, "x", db_path=tmp) is None)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print(f"\n{'ALL PASS' if not fails else f'{fails} FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    if "selftest" in sys.argv:
        sys.exit(selftest())
    if "show" in sys.argv:
        for f in answers():
            print(f"  Q{f['number']:>2} {f['when']:16s} {f['answer']:14s} "
                  f"{float(f['confidence'])*100:4.1f}%  {f['question'][:44]}")
        sys.exit(0)
    if "tally" in sys.argv:
        t = tally()
        print(f"  {t['correct']}/{t['resolved']} resolved correct "
              f"({t['total']} total questions), "
              f"perfect score {'ALIVE' if t['perfect_alive'] else 'GONE'}"
              + (f" -- missed: {t['missed']}" if t['missed'] else ""))
        sys.exit(0)
    if "record" in sys.argv:
        # record NUM ACTUAL ["note text"]
        i = sys.argv.index("record")
        num, actual = int(sys.argv[i + 1]), sys.argv[i + 2]
        note = sys.argv[i + 3] if len(sys.argv) > i + 3 else ""
        res = record_result(num, actual, note=note)
        print(res if res else f"Q{num} not found")
        sys.exit(0 if res else 1)
    if "seed" in sys.argv:
        created, changed = seed()
        print(f"immaculate CRM: {created} created, {changed} changed, "
              f"{len(QUESTIONS)} total. Locks {DEADLINE}.")
        sys.exit(0)
    if "snapshot-leader" in sys.argv:
        # snapshot-leader NUM "current leader" [value] ["note"]
        i = sys.argv.index("snapshot-leader")
        num, leader = int(sys.argv[i + 1]), sys.argv[i + 2]
        value = sys.argv[i + 3] if len(sys.argv) > i + 3 else ""
        note = sys.argv[i + 4] if len(sys.argv) > i + 4 else ""
        res = snapshot_leader(num, leader, value, note=note)
        print(res if res else f"Q{num} not found")
        sys.exit(0 if res else 1)
    # S242: no more silent seed() on an unrecognized/missing argument (a
    # stray `--help` triggered exactly this and wiped Q1's resolved status).
    print("usage: immaculate_store.py {selftest|show|tally|record NUM ACTUAL [NOTE]"
          "|seed|snapshot-leader NUM LEADER [VALUE] [NOTE]}")
    sys.exit(1)
