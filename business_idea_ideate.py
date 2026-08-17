"""business_idea_ideate.py — council-generated business candidates (S66).

The companion to business_idea_scan.py. That script waits for the world to
publish a case study worth noticing; this one asks the funded LLM council
directly: "given EXACTLY this infrastructure and these constraints, what
businesses should Buddy start?" Buddy's ask was explicit -- "let's use our
foundational models to help too, getting inputs" -- and it's the difference
between having a shortlist today versus in three weeks.

Two deliberate design choices:

1. DIVERSE LENSES, not one generic ask. A single "give me business ideas"
   prompt returns ten variations of the same idea. Instead each lens below
   frames a genuinely different business shape and generates independently,
   the same reason self_review's council uses multiple providers rather than
   asking one model twice.

2. GENERATION AND SCORING ARE SEPARATE PASSES. Every generated idea is scored
   through business_idea_scan._relevance -- the SAME gate, same mission, same
   RELEVANCE_MIN bar that RSS-sourced candidates must clear. A model grading
   its own homework is worthless; this makes the council argue with itself,
   and means both intake paths are held to one consistent standard.

Usage:
  python3 business_idea_ideate.py [--lens KEY] [--dry-run]
  python3 business_idea_ideate.py selftest
"""
import json
import re
import sys
from pathlib import Path

import entity_kb
from business_idea_scan import (CAPABILITIES, KB_PROJECT, MISSION,
                                RELEVANCE_MIN, _relevance, resolve_slug)

PROJECT_DIR = Path.home() / "projects/cirrus-digest"

# Each lens is a genuinely different business shape. Buddy's steer: automated
# media (video/shorts/podcast) is explicitly of high interest, but "keep an
# open mind -- anything on the table", so the lenses deliberately span well
# beyond it. Add a lens here rather than broadening an existing one.
LENSES = [
    {
        "key": "automated-media",
        "focus": "Automated media and content businesses -- YouTube channels, short-form "
                 "video, podcasts, or audio products where the research, scripting, "
                 "production, and publishing all run on a schedule without daily human "
                 "involvement. Monetized via ad revenue, sponsorship, affiliate, or a "
                 "paid tier.",
    },
    {
        "key": "compounding-data",
        "focus": "Data and knowledge products whose value COMPOUNDS with time -- a "
                 "knowledge base, index, dataset, ranking, or monitoring service that "
                 "gets more valuable and more defensible the longer it runs, sold by "
                 "subscription or API access. Note Buddy already owns a proven "
                 "compounding-knowledge engine (entity_kb).",
    },
    {
        "key": "self-running-saas",
        "focus": "Software products where the product IS the automation -- a tool or "
                 "service customers subscribe to, whose delivery requires no per-customer "
                 "human labor. Favor ones a single operator can run.",
    },
    {
        "key": "attention-arbitrage",
        "focus": "Businesses that exploit an information or attention gap at machine "
                 "speed -- surfacing, aggregating, translating, summarizing, or "
                 "repackaging information that is public but hard to find, faster or "
                 "deeper than anyone doing it by hand.",
    },
    {
        "key": "wildcard",
        "focus": "Deliberately unconventional ideas that do not fit the categories above. "
                 "Take real swings -- unusual niches, overlooked markets, novel "
                 "applications of always-on GPU compute and a multi-model council. "
                 "Prioritize genuine originality over safety.",
    },
]

_GEN_SYSTEM = (
    "You are a pragmatic operator advising on which business to actually start. "
    "You favor concrete, specific, checkable proposals over vague categories, and "
    "you are honest about what would need to be built. You never propose something "
    "that requires ongoing manual human labor per unit of output."
)

_IDEAS_PER_LENS = 4


def _gen_prompt(lens: dict) -> str:
    return (
        f"{MISSION}\n\n{CAPABILITIES}\n\n"
        f"LENS FOR THIS REQUEST: {lens['focus']}\n\n"
        f"Propose exactly {_IDEAS_PER_LENS} specific businesses fitting this lens. "
        f"Be concrete: name a real niche, a real customer, a real revenue mechanism. "
        f"'An AI newsletter' is useless; 'a daily automated briefing on FDA device "
        f"recalls sold to medical-device compliance teams' is useful.\n\n"
        f"Respond as JSON only, no prose, no markdown fences:\n"
        f'{{"ideas": [{{"name": "<short distinctive name, 3-8 words>", '
        f'"what": "<what it does and for whom, 1-2 sentences>", '
        f'"who_pays": "<who the paying customer is and roughly what they would pay>", '
        f'"autonomous_loop": "<what the scheduled software actually does each day, '
        f'end to end, with no human in the loop>", '
        f'"needs_building": "<what does not exist yet and would have to be built>", '
        f'"why_now": "<why this is viable now and not already saturated>"}}]}}'
    )


