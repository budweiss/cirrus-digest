"""business_idea_scan.py — daily scan for AI-buildable business opportunities (S66).

Buddy's new project: use the same AI environment already running here (Claude-
driven research/extraction/automation — the pattern proven on Bill's HOA CRM)
to find real business ideas he could start, where the AI tooling itself is the
unfair advantage. Deliberately kept SEPARATE from CIRRUS's main daily digest
(its own source list, its own relevance mission) rather than mixed in, per
Buddy's explicit ask.

Reuses proven building blocks, no new architecture:
  - entity_kb.py (project "business_ideas") — same engine as Bill's CRM, one
    entity per idea/case-study, signals for repeat mentions over time.
  - The same "N-model council scores it, Claude judges" relevance-gate
    pattern already proven in self_review.py's mission gate, with its own
    mission text instead of CIRRUS's.
  - cirrus_daily.fetch_article_content for full-text fetch.
  - hoa_monitor.py's load_seen()/save_seen() dedupe pattern.

Sources start deliberately small (4 verified-live Substack feeds spanning
solo-founder building, case-study/growth analysis, and tech-strategy
commentary) — extend SOURCES below as better feeds are found, same way
sources.json grows via self_review proposals. Not exhaustive by design;
Phase 1 proves the pipeline, source breadth grows from there.

Usage:
  python3 business_idea_scan.py [--dry-run]
  python3 business_idea_scan.py selftest
"""
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import entity_kb

PROJECT_DIR = Path.home() / "projects/cirrus-digest"
KB_PROJECT = "business_ideas"
SEEN_PATH = PROJECT_DIR / "config/business_idea_seen.json"
RELEVANCE_MIN = 6  # same bar as self_review.py's mission gate

SOURCES = [
    {"name": "Growth In Reverse", "rss": "https://growthinreverse.substack.com/feed"},
    {"name": "Entrepreneur Loop", "rss": "https://entrepreneurloop.substack.com/feed"},
    {"name": "Lenny's Newsletter", "rss": "https://www.lennysnewsletter.com/feed"},
    {"name": "The Generalist", "rss": "https://thegeneralist.substack.com/feed"},
]

MISSION = """Buddy owns a real, always-on AI automation environment (see CAPABILITIES
below) and wants to start a business that this environment can largely RUN, not
merely assist with. Score how good a candidate THIS article/case-study is as a
blueprint for such a business.

Score HIGH when all of these hold:
1. REAL: an actually-operating business model with identifiable paying
   customers or revenue -- not a hypothetical, a big-tech product
   announcement, or opinion/commentary with no business behind it.
2. MACHINE-OPERABLE: the day-to-day work could be performed by scheduled,
   autonomous software (research, generation, publishing, outreach,
   fulfillment) rather than by Buddy's hands each day. This is the single
   most important criterion. A business that merely "uses AI tools" while
   still requiring daily human labor per unit of output scores LOW.
   Automated media -- YouTube/shorts/podcast/audio/video pipelines that
   generate and publish on a schedule -- is explicitly IN SCOPE and of high
   interest.
3. AI-ECONOMIC: the automation is why the unit economics work, not a
   nice-to-have bolted onto a conventional labor business.
4. DISTINCT: not already saturated by dozens of near-identical competitors,
   or has a defensible angle (proprietary data, accumulating knowledge base,
   niche depth) that compounds over time.
5. STARTABLE SMALL: can begin at small scale as a genuine learning cycle and
   grow, rather than requiring a large launch to work at all. Upfront capital
   is NOT a limiting factor and should not lower a score.

Score LOW / reject:
- Anything in property management, HOAs, real estate, snow removal, or
  seasonal home services. Buddy already operates in these sectors and
  explicitly wants this venture to be UNRELATED to them.
- Businesses whose core is human consulting, done-for-you agency labor, or
  anything billing Buddy's own hours.
- General AI industry news, funding rounds, model releases, or company
  announcements with no startable business for an individual.
- Pure how-to/productivity tips, or opinion pieces with no concrete
  business described."""

