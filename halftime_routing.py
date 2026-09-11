#!/usr/bin/env python3
"""halftime_routing.py — the touring half of the halftime dashboard.

S79. Answers, per home game: who is announced to play within reach of Acrisure
around that date? That is the "Routing through" column, and it is also the
signal that makes the CREDIT pool honest — an act with a halftime credit is
for-hire nostalgia or a stadium headliner depending on whether it is currently
touring at scale, and nothing else tells them apart.

WHY THE COVERAGE RECORD IS THE POINT. Justin asked whether we can bypass
Pollstar. That cannot be answered by assurance. Every sweep records what was
searched and what came back, per metro, so a thin date is visibly thin and the
answer becomes evidence. It is also what lets the dashboard say "we searched
and found nothing" rather than showing a blank cell that reads as the same
thing.

Discovery reuses the catalogue's proven machinery — Brave search, article
fetch, local-model extraction with escalation — rather than adding scraping
infrastructure that would need its own maintenance and its own 403s.

Python 3.9-safe (CIRRUS): no PEP-604 unions.
"""
import json
import os
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_DIR = Path(__file__).resolve().parent
OUT_PATH = PROJECT_DIR / "out" / "halftime" / "routing.json"
LOG_PATH = PROJECT_DIR / "logs" / "halftime-routing.log"

# Metros inside the radius Buddy set (100–200 mi of Acrisure), with the driving
# distance so the dashboard can show WHY something counts as near.
METROS = [
    ("Pittsburgh, PA", 0),
    ("Morgantown, WV", 75),
    ("Youngstown, OH", 65),
    ("Akron, OH", 110),
    ("Erie, PA", 130),
    ("Cleveland, OH", 135),
    ("Columbus, OH", 185),
]
# Days either side of kickoff that count as "in the area". Three days is the
# window an act could plausibly stay over or arrive early for.
WINDOW_DAYS = 3
MAX_SEARCH_RESULTS = 6
MAX_FETCH_CHARS = 12000

_EXTRACT_SYSTEM = """You extract announced live music dates from concert
listings.

Return ONLY a JSON array, no prose. Each element:
{"artist": "the performing act's name",
 "date": "YYYY-MM-DD, the date of the show",
 "venue": "venue name if stated, else ''",
 "city": "city, state if stated, else ''",
 "style": "the kind of music, from: hip hop / rap, rock, classic rock, country,
           pop, r&b / soul, gospel, latin, metal, jazz, classical / orchestral,
           marching / military, other. Use '' if the listing does not make it
           clear -- a GUESSED style is worse than none, because this is the
           field a booker filters on."}

Rules:
- Only shows with a REAL, STATED date. If a listing gives no date, or gives a
  range or a month with no day, SKIP it. A guessed date is worse than a missing
  one here: it puts an act next to a game it is not near, and a client plans
  around that.
- Only MUSIC performances. Skip theatre, comedy, sports, festivals with no
  named act, and "tickets on sale" pages with no show date.
- Use the four-digit year that the source states. Do not assume the current
  year.
- STYLE comes from the listing itself -- a genre tag, a descriptor, a support
  billing, the venue's own categorisation. If the listing does not say, leave
  it empty. Do not infer a style from the act's name.
- If nothing qualifies, return []."""



# ── draw: is this act's scale plausible for a stadium slot ──────────────────
# The first live sweep put Dent May, Post Animal and Eivor against 1 November.
# All three really are in the area, so the sweep was right and the
# PRESENTATION would have been wrong in exactly the way the credit list was:
# a booker reading a 300-capacity room as a halftime option concludes we do
# not know the business.
#
# TWO SIGNALS, and the second is the better one:
#
#   1. THE ROOM. The venue an act is playing is the best public proxy for the
#      draw it carries. A booker knows these rooms by name, so this reads as
#      information rather than a score.
#   2. A SPORTS CREDIT IN OUR OWN CATALOGUE. An act that is BOTH in the area
#      and has already played a halftime somewhere is the highest-value cell in
#      the whole dashboard — the intersection of the two pools, which is the
#      thing neither column can show on its own.
#
# The venue table is a hand-written name list, which is the T36 shape and will
# eventually be out of date. It is acceptable here ONLY because it fails
# VISIBLE: a room we do not recognise is labelled "scale unknown" and keeps its
# place in the list, never quietly dropped. An unknown act is a lead we have
# not sized, not an act we have judged small.

VENUE_TIERS = {
    "stadium": (
        "acrisure stadium", "huntington bank field", "milan puskar stadium"),
    "arena": (
        "ppg paints arena", "petersen events center", "rocket arena",
        "rocket mortgage fieldhouse", "nationwide arena",
        "schottenstein center", "covelli centre", "erie insurance arena",
        "wvu coliseum"),
    "amphitheatre": (
        "the pavilion at star lake", "star lake", "blossom music center",
        "jacobs pavilion", "kemba live"),
    "theatre": (
        "stage ae", "benedum center", "heinz hall", "roxian theatre",
        "carnegie music hall", "the wylie", "agora theatre", "house of blues",
        "newport music hall", "stambaugh auditorium", "akron civic theatre",
        "severance music center", "carnegie of homestead",
        "goodyear theater", "ej thomas hall", "warner theatre",
        "metropolitan theatre", "mr. smalls", "mr smalls"),
    "club": (
        "beachland ballroom", "globe iron", "skully's music diner",
        "skullys music diner", "rumba cafe", "a&r music bar", "club cafe",
        "thunderbird", "grog shop", "ace of cups", "king of clubs",
        "b side liquor lounge"),
}

# Which tiers could carry a stadium halftime at all.
_PLAUSIBLE = ("stadium", "arena", "amphitheatre")

DRAW_ORDER = {"credited": 0, "stadium": 1, "arena": 2, "amphitheatre": 3,
              "unknown": 4, "theatre": 5, "club": 6}


def _norm_venue(name: str) -> str:
    """Strip everything but letters and digits before matching. The live sweep
    returned "E J Thomas Hall", which a plain substring test does not match
    against "ej thomas hall" — a spacing difference should not decide whether
    we recognise a room."""
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def venue_tier(venue: str) -> str:
    if not (venue or "").strip():
        return "unknown"
    low = _norm_venue(venue)
    for tier, names in VENUE_TIERS.items():
        for name in names:
            if _norm_venue(name) in low:
                return tier
    return "unknown"


