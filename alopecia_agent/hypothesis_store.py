"""hypothesis_store.py — persistent hypothesis ledger for the Alopecia
etiology-synthesis agent (S177).

This is the agent's memory: ranked, evidence-graded hypotheses about what
triggers the T-cell attack (ALOPECIA-SPEC.md's standing question, Buddy
S82), refined incrementally across daily wakes rather than starting cold
each time. Evidence grades reuse the exact A-E vocabulary alopecia_brief.py's
grade() already established (A controlled trial ... E unclassified) -- one
grading vocabulary for the whole project, not a second one invented here.

Git-tracked, same as alopecia/AA-FOUNDATION.md -- this is a real, reviewable
research artifact, not scratch state.

    python3 hypothesis_store.py --selftest
"""
import json
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
STATE_PATH = PROJECT_DIR / "alopecia" / "hypothesis_state.json"

VALID_GRADES = ("A", "B", "C", "D", "E")


def load(path=None):
    """Never raises: a missing or corrupt file is a cold start, not an error
    the caller has to handle specially.

    path=None resolves STATE_PATH INSIDE the call, not as a default-argument
    value -- a default arg is bound once at def-time, so a caller (or a
    test) that monkeypatches the module-level STATE_PATH afterward would be
    silently ignored if this were `def load(path=STATE_PATH)`.
    """
    path = path or STATE_PATH
    try:
        state = json.loads(Path(path).read_text())
        state.setdefault("hypotheses", [])
        state.setdefault("last_processed_date", None)
        return state
    except Exception:
        return {"hypotheses": [], "last_processed_date": None}


def save(state, path=None):
    """path=None resolves STATE_PATH at call time -- see load()'s docstring
    for why this can't be a default-argument value."""
    p = Path(path or STATE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def find(state, hyp_id):
    for h in state.get("hypotheses", []):
        if h.get("id") == hyp_id:
            return h
    return None


def upsert(state, hyp_id, statement, evidence_grade, supporting=(),
          contradicting=(), today=None):
    """Create a hypothesis, or refine an existing one with the same id.

    Refining ACCUMULATES supporting/contradicting citations (a hypothesis's
    evidence only grows across wakes, it never silently loses a citation a
    prior pass already found) and appends to confidence_trend only when the
    grade actually CHANGES -- a trend of the same grade repeated daily would
    bury the one entry that matters (an actual grade change) in noise.

    Returns the same state object, mutated in place, for convenience.
    """
    if evidence_grade not in VALID_GRADES:
        raise ValueError(f"evidence_grade must be one of {VALID_GRADES}, "
                         f"got {evidence_grade!r}")
    today = today or datetime.now().strftime("%Y-%m-%d")
    existing = find(state, hyp_id)
    if existing is None:
        state.setdefault("hypotheses", []).append({
            "id": hyp_id,
            "statement": statement,
            "evidence_grade": evidence_grade,
            "supporting_items": sorted(set(supporting)),
            "contradicting_items": sorted(set(contradicting)),
            "confidence_trend": [{"date": today, "grade": evidence_grade}],
            "first_seen": today,
            "last_updated": today,
        })
    else:
        existing["statement"] = statement
        existing["evidence_grade"] = evidence_grade
        existing["supporting_items"] = sorted(
            set(existing.get("supporting_items", [])) | set(supporting))
        existing["contradicting_items"] = sorted(
            set(existing.get("contradicting_items", [])) | set(contradicting))
        trend = existing.setdefault("confidence_trend", [])
        if not trend or trend[-1]["grade"] != evidence_grade:
            trend.append({"date": today, "grade": evidence_grade})
        existing["last_updated"] = today
    return state


def mark_processed(state, date):
    state["last_processed_date"] = date
    return state


def selftest():
    import tempfile
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        ok = ok and cond

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "hypothesis_state.json"

        check("load: a missing file returns an empty state, never raises",
              load(p) == {"hypotheses": [], "last_processed_date": None})

        p.write_text("{not json")
        check("load: a corrupt file also returns an empty state",
              load(p) == {"hypotheses": [], "last_processed_date": None})

        s = load(p)
        try:
            upsert(s, "h0", "x", "Z")
            _bad_grade_raised = False
        except ValueError:
            _bad_grade_raised = True
        check("upsert: an invalid grade letter raises rather than silently "
              "accepting garbage into the ledger", _bad_grade_raised)

        upsert(s, "h1", "viral trigger via molecular mimicry", "C",
              supporting=["pmid:1"], today="2026-09-01")
        save(s, p)
        s2 = load(p)
        check("upsert+save+load: a new hypothesis round-trips",
              len(s2["hypotheses"]) == 1 and s2["hypotheses"][0]["id"] == "h1")
        check("upsert: first_seen and last_updated both stamped on creation",
              s2["hypotheses"][0]["first_seen"] == "2026-09-01"
              and s2["hypotheses"][0]["last_updated"] == "2026-09-01")

        upsert(s2, "h1", "viral trigger via molecular mimicry (refined)", "B",
              supporting=["pmid:2"], today="2026-09-08")
        check("upsert: refining an EXISTING id updates it, does not duplicate",
              len(s2["hypotheses"]) == 1)
        check("upsert: supporting items ACCUMULATE across refinements, "
              "never dropped",
              set(s2["hypotheses"][0]["supporting_items"]) == {"pmid:1", "pmid:2"})
        check("upsert: a grade CHANGE appends to confidence_trend",
              len(s2["hypotheses"][0]["confidence_trend"]) == 2
              and s2["hypotheses"][0]["confidence_trend"][-1]
              == {"date": "2026-09-08", "grade": "B"})
        check("upsert: first_seen is preserved across refinements, "
              "last_updated moves",
              s2["hypotheses"][0]["first_seen"] == "2026-09-01"
              and s2["hypotheses"][0]["last_updated"] == "2026-09-08")

        upsert(s2, "h1", "restated but evidentially unchanged", "B",
              today="2026-09-09")
        check("upsert: an UNCHANGED grade does not pad confidence_trend "
              "with noise", len(s2["hypotheses"][0]["confidence_trend"]) == 2)

        upsert(s2, "h1", "x", "B", contradicting=["pmid:9"], today="2026-09-10")
        check("upsert: a contradicting citation is tracked separately from "
              "supporting, not merged into it",
              s2["hypotheses"][0]["contradicting_items"] == ["pmid:9"]
              and "pmid:9" not in s2["hypotheses"][0]["supporting_items"])

        upsert(s2, "h2", "a genuinely second, unrelated hypothesis", "D",
              today="2026-09-08")
        check("upsert: a different id is a NEW entry, never merged into h1",
              len(s2["hypotheses"]) == 2)
        check("find: locates by id", find(s2, "h2")["statement"]
              == "a genuinely second, unrelated hypothesis")
        check("find: a missing id returns None, not KeyError", find(s2, "nope") is None)

        mark_processed(s2, "2026-09-10")
        check("mark_processed: sets the cursor the agent reads next wake",
              s2["last_processed_date"] == "2026-09-10")

    print("PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
