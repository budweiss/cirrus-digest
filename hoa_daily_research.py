"""hoa_daily_research.py — daily deep-research batch for Bill's Delaware HOA
CRM (S65, HOA-KNOWLEDGE-BASE-PLAN.md increment 2 + the hoa_monitor merge).

Replaces hoa_monitor.py's standalone weekly email with a daily batch job
that writes everything it finds into entity_kb (project hoa_leads_bill)
instead of composing its own send. entity_kb_weekly_digest.py is now the
ONLY thing that emails Bill about Delaware HOA research — this fixes the
duplicate-Monday-email problem (hoa_monitor.py used to run its own X/web/
council search AND send its own email, on the same day the new CRM digest
would also send).

Two things every run, both write-only into entity_kb, no email sent here:

1. DISCOVER — reuses hoa_leads/hoa_monitor.py's existing X+web search + the
   4-LLM council UNCHANGED (gather_x, gather_web, council_filter). Any
   community the council flags that ISN'T already in entity_kb gets added
   with a "new-discovery" signal, so it's flagged first-seen and Monday's
   digest calls it out. Anything already known gets the council's finding
   logged as a regular signal on the existing entity instead.

2. REFRESH — a rotating chunk of already-known entities, oldest
   last_updated first (self-balancing: no separate cursor to maintain,
   naturally cycles through the whole KB over weeks). Each gets a live
   deep_research.deep_research_entity() pass.

Usage:
  python3 hoa_daily_research.py [--chunk-size N] [--dry-run]
  python3 hoa_daily_research.py selftest
"""
import re
import sys
from datetime import datetime
from pathlib import Path

import entity_kb

KB_PROJECT = "hoa_leads_bill"
DEFAULT_CHUNK_SIZE = 5  # ~180 communities / 5 per day ≈ one full cycle every 36 days

# S66: a bare entity name search collides with unrelated same-named orgs
# elsewhere -- caught live: "Auburn Meadows" pulled a WA senior-living
# facility, "Barrett Farm" pulled an unrelated MA organic farm, "Breeders
# Crown" pulled the harness-racing championship instead of the Delaware
# community, "Church Creek"/"Chestnut Ridge" pulled a MD town government and
# a NY village board. Every entity here is a Kent/Sussex/New Castle County
# Delaware HOA -- ground both the search query and the extraction step in
# that, per-entity county when known.
def _hoa_query_terms(entity: dict) -> str:
    county = (entity.get("state") or {}).get("county")
    return f"{county} County Delaware HOA homeowners association" if county \
        else "Delaware HOA homeowners association"

_HOA_CONTEXT_HINT = (
    "This must be a homeowners association / residential community in "
    "Delaware. Ignore any source about a same-named entity in a different "
    "U.S. state, or about a different kind of thing entirely (a business, "
    "farm, sporting event/championship, or municipal government) that "
    "merely shares this name -- it is NOT this Delaware HOA."
)


# S66, Buddy's ask: now that current_mgmt_co/board_contact are grounded in
# real Delaware sources (not the pre-fix pollution above), a genuinely
# out-of-state management company is a real, useful signal rather than
# noise -- it's a lead for Bill: an HOA currently paying an out-of-state PM
# firm is a candidate to pitch local management to. Same detection heuristic
# used to find the pollution in the first place, now repurposed as a
# feature. Distinguishes "no data" (nothing to flag) from "flagged".
_OTHER_STATE_RX = re.compile(
    r"\b(Washington|Maryland|Virginia|New York|Massachusetts|Michigan|"
    r"Oklahoma|California|Pennsylvania|New Jersey|Ohio|Georgia|Florida|"
    r"Texas|Illinois|North Carolina|South Carolina)\b", re.IGNORECASE)
_OUT_OF_STATE_AREA_CODE_RX = re.compile(
    r"\((?!302)(\d{3})\)|(?<!\d)(?!302)(\d{3})-\d{3}-\d{4}")


def out_of_state_mgmt_reason(fields: dict) -> str | None:
    """Fields already grounded to this specific Delaware entity (post-S66 fix)
    -- if the management company's own contact info reads as based in
    another state, that's a real fact about them, not a matching error.
    Returns a short reason string, or None if nothing to flag.

    State-name matching is scoped to current_mgmt_co ONLY, not
    board_contact -- board_contact routinely lists volunteer board members'
    personal names (e.g. "Washington Alava"), and several state names
    double as common first names (Washington, Georgia, Virginia). A company
    name stating a state ("FirstService Residential Maryland Inc.") is a
    reliable signal; a person's name is not. Area-code matching (an
    unambiguous phone-number fact) still applies to both fields."""
    mgmt_co = str(fields.get("current_mgmt_co", ""))
    state_m = _OTHER_STATE_RX.search(mgmt_co)
    if state_m:
        return f"management company name references {state_m.group(1)}"
    blob = " ".join(str(fields.get(k, "")) for k in ("current_mgmt_co", "board_contact"))
    area_m = _OUT_OF_STATE_AREA_CODE_RX.search(blob)
    if area_m:
        code = area_m.group(1) or area_m.group(2)
        return f"management contact phone has a non-Delaware area code ({code})"
    return None


