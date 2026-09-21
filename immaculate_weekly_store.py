#!/usr/bin/env python3
"""Project Immaculate — the separate weekly mini-contest, in the same CRM.

Distinct from immaculate_store.py's fixed 24-question SEASON entry (see
immaculate/WEEK-2-ANSWERS.md: "Confirmed ... this is a DIFFERENT contest
from the original $100K 24-question season entry"). This one recurs roughly
weekly with a variable question count and its own lock each time, so it
doesn't fit a hardcoded 24-question array — but it reuses entity_kb exactly
the same way, under slugs w{week:02d}q{num:02d}, so it shares the CRM's
audit-trail/event-log machinery for free instead of inventing new storage.

S242 fix applied from day one: record_week() never sends status/actual for
a question that already exists, so a re-run can't silently un-resolve a
week already checked against reality. See immaculate_store.py's seed() for
the incident this avoids (it happened live, 2026-09-21).
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import entity_kb  # noqa: E402

PROJECT = "immaculate"


def _slug(week, num):
    return f"w{week:02d}q{num:02d}"


def record_week(week, questions, db_path=None):
    """Seed one week's question set. `questions` is a list of dicts:
    {num, question, options, answer, basis} (confidence optional).
    Idempotent per question — re-running with the same content changes
    nothing; a changed prediction ledgers a field_change, same as
    immaculate_store.seed(). Never touches status/actual on an existing
    entity (S242)."""
    created = changed = 0
    for q in questions:
        slug = _slug(week, q["num"])
        exists = entity_kb.get_entity(PROJECT, slug, db_path=db_path) is not None
        fields = {"week": str(week), "number": str(q["num"]),
                  "question": q["question"], "options": q.get("options", ""),
                  "answer": q["answer"], "basis": q.get("basis", ""),
                  "confidence": str(q.get("confidence", ""))}
        if not exists:
            fields["status"] = "pending"
        res = entity_kb.upsert_entity(
            PROJECT, slug, f"W{week} Q{q['num']}: {q['question']}",
            entity_type="weekly contest question",
            fields=fields, db_path=db_path)
        if res.get("created"):
            created += 1
            continue
        if res.get("changed_fields"):
            changed += 1
    return created, changed


def week_answers(week, db_path=None):
    """All questions for one week, in question-number order."""
    out = []
    for e in entity_kb.list_entities(PROJECT, db_path=db_path):
        f = e.get("state") or {}
        if str(f.get("week", "")) != str(week):
            continue
        try:
            out.append((int(f.get("number", 0)), f))
        except ValueError:
            pass
    return [f for _, f in sorted(out)]


def resolve(week, num, actual, note="", db_path=None):
    """Mark one weekly-contest question resolved against reality. Mirrors
    immaculate_store.record_result(). Returns {slug, answer, actual,
    correct, status}, or None if that (week, num) is not on file."""
    slug = _slug(week, num)
    e = entity_kb.get_entity(PROJECT, slug, db_path=db_path)
    if not e:
        return None
    answer = e.get("state", {}).get("answer")
    correct = (answer == actual)
    entity_kb.record_outcome(PROJECT, slug, "correct" if correct else "incorrect",
                              note=note or f"actual={actual}", db_path=db_path)
    entity_kb.upsert_entity(PROJECT, slug, e.get("name", slug),
                            fields={"status": "resolved", "actual": actual},
                            db_path=db_path)
    return {"slug": slug, "answer": answer, "actual": actual,
            "correct": correct, "status": "resolved"}


def week_tally(week, db_path=None):
    """Same shape as immaculate_store.tally(), scoped to one week."""
    rows = week_answers(week, db_path=db_path)
    resolved = [r for r in rows if r.get("status") == "resolved"]
    correct = [r for r in resolved if r.get("actual") == r.get("answer")]
    missed = [int(r["number"]) for r in resolved if r.get("actual") != r.get("answer")]
    return {"week": week, "resolved": len(resolved), "correct": len(correct),
            "total": len(rows), "missed": sorted(missed)}


def weeks_seeded(db_path=None):
    """Which week numbers have any questions on file, sorted."""
    weeks = set()
    for e in entity_kb.list_entities(PROJECT, db_path=db_path):
        f = e.get("state") or {}
        w = f.get("week")
        if w:
            try:
                weeks.add(int(w))
            except ValueError:
                pass
    return sorted(weeks)


def selftest() -> int:
    import tempfile, os
    fails = 0

    def ck(name, cond):
        nonlocal fails
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        fails += 0 if cond else 1

    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd); os.unlink(tmp)          # T32: never the live CRM
    try:
        qs = [
            {"num": 1, "question": "Who wins?", "options": "A / B", "answer": "A", "basis": "test"},
            {"num": 2, "question": "Total points?", "options": "0-40 / 41+", "answer": "41+", "basis": "test"},
        ]
        c, ch = record_week(2, qs, db_path=tmp)
        ck("recording a new week creates its questions, reports no changes",
           c == 2 and ch == 0)
        c2, ch2 = record_week(2, qs, db_path=tmp)
        ck("re-recording the same week is idempotent", c2 == 0 and ch2 == 0)

        got = week_answers(2, db_path=tmp)
        ck("week_answers returns them in question order",
           len(got) == 2 and [int(f["number"]) for f in got] == [1, 2])

        ck("weeks_seeded finds week 2", weeks_seeded(db_path=tmp) == [2])

        r_hit = resolve(2, 1, "A", note="test", db_path=tmp)
        ck("resolve reports a correct pick", r_hit is not None and r_hit["correct"] is True)
        r_miss = resolve(2, 2, "0-40", note="test", db_path=tmp)
        ck("resolve reports an incorrect pick", r_miss is not None and r_miss["correct"] is False)
        ck("resolve on a question that does not exist returns None",
           resolve(2, 99, "x", db_path=tmp) is None)

        # S242 regression: re-recording the week after resolving must not
        # touch status/actual — the exact bug that hit immaculate_store.py.
        record_week(2, qs, db_path=tmp)
        q1 = next(f for f in week_answers(2, db_path=tmp) if int(f["number"]) == 1)
        ck("S242: re-recording after resolve leaves status alone",
           q1.get("status") == "resolved")
        ck("S242: re-recording after resolve leaves actual alone",
           q1.get("actual") == "A")

        t = week_tally(2, db_path=tmp)
        ck("week_tally counts exactly the resolved questions", t["resolved"] == 2)
        ck("week_tally counts exactly the correct ones", t["correct"] == 1)
        ck("week_tally names the missed question", t["missed"] == [2])
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print(f"\n{'ALL PASS' if not fails else f'{fails} FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    if "selftest" in sys.argv:
        sys.exit(selftest())
    if "tally" in sys.argv:
        i = sys.argv.index("tally")
        week = int(sys.argv[i + 1])
        t = week_tally(week)
        print(f"  Week {week}: {t['correct']}/{t['resolved']} resolved correct "
              f"({t['total']} total)"
              + (f" -- missed: {t['missed']}" if t["missed"] else ""))
        sys.exit(0)
    if "show" in sys.argv:
        i = sys.argv.index("show")
        week = int(sys.argv[i + 1])
        for f in week_answers(week):
            print(f"  Q{f['number']:>2} {f['answer']:<16s} "
                  f"{f.get('status', 'pending'):10s} {f['question'][:44]}")
        sys.exit(0)
    if "resolve" in sys.argv:
        i = sys.argv.index("resolve")
        week, num, actual = int(sys.argv[i + 1]), int(sys.argv[i + 2]), sys.argv[i + 3]
        note = sys.argv[i + 4] if len(sys.argv) > i + 4 else ""
        res = resolve(week, num, actual, note=note)
        print(res if res else f"W{week}Q{num} not found")
        sys.exit(0 if res else 1)
    print("usage: immaculate_weekly_store.py "
          "{selftest|show WEEK|tally WEEK|resolve WEEK NUM ACTUAL [NOTE]}")
    sys.exit(1)
