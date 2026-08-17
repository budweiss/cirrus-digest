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

# Business/founder-focused feeds picked specifically for this project (all
# live-verified). These are ADDITIVE to the Medium/Substack sources the main
# digest already reads -- see inherited_sources() below.
SOURCES = [
    {"name": "Growth In Reverse", "rss": "https://growthinreverse.substack.com/feed"},
    {"name": "Entrepreneur Loop", "rss": "https://entrepreneurloop.substack.com/feed"},
    {"name": "Lenny's Newsletter", "rss": "https://www.lennysnewsletter.com/feed"},
    {"name": "The Generalist", "rss": "https://thegeneralist.substack.com/feed"},
]


def inherited_sources() -> list:
    """The Medium/Substack feeds the main digest already reads.

    S66, Buddy's ask -- he pays for many of these subscriptions and CIRRUS
    already holds working medium.com/substack.com cookies for them, so a
    business idea surfacing there was being silently ignored by this scan.
    Read from config/sources.json rather than copied here, so any source he
    adds to the main digest is picked up automatically and the two lists
    can't drift. Most of these are AI-news feeds where a given article is not
    a business idea at all -- that is fine and expected: the relevance gate
    rejects those cheaply, before the more expensive critique ever runs."""
    try:
        cfg = json.loads((PROJECT_DIR / "config/sources.json").read_text())
    except Exception:
        return []
    out = []
    for s in cfg.get("web_sources", []) or []:
        if s.get("type") in ("medium", "substack", "newsletter") and s.get("rss"):
            out.append({"name": s.get("name", s["rss"]), "rss": s["rss"]})
    return out


def trial_sources() -> list:
    """Feeds currently on trial or promoted by business_idea_feeds.py.

    Imported lazily inside the function: business_idea_feeds imports
    all_sources() from this module to know what's already covered, so a
    module-level import here would be circular."""
    try:
        from business_idea_feeds import active_feeds
        return active_feeds()
    except Exception:
        return []


def all_sources(include_trials: bool = True) -> list:
    """Project-specific feeds + inherited ones + feeds under trial, deduped
    by RSS URL. include_trials=False breaks the recursion when
    business_idea_feeds asks what is already covered."""
    seen, out = set(), []
    pool = SOURCES + inherited_sources() + (trial_sources() if include_trials else [])
    for s in pool:
        if s["rss"] not in seen:
            seen.add(s["rss"])
            out.append(s)
    return out


# ── Targeted search ──────────────────────────────────────────────────────────
# S66, Buddy's point: reading the feeds is PASSIVE -- it only finds a business
# idea if one happens to be published in a feed we already follow, and the
# inherited feeds are AI-news oriented, not business-opportunity oriented.
# These queries actively hunt for the thing this project is actually looking
# for. Deliberately phrased around REVENUE and OPERATING businesses (not
# "ideas" or "trends"), because the mission rejects hypotheticals -- searching
# for "business ideas" returns listicles, searching for revenue figures
# returns real operators. Distinct from self_review.py's system-improvement
# keywords, which look for ways to improve CIRRUS itself; nothing is shared.
BUSINESS_SEARCH_QUERIES = [
    "solo founder automated business monthly recurring revenue case study",
    "one person software business run entirely by automation revenue",
    "bootstrapped niche data subscription business monthly revenue",
    "productized service AI automation recurring revenue teardown",
    "indie hacker profitable side business automated no employees",
    "small newsletter or media business automated production revenue",
    "underserved niche B2B data product solo operator revenue",
    "automated monitoring alerting service niche industry subscribers",
]
_QUERIES_PER_RUN = 3
_RESULTS_PER_QUERY = 4


def todays_queries(n: int = _QUERIES_PER_RUN) -> list:
    """Rotate through the query list by date, same reasoning as the ideation
    lens rotation: running all of them daily is expensive and returns heavily
    overlapping results, while a rotating slice covers the whole list over a
    few days and keeps each day's findings fresh."""
    from datetime import date
    start = (date.today().toordinal() * n) % len(BUSINESS_SEARCH_QUERIES)
    return [BUSINESS_SEARCH_QUERIES[(start + i) % len(BUSINESS_SEARCH_QUERIES)]
            for i in range(min(n, len(BUSINESS_SEARCH_QUERIES)))]

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