def draw_signal(event: Dict, credited_names=()) -> Dict:
    """What we can honestly say about this act's scale."""
    from_credits = canonical_key(event.get("artist", "")) in set(credited_names)
    tier = venue_tier(event.get("venue", ""))

    if from_credits:
        return {"tier": "credited", "plausible": True,
                "label": "In the area AND has a halftime credit",
                "why": "This act appears in the credit catalogue as having "
                       "played a sports slot, and is announced near this "
                       "date. Both pools at once."}
    if tier in _PLAUSIBLE:
        return {"tier": tier, "plausible": True,
                "label": "{}-scale room".format(tier.capitalize()),
                "why": "Playing {} — a room whose scale is consistent with a "
                       "stadium slot.".format(event.get("venue") or "a large room")}
    if tier == "unknown":
        return {"tier": "unknown", "plausible": None,
                "label": "Scale unknown",
                "why": "We do not recognise {} , so this act is unsized rather "
                       "than judged small. Worth a look if the name is "
                       "familiar to you.".format(
                           event.get("venue") or "the venue")}
    # The room measures the SHOW, not the artist. A major act doing an
    # intimate theatre run reads small here, and that is a real limit of the
    # proxy rather than a fact about the act — so the page says so instead of
    # letting the ranking imply something it cannot support.
    caveat = ("" if tier == "club" else
              " Note this sizes the SHOW, not the artist — a major act on an "
              "intimate run looks small by this measure.")
    return {"tier": tier, "plausible": False,
            "label": "{}-scale room".format(tier.capitalize()),
            "why": "Playing {} — below stadium draw. Listed because they are "
                   "genuinely in the area, not as a halftime "
                   "suggestion.{}".format(
                       event.get("venue") or "a small room", caveat)}


def canonical_key(name: str) -> str:
    """Match routing artists to catalogue acts the same way the dashboard
    collapses name variants, so the two pools can be cross-referenced."""
    import re as _re
    out = _re.sub(r"\s*\([^)]*\)\s*$", "", (name or "").strip())
    if out.lower().startswith("the "):
        out = out[4:]
    return " ".join(out.lower().split())


