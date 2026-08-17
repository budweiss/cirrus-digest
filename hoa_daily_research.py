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
    result = {"attempted": [e["name"] for e in chunk], "refreshed": 0, "found_new_info": 0}
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