# ── Adversarial pass ─────────────────────────────────────────────────────────
# CALIBRATION RECORD (S66) -- re-measure with these controls before changing
# the prompt or thresholds below; a critique that scores everything low is
# indistinguishable from no critique at all.
#
# First version ("assume it will fail, default to skepticism", scored against
# no explicit success bar) returned 2/10 for EVERYTHING, including two
# deliberately strong controls -- it found an incumbent for every B2B data
# idea, which is always true and therefore says nothing. Fixed by telling it
# the ACTUAL bar (one operator, $2-5k/month, not market leadership) and that
# an incumbent's mere existence is not fatal.
#
#   control                                    before  after
#   50-state utility rate-filing normalizer      2/10    8/10   (good)
#   continuous independent-pharmacy directory    2/10    8/10   (good)
#   faceless AI quote YouTube channel            1/10    2/10   (bad)
#   bulk LinkedIn scraper sold to recruiters     1/10    1/10   (bad)
#
# After: clear separation, and the flaws named on the GOOD ideas became
# useful engineering warnings (scraper maintenance across 50 changing sites)
# rather than defeatist non-information.
# S66: the fit-scoring gate above discriminates well on ARTICLES (they weren't
# written to satisfy our mission -- the first live run rejected 2 of 4) but
# poorly on GENERATED ideas, which are constructed to satisfy it and so score
# 8-9/10 almost uniformly. That measures "well-formed proposal", not "good
# business". This second pass asks the council for the strongest reason each
# idea FAILS, and the final score is min(fit, survival) -- one strong concrete
# objection sinks an idea no matter how neatly it fits the brief. Same
# adversarial-verify shape used elsewhere in this codebase for findings.
_CRITIQUE_SYSTEM = (
    "You are a skeptical operator who kills bad business ideas before months "
    "and money go into them. Find the single strongest, most concrete reason "
    "this would fail. Be specific and checkable -- name the actual blocker, "
    "not a generic risk like 'execution is hard' or 'needs marketing'.\n\n"
    "CALIBRATE TO THE ACTUAL GOAL. The operator is one person with strong "
    "automation infrastructure aiming for a small, self-running business "
    "reaching roughly $2,000-$5,000/month within a year, as a learning "
    "venture he can grow. He does NOT need to beat the market leader, win a "
    "category, or reach venture scale.\n\n"
    "This matters enormously for how you score:\n"
    "- The mere EXISTENCE of large incumbents is NOT fatal. Almost every "
    "market has them, and profitable small operators coexist with giants all "
    "the time by serving a slice the giant ignores, a buyer who cannot afford "
    "the giant, or a need the giant serves badly. Only call an incumbent "
    "fatal if it plausibly blocks reaching ANY paying slice at all -- and say "
    "concretely why.\n"
    "- 'A big company already does something similar' is a reason to sharpen "
    "the niche, not a reason to score 2/10.\n"
    "What IS genuinely fatal: no reachable buyer at any price; no plausible "
    "way to find the first ~20 customers; automated output that cannot meet "
    "the minimum quality the buyer requires; an actual legal or ToS "
    "prohibition; or total dependence on a platform that forbids or "
    "demonetizes the model."
)

_CRITIQUE_QUESTIONS = """Attack it on these specifically:
- DISTRIBUTION: how would the first ~20 paying customers actually be found?
  "SEO" and "post on social" are not answers. Note the bar is 20, not 1000 --
  a narrow, reachable channel (a niche forum, a trade association list, direct
  outreach to an identifiable list of firms) counts as a real answer.
- WILLINGNESS TO PAY: is there a buyer for whom this is worth real money, even
  if it is a small buyer? A cheaper, narrower tool for buyers priced out of the
  incumbent is a legitimate answer.
- QUALITY BAR: would automated output be good enough for this buyer, or is this
  a domain where being 90% right is worthless or legally dangerous?
- MOAT: what stops this being trivially copied? Accumulating proprietary data,
  or assembly work across many fragmented sources, are real moats at this scale.
- PLATFORM / LEGAL RISK: does it depend on scraping in violation of ToS, or on
  a platform whose rules forbid or demonetize this model? This one IS usually
  fatal when true."""