# Honest inventory of what actually exists here, used to ground scoring and
# (in business_idea_ideate.py) generation. Deliberately lists what is NOT
# built as well -- an idea requiring video/voice/payments is still viable,
# but the models should know it's a build, not a given.
CAPABILITIES = """CAPABILITIES ALREADY BUILT AND RUNNING (as of 2026-08):
- Two always-on machines: CIRRUS (Mac Studio, local Ollama/qwen) and CUMULUS
  (NVIDIA DGX Spark, Linux+GPU, a second unit arriving). Both run scheduled
  jobs unattended (launchd/systemd) and have been stable for months.
- A funded 4-provider LLM council (Anthropic Claude, Gemini, OpenAI, Grok)
  with automatic failover, cost ledgers, and per-month spend caps.
- Autonomous research pipeline: paid Brave Search + Gemini grounded search,
  article fetching (incl. paywall detection and cookie-based access), and
  multi-model extraction of structured facts from web sources.
- entity_kb: a generic SQLite entity/knowledge-base engine with an append-only
  event ledger -- purpose-built for knowledge that COMPOUNDS over time, with
  proven multi-tenant isolation. Currently holds 184 researched entities for
  one live client.
- Email pipelines in both directions: IMAP intake with sender allowlisting and
  automatic classification, plus SMTP send. Live clients already receive
  autonomous, research-grounded email replies.
- RSS/podcast ingestion, X/Twitter API read access, Telegram bot.
- A public HTTPS API surface (Cloudflare tunnel + owned domains).
- Skywarden: an autonomous supervisor agent with real tool use, budget caps,
  and an audit ledger -- it monitors and repairs the other pipelines.
- Full CI/CD: git-based deploy to both machines, rollback, secret encryption.

NOT YET BUILT (possible, but count as real build work in your assessment):
- Video generation/editing, text-to-speech/voice, image generation.
- Payment processing, billing, subscriptions.
- Any public-facing consumer web product or mobile app.
- Social/video platform publishing integrations (YouTube, TikTok, etc.)."""


def _gate_prompt(title: str, text: str) -> str:
    return (f"{MISSION}\n\n{CAPABILITIES}\n\nCandidate article:\nTITLE: {title}\n"
            f"EXCERPT: {text[:3000]}\n\n"
            f"How good a candidate is this? Reply with EXACTLY one line: "
            f"SCORE: <0-10> | WHY: <one short sentence> | IDEA: <a short "
            f"business-idea name/label, 3-8 words, or NONE if score < 6>")


def _parse_score(text: str):
    m = re.search(r"SCORE:\s*(\d+)", text or "")
    if not m:
        return None
    why_m = re.search(r"WHY:\s*(.+?)(?:\||$)", text)
    idea_m = re.search(r"IDEA:\s*(.+)$", text)
    return (min(int(m.group(1)), 10),
            (why_m.group(1).strip()[:160] if why_m else ""),
            (idea_m.group(1).strip()[:80] if idea_m else ""))


def _relevance(title: str, text: str, creds: dict):
    """Score via the council when available, else the local model. ALWAYS
    fail-open (0, ...) on total failure -- same fail-open policy as
    self_review.py's gate, but inverted default: a gate error here should
    NOT auto-admit an idea into a running business shortlist the way it
    should for a cheap RSS-source add, so fail CLOSED (score 0) instead."""
    try:
        import ensemble
        _, text_out = ensemble.best_answer(
            "You are one of several AI models scoring a candidate business "
            "idea against a mission. Reply with EXACTLY one line: "
            "SCORE: <0-10> | WHY: <one short sentence> | IDEA: <short label>.",
            _gate_prompt(title, text), creds, max_tokens=200,
            task="business-idea-gate", mode="council")
        parsed = _parse_score(text_out)
        if parsed:
            return parsed
    except Exception:
        pass
    return (0, "gate unavailable -- fail-closed (not admitted)", "")


def _slug_for(idea_label: str, title: str) -> str:
    return entity_kb.slugify(idea_label or title)


def resolve_slug(idea_label: str, title: str, db_path: str = None) -> tuple:
    """Return (slug, is_new). Checks entity_kb for an existing entity that
    already covers this idea before minting a new slug.

    S66: the first live run created two separate entities ("AI-Native Solo
    Fashion Brand" / "AI-Orchestrated Niche Fashion Brand") from a podcast
    episode and its companion post about the SAME underlying story -- the
    LLM labels each article slightly differently, so slug-only matching
    can't collapse them. Same fuzzy-match-first pattern
    hoa_daily_research.run_discovery() already uses for communities."""
    name = idea_label or title
    try:
        matches = entity_kb.search_entities(KB_PROJECT, name, db_path=db_path, limit=1)
    except Exception:
        matches = []
    if matches:
        return matches[0]["slug"], False
    return entity_kb.slugify(name), True