def _parse_ideas(text: str) -> list:
    """Tolerant JSON extraction -- same fenced-block handling as
    deep_research._parse_extraction, plus a brace-scan fallback for models
    that wrap JSON in stray prose despite being told not to."""
    if not text:
        return []
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    try:
        return (json.loads(t) or {}).get("ideas", []) or []
    except Exception:
        pass
    m = re.search(r"\{.*\}", t, re.DOTALL)
    if m:
        try:
            return (json.loads(m.group(0)) or {}).get("ideas", []) or []
        except Exception:
            pass
    return []


def _idea_as_text(idea: dict) -> str:
    """Flatten a generated idea into the same shape the scoring gate expects
    from an article excerpt, so one gate serves both intake paths."""
    return (f"{idea.get('what', '')}\n\n"
            f"Paying customer: {idea.get('who_pays', '')}\n"
            f"Autonomous daily loop: {idea.get('autonomous_loop', '')}\n"
            f"Still to build: {idea.get('needs_building', '')}\n"
            f"Why now: {idea.get('why_now', '')}")


def generate(lens: dict, creds: dict) -> list:
    """Council-generate candidate ideas for one lens. Returns [] on any
    failure -- one dead lens must not abort the whole run."""
    try:
        import ensemble
        _meta, text = ensemble.best_answer(
            _GEN_SYSTEM, _gen_prompt(lens), creds, max_tokens=3000,
            task="business-idea-ideate", mode="council")
        return _parse_ideas(text)
    except Exception as e:
        print(f"  lens {lens['key']}: generation failed ({e})")
        return []


def run(only_lens: str = None, dry_run: bool = False, db_path: str = None) -> dict:
    try:
        creds = json.loads((PROJECT_DIR / "config/credentials.json").read_text())
    except Exception:
        creds = {}

    lenses = [l for l in LENSES if not only_lens or l["key"] == only_lens]
    result = {"generated": 0, "admitted": [], "corroborated": [], "rejected": []}

    for lens in lenses:
        print(f"[lens] {lens['key']}...")
        ideas = generate(lens, creds)
        result["generated"] += len(ideas)
        for idea in ideas:
            name = (idea.get("name") or "").strip()
            if not name:
                continue
            score, why, _label = _relevance(name, _idea_as_text(idea), creds)
            if score < RELEVANCE_MIN:
                result["rejected"].append(f"{name} ({score}/10)")
                continue
            if dry_run:
                result["admitted"].append(f"{name} ({score}/10) [dry-run]")
                continue

            slug, is_new = resolve_slug(name, name, db_path=db_path)
            entity_kb.upsert_entity(
                KB_PROJECT, slug, name, entity_type="business_idea",
                fields={"category": f"ideated:{lens['key']}",
                        "what": idea.get("what", ""),
                        "who_pays": idea.get("who_pays", ""),
                        "autonomous_loop": idea.get("autonomous_loop", ""),
                        "needs_building": idea.get("needs_building", ""),
                        "why_now": idea.get("why_now", "")},
                db_path=db_path)
            entity_kb.add_signal(
                KB_PROJECT, slug, "candidate",
                f"[{score}/10] {why} (council-ideated, lens: {lens['key']})",
                confidence="medium", db_path=db_path)
            (result["admitted"] if is_new else result["corroborated"]).append(
                f"{name} ({score}/10)")

    return result


def selftest() -> bool:
    """Offline: JSON parsing tolerance and idea flattening. The live
    generate/score pass needs network + API keys -- verified live after
    deploy, same reasoning as business_idea_scan.py."""
    checks = []
    checks.append(("plain JSON parses", len(_parse_ideas(
        '{"ideas": [{"name": "A"}, {"name": "B"}]}')) == 2))
    checks.append(("fenced ```json block parses", len(_parse_ideas(
        '```json\n{"ideas": [{"name": "A"}]}\n```')) == 1))
    checks.append(("JSON wrapped in stray prose still parses", len(_parse_ideas(
        'Sure, here you go:\n{"ideas": [{"name": "A"}]}\nHope that helps!')) == 1))
    checks.append(("garbage returns empty, not an exception",
                   _parse_ideas("no json at all here") == []))
    checks.append(("empty input returns empty", _parse_ideas("") == []))
    flat = _idea_as_text({"what": "W", "who_pays": "P", "autonomous_loop": "L",
                          "needs_building": "B", "why_now": "N"})
    checks.append(("flattened idea keeps every field",
                   all(x in flat for x in ("W", "P", "L", "B", "N"))))
    checks.append(("every lens has a unique key",
                   len({l["key"] for l in LENSES}) == len(LENSES)))

    all_ok = all(ok for _, ok in checks)
    for desc, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
    return all_ok


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(0 if selftest() else 1)
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--lens", default=None, help="run only this lens key")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(only_lens=args.lens, dry_run=args.dry_run), indent=2))
