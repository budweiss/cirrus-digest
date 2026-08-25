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
                                RELEVANCE_MIN, _relevance, critique, estimate,
                                final_score, resolve_slug)

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
        "key": "volume-work",
        "focus": "WORK ALREADY BEING PAID FOR, BY THE UNIT -- Buddy's stated target "
                 "(2026-08-25). Find a recurring pile of work some business already "
                 "pays to get through: converting files between formats, translating "
                 "or localising, transcribing, captioning, OCR of scanned or "
                 "handwritten material, extracting structured records from documents, "
                 "normalising or enriching a product catalogue, tagging or writing alt "
                 "text for image libraries. The customer sends volume, we return "
                 "finished output, they pay per page/word/minute/record. Anchor every "
                 "proposal to the CURRENT going rate for that work -- a published "
                 "per-unit price is the whole point. We build and run the pipeline; no "
                 "human touches each unit.",
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

# S66: the first adversarial run killed 4/4 ideas for the SAME structural
# reason -- "automated briefing on public data X" dies because a free
# official feed or an entrenched paid incumbent already owns that niche, and
# automated output carries liability in high-stakes domains. Without memory
# the council cheerfully re-proposes that shape forever. Rejected ideas are
# kept in entity_kb (lead_state="rejected") precisely so they can be fed back
# here, making each run start from what the last one learned.
_MAX_LESSONS = 15


def past_failures(db_path: str = None) -> str:
    """Recently-killed ideas + why, formatted for the generation prompt.
    Returns '' when there's no history yet (first run)."""
    try:
        rejected = entity_kb.list_entities(KB_PROJECT, lead_state="rejected",
                                           db_path=db_path)
    except Exception:
        return ""
    if not rejected:
        return ""
    lines = []
    for e in rejected[:_MAX_LESSONS]:
        risk = (e.get("state") or {}).get("main_risk", "")
        lines.append(f"- {e['name']}: {str(risk)[:200]}")
    return (
        "ALREADY PROPOSED AND KILLED -- do not propose these again, and more "
        "importantly do not propose anything that fails for the SAME "
        "STRUCTURAL REASON:\n" + "\n".join(lines) +
        "\n\nStudy those failure modes before answering. If your idea is 'an "
        "automated briefing/newsletter/feed summarizing public data source X', "
        "check first whether X already publishes its own free alerts, whether "
        "an entrenched paid incumbent already serves that buyer, and whether "
        "an error in automated output would carry legal or financial "
        "liability -- if any of those hold, propose something else entirely."
    )

# The failure memory above blocks a repeated IDEA. It never blocked a repeated
# SHAPE -- so 41 of the first 43 proposals were "watch a public data source and
# sell alerts", each with a different subject and therefore never flagged as a
# duplicate. This counts the shape and puts the number in front of the model.
_MONITOR_RX = re.compile(
    r"\b(watch|watchdog|monitor|tracker|alert|feed|digest|radar|ledger|wire|"
    r"index|scanner|briefing|bulletin|early.warning)\b", re.IGNORECASE)


def shape_census(db_path: str = None) -> str:
    try:
        ents = entity_kb.list_entities(KB_PROJECT, db_path=db_path)
    except Exception:
        return ""
    if not ents:
        return ""
    monitoring = [e for e in ents if _MONITOR_RX.search(e["name"] or "")]
    if not monitoring:
        return ""
    pct = round(100 * len(monitoring) / len(ents))
    return (
        f"SHAPE SATURATION WARNING: {len(monitoring)} of {len(ents)} ideas this "
        f"pipeline has EVER produced ({pct}%) are the same business shape -- "
        f"monitor a public data source, emit alerts/digests/a tracker to "
        f"subscribers. Buddy has rejected that shape wholesale. Changing the "
        f"data source does NOT make it a different business. Do not propose it "
        f"again in any subject area. If your idea's name would naturally contain "
        f"Watch, Monitor, Tracker, Alert, Feed, Digest, Radar, Ledger, Index or "
        f"Briefing, it is the wrong shape -- propose something else entirely.\n\n"
        f"Shapes we have NONE of, for contrast: a product someone OWNS after "
        f"buying, an audience business with sponsors, a marketplace, a tool that "
        f"does a job on the customer's own material, a service sold to consumers "
        f"rather than companies."
    )