def flag_out_of_state_mgmt(slug: str, name: str, fields: dict, db_path: str = None) -> bool:
    """Adds an out-of-state-mgmt signal if warranted and not already present
    for this entity (avoid re-flagging the same fact every refresh cycle).
    Returns True if a new signal was added."""
    reason = out_of_state_mgmt_reason(fields)
    if not reason:
        return False
    existing = entity_kb.get_events(KB_PROJECT, slug=slug, event_type="signal", db_path=db_path)
    if any(e.get("signal_kind") == "out-of-state-mgmt" for e in existing):
        return False
    entity_kb.add_signal(
        KB_PROJECT, slug, "out-of-state-mgmt",
        f"Currently managed by an out-of-state company ({reason}) — potential "
        f"local-PM pitch opportunity.", confidence="medium", db_path=db_path)
    return True


def pick_refresh_chunk(chunk_size: int = DEFAULT_CHUNK_SIZE, db_path: str = None) -> list:
    """Least-recently-updated entities first. Self-balancing rotation, no
    persisted cursor needed -- whatever hasn't been touched longest comes
    up next, including entities discovery just added."""
    entities = entity_kb.list_entities(KB_PROJECT, db_path=db_path)
    entities.sort(key=lambda e: e.get("last_updated") or "")
    return entities[:chunk_size]


def run_refresh(chunk_size: int = DEFAULT_CHUNK_SIZE, creds: dict = None,
                 dry_run: bool = False, db_path: str = None) -> dict:
    """Deep-research a rotating chunk of known entities. dry_run picks the
    same chunk but does no live search/write -- lets you see what WOULD run
    without spending anything."""
    import deep_research  # lazy: pulls in cirrus_daily/ensemble transitively

    chunk = pick_refresh_chunk(chunk_size, db_path=db_path)
    result = {"attempted": [e["name"] for e in chunk], "refreshed": 0, "found_new_info": 0,
              "out_of_state_mgmt": []}
    if dry_run or not chunk:
        return result
    for e in chunk:
        try:
            _recap, found = deep_research.deep_research_entity(
                KB_PROJECT, e["name"], e["slug"], creds,
                extra_query_terms=_hoa_query_terms(e),
                context_hint=_HOA_CONTEXT_HINT, db_path=db_path)
            result["refreshed"] += 1
            if found:
                result["found_new_info"] += 1
            after = entity_kb.get_entity(KB_PROJECT, e["slug"], db_path=db_path)
            if after and flag_out_of_state_mgmt(e["slug"], e["name"], after["state"], db_path=db_path):
                result["out_of_state_mgmt"].append(e["name"])
        except Exception:
            continue
    return result


def run_discovery(creds: dict = None, dry_run: bool = False, db_path: str = None) -> dict:
    """Search for community mentions via hoa_monitor's existing gather+
    council pipeline (unchanged); anything new goes into entity_kb flagged
    first-seen, anything already known gets logged as a regular signal.
    dry_run runs the real search/council (so you see genuine candidates)
    but writes nothing to entity_kb."""
    sys.path.insert(0, str(Path(__file__).parent / "hoa_leads"))
    import hoa_monitor  # lazy: transitively needs cirrus_daily/requests

    cands = hoa_monitor.gather_x() + hoa_monitor.gather_web()
    seen = hoa_monitor.load_seen()
    fresh = [c for c in cands if c["url"] not in seen]
    leads = hoa_monitor.council_filter(fresh[:hoa_monitor.MAX_CANDIDATES], creds) if fresh else []

    result = {"candidates": len(cands), "fresh_candidates": len(fresh),
               "council_kept": len(leads), "new_entities": [], "updated_entities": []}

    newly_seen = set()
    for lead in leads:
        name = (lead.get("community") or "").strip()
        if not name:
            continue
        matches = entity_kb.search_entities(KB_PROJECT, name, db_path=db_path, limit=1)
        is_new = len(matches) == 0
        slug = matches[0]["slug"] if matches else entity_kb.slugify(name)

        if is_new:
            result["new_entities"].append(name)
        else:
            result["updated_entities"].append(name)

        if not dry_run:
            # Resolve search-redirect links (Gemini grounding, etc.) to the real
            # destination URL before storing -- same reason hoa_monitor.py's own
            # main() does this before composing Bill's email: an opaque
            # vertexaisearch.cloud.google.com redirect link is useless to a
            # human reader and the redirect can expire.
            resolved_url = hoa_monitor._resolve_url(lead.get("url"))
            entity_kb.upsert_entity(KB_PROJECT, slug, name, entity_type="hoa", db_path=db_path)
            if is_new:
                summary = (f"First seen {datetime.now():%Y-%m-%d} via automated search — "
                           f"not previously in our records. {lead.get('why', '')}").strip()
                entity_kb.add_signal(KB_PROJECT, slug, "new-discovery", summary,
                                     source_url=resolved_url, confidence="medium",
                                     db_path=db_path)
            else:
                entity_kb.add_signal(KB_PROJECT, slug, lead.get("type", "other"),
                                     lead.get("why", ""), source_url=resolved_url,
                                     confidence="medium", db_path=db_path)
            newly_seen.add(lead["url"])

    if not dry_run and newly_seen:
        seen |= newly_seen
        hoa_monitor.save_seen(seen)

    return result


