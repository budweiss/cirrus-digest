#!/usr/bin/env python3
"""
opportunity_scout.py — nightly business-opportunity scouting on CUMULUS.
========================================================================
Buddy, 2026-08-25: *"send different questions to the LLMs and see how they
respond back. We need to be creative here, not just looking at some of my
examples but try and brainstorm other options... I like our daily jobs to keep
thinking and learning of different ways to build a business opportunity."*

How this differs from `business_idea_ideate.py`, which already runs on CIRRUS:

  ideate  = ONE question, asked of a COUNCIL that converges on one answer.
  scout   = a DIFFERENT question per provider, deliberately kept apart, then
            each model cross-examines a RIVAL's answer.

Convergence was the problem. A council optimises toward the median answer, and
the median answer is why 41 of the first 43 ideas were the same monitoring
feed. Divergence is the point here: five models, five different questions, no
shared context, and the disagreement preserved rather than averaged away.

Three stages:
  1. DIVERGE       one angle per provider, rotating nightly so the space keeps
                   getting explored rather than re-sampled.
  2. GROUND        any proposal naming priced work gets checked against REAL
                   rate cards, fetched DIRECTLY by URL. Search-and-summarise
                   could not reach them: two research runs on 2026-08-25 both
                   concluded "no good option found" while every rate figure
                   came back UNSOURCED, because the pipeline searched *around*
                   vendor pricing pages instead of opening them.
  3. CROSS-EXAMINE provider B attacks provider A's idea. Round-robin, so no
                   model marks its own homework.

Ideas land in the same entity_kb project as the CIRRUS chain, on purpose: the
shape census and failure memory there only work if they see everything.

Usage:
  python3 opportunity_scout.py [--dry-run] [--angles N]
  python3 opportunity_scout.py selftest
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import entity_kb
import llm_providers

APP_DIR = Path(__file__).resolve().parent
KB_PROJECT = "business_ideas"

# ── Primary rate cards, fetched DIRECTLY. No search step. ────────────────────
# The whole reason this registry exists: on 2026-08-25 the council reported
# every per-unit rate as UNSOURCED -- not because the numbers are secret, but
# because a search-and-summarise pipeline never opens the vendor's own pricing
# page. These are opened by URL. Add a source here rather than hoping search
# finds it.
PRIMARY_SOURCES = {
    "transcription/captioning": "https://www.rev.com/pricing",
    "ocr/document-extraction": "https://aws.amazon.com/textract/pricing/",
    "document-ai": "https://cloud.google.com/document-ai/pricing",
    "translation-machine": "https://cloud.google.com/translate/pricing",
    "speech-to-text": "https://cloud.google.com/speech-to-text/pricing",
    "text-to-speech": "https://cloud.google.com/text-to-speech/pricing",
    "freelance-market-rates": "https://www.upwork.com/hire/transcribers/cost/",
    "accessibility-remediation": "https://www.section508.gov/sell/acr-vpat-faq/",
}

# ── The angles. Deliberately NOT variations on one theme. ────────────────────
# Buddy's steer: his own examples (format conversion, translation) are a
# STARTING point, not the boundary. Several angles below exist to push away
# from them -- including the one he flagged as visibly working in the market
# right now (paid cheat sheets, templates and short courses).
ANGLES = [
    {
        "key": "priced-by-the-unit",
        "q": "What work do businesses currently BUY BY THE UNIT -- per page, per "
             "word, per minute, per record -- at a published rate, where the buyer "
             "sends a pile of material and gets finished output back? Name the "
             "work, the going rate, who publishes that rate, and who the buyers "
             "are. Prefer work where accuracy, liability or format complexity has "
             "so far stopped it collapsing to near-zero price.",
    },
    {
        "key": "paid-knowledge-artifacts",
        "q": "People are paying real money for cheat sheets, reference cards, "
             "templates, notion systems, prompt libraries and short courses. Which "
             "SPECIFIC audiences are currently paying for this kind of packaged "
             "knowledge, what exactly are they buying, what does it sell for, and "
             "where do they buy it? Name marketplaces and named sellers with "
             "evidence of sales, not categories.",
    },
    {
        "key": "expensive-and-slow",
        "q": "Where do people still wait days and pay hundreds for something a "
             "machine could now return in minutes? Look for quotes, appraisals, "
             "assessments, reports, plans, filings and certificates that are "
             "currently produced by a human professional at a published fee. Name "
             "the artefact, the fee, and what legally or practically still "
             "requires the human.",
    },
    {
        "key": "boring-back-office",
        "q": "What unglamorous recurring back-office task does a specific industry "
             "hate, currently pay staff or an outsourcer to do, and complain about "
             "in public forums? Name the industry, the task, the current cost, and "
             "quote the complaint. Avoid anything that amounts to monitoring or "
             "reporting on third parties.",
    },
    {
        "key": "underserved-language-and-locale",
        "q": "Where does a language, dialect, locale or accessibility gap lock "
             "people out of something they would pay to reach? Consider "
             "non-English markets, regional documents, and material that exists "
             "only in a form some people cannot use. Name who pays, and what they "
             "pay today.",
    },
    {
        "key": "one-person-leverage",
        "q": "Which businesses does a SINGLE person demonstrably run at meaningful "
             "revenue today, where the leverage comes from software doing the work "
             "rather than the owner's hours? Name the operator, the business, the "
             "evidence of revenue, and be explicit about what the owner still does "
             "by hand each week. Treat unnamed 'case studies' and round numbers "
             "with no primary source as worthless.",
    },
    {
        "key": "wildcard-contrarian",
        "q": "Propose a business nobody in this conversation would think of. Take a "
             "real swing: an overlooked market, a strange niche, an unfashionable "
             "industry, a novel use of always-on compute. Argue for it honestly "
             "including who pays. Originality matters more than safety here -- but "
             "it must still be something someone would actually buy.",
    },
]

_SYSTEM = (
    "You advise an operator who will ACT on your answer and cannot afford a "
    "flattering one. You have an always-on automation environment: several "
    "frontier LLM APIs, local GPU models, scheduled jobs, web scraping, email "
    "and document generation. Paid APIs are acceptable -- cost is a later "
    "optimisation, not a constraint on the idea.\n\n"
    "HARD RULES:\n"
    "1. Never propose a business whose product is detecting other people's "
    "wrongdoing and reporting it to them or an authority. No compliance "
    "monitoring, infringement watching, or 'we found a problem with your "
    "company' outreach. The operator has rejected this outright.\n"
    "2. Never propose 'monitor a public data source and sell alerts/digests'. "
    "That shape is saturated and rejected. Changing the data source does not "
    "make it a different business.\n"
    "3. Every claim about money carries its source. No source, write UNSOURCED "
    "next to it. Invented round numbers are worse than no answer.\n"
    "4. Say plainly when you do not know."
)


def _ask(provider: str, question: str, creds: dict, max_tokens: int = 2500) -> str:
    try:
        return llm_providers.call(provider, _SYSTEM, question, creds,
                                  max_tokens=max_tokens)
    except Exception as e:
        print(f"  [{provider}] failed: {type(e).__name__}: {e}")
        return ""


def assign(providers: list, angles: list, day_seed: int) -> list:
    """One angle per provider, rotating by day so the space keeps being
    explored instead of re-sampled. Returns [(provider, angle)]."""
    if not providers or not angles:
        return []
    return [(p, angles[(day_seed + i) % len(angles)])
            for i, p in enumerate(providers)]


def fetch_rate_cards(keys=None) -> dict:
    """Open the vendor pricing pages DIRECTLY. Returns {key: text}."""
    import urllib.request
    out = {}
    for key, url in PRIMARY_SOURCES.items():
        if keys and key not in keys:
            continue
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel "
                                            "Mac OS X 10_15_7)"})
            with urllib.request.urlopen(req, timeout=45) as r:
                html = r.read().decode("utf-8", errors="replace")
            text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html,
                          flags=re.S | re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            out[key] = f"SOURCE {url}\n{text[:6000]}"
            print(f"  rate card [{key}]: {len(text)} chars from {url}")
        except Exception as e:
            print(f"  rate card [{key}] FAILED: {type(e).__name__}: {e}")
    return out


def cross_examine(pairs: list, creds: dict) -> list:
    """Provider B attacks provider A's idea. Round-robin so nothing marks its
    own homework -- the disagreement is the output, not a merged consensus."""
    results = []
    n = len(pairs)
    for i, (provider, angle, answer) in enumerate(pairs):
        if not answer:
            continue
        critic = pairs[(i + 1) % n][0] if n > 1 else provider
        prompt = (
            "Another advisor proposed the following. Attack it. Which claim is "
            "least supported? Who specifically would refuse to pay, and why? "
            "What would make it fail in the first 90 days? If it is actually "
            "sound, say so plainly and name the ONE thing that must be true.\n\n"
            f"--- PROPOSAL ---\n{answer[:6000]}"
        )
        verdict = _ask(critic, prompt, creds, max_tokens=1200)
        results.append({"angle": angle["key"], "author": provider,
                        "critic": critic, "proposal": answer,
                        "critique": verdict})
        print(f"  {critic} cross-examined {provider} on '{angle['key']}'")
    return results


def run(dry_run: bool = False, n_angles: int = None) -> dict:
    creds_path = APP_DIR / "config/credentials.json"
    try:
        creds = json.loads(creds_path.read_text())
    except Exception as e:
        return {"ok": False, "reason": f"no creds: {e}"}

    providers = llm_providers.available(creds)
    # Deliberately loud: this job's whole value is BREADTH of opinion. Running
    # it on fewer models than the box could reach is a quiet degradation.
    print(f"providers available: {providers}")
    if len(providers) < 2:
        return {"ok": False, "reason": f"need >=2 providers, have {providers}"}

    angles = ANGLES[:n_angles] if n_angles else ANGLES
    day_seed = int(datetime.now().strftime("%j"))
    pairing = assign(providers, angles, day_seed)
    print(f"tonight's assignment ({len(pairing)} model(s), rotating by day):")
    for p, a in pairing:
        print(f"  {p:10s} -> {a['key']}")

    if dry_run:
        print("\n== DRY RUN — no model calls, no writes ==")
        return {"ok": True, "dry_run": True,
                "assignment": [(p, a["key"]) for p, a in pairing]}

    cards = fetch_rate_cards()
    grounding = ""
    if cards:
        grounding = ("\n\nREAL PUBLISHED RATES, fetched directly from the "
                     "vendors just now. Use these instead of guessing, and cite "
                     "the SOURCE line:\n\n" + "\n\n".join(cards.values())[:20000])

    answered = []
    for provider, angle in pairing:
        print(f"[{provider}] {angle['key']}...")
        answered.append((provider, angle,
                         _ask(provider, angle["q"] + grounding, creds)))

    live = [t for t in answered if t[2]]
    if not live:
        return {"ok": False, "reason": "every provider failed"}

    examined = cross_examine(live, creds)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = APP_DIR / "out" / "scout"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"scout-{stamp}.md"
    with open(path, "w") as f:
        f.write(f"# Opportunity scout — {datetime.now():%Y-%m-%d %H:%M}\n\n")
        f.write(f"*{len(live)} model(s), a different question each; "
                f"{len(cards)} rate card(s) fetched directly*\n\n")
        for r in examined:
            f.write(f"\n---\n\n## {r['angle']}  ·  {r['author']}\n\n")
            f.write(r["proposal"] + "\n\n")
            f.write(f"### Cross-examined by {r['critic']}\n\n")
            f.write((r["critique"] or "(no critique returned)") + "\n")

    print(f"\nreport: {path}")
    return {"ok": True, "dry_run": False, "providers": providers,
            "angles": [a["key"] for _, a in pairing],
            "answered": len(live), "rate_cards": len(cards),
            "report": str(path)}


def selftest() -> int:
    failures = 0

    def check(name, cond):
        nonlocal failures
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            failures += 1

    provs = ["anthropic", "gemini", "grok", "openai"]
    a1 = assign(provs, ANGLES, 0)
    check("every provider gets an angle", len(a1) == len(provs))
    check("no two providers share an angle on one night",
          len({a["key"] for _, a in a1}) == len(a1))
    a2 = assign(provs, ANGLES, 1)
    check("the assignment rotates with the day",
          [a["key"] for _, a in a1] != [a["key"] for _, a in a2])
    check("rotation eventually covers every angle",
          {a["key"] for d in range(len(ANGLES)) for _, a in assign(provs, ANGLES, d)}
          == {a["key"] for a in ANGLES})
    check("fewer providers than angles is fine", len(assign(["x"], ANGLES, 3)) == 1)
    check("no providers yields no work", assign([], ANGLES, 0) == [])

    check("the snitch shape is forbidden in the system prompt",
          "reporting it to them" in _SYSTEM)
    check("the monitoring shape is forbidden in the system prompt",
          "sell alerts/digests" in _SYSTEM)
    check("unsourced money claims must be labelled", "UNSOURCED" in _SYSTEM)
    check("Buddy's cheat-sheet trend is an angle",
          any("cheat sheet" in a["q"] for a in ANGLES))
    check("angles reach beyond the per-unit examples", len(ANGLES) >= 6)
    check("rate cards are addressed by URL, not by search",
          all(v.startswith("http") for v in PRIMARY_SOURCES.values()))

    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(selftest())
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--angles", type=int, default=None)
    a = ap.parse_args()
    outcome = run(dry_run=a.dry_run, n_angles=a.angles)
    print(json.dumps(outcome, indent=2, default=str))
    if not a.dry_run:
        try:
            import job_status
            job_status.record("opportunityscout", bool(outcome.get("ok")),
                              f"{outcome.get('answered', 0)} model answer(s), "
                              f"{outcome.get('rate_cards', 0)} rate card(s)"
                              if outcome.get("ok")
                              else f"FAILED: {outcome.get('reason', '?')[:60]}")
        except Exception as e:
            print(f"job_status.record failed: {e}")
    sys.exit(0 if outcome.get("ok") else 1)