_IDEAS_PER_LENS = 4


def _gen_prompt(lens: dict, lessons: str = "", census: str = "") -> str:
    return (
        f"{MISSION}\n\n{CAPABILITIES}\n\n"
        + (f"{census}\n\n" if census else "")
        + (f"{lessons}\n\n" if lessons else "") +
        f"LENS FOR THIS REQUEST: {lens['focus']}\n\n"
        f"Propose exactly {_IDEAS_PER_LENS} specific businesses fitting this lens. "
        f"Be concrete: name a real niche, a real customer, a real revenue mechanism. "
        f"'An AI newsletter' is useless.\n\n"
        # The example here used to be 'a daily automated briefing on FDA device
        # recalls sold to medical-device compliance teams'. The pipeline then
        # generated 'FDA Medical Device Recall Daily' -- and 40 more of the same
        # shape. A worked example is not an illustration, it is an instruction.
        # It is now deliberately a shape we have NONE of (2026-08-25).
        f"Good shape: 'a paid archive of hard-to-find sewing patterns for "
        f"vintage sizes, redrafted to modern measurements, sold per-pattern to "
        f"home sewers' -- a specific buyer, a thing they own afterwards, and no "
        f"dependence on watching anyone else's behaviour.\n\n"
        f"Respond as JSON only, no prose, no markdown fences:\n"
        f'{{"ideas": [{{"name": "<short distinctive name, 3-8 words>", '
        f'"what": "<what it does and for whom, 1-2 sentences>", '
        f'"who_pays": "<who the paying customer is and roughly what they would pay>", '
        f'"demand_evidence": "<WHO ALREADY PAYS FOR SOMETHING LIKE THIS TODAY -- '
        f'name a competitor with customers, a paid product, a marketplace with '
        f'sales, or a creator publishing income. If you cannot name one, say '
        f'NONE and expect to be rejected>", '
        f'"autonomous_loop": "<what the scheduled software actually does each day, '
        f'end to end, with no human in the loop>", '
        f'"needs_building": "<what does not exist yet and would have to be built>", '
        f'"why_now": "<why this is viable now and not already saturated>"}}]}}'
    )


def _salvage_objects(text: str) -> list:
    """Recover every complete, balanced {...} object that looks like an idea,
    even from TRUNCATED JSON.

    S66: hit this live -- the council's synthesis ran past max_tokens and cut
    off mid-array, so the outer {"ideas": [...]} never closed and strict
    parsing dropped all four ideas including three that were fully intact.
    Raising max_tokens fixed that instance, but truncation recurs whenever a
    model is verbose, and silently discarding good ideas is the worst
    failure mode here. String-aware so braces inside text don't miscount."""
    out, stack = [], []
    in_str = esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            start = stack.pop()
            try:
                obj = json.loads(text[start:i + 1])
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("name"):
                out.append(obj)
    return out


def _parse_ideas(text: str) -> list:
    """Tolerant JSON extraction -- fenced-block handling like
    deep_research._parse_extraction, a brace-scan for models that wrap JSON
    in stray prose, then per-object salvage for truncated output."""
    if not text:
        return []
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    try:
        ideas = (json.loads(t) or {}).get("ideas", []) or []
        if ideas:
            return ideas
    except Exception:
        pass
    m = re.search(r"\{.*\}", t, re.DOTALL)
    if m:
        try:
            ideas = (json.loads(m.group(0)) or {}).get("ideas", []) or []
            if ideas:
                return ideas
        except Exception:
            pass
    seen, uniq = set(), []
    for obj in _salvage_objects(t):
        key = obj["name"].strip().lower()
        if key not in seen:
            seen.add(key)
            uniq.append(obj)
    return uniq