def critique(name: str, text: str, creds: dict) -> tuple:
    """Adversarially score an idea's SURVIVAL, 0-10. Returns
    (survival_score, fatal_flaw). Fails CLOSED (0) -- same reasoning as
    _relevance: an un-critiqued idea must not slip onto the shortlist."""
    prompt = (
        f"{CAPABILITIES}\n\nCandidate business:\nNAME: {name}\n{text[:3000]}\n\n"
        f"{_CRITIQUE_QUESTIONS}\n\n"
        f"Reply with EXACTLY one line: SURVIVAL: <0-10> | FLAW: <the single "
        f"strongest concrete reason this fails or is hard, one sentence>\n"
        f"Score against the $2-5k/month solo-operator bar described above, NOT "
        f"against market leadership:\n"
        f"0-3 = fatally flawed: no reachable buyer, legally/ToS blocked, or "
        f"automation cannot meet the required quality. Reserve this for real "
        f"blockers, not for 'a bigger competitor exists'.\n"
        f"4-5 = a serious unresolved problem that must be answered first.\n"
        f"6-7 = real problems, but a plausible path to the first few thousand "
        f"dollars a month exists. THIS IS THE NORMAL SCORE for a decent, "
        f"non-obvious idea -- most workable small businesses land here.\n"
        f"8-10 = genuinely strong: a clear reachable buyer and a real edge."
    )
    try:
        import ensemble
        _meta, out = ensemble.best_answer(
            _CRITIQUE_SYSTEM, prompt, creds, max_tokens=300,
            task="business-idea-critique", mode="council")
        m = re.search(r"SURVIVAL:\s*(\d+)", out or "")
        if not m:
            return 0, "critique unparseable -- fail-closed (not admitted)"
        flaw_m = re.search(r"FLAW:\s*(.+)", out or "")
        return (min(int(m.group(1)), 10),
                flaw_m.group(1).strip()[:300] if flaw_m else "")
    except Exception as e:
        return 0, f"critique unavailable ({e}) -- fail-closed (not admitted)"


def final_score(fit: int, survival: int) -> int:
    """One strong objection sinks an idea regardless of how well it fits the
    brief -- so the final score is the WEAKER of the two, never an average.
    Averaging would let a 9/10 fit paper over a 2/10 fatal flaw."""
    return min(fit, survival)


# ── Local pre-filter ─────────────────────────────────────────────────────────
# S66: the sender allowlist is deliberately WIDE (whole domains), because a
# business idea can surface in a newsletter that isn't about business at all
# -- a policy piece noting a regulation, a stock letter describing an
# operating model. Narrowing it would substitute a guess about which writers
# are productive for the evaluation system that exists to decide that.
#
# So instead of narrowing the net, make the net cheap: CIRRUS runs Ollama
# locally at zero marginal cost, so a local triage pass rejects the obvious
# non-starters (political commentary, product promos, personal essays) before
# any paid council call. Same shape as self_review._relevance_local().
#
# FAILS OPEN, and the direction matters: if the local model is down, slow, or
# unparseable, the item is ESCALATED to the paid council rather than dropped.
# Spending a cent unnecessarily is a much cheaper mistake than silently
# discarding a real opportunity.
_PREFILTER_PROMPT = (
    "Does the following text describe a REAL, OPERATING business, product, or "
    "revenue model -- something with actual customers or income -- in enough "
    "detail that someone could study how it works?\n\n"
    "Answer NO for: political or cultural commentary, news about large public "
    "companies, personal essays, product marketing, tutorials, opinion pieces, "
    "and anything with no identifiable business being run.\n"
    "Answer YES if there is any concrete business whose model could be "
    "examined, even briefly mentioned.\n\n"
    "TITLE: {title}\n\nTEXT: {text}\n\n"
    "Reply with exactly one word: YES or NO."
)


def prefilter_local(title: str, text: str) -> tuple:
    """Cheap local triage before paying for the council.
    Returns (should_escalate: bool, reason: str)."""
    try:
        import cirrus_daily as B
        import requests
        r = requests.post(
            f"{B.OLLAMA_HOST}/api/generate",
            json={"model": B.MODEL,
                  "prompt": _PREFILTER_PROMPT.format(title=title[:200], text=text[:4000]),
                  "stream": False,
                  "options": {"temperature": 0, "num_ctx": 4096}},
            timeout=60)
        r.raise_for_status()
        ans = (r.json().get("response") or "").strip().upper()
        if ans.startswith("NO"):
            return False, "local triage: no operating business described"
        if ans.startswith("YES"):
            return True, "local triage: passed"
        return True, "local triage unparseable — escalated (fail-open)"
    except Exception as e:
        return True, f"local triage unavailable ({type(e).__name__}) — escalated (fail-open)"


# ── Source productivity tracking ─────────────────────────────────────────────
# Which sources actually produce candidates, versus only ever producing
# rejections. Buddy's ask: decide what to filter after a week or two of
# EVIDENCE rather than intuition -- and this is also what lets a trial feed
# judge itself (see business_idea_feeds.py).
SOURCE_STATS_PATH = PROJECT_DIR / "config/business_idea_source_stats.json"


def _load_stats() -> dict:
    try:
        return json.loads(SOURCE_STATS_PATH.read_text())
    except Exception:
        return {}


def record_source(source_name: str, outcome: str) -> None:
    """outcome ∈ prefiltered | scored_low | killed | admitted. Never raises."""
    try:
        stats = _load_stats()
        key = (source_name or "unknown")[:80]
        row = stats.setdefault(key, {"prefiltered": 0, "scored_low": 0,
                                     "killed": 0, "admitted": 0,
                                     "first_seen": datetime.now().strftime("%Y-%m-%d")})
        row[outcome] = row.get(outcome, 0) + 1
        row["last_seen"] = datetime.now().strftime("%Y-%m-%d")
        if outcome == "admitted":
            row["last_admitted"] = datetime.now().strftime("%Y-%m-%d")
        SOURCE_STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SOURCE_STATS_PATH.write_text(json.dumps(stats, indent=1, sort_keys=True))
    except Exception:
        pass