def log(msg: str) -> None:
    line = "[{}] {}".format(
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_events(raw: str) -> Optional[List[Dict]]:
    """None = unusable output (caller escalates); [] = genuinely nothing.

    Same tri-state as the catalogue's parser, for the same reason: a model that
    returned garbage and a model that found nothing must not look alike.
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        artist = str(item.get("artist") or "").strip()
        date = str(item.get("date") or "").strip()
        if not artist or not _DATE_RX.match(date):
            continue          # a show with no real date cannot be matched
        # S141: style is carried through because it is Justin's scoring
        # criterion #3 ("style-of-music fit for a family/TV stadium crowd") and
        # the touring column had it on 0 of 71 acts while the for-hire column
        # had it on 29 of 33. Without it the 1 November card read as noise: a
        # booker cannot tell that "Forbidden" and "With A Vengeance" are thrash
        # and metalcore without leaving the page, on the one date whose whole
        # brief is a military/patriotic tie for a 45+ daytime crowd.
        # Normalised to the SAME vocabulary the catalogue uses, so the two
        # pools can be read side by side; anything unrecognised is dropped
        # rather than shown, because a wrong style is worse than none.
        out.append({"artist": artist[:120], "date": date,
                    "venue": str(item.get("venue") or "").strip()[:120],
                    "city": str(item.get("city") or "").strip()[:80],
                    "style": normalise_style(item.get("style"))})
    return out


# S141 — the style vocabulary, shared with halftime_catalogue's _MUSIC_SYSTEM
# so the two columns of the dashboard can be compared. A model that answers
# with something outside it is answering a different question, and the honest
# render is a blank rather than a guess.
STYLE_VOCAB = {
    "hip hop / rap", "rock", "classic rock", "country", "pop", "r&b / soul",
    "gospel", "latin", "metal", "jazz", "classical / orchestral",
    "marching / military", "other",
}
_STYLE_ALIASES = {
    "hip hop": "hip hop / rap", "rap": "hip hop / rap",
    "hip-hop": "hip hop / rap", "hip hop/rap": "hip hop / rap",
    "r&b": "r&b / soul", "soul": "r&b / soul", "rnb": "r&b / soul",
    "classical": "classical / orchestral", "orchestral": "classical / orchestral",
    "symphonic": "classical / orchestral", "opera": "classical / orchestral",
    "metalcore": "metal", "heavy metal": "metal", "thrash": "metal",
    "punk": "rock", "indie": "rock", "alternative": "rock",
    "americana": "country", "folk": "country",
    "military": "marching / military", "marching": "marching / military",
}


def normalise_style(raw) -> str:
    """A style from the shared vocabulary, or "" — never a guess."""
    v = str(raw or "").strip().lower()
    if not v:
        return ""
    if v in STYLE_VOCAB:
        return v
    return _STYLE_ALIASES.get(v, "")


def near_game(event_date: str, game_date: str, window: int = WINDOW_DAYS) -> bool:
    """Is this show inside the window around kickoff?"""
    try:
        e = datetime.strptime(event_date, "%Y-%m-%d")
        g = datetime.strptime(game_date, "%Y-%m-%d")
    except Exception:
        return False
    return abs((e - g).days) <= window


def gap_days(event_date: str, game_date: str) -> Optional[int]:
    try:
        e = datetime.strptime(event_date, "%Y-%m-%d")
        g = datetime.strptime(game_date, "%Y-%m-%d")
    except Exception:
        return None
    return (e - g).days


def queries_for(game: Dict) -> List[tuple]:
    """(metro, miles, query) for one game — one search per metro."""
    date = game.get("date")
    if not date:
        return []
    try:
        d = datetime.strptime(date, "%Y-%m-%d")
    except Exception:
        return []
    month = d.strftime("%B %Y")
    return [(metro, miles,
             "concerts {} {} schedule live music".format(metro, month))
            for metro, miles in METROS]


# S119. The metro sweep runs concurrently.
#
# ★ WHAT THIS ACTUALLY BUYS TODAY: **about 8%.** Not the 2.7x the first version
# appeared to give. Measured live, same game, 7 metros, one run each:
#
#     serial    workers=1   498.6s   local=4 escalated=3
#     parallel  workers=7   462.3s   local=5 escalated=2   -> 1.08x
#
# I expected far more, on the reasoning that the ~63 s between metros was mostly
# Brave search and article fetches. **That reasoning was wrong.** Back out the
# numbers: 7-way search parallelism saved 36 s of 498 s, so the web portion is
# only ~6 s per metro and the MODEL call is ~65 s of it -- roughly 90% of the
# sweep. Overlapping the web half cannot move a total the model dominates.
#
# The model half does not overlap under ollama, which does not batch: aggregate
# throughput is flat at ~22.7 tok/s however many requests are in flight
# (docs/DGX-SPARK-PERFORMANCE.md section 12), so extractions are throttled to
# one on purpose -- see DEFAULT_EXTRACT_WORKERS below for what happened when
# they were not.
#
# **So this change is the PREREQUISITE, not the win.** The structure is now in
# place and provably safe; the payoff arrives when the engine can serve the
# model calls concurrently. Under vLLM the same box did 325 tok/s at 16-way,
# and raising HALFTIME_ROUTING_EXTRACT_WORKERS is then a one-line change.
#
# 7 metros per game, so 7 saturates one game's worth of work; games still run
# one after another, capping requests in flight at `workers` regardless of how
# many games are due.
#
# **Set HALFTIME_ROUTING_WORKERS=1 to get the exact serial path back** -- not an
# approximation of it: at 1 the executor is skipped entirely. That is the kill
# switch if concurrency is ever suspected, and it is what the selftest compares
# against to prove the two paths agree.
DEFAULT_ROUTING_WORKERS = 7
MAX_ROUTING_WORKERS = 16

# ── S119, MEASURED THE HARD WAY: cap concurrent MODEL calls separately ───────
# The first parallel version ran everything 7-wide and a live A/B caught the
# cost: same game, same 7 metros, escalation went 2/7 serial -> 6/7 parallel.
# Escalation is a PAID Anthropic call, so "2.7x faster" also meant "3x the API
# bill", and nothing in the artefact would have shown it.
#
# The mechanism is arithmetic, not bad luck. ollama does not batch -- aggregate
# throughput is flat at ~22.7 tok/s however many requests are in flight
# (docs/DGX-SPARK-PERFORMANCE.md section 12). Seven concurrent extractions of
# ~500 tokens each therefore need ~7*500/22.7 = 154s of wall time, and
# llm_providers._TIMEOUT is 120. The later requests time out, and a timeout
# escalates. Concurrency did not make the model faster; it made it fail.
#
# So search and fetch stay 7-wide -- they are network wait and genuinely
# parallel -- while the model calls are throttled to what the engine can
# actually serve. Default 1, which is what ollama is.
#
# RAISE THIS WHEN THE ENGINE CHANGES: under vLLM the same box served 325 tok/s
# at 16-way concurrency, so HALFTIME_ROUTING_EXTRACT_WORKERS could go to the
# metro count and the timeouts would not come back.
DEFAULT_EXTRACT_WORKERS = 1
MAX_EXTRACT_WORKERS = 16


# ── Extraction token budget (S141) ───────────────────────────────────────────
# The local model is a REASONING model: its thinking is charged against
# max_tokens before a single character of JSON is emitted. Measured on the
# 2026-09-09 06:30 run's own sources: reasoning 2,500-2,700 tokens, the JSON
# answer ~600 more -- 3,256 of a 4,000 budget, 744 tokens of headroom. When the
# sources come back a little messier the reasoning runs longer, the JSON array
# is cut off mid-element, `parse_acts` finds no closing bracket and returns
# None, and the chain escalates to a PAID model. Reproduced exactly: at
# max_tokens=3000 the same block returns finish_reason=length and parse_acts
# None; at 4000 it returns 4 acts.
#
# That is not the escalation this pipeline is for. Escalation is meant to mean
# "the local model could not produce usable JSON" -- a judgement about the
# MODEL -- not "we clipped its answer off". The tight budget was costing real
# money: every truncation bought a Haiku call, and on 09-09 two of them
# returned 0 acts each.
#
# It also explains S140's low-vs-medium result. Low effort was not answering
# BETTER; its reasoning is ~700-1,200 tokens instead of ~2,500, so it left room
# for the answer. 8/8 local on all three low runs, 7/8 at medium.
#
# The endpoint is local and free, and vllm_timeout is 900s against ~60 tok/s
# (8,000 tokens ≈ 133s worst case), so headroom here costs nothing. The PAID
# tier deliberately keeps the smaller budget: it has no hidden reasoning spend
# on this prompt, and a bigger number there would be billed.
LOCAL_EXTRACT_MAX_TOKENS = 8000
PAID_EXTRACT_MAX_TOKENS = 4000


def _extract_worker_count() -> int:
    """How many model calls may be in flight. 1 = what ollama can actually do."""
    raw = os.environ.get("HALFTIME_ROUTING_EXTRACT_WORKERS")
    try:
        want = int(raw) if raw not in (None, "") else DEFAULT_EXTRACT_WORKERS
    except (TypeError, ValueError):
        want = DEFAULT_EXTRACT_WORKERS
    return max(1, min(want, MAX_EXTRACT_WORKERS))


def _worker_count(n_tasks: int) -> int:
    """How many metros to sweep at once. Clamped, and never more than tasks."""
    raw = os.environ.get("HALFTIME_ROUTING_WORKERS")
    try:
        want = int(raw) if raw not in (None, "") else DEFAULT_ROUTING_WORKERS
    except (TypeError, ValueError):
        want = DEFAULT_ROUTING_WORKERS
    want = max(1, min(want, MAX_ROUTING_WORKERS))
    return max(1, min(want, n_tasks or 1))


def sweep_game(game: Dict, creds: Dict, searcher=None, fetcher=None,
               extractor=None, llm_stats: Optional[Dict] = None) -> Dict:
    """One game, every metro. Returns events + a coverage record per metro.

    The injectable searcher/fetcher/extractor exist so the selftest can drive
    the whole path offline — T32: a test must never reach the live web or a
    real config.

    `llm_stats` (S103) accumulates the local/escalated/unusable split across
    metros. An INJECTED extractor never touches it, which is correct: no model
    was called, so there is no rate to report — and a caller must be able to
    tell that apart from a real 0%.
    """
    # Import the network stack ONLY if a real one is actually needed. Importing
    # it unconditionally made the module unusable with injected dependencies —
    # which is to say, untestable offline on a box without `requests`. A
    # default should not be a requirement.
    if searcher is None or fetcher is None:
        import cirrus_daily
        # S90: name THIS job as the caller. Without it every Brave search the
        # routing sweep makes was billed to "daily_digest" in the usage report,
        # so the sweep's cost was invisible -- which mattered the moment Buddy
        # asked to run it DAILY instead of weekly, i.e. to multiply an unmeasured
        # spender by seven against a $25/mo cap.
        searcher = searcher or (
            lambda q: cirrus_daily.search_web(q, max_results=MAX_SEARCH_RESULTS,
                                              caller="halftime_routing"))
        fetcher = fetcher or (lambda u: cirrus_daily.fetch_article_content(u)[0])
    llm_stats = {} if llm_stats is None else llm_stats
    # Remembered before the default is built: an INJECTED extractor is used as
    # given and never gets a per-task stats dict, which preserves the documented
    # contract that injection reports no rate at all (see this function's
    # docstring) rather than a misleading 0%.
    _injected_extractor = extractor is not None
    extractor = extractor or (lambda block: _extract(block, creds, llm_stats))

    todo = list(queries_for(game))

    def _one(item):
        """One metro, start to finish. Returns everything the caller assembles.

        Returns its OWN stats dict and its OWN log lines rather than touching
        shared state: merging afterwards in list order keeps the result
        byte-identical to the serial version, which a lock would not (two
        threads incrementing llm_stats is a lost update, and interleaved log()
        calls scramble the run log).
        """
        metro, miles, query = item
        stats = {}
        ex = extractor if _injected_extractor else (
            lambda block: _extract(block, creds, stats))
        rec = {"metro": metro, "miles": miles, "query": query,
               "swept_at": _now(), "sources": 0, "found": 0, "error": None}
        try:
            urls = searcher(query)
        except Exception as e:
            rec["error"] = "search failed: {}".format(e)[:200]
            return rec, [], stats, []
        blocks = []
        for url in urls or []:
            try:
                content = fetcher(url)
            except Exception:
                continue
            if content:
                blocks.append("SOURCE: {}\n{}".format(
                    url, content[:MAX_FETCH_CHARS]))
        rec["sources"] = len(blocks)
        if not blocks:
            rec["error"] = "no fetchable source"
            return rec, [], stats, []
        with _extract_gate:          # the model is the serialised resource
            found = ex("\n\n".join(blocks))
        if found is None:
            rec["error"] = "extraction unusable"
            return rec, [], stats, []
        near = [dict(e, metro=metro, miles=miles,
                     gap=gap_days(e["date"], game["date"]))
                for e in found if near_game(e["date"], game["date"])]
        rec["found"] = len(near)
        return rec, near, stats, [
            "  {} — {} source(s), {} of {} show(s) inside the window".format(
                metro, len(blocks), len(near), len(found))]

    # One gate for the whole sweep, so the cap is across metros, not per task.
    _extract_gate = threading.BoundedSemaphore(_extract_worker_count())
    workers = _worker_count(len(todo))
    if workers > 1 and len(todo) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_one, todo))
    else:
        results = [_one(item) for item in todo]

    # Assemble in the ORIGINAL metro order, never completion order. This is what
    # makes the parallel path produce the same routing.json as the serial one.
    events, coverage = [], []
    for rec, near, stats, lines in results:
        coverage.append(rec)
        events.extend(near)
        for k, v in stats.items():
            llm_stats[k] = llm_stats.get(k, 0) + v
        for line in lines:
            log(line)
    return {"events": events, "coverage": coverage, "llm": llm_stats}


def _extract(block: str, creds: Dict, stats: Optional[Dict] = None):
    """Events, or None when nothing usable came back.

    S103: `stats` counts WHICH path answered. Until now this lane threw that
    away — it is the same local-first/escalate shape as halftime_catalogue, but
    only the catalogue's escalation rate reached the monitored note. So when
    `ollama_model` moved 72b -> qwen3.8:27b (2026-09-05) the acceptance test
    covered one of the two Justin lanes that changed, and this one produced no
    evidence about the model at all. The counter is the evidence.

    `unusable` is counted separately and deliberately NOT folded into the
    denominator: "the local model was fine" and "both models failed" must not
    render as the same 0%.
    """
    import llm_providers
    if stats is None:
        stats = {}
    user = "LISTINGS:\n\n{}".format(block[:24000])
    # S125 (CUMULUS2-TP2-PLAN.md): TP=2 vLLM endpoint first when configured;
    # any failure falls through to ollama and is counted as `vllm_fallback`,
    # separately from `escalated`, so a dead endpoint is seen, not paid for.
    if creds.get("vllm_url"):
        try:
            raw = llm_providers.call("vllm", _EXTRACT_SYSTEM, user, creds,
                                     max_tokens=LOCAL_EXTRACT_MAX_TOKENS, retries=0)
            got = parse_events(raw)
            if got is not None:
                stats["local"] = stats.get("local", 0) + 1
                stats["vllm"] = stats.get("vllm", 0) + 1
                return got
        except Exception:
            pass
        stats["vllm_fallback"] = stats.get("vllm_fallback", 0) + 1
    try:
        raw = llm_providers.call("ollama", _EXTRACT_SYSTEM, user, creds,
                                 max_tokens=LOCAL_EXTRACT_MAX_TOKENS, retries=0)
        got = parse_events(raw)
        if got is not None:
            stats["local"] = stats.get("local", 0) + 1
            return got
    except Exception:
        pass
    try:
        _provider, raw = llm_providers.escalate(
            _EXTRACT_SYSTEM, user, creds, max_tokens=PAID_EXTRACT_MAX_TOKENS, mode="single")
        got = parse_events(raw)
        if got is not None:
            stats["escalated"] = stats.get("escalated", 0) + 1
            return got
    except Exception:
        pass
    stats["unusable"] = stats.get("unusable", 0) + 1
    return None


LOCK_PATH = PROJECT_DIR / "logs" / "halftime_routing.lock"


class _Lock:
    """Refuse to run twice, whoever started it.

    There are now TWO launch paths — the runner (pid-file guarded) and the
    systemd timer (not) — so the guard has to live in the script rather than in
    one caller. Two concurrent sweeps would both write routing.json and the
    loser would silently clobber the winner.
    """

    def __init__(self, path=None):
        self.path = Path(path) if path else LOCK_PATH
        self.fh = None

    def __enter__(self):
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            return False
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return True

    def __exit__(self, *exc):
        if self.fh:
            try:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            finally:
                self.fh.close()
        return False


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temp file and rename. The dashboard reads this file; a
    reader must never catch it half-written and conclude the week was thin."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(str(tmp), str(path))


def note_for(res: Dict) -> str:
    """The monitored note. ONE definition, called by main() and the selftest.

    S103: the first cut of this had the selftest assert against its own copy of
    the format string. Removing the rate from the real note then passed the
    suite — the S102 `sm_prov` trap ("asserted a hardcoded copy of the thing it
    was checking") reproduced within a day of being written down. Caught by
    mutating the production line and watching the tests not care.
    """
    esc = res.get("escalated", 0)
    tot = esc + res.get("local", 0)
    rate = f"{100.0 * esc / tot:.0f}%" if tot else "n/a"
    bad = res.get("unusable", 0)
    vfb = res.get("vllm_fallback", 0)
    return (f"{res.get('events', 0)} event(s) across "
            f"{res.get('games_swept', 0)} game(s), "
            f"escalated {esc}/{tot} ({rate})"
            + (f", {bad} unusable" if bad else "")
            + (f", vllm fell back {vfb}x" if vfb else ""))


def run(games: Optional[List[Dict]] = None, only_targets: bool = False,
        creds: Optional[Dict] = None, out_path: Optional[Path] = None,
        searcher=None, fetcher=None) -> Dict:
    """S103: searcher/fetcher are injectable here for the same reason they
    already were on sweep_game — so the selftest can drive the WHOLE path
    offline. Without it the aggregation between sweep_game and this function
    was untested, and two mutations that silently zeroed the escalation rate
    for good passed the entire suite."""
    import halftime_dashboard
    games = games if games is not None else halftime_dashboard.HOME_GAMES
    creds = creds if creds is not None else json.loads(
        (PROJECT_DIR / "config/credentials.json").read_text())

    todo = [g for g in games
            if g.get("date") and g.get("at_venue", True)
            and (not only_targets or g.get("target"))]
    result = {"generated_at": _now(), "window_days": WINDOW_DAYS, "games": {}}
    # S103: one counter across every game, so the escalation rate is a per-RUN
    # figure like halftime_catalogue's. It is deliberately kept OUT of
    # routing.json — that file is the dashboard's input and its shape stays
    # exactly as it was.
    llm = {}
    for game in todo:
        gid = halftime_dashboard.game_id(game)
        log("game {} — {} vs {}".format(gid, game["date"], game["opponent"]))
        swept = sweep_game(game, creds, llm_stats=llm,
                           searcher=searcher, fetcher=fetcher)
        result["games"][gid] = {k: v for k, v in swept.items() if k != "llm"}
    out = Path(out_path) if out_path else OUT_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(out, json.dumps(result, indent=2))
    total = sum(len(v["events"]) for v in result["games"].values())
    log("done: {} game(s) swept, {} show(s) in window, extraction {}".format(
        len(todo), total, llm or "(none called)"))
    return {"games_swept": len(todo), "events": total, "out": str(out),
            "local": llm.get("local", 0),
            "escalated": llm.get("escalated", 0),
            "unusable": llm.get("unusable", 0),
            "vllm": llm.get("vllm", 0),
            "vllm_fallback": llm.get("vllm_fallback", 0)}


def _extract_src() -> str:
    """The module's CALL-SITE source, for the S141 budget checks below.

    Everything from `def selftest` onward is cut, and comment lines are
    dropped. Without both, the check counts its OWN text -- it names the
    constants it is looking for, and the constants' explanatory comment names
    the numbers. That is the third time a source-level check in this repo has
    matched its own explanation (S103); it is cheaper to cut the haystack than
    to remember."""
    try:
        src = Path(__file__).read_text()
    except Exception:
        return ""
    src = src.split("def selftest", 1)[0]
    return "\n".join(l for l in src.splitlines()
                      if not l.lstrip().startswith("#"))


def selftest() -> int:
    # S110: EVERY path in here reaches log(), which appends to the LIVE
    # logs/halftime-routing.log -- sweep_game() logs per metro, run() logs a
    # `done:` summary. That put fake "done: 2 game(s) swept ... {'escalated':
    # 14}" rows into the exact file the escalation trend is read from.
    # Redirect for the WHOLE function: a first attempt covered only the run()
    # block and still leaked 7 lines per invocation, which a before/after
    # byte-check on the live file caught.
    import shutil as _shutil
    import tempfile as _tempfile
    _real_log_path = LOG_PATH
    _tmp_log_dir = _tempfile.mkdtemp(prefix="halftime-routing-selftest-")
    globals()["LOG_PATH"] = Path(_tmp_log_dir) / "halftime-routing.log"
    try:
        return _selftest_body(_real_log_path)
    finally:
        globals()["LOG_PATH"] = _real_log_path
        _shutil.rmtree(_tmp_log_dir, ignore_errors=True)


def _selftest_body(_real_log_path) -> int:
    failures = []

    def check(label, ok):
        print(("  PASS  " if ok else "  FAIL  ") + label)
        if not ok:
            failures.append(label)

    # S141: the LOCAL model's thinking is billed against max_tokens before any
    # JSON is emitted, so the local tiers need more room than the paid one --
    # if they are ever re-unified, a truncated local reply silently becomes a
    # PAID call again. Measured: reasoning 2,500-2,700 tokens on the 09-09
    # sources, and max_tokens=3000 reproduced finish_reason=length +
    # parse_acts None on that exact block.
    check("the LOCAL extract budget is bigger than the PAID one",
          LOCAL_EXTRACT_MAX_TOKENS > PAID_EXTRACT_MAX_TOKENS)
    check("...with room for ~2,700 reasoning tokens AND the answer",
          LOCAL_EXTRACT_MAX_TOKENS >= 6000)
    _src = _extract_src()
    check("both local tiers use the local budget, the paid tier does not",
          _src.count("max_tokens=LOCAL_EXTRACT_MAX_TOKENS") == 2
          and _src.count("max_tokens=PAID_EXTRACT_MAX_TOKENS") == 1
          and "max_tokens=4000" not in _src)

    check("a clean array parses",
          parse_events('[{"artist":"A","date":"2026-11-01"}]')
          == [{"artist": "A", "date": "2026-11-01", "venue": "", "city": "",
               "style": ""}])

    # ── S141: style, Justin's scoring criterion #3 ──────────────────────────
    # The touring column carried it on 0 of 71 acts while the for-hire column
    # carried it on 29 of 33, so the 1 November card -- a military/patriotic
    # brief for a 45+ daytime crowd -- led with thrash and metalcore and said
    # nothing about it. A wrong style is worse than none here, so anything
    # outside the shared vocabulary is dropped rather than shown.
    check("style: a listing that states one carries it through",
          parse_events('[{"artist":"A","date":"2026-11-01","style":"country"}]'
                       )[0]["style"] == "country")
    check("style: a listing that states none leaves it EMPTY, never guessed",
          parse_events('[{"artist":"A","date":"2026-11-01"}]')[0]["style"] == "")
    check("style: common wordings map onto the catalogue's vocabulary, so the "
          "two columns read side by side",
          normalise_style("Hip-Hop") == "hip hop / rap"
          and normalise_style("metalcore") == "metal"
          and normalise_style("Symphonic") == "classical / orchestral")
    check("style: something outside the vocabulary is DROPPED, not shown",
          normalise_style("vaporwave") == "" and normalise_style("???") == "")
    check("style: the vocabulary matches the one the catalogue prompt offers",
          {"hip hop / rap", "classic rock", "country", "marching / military"}
          <= STYLE_VOCAB)
    check("JSON wrapped in prose still parses",
          parse_events('here you go [{"artist":"A","date":"2026-11-01"}] ok')
          is not None)
    check("an EMPTY array means 'none found', not 'broken'",
          parse_events("[]") == [])
    check("unusable output returns None so the caller ESCALATES",
          parse_events("I could not find anything") is None)
    check("empty output returns None, not an empty result",
          parse_events("") is None)
    check("a show with NO date is dropped, never guessed",
          parse_events('[{"artist":"A"}]') == [])
    check("a vague date is dropped",
          parse_events('[{"artist":"A","date":"November 2026"}]') == [])
    check("a show with no artist is dropped",
          parse_events('[{"date":"2026-11-01"}]') == [])

    check("a show on the day counts as near", near_game("2026-11-01", "2026-11-01"))
    check("three days before counts", near_game("2026-10-29", "2026-11-01"))
    check("three days after counts", near_game("2026-11-04", "2026-11-01"))
    check("four days out does NOT count", not near_game("2026-11-05", "2026-11-01"))
    check("a garbage date is not near anything",
          not near_game("soon", "2026-11-01"))
    check("the gap is signed, so before and after are distinguishable",
          gap_days("2026-10-29", "2026-11-01") == -3
          and gap_days("2026-11-04", "2026-11-01") == 3)

    # --- draw signal ------------------------------------------------------
    check("an arena is plausible for a stadium slot",
          draw_signal({"venue": "PPG Paints Arena"})["plausible"] is True)
    check("a stadium is plausible",
          draw_signal({"venue": "Acrisure Stadium"})["tier"] == "stadium")
    check("an amphitheatre is plausible",
          draw_signal({"venue": "Blossom Music Center"})["plausible"] is True)
    check("a 300-capacity club is NOT a halftime draw",
          draw_signal({"venue": "Rumba Cafe"})["plausible"] is False)
    check("a theatre is below stadium draw",
          draw_signal({"venue": "Newport Music Hall"})["plausible"] is False)
    check("...but a club act is still LISTED, not dropped",
          "genuinely in the area" in draw_signal({"venue": "Rumba Cafe"})["why"])
    check("an unrecognised room is UNSIZED, never judged small",
          draw_signal({"venue": "Some New Room"})["plausible"] is None)
    check("...and says so, so the list failing behind reality is visible",
          "do not recognise" in draw_signal({"venue": "Some New Room"})["why"])
    check("a missing venue is unknown, not club",
          draw_signal({"venue": ""})["tier"] == "unknown")

    import tempfile as _tmpf
    with _tmpf.TemporaryDirectory() as _td:
        lp = Path(_td) / "x.lock"
        a, b = _Lock(lp), _Lock(lp)
        check("the first sweep takes the lock", a.__enter__() is True)
        check("a SECOND sweep is refused, whoever launched it",
              b.__enter__() is False)
        a.__exit__()
        check("the lock is released when the sweep finishes",
              _Lock(lp).__enter__() is True)
        op = Path(_td) / "out.json"
        _write_atomic(op, '{"a":1}')
        check("an atomic write lands the whole file",
              json.loads(op.read_text()) == {"a": 1})
        check("...and leaves no temp file behind",
              not (Path(_td) / "out.json.tmp").exists())

    check("spacing does not decide whether we recognise a room",
          venue_tier("E J Thomas Hall") == venue_tier("EJ Thomas Hall")
          == "theatre")
    check("punctuation does not either",
          venue_tier("Skully\'s Music Diner") == "club")
    check("a theatre says the room sizes the SHOW, not the artist",
          "not the artist" in draw_signal({"venue": "E J Thomas Hall"})["why"])
    check("a club does not carry that caveat — it is small either way",
          "not the artist" not in draw_signal({"venue": "Rumba Cafe"})["why"])

    credited = {canonical_key("Bret Michaels"), canonical_key("Styx")}
    both = draw_signal({"artist": "Bret Michaels", "venue": "Rumba Cafe"},
                       credited)
    check("a halftime credit BEATS the room it happens to be playing",
          both["tier"] == "credited" and both["plausible"] is True)
    check("...and is named as the intersection of both pools",
          "Both pools at once" in both["why"])
    check("'The Band' and 'Band' match for cross-referencing",
          canonical_key("The Styx") == canonical_key("Styx"))
    check("a parenthetical does not break the cross-reference",
          canonical_key("Styx (live)") == canonical_key("Styx"))
    check("an uncredited act is not falsely credited",
          draw_signal({"artist": "Dent May", "venue": "Rumba Cafe"},
                      credited)["tier"] == "club")
    check("the ordering puts credited acts above every room tier",
          DRAW_ORDER["credited"] < min(
              DRAW_ORDER[t] for t in DRAW_ORDER if t != "credited"))
    check("unknown outranks the tiers we know are too small",
          DRAW_ORDER["unknown"] < DRAW_ORDER["theatre"] < DRAW_ORDER["club"])

    game = {"date": "2026-11-01", "opponent": "Cleveland Browns", "week": 8}
    qs = queries_for(game)
    check("every metro in the radius gets its own search",
          len(qs) == len(METROS))
    check("the search names the month of the game",
          all("November 2026" in q for _m, _mi, q in qs))
    check("a game with no date yields no queries, rather than a bad one",
          queries_for({"date": None}) == [])

    # Whole path, offline (T32: never touches the live web or a real config).
    fake = [{"artist": "In Window", "date": "2026-10-31", "venue": "V",
             "city": "Pittsburgh, PA"},
            {"artist": "Far Away", "date": "2026-12-25", "venue": "V",
             "city": "Pittsburgh, PA"}]
    res = sweep_game(game, {},
                     searcher=lambda q: ["http://x"],
                     fetcher=lambda u: "listing text",
                     extractor=lambda b: fake)
    check("only shows inside the window are kept",
          [e["artist"] for e in res["events"]] == ["In Window"] * len(METROS))
    check("every metro produces a coverage row, hit or miss",
          len(res["coverage"]) == len(METROS))
    check("coverage records the distance, so 'near' is checkable",
          all("miles" in c for c in res["coverage"]))

    broke = sweep_game(game, {}, searcher=lambda q: ["http://x"],
                       fetcher=lambda u: "text", extractor=lambda b: None)
    check("an unusable extraction is recorded as an ERROR, not as zero shows",
          all(c["error"] == "extraction unusable" for c in broke["coverage"])
          and broke["events"] == [])
    dead = sweep_game(game, {},
                      searcher=lambda q: (_ for _ in ()).throw(RuntimeError("boom")),
                      fetcher=lambda u: "", extractor=lambda b: [])
    check("a failed search is recorded as an error, not as 'nothing on'",
          all(c["error"] and "search failed" in c["error"]
              for c in dead["coverage"]))

    # ── S119: the metro sweep is CONCURRENT. Two things must hold, and the
    # second is the one a naive test would miss.
    import os as _os
    import threading as _threading
    import time as _time

    def _sweep_with(workers, searcher=None):
        """Run the same game at a given worker count. Restores the env after."""
        prev = _os.environ.get("HALFTIME_ROUTING_WORKERS")
        _os.environ["HALFTIME_ROUTING_WORKERS"] = str(workers)
        try:
            return sweep_game(
                game, {},
                searcher=searcher or (lambda q: ["http://x"]),
                fetcher=lambda u: "listing text",
                extractor=lambda b: fake)
        finally:
            if prev is None:
                _os.environ.pop("HALFTIME_ROUTING_WORKERS", None)
            else:
                _os.environ["HALFTIME_ROUTING_WORKERS"] = prev

    serial = _sweep_with(1)
    parallel = _sweep_with(7)
    # THE property. routing.json feeds Justin's dashboard, so concurrency is
    # only acceptable if the artefact is unchanged -- including ORDER, which is
    # why results are assembled by list position and never by completion.
    check("parallel sweep produces byte-identical output to the serial sweep",
          json.dumps(serial, sort_keys=True) == json.dumps(parallel, sort_keys=True))
    check("...and the metros come back in METRO order, not completion order",
          [c["metro"] for c in parallel["coverage"]] == [m for m, _ in METROS])

    # ...and it is ACTUALLY parallel. Without this, a bug that silently ran the
    # tasks one at a time would pass every check above -- the output would be
    # identical because it always is. That is T8: a check that cannot fail.
    inflight = {"now": 0, "max": 0}
    guard = _threading.Lock()

    def _slow_searcher(q):
        with guard:
            inflight["now"] += 1
            inflight["max"] = max(inflight["max"], inflight["now"])
        _time.sleep(0.05)
        with guard:
            inflight["now"] -= 1
        return ["http://x"]

    inflight["max"] = 0
    _sweep_with(7, searcher=_slow_searcher)
    check("the parallel path really does overlap metros (max in-flight > 1)",
          inflight["max"] > 1)
    inflight["max"] = 0
    _sweep_with(1, searcher=_slow_searcher)
    check("...and WORKERS=1 is genuinely serial — the documented kill switch",
          inflight["max"] == 1)

    # ── S119: the MODEL calls must stay throttled even while search fans out.
    # A live A/B measured escalation going 2/7 -> 6/7 when everything ran
    # 7-wide: ollama does not batch, so concurrent extractions queue past the
    # 120s timeout and a timeout escalates to a PAID call. Speed that triples
    # the API bill is not speed. These assert the two halves behave differently.
    ex_inflight = {"now": 0, "max": 0}
    se_inflight = {"now": 0, "max": 0}
    exguard = _threading.Lock()

    def _tracked(counter):
        def _fn(*_a, **_k):
            with exguard:
                counter["now"] += 1
                counter["max"] = max(counter["max"], counter["now"])
            _time.sleep(0.05)
            with exguard:
                counter["now"] -= 1
            return ["http://x"] if counter is se_inflight else fake
        return _fn

    prev_ex = _os.environ.get("HALFTIME_ROUTING_EXTRACT_WORKERS")
    _os.environ.pop("HALFTIME_ROUTING_EXTRACT_WORKERS", None)
    prev_w = _os.environ.get("HALFTIME_ROUTING_WORKERS")
    _os.environ["HALFTIME_ROUTING_WORKERS"] = "7"
    try:
        sweep_game(game, {}, searcher=_tracked(se_inflight),
                   fetcher=lambda u: "listing text",
                   extractor=_tracked(ex_inflight))
        check("search still fans out across metros (max in-flight > 1)",
              se_inflight["max"] > 1)
        check("but MODEL calls are throttled to one — ollama cannot batch, and "
              "queued calls time out into paid escalations",
              ex_inflight["max"] == 1)
        _os.environ["HALFTIME_ROUTING_EXTRACT_WORKERS"] = "4"
        ex_inflight["max"] = 0
        sweep_game(game, {}, searcher=lambda q: ["http://x"],
                   fetcher=lambda u: "listing text",
                   extractor=_tracked(ex_inflight))
        check("...and the cap lifts when told to, for an engine that CAN batch",
              ex_inflight["max"] > 1)
    finally:
        for k, v in (("HALFTIME_ROUTING_EXTRACT_WORKERS", prev_ex),
                     ("HALFTIME_ROUTING_WORKERS", prev_w)):
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v

    check("_worker_count clamps to the task count, never oversubscribes",
          _worker_count(3) <= 3 and _worker_count(0) == 1)
    check("_worker_count survives a garbage env value instead of crashing a run",
          (lambda: (_os.environ.__setitem__("HALFTIME_ROUTING_WORKERS", "banana"),
                    _worker_count(7) == DEFAULT_ROUTING_WORKERS,
                    _os.environ.pop("HALFTIME_ROUTING_WORKERS", None))[1])())

    # ── S103: the escalation counter ──────────────────────────────────────
    # Every assertion below is paired with its inverse. S102 shipped seven
    # things that reported a success they had not earned, two of them tests;
    # the ones that were caught were caught by asserting the negative case
    # beside the positive one. A counter is exactly the kind of code that
    # passes by never being incremented.
    import types

    def _fake_llm(local_raw=None, escalate_raw=None):
        """A stand-in llm_providers. `None` raw = that provider blows up."""
        m = types.ModuleType("llm_providers")

        def call(_provider, _sys, _user, _creds, **_kw):
            if local_raw is None:
                raise RuntimeError("no local model")
            return local_raw

        def escalate(_sys, _user, _creds, **_kw):
            if escalate_raw is None:
                raise RuntimeError("no cloud provider")
            return ("anthropic", escalate_raw)
        m.call, m.escalate = call, escalate
        return m

    _good = '[{"artist": "A", "date": "2026-11-01", "venue": "V", "city": "C"}]'
    _real = sys.modules.get("llm_providers")
    try:
        sys.modules["llm_providers"] = _fake_llm(local_raw=_good)
        st = {}
        _extract("block", {}, st)
        check("a local answer counts as LOCAL",
              st.get("local") == 1)
        check("...and NOT as escalated — the inverse, which is the whole point",
              st.get("escalated", 0) == 0 and st.get("unusable", 0) == 0)

        sys.modules["llm_providers"] = _fake_llm(local_raw="not json at all",
                                                 escalate_raw=_good)
        st = {}
        got = _extract("block", {}, st)
        check("an unusable LOCAL answer escalates, and is counted as escalated",
              st.get("escalated") == 1 and st.get("local", 0) == 0)
        check("...and the escalated events still reach the caller",
              got and got[0]["artist"] == "A")

        # ── S125: the vLLM-first path, three shapes ──────────────────────
        def _fake_vllm(vllm_raw, ollama_raw):
            m = types.ModuleType("llm_providers")

            def call(provider, _s, _u, _c, **_kw):
                raw = vllm_raw if provider == "vllm" else ollama_raw
                if raw is None:
                    raise RuntimeError("%s down" % provider)
                return raw

            def escalate(_s, _u, _c, **_kw):
                raise RuntimeError("no cloud provider")
            m.call, m.escalate = call, escalate
            return m

        sys.modules["llm_providers"] = _fake_vllm(_good, "not json")
        st = {}
        _extract("block", {"vllm_url": "http://v"}, st)
        check("vllm answers first when vllm_url is set — counted local + vllm",
              st.get("vllm") == 1 and st.get("local") == 1
              and st.get("vllm_fallback", 0) == 0)

        sys.modules["llm_providers"] = _fake_vllm(None, _good)
        st = {}
        got = _extract("block", {"vllm_url": "http://v"}, st)
        check("a DEAD vllm falls back to ollama, counted as vllm_fallback, "
              "NOT as escalated (the S92-with-a-bill inverse)",
              st.get("vllm_fallback") == 1 and st.get("local") == 1
              and st.get("escalated", 0) == 0 and got[0]["artist"] == "A")

        sys.modules["llm_providers"] = _fake_vllm(_good, None)
        st = {}
        _extract("block", {}, st)
        check("without vllm_url the vllm provider is never called at all",
              st.get("vllm", 0) == 0 and st.get("vllm_fallback", 0) == 0
              and st.get("local", 0) == 0)
        check("note_for prints the fallback count only when it is non-zero",
              "vllm fell back 2x" in note_for({"events": 1, "games_swept": 1,
                                               "vllm_fallback": 2})
              and "vllm" not in note_for({"events": 1, "games_swept": 1}))

        sys.modules["llm_providers"] = _fake_llm()          # both blow up
        st = {}
        check("both providers failing returns None, not []",
              _extract("block", {}, st) is None)
        check("a total failure is UNUSABLE — never a silent 'local answered'",
              st.get("unusable") == 1
              and st.get("local", 0) == 0 and st.get("escalated", 0) == 0)

        # The note is what a monitor actually reads, so assert the STRING --
        # from note_for(), the function main() calls. Never from a copy of it.
        check("the note carries the rate",
              "escalated 2/8 (25%)" in note_for({"escalated": 2, "local": 6}))
        check("a zero denominator says n/a — it never divides, and never "
              "renders as a flattering 0%",
              "escalated 0/0 (n/a)" in note_for({}))
        check("unusable extractions are named in the note, not folded into 0%",
              "3 unusable" in note_for({"local": 1, "unusable": 3})
              and "unusable" not in note_for({"local": 1}))

        # ...and that main()'s recording path actually USES it. Without this,
        # main() could stop calling note_for() and every test above still
        # passes -- verified by mutating exactly that and watching it slip.
        seen = []
        _js = types.ModuleType("job_status")
        _js.record = lambda *a: seen.append(a)
        _real_js = sys.modules.get("job_status")
        sys.modules["job_status"] = _js
        try:
            note = record_run({"events": 4, "games_swept": 2,
                               "escalated": 1, "local": 3})
            check("what gets RECORDED is the note, not something re-derived",
                  seen and seen[0] == ("halftimerouting", True, note)
                  and "escalated 1/4 (25%)" in seen[0][2])
            _js.record = lambda *a: 1 / 0
            check("a ledger that blows up cannot take the job down with it",
                  record_run({}).startswith("0 event"))
        finally:
            if _real_js is not None:
                sys.modules["job_status"] = _real_js
            else:
                sys.modules.pop("job_status", None)

        # WHOLE PATH, offline: extraction -> sweep_game -> run -> note.
        # The counter being right is not the same as the counter SURVIVING the
        # trip to the note; two mutations that zeroed it permanently passed
        # every test above until this one existed.
        import tempfile
        sys.modules["llm_providers"] = _fake_llm(local_raw="junk",
                                                 escalate_raw=_good)
        # S110: redirect LOG_PATH too. run() calls log(), which appends to the
        # LIVE logs/halftime-routing.log -- so this test was writing fake
        # "done: 2 game(s) swept ... {'escalated': 14}" rows into the same file
        # the escalation trend is read from. Found by running the scheduled
        # check early: its `tail` of done-lines showed three selftest runs and
        # no real one. T32 in its interprocedural form -- the AST lint only
        # sees writes made DIRECTLY in the selftest, not ones reached through
        # two calls.
        with tempfile.TemporaryDirectory() as td:
            out = run(games=[{"date": "2026-11-01", "opponent": "Team A",
                              "week": 8, "at_venue": True},
                             {"date": "2026-11-08", "opponent": "Team B",
                              "week": 9, "at_venue": True}],
                      creds={}, out_path=Path(td) / "routing.json",
                      searcher=lambda q: ["http://x"],
                      fetcher=lambda u: "listing text")
            per_run = 2 * len(METROS)
            check("run() carries the escalation count out of every game",
                  out["escalated"] == per_run and out["local"] == 0)
            check("...as ONE per-run figure, not one game's worth",
                  note_for(out).endswith(f"escalated {per_run}/{per_run} (100%)"))
            check("routing.json keeps the shape the dashboard reads — no "
                  "counter leaks into the client artefact",
                  all("llm" not in g for g in
                      json.loads((Path(td) / "routing.json").read_text())
                      ["games"].values()))
            check("the WHOLE selftest logs to a temp path, never to the live "
                  "file the escalation trend is read from",
                  LOG_PATH != _real_log_path
                  and not str(LOG_PATH).startswith(str(PROJECT_DIR)))
    finally:
        if _real is not None:
            sys.modules["llm_providers"] = _real
        else:
            sys.modules.pop("llm_providers", None)

    # An INJECTED extractor must leave the counter untouched: a test harness
    # must never be able to look like a real 0% escalation run.
    check("an injected extractor records no rate at all (n/a, not 0%)",
          res.get("llm") == {})

    print()
    if failures:
        print("FAILURES: {}".format(len(failures)))
        return 1
    print("ALL PASS")
    return 0


def note_samples():
    """Every note shape this job writes. Built by CALLING note_for (S150).

    note_for already existed and is already the one definition -- S103 made it a
    function precisely so the selftest could not assert against its own copy.
    This adds the shapes the LIVE LEDGER never shows: a quiet announcement week,
    and a sweep with no games at all.
    """
    return [
        ("a busy announcement week",
         note_for({"events": 82, "games_swept": 7, "escalated": 6, "local": 41,
                   "unusable": 2, "vllm_fallback": 8}), "productive"),
        # Documented in completeness' selftest as PRODUCTIVE on purpose: sweeping
        # 7 games and finding no announcements IS the work.
        ("a quiet announcement week",
         note_for({"events": 0, "games_swept": 7, "escalated": 0, "local": 7}),
         "productive"),
        # ...and this is the zero case: no games to sweep at all, which means
        # HOME_GAMES is empty -- a config fault, not a quiet week.
        ("no games swept at all",
         note_for({"events": 0, "games_swept": 0, "escalated": 0, "local": 0}),
         "zero"),
    ]


def record_run(res: Dict) -> str:
    """Write the run into the job_status ledger. Returns the note it recorded.

    S81: this job used to run unwatched, so an overdue or failed sweep was
    invisible. Best-effort and never allowed to change the exit status --
    monitoring must not break the thing it monitors.

    S103: it is a function so the selftest can assert what main() ACTUALLY
    records. Testing note_for() alone left a live gap -- main() could stop
    calling it and every test still passed.

    The call below is written out literally, NOT through an injected recorder,
    because job_status.py's own placement check greps for exactly this shape to
    prove every watched job writes its own row. An indirection here reads to
    that guard as "halftimerouting records nothing" -- which is how the first
    cut of this function was caught. The test stubs the MODULE instead.
    """
    note = note_for(res)
    try:
        import job_status
        job_status.record("halftimerouting", True, note)
    except Exception as e:
        print(f"job_status.record failed: {e}")
    return note


def main() -> int:
    args = sys.argv[1:]
    if "selftest" in args:
        return selftest()
    lock = _Lock()
    if not lock.__enter__():
        log("another sweep is already running — not starting a second one.")
        return 0
    try:
        res = run(only_targets="--targets" in args)
    finally:
        lock.__exit__()
    print(json.dumps(res))
    record_run(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