def run(dry_run: bool = False, db_path: str = None) -> dict:
    import cirrus_daily  # lazy: needs requests/bs4/feedparser, live venv only
    import feedparser

    try:
        creds = json.loads((PROJECT_DIR / "config/credentials.json").read_text())
    except Exception:
        creds = {}

    try:
        seen = set(json.loads(SEEN_PATH.read_text()))
    except Exception:
        seen = set()

    since = datetime.now() - timedelta(hours=48)
    result = {"fetched": 0, "fresh": 0, "admitted": [], "corroborated": [],
              "scored_low": 0}
    newly_seen = set()

    for source in SOURCES:
        try:
            feed = feedparser.parse(source["rss"])
        except Exception:
            continue
        for entry in feed.entries:
            url = entry.get("link", "")
            if not url or url in seen:
                continue
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                published = datetime(*entry.published_parsed[:6])
                if published < since:
                    continue
            result["fetched"] += 1
            newly_seen.add(url)
            title = entry.get("title", "").strip()
            if not title:
                continue

            try:
                content, _paywalled = cirrus_daily.fetch_article_content(url)
            except Exception:
                content = ""
            text = content or entry.get("summary", "") or ""
            if not text.strip():
                continue
            result["fresh"] += 1

            if dry_run:
                continue

            score, why, idea_label = _relevance(title, text, creds)
            if score < RELEVANCE_MIN:
                result["scored_low"] += 1
                continue

            slug, is_new = resolve_slug(idea_label, title, db_path=db_path)
            entity_kb.upsert_entity(KB_PROJECT, slug, idea_label or title,
                                    entity_type="business_idea",
                                    fields={"category": source["name"]},
                                    db_path=db_path)
            entity_kb.add_signal(
                KB_PROJECT, slug, "candidate",
                f"[{score}/10] {why} (from {source['name']}: \"{title}\")",
                source_url=url, confidence="medium", db_path=db_path)
            if is_new:
                result["admitted"].append(f"{idea_label or title} ({score}/10)")
            else:
                result.setdefault("corroborated", []).append(
                    f"{idea_label or title} ({score}/10)")

    if not dry_run and newly_seen:
        seen |= newly_seen
        SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        SEEN_PATH.write_text(json.dumps(sorted(seen), indent=1))

    return result


def selftest() -> bool:
    """Offline-testable parts: score-line parsing, slug derivation. The live
    fetch/council pass needs network + API keys -- verified live after
    deploy, same reasoning as hoa_daily_research.py's discover/refresh."""
    checks = []
    checks.append(("plain score line parses",
                   _parse_score("SCORE: 8 | WHY: real business | IDEA: Newsletter ops agency")
                   == (8, "real business", "Newsletter ops agency")))
    checks.append(("score is clamped to 10 max",
                   _parse_score("SCORE: 15 | WHY: x | IDEA: y")[0] == 10))
    checks.append(("malformed line returns None", _parse_score("not a score line") is None))
    checks.append(("slug prefers the idea label over the raw title",
                   _slug_for("AI Bookkeeping Agency", "Some Article Title About Stuff")
                   == entity_kb.slugify("AI Bookkeeping Agency")))
    checks.append(("slug falls back to title when no idea label",
                   _slug_for("", "Some Article Title") == entity_kb.slugify("Some Article Title")))

    # S66 regression: two articles about the same story got two entities.
    import os
    import tempfile
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)
    try:
        slug1, new1 = resolve_slug("AI-Native Solo Fashion Brand", "t1", db_path=db_path)
        checks.append(("a genuinely new idea resolves as new", new1 is True))
        entity_kb.upsert_entity(KB_PROJECT, slug1, "AI-Native Solo Fashion Brand",
                                db_path=db_path)
        _slug2, new2 = resolve_slug("AI-Native Solo Fashion Brand", "t2", db_path=db_path)
        checks.append(("the same idea seen again is NOT a second entity", new2 is False))
        _slug3, new3 = resolve_slug("Automated Podcast Clip Service", "t3", db_path=db_path)
        checks.append(("an unrelated idea still resolves as new", new3 is True))
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)

    all_ok = all(ok for _, ok in checks)
    for desc, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
    return all_ok


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(0 if selftest() else 1)
    dry = "--dry-run" in sys.argv
    print(run(dry_run=dry))