def source_productivity() -> list:
    """[(source, seen, admitted, verdict)] worst-first, for the report and for
    deciding which trial feeds to keep."""
    out = []
    for name, r in _load_stats().items():
        seen = sum(r.get(k, 0) for k in ("prefiltered", "scored_low", "killed", "admitted"))
        adm = r.get("admitted", 0)
        if seen < 5:
            verdict = "too early to judge"
        elif adm == 0:
            verdict = "no candidates yet"
        else:
            verdict = f"{adm} candidate(s)"
        out.append((name, seen, adm, verdict))
    return sorted(out, key=lambda t: (t[2], -t[1]))


# ── Effort / cost estimate ───────────────────────────────────────────────────
# S66, Buddy's ask: "can you estimate what it would take for each". A score
# says whether an idea is worth considering; an estimate says whether it is
# worth HIS time this month. Grounded in CAPABILITIES so the model prices
# only the delta -- most of these need a scraper and a billing hookup, not a
# research stack, because the research stack already exists.
_ESTIMATE_SYSTEM = (
    "You estimate what it would actually take to build and run a small "
    "automated business, for an operator who ALREADY owns the infrastructure "
    "described. Price only the DELTA -- what genuinely has to be built or paid "
    "for on top of what exists. Be concrete and conservative; prefer a range "
    "over false precision. Do not pad estimates to seem thorough."
)