def _idea_as_text(idea: dict) -> str:
    """Flatten a generated idea into the same shape the scoring gate expects
    from an article excerpt, so one gate serves both intake paths."""
    return (f"{idea.get('what', '')}\n\n"
            f"Paying customer: {idea.get('who_pays', '')}\n"
            f"Autonomous daily loop: {idea.get('autonomous_loop', '')}\n"
            f"Still to build: {idea.get('needs_building', '')}\n"
            f"Why now: {idea.get('why_now', '')}")


def generate(lens: dict, creds: dict, lessons: str = "", census: str = "") -> list:
    """Council-generate candidate ideas for one lens. Returns [] on any
    failure -- one dead lens must not abort the whole run."""
    try:
        import ensemble
        # 8000, not 3000: the council's synthesis must restate all
        # _IDEAS_PER_LENS ideas with six fields each, and 3000 truncated it
        # mid-array live (S66). The salvage parser covers the rest.
        _meta, text = ensemble.best_answer(
            _GEN_SYSTEM, _gen_prompt(lens, lessons, census), creds, max_tokens=8000,
            task="business-idea-ideate", mode="council")
        return _parse_ideas(text)
    except Exception as e:
        print(f"  lens {lens['key']}: generation failed ({e})")
        return []


def todays_lens() -> dict:
    """Deterministically pick one lens per day, cycling through all of them.

    S66: Buddy wants a DAILY idea email. Running all five lenses every day
    would be repetitive and noisy (~20 candidates/day, heavily overlapping);
    running them weekly would leave most daily emails empty. One rotating
    lens per day gives a steady drip with a genuinely different angle each
    day, cycles every len(LENSES) days, and lets the corroboration signal in
    resolve_slug() surface ideas that recur across cycles -- which is exactly
    the ranking signal we want."""
    from datetime import date
    return LENSES[date.today().toordinal() % len(LENSES)]