def run(chunk_size: int = DEFAULT_CHUNK_SIZE, dry_run: bool = False, db_path: str = None) -> dict:
    import json
    from pathlib import Path as _Path

    creds_path = _Path.home() / "projects/cirrus-digest/config/credentials.json"
    try:
        creds = json.loads(creds_path.read_text())
    except Exception as e:
        return {"ok": False, "reason": f"no creds: {e}"}

    discovery = run_discovery(creds, dry_run=dry_run, db_path=db_path)
    refresh = run_refresh(chunk_size, creds, dry_run=dry_run, db_path=db_path)
    return {"ok": True, "dry_run": dry_run, "discovery": discovery, "refresh": refresh}


def selftest() -> bool:
    """Offline-testable part only: pick_refresh_chunk's rotation logic. The
    discover/refresh live pipelines need network + API keys, same reasoning
    as deep_research.py/task_solver.py -- verified live after deploy."""
    import os
    import tempfile

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)
    checks = []
    try:
        entity_kb.upsert_entity(KB_PROJECT, "old-one", "Old One HOA", db_path=db_path,
                                occurred_at="2020-01-01 00:00:00")
        entity_kb.upsert_entity(KB_PROJECT, "new-one", "New One HOA", db_path=db_path,
                                occurred_at="2026-08-01 00:00:00")
        entity_kb.upsert_entity(KB_PROJECT, "middle-one", "Middle One HOA", db_path=db_path,
                                occurred_at="2023-01-01 00:00:00")

        chunk = pick_refresh_chunk(chunk_size=2, db_path=db_path)
        checks.append(("chunk picks the 2 least-recently-updated entities",
                       [e["slug"] for e in chunk] == ["old-one", "middle-one"]))

        full_chunk = pick_refresh_chunk(chunk_size=10, db_path=db_path)
        checks.append(("chunk size larger than the KB just returns everything",
                       len(full_chunk) == 3))

        empty_chunk = pick_refresh_chunk(chunk_size=0, db_path=db_path)
        checks.append(("chunk size 0 returns nothing", empty_chunk == []))

        checks.append(("out-of-state company name is flagged", bool(
            out_of_state_mgmt_reason({"current_mgmt_co": "FirstService Residential Maryland Inc."}))))
        checks.append(("out-of-state area code is flagged", bool(
            out_of_state_mgmt_reason({"board_contact": "Phone: (405) 348-1436"}))))
        checks.append(("a real Delaware area code (302) is NOT flagged", not
                       out_of_state_mgmt_reason({"board_contact": "Phone: (302) 555-1234"})))
        checks.append(("empty fields are NOT flagged", not
                       out_of_state_mgmt_reason({"current_mgmt_co": "", "board_contact": ""})))
        # S66 regression: caught live in the first backfill run -- a board
        # member literally named "Washington Alava" (real Woodside, DE HOA)
        # was mis-flagged before state-name matching was scoped to
        # current_mgmt_co only.
        checks.append(("a board member's name containing a state word is NOT flagged", not
                       out_of_state_mgmt_reason({"board_contact": "WASHINGTON ALAVA — Board "
                                                 "member, WOODSIDE DE"})))

        entity_kb.upsert_entity(KB_PROJECT, "oos-test", "OOS Test HOA", db_path=db_path)
        first = flag_out_of_state_mgmt("oos-test", "OOS Test HOA",
                                       {"board_contact": "(405) 348-1436"}, db_path=db_path)
        second = flag_out_of_state_mgmt("oos-test", "OOS Test HOA",
                                        {"board_contact": "(405) 348-1436"}, db_path=db_path)
        checks.append(("first out-of-state flag on an entity adds a signal", first is True))
        checks.append(("re-flagging the same entity is a no-op, not a duplicate signal",
                       second is False))
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)

    all_ok = all(ok for _, ok in checks)
    for desc, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
    return all_ok


if __name__ == "__main__":
    import argparse

    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(0 if selftest() else 1)

    parser = argparse.ArgumentParser()
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    outcome = run(chunk_size=args.chunk_size, dry_run=args.dry_run)
    print(outcome)
    sys.exit(0 if outcome.get("ok") else 1)