def estimate(name: str, text: str, creds: dict) -> dict:
    """Return {build_effort, run_cost, time_to_revenue, first_step} or {}.
    Never raises -- an estimate is useful context, not a gate, so a failure
    here must not affect whether an idea is admitted."""
    prompt = (
        f"{CAPABILITIES}\n\nBusiness to estimate:\nNAME: {name}\n{text[:2500]}\n\n"
        f"Reply with JSON only, no prose, no fences:\n"
        f'{{"build_effort": "<realistic build time for one experienced person '
        f'using AI coding tools, e.g. \'3-5 days\' or \'2-3 weeks\'; count ONLY '
        f'what is not already built>", '
        f'"run_cost": "<estimated monthly running cost in USD: APIs, data '
        f'feeds, hosting; note anything with a real per-unit cost>", '
        f'"time_to_revenue": "<realistic time to the first paying customer, '
        f'assuming part-time effort>", '
        f'"first_step": "<the single most useful thing to do FIRST to test '
        f'whether this is real, ideally before building anything>"}}'
    )
    try:
        import ensemble
        _meta, out = ensemble.best_answer(
            _ESTIMATE_SYSTEM, prompt, creds, max_tokens=700,
            task="business-idea-estimate", mode="council")
        t = (out or "").strip()
        if t.startswith("```"):
            t = t.strip("`")
            if t.lower().startswith("json"):
                t = t[4:]
        try:
            data = json.loads(t)
        except Exception:
            m = re.search(r"\{.*\}", t, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
        return {k: str(v)[:400] for k, v in (data or {}).items()
                if k in ("build_effort", "run_cost", "time_to_revenue", "first_step") and v}
    except Exception:
        return {}


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


# ── Email intake (the Medium answer) ─────────────────────────────────────────
# S66: Medium blocks automated fetching at the Cloudflare WAF level -- not a
# paywall, not cookies (verified: fresh session cookies + matching Safari UA
# still get "Sorry, you have been blocked"). RSS gives ~110 characters. But
# Medium will EMAIL Buddy the writers he follows, and CIRRUS already reads
# three of his inboxes -- so the content arrives legitimately, sent by Medium
# to a subscriber, with no scraping and nothing to circumvent.
#
# Deliberately NOT reusing cirrus_daily.fetch_emails(), for two reasons:
#   1. It filters on EMAIL_CFG["keywords"] -- the system-IMPROVEMENT keywords.
#      This project needs the business mission instead (Buddy raised exactly
#      this distinction).
#   2. It ADVANCES the shared config/email_state.json UID cursor. Calling it
#      here would consume messages the main digest hasn't processed yet and
#      make them silently vanish from the daily digest.
# So this is a separate, strictly read-only pass with its own state file.
EMAIL_SEEN_PATH = PROJECT_DIR / "config/business_idea_email_seen.json"
_EMAIL_LOOKBACK_DAYS = 2
_MAX_EMAILS_PER_RUN = 40

# A sender allowlist is not optional here. The live inboxes are mostly
# promotional mail (retail sales, marketplace listings, spam with obfuscated
# headers) -- a first live read returned 40 messages of which zero were
# relevant. Every message scored costs a council call, so scanning
# indiscriminately would burn real money on Harley-Davidson listings.
# Matched case-insensitively against the raw From header, so it covers both
# the display name and the address. Extend this list, not the logic.
EMAIL_SENDER_PATTERNS = [
    "medium.com",          # the whole reason this path exists
    "substack.com",
    "beehiiv.com",
    "ghost.io",
    "convertkit",
    "lennysnewsletter",
    "notboring",
    "latent.space",
    "stratechery",
    "indiehackers",
]


def email_sender_allowed(sender: str) -> bool:
    s = (sender or "").lower()
    return any(p in s for p in EMAIL_SENDER_PATTERNS)


def fetch_business_emails(creds: dict, lookback_days: int = _EMAIL_LOOKBACK_DAYS) -> list:
    """Read recent messages from the configured inboxes WITHOUT touching the
    digest's UID state. Returns [{message_id, subject, sender, body}].
    Dedupes on Message-ID, which is stable across accounts and re-runs."""
    import email as _email
    import imaplib
    from email.header import decode_header, make_header

    try:
        cfg = json.loads((PROJECT_DIR / "config/sources.json").read_text())
        accounts = (cfg.get("email") or {}).get("accounts", []) or []
    except Exception:
        return []

    try:
        seen_ids = set(json.loads(EMAIL_SEEN_PATH.read_text()))
    except Exception:
        seen_ids = set()

    since_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
    out, new_ids = [], set()

    for account in accounts:
        if not account.get("enabled", True):
            continue
        password = creds.get(account.get("credential_key", ""))
        if not password or not account.get("address"):
            continue
        try:
            mail = imaplib.IMAP4_SSL(account["imap_server"],
                                     account.get("imap_port", 993), timeout=60)
            mail.login(account["address"], password)
            mail.select("inbox", readonly=True)  # readonly: never marks read

            # Filter SERVER-SIDE, one IMAP search per allowlisted sender.
            # S66 bug caught live: fetching the most recent N and filtering
            # client-side returned ZERO relevant messages on Buddy's Yahoo
            # inbox -- it receives ~660 messages per 2 days, so the newest 40
            # were all promotional and the 967 Medium emails sitting in that
            # same mailbox were never even looked at. Asking the server for
            # the senders we want is both correct and far cheaper.
            uids = []
            for pattern in EMAIL_SENDER_PATTERNS:
                try:
                    _typ, ids = mail.uid("search", None,
                                         f'(SINCE {since_date} FROM "{pattern}")')
                except Exception:
                    continue
                if ids and ids[0]:
                    uids.extend(ids[0].split())
            # newest first, then cap -- the cap now applies to ALREADY-relevant
            # mail rather than silently discarding it
            uids = sorted(set(uids), key=lambda u: int(u), reverse=True)[:_MAX_EMAILS_PER_RUN]
            for uid in uids:
                try:
                    _t, data = mail.uid("fetch", uid, "(RFC822)")
                    msg = _email.message_from_bytes(data[0][1])
                except Exception:
                    continue
                mid = (msg.get("Message-ID") or "").strip()
                if not mid or mid in seen_ids or mid in new_ids:
                    continue
                # Filter BEFORE marking seen and before any scoring, so a
                # sender added to the allowlist later still gets picked up
                # on the next run rather than being permanently skipped.
                if not email_sender_allowed(msg.get("From") or ""):
                    continue
                new_ids.add(mid)
                try:
                    subject = str(make_header(decode_header(msg.get("Subject", ""))))
                except Exception:
                    subject = msg.get("Subject", "") or ""
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/html":
                            body = part.get_payload(decode=True).decode(
                                part.get_content_charset() or "utf-8", "ignore")
                            break
                        if part.get_content_type() == "text/plain" and not body:
                            body = part.get_payload(decode=True).decode(
                                part.get_content_charset() or "utf-8", "ignore")
                else:
                    try:
                        body = msg.get_payload(decode=True).decode(
                            msg.get_content_charset() or "utf-8", "ignore")
                    except Exception:
                        body = ""
                out.append({"message_id": mid, "subject": subject.strip(),
                            "sender": (msg.get("From") or "").strip(), "body": body})
            mail.logout()
        except Exception:
            continue

    # NOTE: deliberately does NOT record these as seen. S66 -- an SSH drop
    # mid-run proved the hazard: the seen-file was written here at FETCH
    # time, so 40 messages were permanently marked processed while the
    # scoring loop that should have consumed them never ran. A transient
    # network blip silently cost a day of Medium content, unrecoverably.
    # mark_emails_seen() is now called by the caller AFTER scoring, matching
    # how the RSS path only persists SEEN_PATH at the end of a completed run.
    return out


def mark_emails_seen(message_ids) -> None:
    """Persist Message-IDs as processed. Call only after scoring succeeds."""
    ids = {m for m in message_ids if m}
    if not ids:
        return
    try:
        existing = set(json.loads(EMAIL_SEEN_PATH.read_text()))
    except Exception:
        existing = set()
    try:
        EMAIL_SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        EMAIL_SEEN_PATH.write_text(json.dumps(sorted(existing | ids), indent=1))
    except Exception:
        pass


def _score_and_store(url: str, title: str, text: str, source_name: str,
                     creds: dict, result: dict, db_path: str = None) -> None:
    """Score one article and store it if it clears both bars. Shared by the
    RSS-feed phase and the targeted-search phase so a candidate means exactly
    the same thing regardless of how it was found -- same mission, same
    RELEVANCE_MIN, same adversarial critique."""
    # Free local triage first -- most inbound text is not about a business at
    # all, and there is no reason to pay a council to tell us that.
    escalate, why_local = prefilter_local(title, text)
    if not escalate:
        result["prefiltered"] = result.get("prefiltered", 0) + 1
        record_source(source_name, "prefiltered")
        return

    score, why, idea_label = _relevance(title, text, creds)
    if score < RELEVANCE_MIN:
        result["scored_low"] += 1
        record_source(source_name, "scored_low")
        return

    survival, flaw = critique(idea_label or title, text, creds)
    final = final_score(score, survival)
    if final < RELEVANCE_MIN:
        result["scored_low"] += 1
        record_source(source_name, "killed")
        result.setdefault("killed_by_critique", []).append(
            f"{idea_label or title} (fit {score}, survival {survival}): {flaw}")
        return

    # Only survivors get an estimate -- no point pricing a dead idea.
    est = estimate(idea_label or title, text, creds)
    slug, is_new = resolve_slug(idea_label, title, db_path=db_path)
    entity_kb.upsert_entity(KB_PROJECT, slug, idea_label or title,
                            entity_type="business_idea",
                            lead_state="candidate",
                            fields={"category": source_name,
                                    "fit_score": score,
                                    "survival_score": survival,
                                    "final_score": final,
                                    "main_risk": flaw,
                                    **est},
                            db_path=db_path)
    entity_kb.add_signal(
        KB_PROJECT, slug, "candidate",
        f"[{final}/10 | fit {score}, survives critique {survival}] {why} "
        f"(from {source_name}: \"{title}\")\n"
        f"    Main risk: {flaw}",
        source_url=url, confidence="medium", db_path=db_path)
    record_source(source_name, "admitted")
    if is_new:
        result["admitted"].append(f"{idea_label or title} ({final}/10)")
    else:
        result["corroborated"].append(f"{idea_label or title} ({final}/10)")


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

    for source in all_sources():
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

            paywalled = False
            try:
                content, paywalled = cirrus_daily.fetch_article_content(url)
            except Exception:
                content = ""
            if paywalled:
                # Buddy pays for many of these subscriptions -- a paywall hit
                # means either a cookie needs refreshing or he has an article
                # worth reading that we couldn't. Route it into the SAME
                # paywalls.log the morning brief already reports (with domain,
                # title, and all-time hit count), rather than inventing a
                # second channel he'd have to remember to check.
                try:
                    cirrus_daily.log_paywall_hit(url, source["name"], title)
                except Exception:
                    pass
                result.setdefault("paywalled", []).append(f"{source['name']}: {title}")
            text = content or entry.get("summary", "") or ""
            if not text.strip():
                continue
            result["fresh"] += 1

            if dry_run:
                continue
            _score_and_store(url, title, text, source["name"], creds, result,
                             db_path=db_path)

    # ── Phase 2: email intake ─────────────────────────────────────────────
    # Where Medium content actually reaches us (see fetch_business_emails).
    if not dry_run:
        for msg in fetch_business_emails(creds):
            body = cirrus_daily.clean_text(msg["body"], 20000)
            if body.strip() and len(body) >= 200:
                result["emails"] = result.get("emails", 0) + 1
                _score_and_store("", msg["subject"] or msg["sender"], body,
                                 f"email: {msg['sender'][:50]}", creds, result,
                                 db_path=db_path)
            # Marked seen per-message, immediately AFTER it is scored, so an
            # interrupted run loses at most the one message in flight rather
            # than the whole batch.
            mark_emails_seen([msg["message_id"]])

    # ── Phase 3: targeted search ──────────────────────────────────────────
    # Actively hunt for operating businesses rather than waiting for one to
    # appear in a followed feed.
    if not dry_run:
        for query in todays_queries():
            try:
                urls = cirrus_daily.search_web(query, max_results=_RESULTS_PER_QUERY)
            except Exception:
                continue
            result["searched"] = result.get("searched", 0) + 1
            for url in urls:
                if not url or url in seen or url in newly_seen:
                    continue
                newly_seen.add(url)
                result["fetched"] += 1
                paywalled = False
                try:
                    content, paywalled = cirrus_daily.fetch_article_content(url)
                except Exception:
                    content = ""
                title = (content or "").strip().splitlines()[0][:120] if content else url
                if paywalled:
                    try:
                        cirrus_daily.log_paywall_hit(url, f"search: {query[:40]}", title)
                    except Exception:
                        pass
                    result.setdefault("paywalled", []).append(f"search result: {url}")
                if not (content or "").strip():
                    continue
                result["fresh"] += 1
                _score_and_store(url, title, content, f"search: {query[:40]}",
                                 creds, result, db_path=db_path)

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

    # S66: the scan must cover the Medium/Substack feeds Buddy already pays
    # for via the main digest, not just this project's own four.
    combined = all_sources()
    checks.append(("all_sources includes this project's own feeds",
                   all(any(c["rss"] == s["rss"] for c in combined) for s in SOURCES)))
    checks.append(("all_sources dedupes by RSS url",
                   len({s["rss"] for s in combined}) == len(combined)))
    checks.append(("every source has a name and an rss url",
                   all(s.get("name") and s.get("rss") for s in combined)))

    # S66: targeted search must use BUSINESS terms, distinct from
    # self_review.py's system-improvement criteria.
    checks.append((f"query rotation returns {_QUERIES_PER_RUN} queries",
                   len(todays_queries()) == _QUERIES_PER_RUN))
    checks.append(("rotated queries are all real, distinct queries",
                   set(todays_queries()) <= set(BUSINESS_SEARCH_QUERIES)
                   and len(set(todays_queries())) == _QUERIES_PER_RUN))
    from datetime import date as _d, timedelta as _t
    covered = set()
    for i in range(len(BUSINESS_SEARCH_QUERIES)):
        start = ((_d.today() + _t(days=i)).toordinal() * _QUERIES_PER_RUN) % len(BUSINESS_SEARCH_QUERIES)
        covered |= {BUSINESS_SEARCH_QUERIES[(start + j) % len(BUSINESS_SEARCH_QUERIES)]
                    for j in range(_QUERIES_PER_RUN)}
    checks.append(("rotation eventually covers every query",
                   covered == set(BUSINESS_SEARCH_QUERIES)))
    # S66: the email sender allowlist -- a live inbox read returned 40
    # messages, all promotional, none relevant. Without this every one costs
    # a council call.
    checks.append(("Medium senders are allowed (the point of email intake)",
                   email_sender_allowed("Medium Daily Digest <noreply@medium.com>")))
    checks.append(("Substack senders are allowed",
                   email_sender_allowed("Some Writer <x@substack.com>")))
    checks.append(("retail promo mail is rejected",
                   not email_sender_allowed('"Bose Certified" <Email@email.bose.com>')))
    checks.append(("marketplace listings are rejected",
                   not email_sender_allowed('"Facebook Marketplace" <marketplace@facebookmail.com>')))
    checks.append(("an empty sender is rejected, not allowed by default",
                   not email_sender_allowed("")))

    # S66: the local pre-filter must fail OPEN -- if the local model is down,
    # items escalate to the paid council rather than being silently dropped.
    # Getting this backwards would quietly discard opportunities to save cents.
    _ok, _why = prefilter_local("x", "y")  # no Ollama in the dev checkout
    checks.append(("prefilter fails OPEN when the local model is unreachable",
                   _ok is True and "fail-open" in _why))

    # Source stats: the evidence Buddy will use to tighten filters later.
    import os as _os
    import tempfile as _tf
    _fd, _sp = _tf.mkstemp(suffix=".json")
    _os.close(_fd)
    _os.unlink(_sp)
    _orig_sp = globals()["SOURCE_STATS_PATH"]
    try:
        globals()["SOURCE_STATS_PATH"] = Path(_sp)
        for _ in range(6):
            record_source("Barren Feed", "scored_low")
        record_source("Good Feed", "admitted")
        rows = {r[0]: r for r in source_productivity()}
        checks.append(("a source with an admit is reported as productive",
                       rows["Good Feed"][2] == 1))
        checks.append(("a well-sampled source with no admits is flagged",
                       rows["Barren Feed"][3] == "no candidates yet"))
        checks.append(("a barely-sampled source is not judged yet",
                       rows["Good Feed"][3] != "no candidates yet"))
    finally:
        globals()["SOURCE_STATS_PATH"] = _orig_sp
        if _os.path.exists(_sp):
            _os.unlink(_sp)

    checks.append(("search terms target revenue/operating businesses, not 'ideas'",
                   all(any(w in q for w in ("revenue", "profitable", "subscribers",
                                            "business"))
                       for q in BUSINESS_SEARCH_QUERIES)))

    checks.append(("a fatal flaw sinks a high-fit idea (min, not average)",
                   final_score(9, 2) == 2))
    checks.append(("a weak-fit idea is not rescued by surviving critique",
                   final_score(3, 10) == 3))
    checks.append(("both strong keeps the score", final_score(8, 8) == 8))
    checks.append(("a fail-closed critique (0) always sinks the idea",
                   final_score(10, 0) == 0))
    checks.append(("final score never exceeds either input",
                   all(final_score(a, b) <= min(a, b)
                       for a in range(11) for b in range(11))))

    # S66 regression: CRASH SAFETY. Both email bugs shipped with a fully
    # green selftest, because nothing tested the fetch/mark contract -- the
    # tests only covered pure helpers. A network drop mid-run then
    # permanently lost 40 messages. These assert the properties that
    # actually failed, without needing a live IMAP server.
    import ast as _ast
    import inspect as _inspect
    import textwrap as _tw
    _fetch_src = _inspect.getsource(fetch_business_emails)

    # AST, not text search: the function's own explanatory comment mentions
    # mark_emails_seen(), and a substring check would flag that as a call.
    # (Made exactly that mistake writing this test -- comments are not code.)
    def _calls_in(fn):
        names = set()
        for n in _ast.walk(_ast.parse(_tw.dedent(_inspect.getsource(fn)))):
            if isinstance(n, _ast.Call):
                f = n.func
                if isinstance(f, _ast.Name):
                    names.add(f.id)
                elif isinstance(f, _ast.Attribute):
                    base = getattr(f.value, "id", "")
                    names.add(f"{base}.{f.attr}" if base else f.attr)
        return names

    _fetch_calls = _calls_in(fetch_business_emails)
    checks.append(("fetch does NOT persist seen-state (crash safety)",
                   "mark_emails_seen" not in _fetch_calls
                   and "EMAIL_SEEN_PATH.write_text" not in _fetch_calls))
    checks.append(("the write path is confined to mark_emails_seen",
                   "EMAIL_SEEN_PATH.write_text" in _calls_in(mark_emails_seen)))
    _run_src = _inspect.getsource(run)
    checks.append(("the run loop marks each email seen after scoring it",
                   "mark_emails_seen" in _run_src))
    checks.append(("scoring happens before marking seen, not after",
                   _run_src.index('_score_and_store("",')
                   < _run_src.index("mark_emails_seen")))
    # The cap must never be applied before the sender filter -- that bug hid
    # 967 Medium emails behind 660 promos in a busy inbox.
    checks.append(("sender filtering happens server-side, before the cap",
                   _fetch_src.index('FROM "') < _fetch_src.index("_MAX_EMAILS_PER_RUN")))

    import os
    import tempfile
    fd, _edb = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        _orig = globals()["EMAIL_SEEN_PATH"]
        globals()["EMAIL_SEEN_PATH"] = Path(_edb)
        Path(_edb).write_text("[]")
        mark_emails_seen(["<a@x>", "<b@x>"])
        first = set(json.loads(Path(_edb).read_text()))
        mark_emails_seen(["<b@x>", "<c@x>"])
        second = set(json.loads(Path(_edb).read_text()))
        checks.append(("mark_emails_seen accumulates rather than overwriting",
                       first == {"<a@x>", "<b@x>"}
                       and second == {"<a@x>", "<b@x>", "<c@x>"}))
        mark_emails_seen([])
        checks.append(("marking nothing is a safe no-op",
                       set(json.loads(Path(_edb).read_text())) == second))
    finally:
        globals()["EMAIL_SEEN_PATH"] = _orig
        if os.path.exists(_edb):
            os.unlink(_edb)

    # S66 regression: two articles about the same story got two entities.
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
    # Record health so a silent failure surfaces in the morning brief rather
    # than going unnoticed for days -- these run unattended every morning.
    try:
        outcome = run(dry_run=dry)
        print(outcome)
        if not dry:
            import job_status
            job_status.record("businessideascan", True,
                              f"{len(outcome.get('admitted', []))} new, "
                              f"{outcome.get('scored_low', 0)} rejected, "
                              f"{outcome.get('emails', 0)} emails")
    except Exception as exc:
        if not dry:
            try:
                import job_status
                job_status.record("businessideascan", False, str(exc)[:180])
            except Exception:
                pass
        raise