def run(only_lens: str = None, dry_run: bool = False, db_path: str = None,
        rotate: bool = False) -> dict:
    try:
        creds = json.loads((PROJECT_DIR / "config/credentials.json").read_text())
    except Exception:
        creds = {}

    if rotate:
        lenses = [todays_lens()]
    else:
        lenses = [l for l in LENSES if not only_lens or l["key"] == only_lens]
    result = {"generated": 0, "admitted": [], "corroborated": [], "rejected": []}

    lessons = past_failures(db_path=db_path)
    census = shape_census(db_path=db_path)
    if census:
        print(f"  {census.splitlines()[0]}")
    if lessons:
        print(f"[memory] feeding back {lessons.count(chr(10) + '- ')} past failure(s)")

    for lens in lenses:
        print(f"[lens] {lens['key']}...")
        ideas = generate(lens, creds, lessons, census)
        result["generated"] += len(ideas)
        for idea in ideas:
            name = (idea.get("name") or "").strip()
            if not name:
                continue
            idea_text = _idea_as_text(idea)
            score, why, _label = _relevance(name, idea_text, creds)
            if score < RELEVANCE_MIN:
                result["rejected"].append(f"{name} (fit {score}/10)")
                continue

            # The pass that actually does the work here: a generated idea is
            # BUILT to satisfy the mission, so a high fit score is close to
            # guaranteed and means little. Survival is the real filter.
            survival, flaw = critique(name, idea_text, creds)
            final = final_score(score, survival)
            if final < RELEVANCE_MIN:
                result["rejected"].append(
                    f"{name} (fit {score}, survival {survival}): {flaw}")
                # Killed ideas are RECORDED, not discarded -- they're the
                # failure memory past_failures() feeds back into the next
                # run, they stop the same dead idea being re-proposed
                # (resolve_slug matches them), and Buddy explicitly wants to
                # see what died and why so he can tune the criteria.
                if not dry_run:
                    rslug, _rnew = resolve_slug(name, name, db_path=db_path)
                    entity_kb.upsert_entity(
                        KB_PROJECT, rslug, name, entity_type="business_idea",
                        lead_state="rejected",
                        fields={"category": f"ideated:{lens['key']}",
                                "what": idea.get("what", ""),
                                "who_pays": idea.get("who_pays", ""),
                                "fit_score": score,
                                "survival_score": survival,
                                "final_score": final,
                                "main_risk": flaw},
                        db_path=db_path)
                    entity_kb.add_signal(
                        KB_PROJECT, rslug, "rejected",
                        f"REJECTED [{final}/10 | fit {score}, survival {survival}]\n"
                        f"    What it was: {idea.get('what', '')}\n"
                        f"    Killed by: {flaw}\n"
                        f"    (council-ideated, lens: {lens['key']})",
                        confidence="medium", db_path=db_path)
                continue
            if dry_run:
                result["admitted"].append(
                    f"{name} ({final}/10 | fit {score}, survival {survival}) "
                    f"risk: {flaw} [dry-run]")
                continue

            est = estimate(name, idea_text, creds)  # survivors only
            slug, is_new = resolve_slug(name, name, db_path=db_path)
            entity_kb.upsert_entity(
                KB_PROJECT, slug, name, entity_type="business_idea",
                lead_state="candidate",  # vs "rejected" above -- keeps the
                                         # shortlist queryable and lets a
                                         # previously-killed idea be promoted
                                         # if the criteria later change
                fields={"category": f"ideated:{lens['key']}",
                        "what": idea.get("what", ""),
                        "who_pays": idea.get("who_pays", ""),
                        "autonomous_loop": idea.get("autonomous_loop", ""),
                        "needs_building": idea.get("needs_building", ""),
                        "why_now": idea.get("why_now", ""),
                        "fit_score": score,
                        "survival_score": survival,
                        "final_score": final,
                        "main_risk": flaw,
                        **est},
                db_path=db_path)
            entity_kb.add_signal(
                KB_PROJECT, slug, "candidate",
                f"[{final}/10 | fit {score}, survives critique {survival}] {why}\n"
                f"    What: {idea.get('what', '')}\n"
                f"    Who pays: {idea.get('who_pays', '')}\n"
                f"    Runs itself by: {idea.get('autonomous_loop', '')}\n"
                f"    Needs building: {idea.get('needs_building', '')}\n"
                f"    Main risk: {flaw}\n"
                f"    (council-ideated, lens: {lens['key']})",
                confidence="medium", db_path=db_path)
            (result["admitted"] if is_new else result["corroborated"]).append(
                f"{name} ({final}/10)")

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
    # S66 regression: live truncation dropped 3 intact ideas along with the
    # 4th partial one, because the outer wrapper never closed.
    truncated = ('{"ideas": [{"name": "A", "what": "x"}, {"name": "B", "what": "y"}, '
                 '{"name": "C", "what": "part')
    salvaged = _parse_ideas(truncated)
    checks.append(("truncated JSON still yields its complete ideas",
                   [i["name"] for i in salvaged] == ["A", "B"]))
    # Braces inside a string value must not miscount depth -- "A" survives
    # intact; the genuinely-truncated "B" is correctly NOT salvaged.
    brace_salvage = _parse_ideas(
        '{"ideas": [{"name": "A", "what": "uses { and } chars"}, '
        '{"name": "B", "what": "tr')
    checks.append(("a brace inside a string does not break salvage",
                   [i["name"] for i in brace_salvage] == ["A"]
                   and brace_salvage[0]["what"] == "uses { and } chars"))
    checks.append(("salvage dedupes repeated names",
                   len(_parse_ideas('{"ideas": [{"name": "A"}, {"name": "A"}, {"x"')) == 1))
    flat = _idea_as_text({"what": "W", "who_pays": "P", "autonomous_loop": "L",
                          "needs_building": "B", "why_now": "N"})
    checks.append(("flattened idea keeps every field",
                   all(x in flat for x in ("W", "P", "L", "B", "N"))))
    checks.append(("every lens has a unique key",
                   len({l["key"] for l in LENSES}) == len(LENSES)))
    checks.append(("today's lens is a real lens", todays_lens() in LENSES))

    # S66: rejected ideas must persist as failure memory and reach the prompt.
    import os
    import tempfile
    fd, _db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(_db)
    try:
        checks.append(("no history yields no lessons block", past_failures(db_path=_db) == ""))
        entity_kb.upsert_entity(
            KB_PROJECT, "dead-idea", "Dead Idea", lead_state="rejected",
            fields={"main_risk": "incumbent already owns this"}, db_path=_db)
        entity_kb.upsert_entity(
            KB_PROJECT, "live-idea", "Live Idea", lead_state="candidate",
            fields={"main_risk": "manageable"}, db_path=_db)
        lessons = past_failures(db_path=_db)
        checks.append(("a rejected idea becomes a lesson", "Dead Idea" in lessons))
        checks.append(("its actual flaw is included, not just the name",
                       "incumbent already owns this" in lessons))
        checks.append(("a surviving candidate is NOT fed back as a failure",
                       "Live Idea" not in lessons))
        # S77: the shape census. The failure memory above blocks a repeated
        # IDEA; this is what blocks a repeated SHAPE.
        entity_kb.upsert_entity(KB_PROJECT, "fda-recall-daily",
                                "FDA Medical Device Recall Daily",
                                db_path=_db, lead_state="rejected")
        entity_kb.upsert_entity(KB_PROJECT, "trademark-watch",
                                "USPTO Trademark Squatter Watch",
                                db_path=_db, lead_state="rejected")
        cen = shape_census(db_path=_db)
        checks.append(("monitoring-shaped names are counted", "SHAPE SATURATION" in cen))
        checks.append(("the census names the banned words", "Tracker" in cen))
        checks.append(("the census reaches the generation prompt",
                       "SHAPE SATURATION" in _gen_prompt(LENSES[0], "", cen)))
        checks.append(("demand evidence is a required output field",
                       "demand_evidence" in _gen_prompt(LENSES[0])))
        checks.append(("the worked example is no longer a monitoring product",
                       "FDA device" not in _gen_prompt(LENSES[0])))

        checks.append(("lessons reach the generation prompt",
                       "Dead Idea" in _gen_prompt(LENSES[0], lessons)))
    finally:
        if os.path.exists(_db):
            os.unlink(_db)
    from datetime import date, timedelta as _td
    span = {LENSES[(date.today() + _td(days=i)).toordinal() % len(LENSES)]["key"]
            for i in range(len(LENSES))}
    checks.append((f"rotation covers all {len(LENSES)} lenses in {len(LENSES)} days",
                   span == {l["key"] for l in LENSES}))

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
    parser.add_argument("--rotate", action="store_true",
                        help="run only today's lens (rotates daily) -- the scheduled mode")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        outcome = run(only_lens=args.lens, dry_run=args.dry_run, rotate=args.rotate)
        print(json.dumps(outcome, indent=2))
        if not args.dry_run:
            import job_status
            job_status.record("businessideaideate", True,
                              f"{outcome.get('generated', 0)} generated, "
                              f"{len(outcome.get('admitted', []))} kept, "
                              f"{len(outcome.get('rejected', []))} rejected")
    except Exception as exc:
        if not args.dry_run:
            try:
                import job_status
                job_status.record("businessideaideate", False, str(exc)[:180])
            except Exception:
                pass
        raise
