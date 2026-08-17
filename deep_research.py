"""On-demand deep research for one entity_kb entity (S65).

Shared engine, not HOA-specific — search + fetch + 4-LLM-council extraction,
writing findings into entity_kb.py. Two intended callers:

1. task_solver.py's refresh trigger — a client emails asking for updated
   info on something specific; this runs live, once, for that one entity.
2. (Future, not built) a daily rotating-batch job that deep-dives N entities
   per project per day (increment 2 of docs/HOA-KNOWLEDGE-BASE-PLAN.md) —
   that job would call deep_research_entity() the same way, just in a loop
   instead of triggered by an email.

Reuses cirrus_daily.search_web / fetch_article_content (the same building
blocks hoa_leads/hoa_monitor.py already uses) and ensemble.best_answer's
4-provider council for extraction — no new search/fetch/council code.
"""
import json
from datetime import datetime

import entity_kb

# cirrus_daily/ensemble imported lazily inside deep_research_entity() --
# cirrus_daily needs `requests`/`bs4`, only installed in CIRRUS/CUMULUS's
# live venvs, not the local dev checkout. Keeps this module importable (and
# selftest() runnable) without those deps present.

MAX_SEARCH_RESULTS = 4
MAX_FETCH_CHARS = 6000  # per source, keeps the council prompt bounded

_EXTRACT_SYSTEM = (
    "You are extracting structured facts about a specific organization from "
    "web page excerpts. A generic entity name can collide with unrelated "
    "organizations elsewhere (a same-named business, farm, event, or "
    "municipality in a different location) -- when a CONTEXT line is given, "
    "first check each source is actually about that specific entity in that "
    "context; if a source is clearly about a different, unrelated "
    "organization that merely shares the name, IGNORE that source entirely "
    "-- do not extract any fact from it, even a plausible-sounding one. "
    "Given source texts that pass that check, extract ONLY what the text "
    "directly supports: current management company (if any), public board/"
    "contact info, and notable recent signals (leadership change, distress, "
    "RFP, policy change, complaint) each with a date if known and which "
    "source it came from. Never invent or guess a fact the text doesn't "
    "support. Respond as JSON only, no prose, no markdown fences: "
    '{"fields": {"current_mgmt_co": "...", "board_contact": "..."}, '
    '"signals": [{"kind": "...", "summary": "...", "source_url": "...", '
    '"confidence": "high|medium|low"}]}. If nothing new is found (including '
    'when every source was ignored as off-context), return '
    '{"fields": {}, "signals": []}.'
)


def _parse_extraction(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    return json.loads(text)


def deep_research_entity(kb_project: str, entity_name: str, slug: str, creds: dict,
                          extra_query_terms: str = "", context_hint: str = "",
                          db_path: str = None) -> tuple:
    """Search + fetch + council-extract fresh info about one entity, write
    findings into entity_kb (project=kb_project, slug=slug). Returns
    (recap_text, findings_count) -- findings_count is 0 if the pipeline
    found nothing new (search failure, no fetchable content, or the council
    genuinely found nothing beyond what's already on record). entity_kb is
    still touched (last_updated bumped) even when nothing new is found, so
    "we checked and there's nothing new" is itself recorded.

    extra_query_terms / context_hint (S66): a bare entity_name search can
    collide with an unrelated same-named organization elsewhere (e.g. an
    HOA called "Auburn Meadows" pulling in a Washington-state senior-living
    facility, or "Breeders Crown" pulling in the harness-racing
    championship instead of the Delaware community). extra_query_terms
    narrows the SEARCH (e.g. "Kent County Delaware HOA"); context_hint tells
    the EXTRACTION step what to reject a source for. Both are caller-
    supplied (not hardcoded here) since this module is shared across
    projects, not HOA-specific -- see hoa_daily_research.py / task_solver.py
    for how the HOA project fills them in."""
    import cirrus_daily
    import ensemble

    query = f"{entity_name} {extra_query_terms}".strip()
    try:
        urls = cirrus_daily.search_web(query, max_results=MAX_SEARCH_RESULTS)
    except Exception:
        urls = []

    sources = []
    for url in urls:
        try:
            content, _paywalled = cirrus_daily.fetch_article_content(url)
        except Exception:
            continue
        if content:
            sources.append((url, content[:MAX_FETCH_CHARS]))

    if not sources:
        entity_kb.upsert_entity(kb_project, slug, entity_name, db_path=db_path)
        return entity_kb.recap_text(kb_project, slug, db_path=db_path), 0

    source_block = "\n\n".join(f"SOURCE: {url}\n{text}" for url, text in sources)
    context_line = f"CONTEXT: {context_hint}\n" if context_hint else ""
    question = f"Extract facts about: {entity_name}\n{context_line}\n{source_block}"

    try:
        _, text = ensemble.best_answer(
            _EXTRACT_SYSTEM, question, creds, task="entity-kb-deep-research",
            session_id=f"deep-research-{slug}-{datetime.now().isoformat()}",
            mode="council")
        parsed = _parse_extraction(text)
    except Exception:
        entity_kb.upsert_entity(kb_project, slug, entity_name, db_path=db_path)
        return entity_kb.recap_text(kb_project, slug, db_path=db_path), 0

    fields = {k: v for k, v in (parsed.get("fields") or {}).items() if v}
    entity_kb.upsert_entity(kb_project, slug, entity_name, fields=fields, db_path=db_path)

    count = 0
    for sig in parsed.get("signals") or []:
        if not sig.get("summary"):
            continue
        entity_kb.add_signal(
            kb_project, slug, sig.get("kind", "other"), sig["summary"],
            source_url=sig.get("source_url"), confidence=sig.get("confidence"),
            db_path=db_path)
        count += 1

    return entity_kb.recap_text(kb_project, slug, db_path=db_path), count


def selftest() -> bool:
    """Offline-testable part only: the JSON extraction parser. The actual
    search/fetch/council pipeline needs live network + API keys, same as
    task_solver.py's solve_and_answer/try_auto_resend -- not unit-tested,
    verified live after deploy instead."""
    checks = []
    checks.append(("plain JSON parses", _parse_extraction(
        '{"fields": {"a": "b"}, "signals": []}') == {"fields": {"a": "b"}, "signals": []}))
    checks.append(("fenced ```json block parses", _parse_extraction(
        '```json\n{"fields": {}, "signals": []}\n```') == {"fields": {}, "signals": []}))
    checks.append(("bare fenced block parses", _parse_extraction(
        '```\n{"fields": {}, "signals": []}\n```') == {"fields": {}, "signals": []}))

    all_ok = all(ok for _, ok in checks)
    for desc, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
    return all_ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
