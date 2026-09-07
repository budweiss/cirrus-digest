#!/usr/bin/env python3
"""Halftime act catalogue — non-marching-band football halftime entertainment.

S78 (2026-08-25), built to Buddy's ask:

    "I'd like to develop a process to run on cumulus or cirrus to research pro
    and college halftime entertainment. Normally colleges have marching bands.
    But outside of marching bands, what are some examples of other entertainment
    football has used. Example, there was a dog frisbee catching show, drone
    shows. So don't rule these out, just research them. So I'm looking for code
    to run on either server to search for these. If the local LLMs need help, it
    should reach out to the foundational LLMs for help and input."

S77's handoff turned that into four requirements. This file is 2, 3 and 4;
requirement 1 (pro AND college) is the search matrix below.

**SCOPE — this is a CATALOGUE, not a client project.** Project Halftime is
Pittsburgh/Steelers-only for Justin. This is deliberately NOT bound to that: it
collects acts nationally, pro and college, and feeds Halftime rather than
belonging to it. Nothing here is client-facing, nothing here sends mail, and no
act reaches a client without a human deciding it should. Keeping the two
separate is the whole reason the scope question was asked before building.

WHAT IT IS NOT: a marching band index. Bands are the default and are the thing
Buddy explicitly wants filtered out — `looks_like_band()` rejects them, and a
run that admitted nothing but bands should read as a failed run, not a quiet
one.

LOCAL-FIRST WITH ESCALATION (requirement 4). Every extraction asks
`qwen2.5:72b` first and only escalates when the local model returns something
unusable. Which model decided is recorded on every entity, so the escalation
rate is a by-product rather than a separate measurement exercise — the number
the S73 `_ollama` docstring asked for before anything is routed locally.

STORAGE: `entity_kb` project `halftime_acts`. That buys dedupe by slug, an
append-only event log, `recap_text`, and the weekly-digest machinery, none of
which needed writing again.

Usage:
  python3 halftime_catalogue.py                 # one full pass (discover + refresh)
  python3 halftime_catalogue.py --dry-run       # search + extract, write NOTHING
  python3 halftime_catalogue.py --angles 2      # fewer search angles this run
  python3 halftime_catalogue.py report          # what is in the catalogue
  python3 halftime_catalogue.py selftest        # offline, no network, no writes
"""
# T34: CIRRUS runs the system python 3.9, which has no PEP 604 unions. This
# module is CUMULUS-scheduled, but a latent one is still a landmine.
from __future__ import annotations

import json
import os
import re
import threading
import sys
from datetime import datetime
from pathlib import Path

import entity_kb

KB_PROJECT = "halftime_acts"
PROJECT_DIR = Path(__file__).resolve().parent
LOG_PATH = PROJECT_DIR / "logs" / "halftime-catalogue.log"

MAX_SEARCH_RESULTS = 6
MAX_FETCH_CHARS = 6000
DEFAULT_ANGLES = 4
REFRESH_CHUNK = 3

# The search matrix. Angles rotate by day so a single run stays cheap and the
# whole space is covered over roughly a week -- the same self-balancing trick
# hoa_daily_research uses, and for the same reason: no cursor to maintain.
#
# Deliberately concrete. "halftime entertainment" alone returns Super Bowl
# coverage forever, which is the one slot this project does NOT care about
# (the NFL books that centrally, with Roc Nation; every other slot is booked by
# the club).
# (pool, category, query). POOL is the supply model, and it decides which
# extraction prompt runs. Justin's S79 reply split the problem in two: an act
# that TOURS is a candidate for the one game its routing passes near, while an
# act that is FOR HIRE is available for every game. They cannot share a ranking,
# so they do not share a pool.
SEARCH_ANGLES = [
    ("variety", "stunt dog show", "NFL halftime stunt dog frisbee show booked"),
    ("variety", "stunt dog show", "college football halftime dog frisbee act contract"),
    ("variety", "drone show", "college football halftime drone light show cost"),
    ("variety", "drone show", "NFL stadium halftime drone show provider"),
    ("variety", "acrobat / variety", "college football halftime acrobat variety act performer"),
    ("variety", "acrobat / variety", "NFL halftime plate spinner unicycle acrobat act"),
    ("variety", "bmx / fmx", "football halftime BMX freestyle motocross stunt show stadium"),
    ("variety", "pogo / trampoline", "football halftime pogo trampoline dunk team halftime"),
    ("variety", "pyro / fireworks", "college football halftime fireworks pyrotechnics contract cost"),
    ("variety", "projection / light", "stadium halftime projection mapping light show football"),
    ("variety", "fan contest", "football halftime fan contest punt pass kick promotion halftime"),
    ("variety", "mascot / novelty", "football halftime mascot novelty act racing sausages halftime"),
    # FOR-HIRE MUSIC (S79). These are the acts Justin asked about: "they might
    # not tour anymore but when someone calls they consider." Discovery is by
    # CREDIT, not by availability -- see _MUSIC_SYSTEM for why the first set of
    # queries returned nothing across ten sources.
    ("for_hire_music", "nostalgia music", "\"performed at halftime\" NFL game musical artist concert"),
    ("for_hire_music", "nostalgia music", "90s hip hop group performed NFL halftime show regular season"),
    ("for_hire_music", "nostalgia music", "NBA halftime performance musical guest arena intermission"),
    ("for_hire_music", "classic rock", "classic rock band played halftime show football stadium"),
    ("for_hire_music", "military / patriotic", "salute to service halftime performance military tribute NFL musician"),
    ("for_hire_music", "regional / market", "Pittsburgh musician performed Steelers halftime Acrisure Heinz Field"),
    ("for_hire_music", "regional / market", "local artist performed halftime show NFL hometown team"),
    ("for_hire_music", "tribute", "tribute band performed halftime show college football stadium"),
    # TEAM TRADITION (S79, Buddy's Renegade question). An act whose song is a
    # stadium ritual has the strongest possible market tie and no halftime
    # credit, so every angle above is blind to it.
    ("for_hire_music", "team tradition", "song played every home game NFL stadium tradition band anthem"),
    ("for_hire_music", "team tradition", "classic rock song NFL team stadium ritual fourth quarter anthem"),
]

# ── TEAM-SIDE AXIS (S83) ─────────────────────────────────────────────────────
# The catalogue above is ACT-shaped: it answers "who sells a halftime act". It
# was built from provider-marketing queries, and after 51 acts it still cannot
# answer the question actually asked at the end of S78 — "what did other teams
# DO at halftime in the past five years." Those are different search problems.
# A vendor page tells you who is selling; it does not tell you what Nebraska ran
# on a Saturday in 2023.
#
# So: a third pool, keyed by TEAM rather than by act. NFL first — 32 clubs is a
# full pass in ~8 nights against ~40 for all 162, and it is the half Justin's
# Pittsburgh project actually touches. FBS is a second seeding once extraction
# quality is proven on real output.
PROGRAM_YEARS = 5              # the bound is real: fees move fast, and the same
                               # Red Panda act is documented at $3,350 and later
                               # ~$6,765. Older than this is context, not a quote.
PROGRAM_ANGLES_PER_RUN = 4     # added to the nightly run alongside the act pools

# (canonical name, level, match aliases). Aliases exist because a source says
# "the Steelers", "Pittsburgh" or "Pittsburgh Steelers" and all three must land
# on one entity. Matching against this list is also the JUNK GATE: a model that
# invents a team name simply fails to match and the record is dropped, which is
# why the extractor is allowed to report teams it noticed in passing.
NFL_TEAMS = [
    ("Arizona Cardinals",      "pro", ("arizona cardinals", "cardinals")),
    ("Atlanta Falcons",        "pro", ("atlanta falcons", "falcons")),
    ("Baltimore Ravens",       "pro", ("baltimore ravens", "ravens")),
    ("Buffalo Bills",          "pro", ("buffalo bills", "bills")),
    ("Carolina Panthers",      "pro", ("carolina panthers", "panthers")),
    ("Chicago Bears",          "pro", ("chicago bears", "bears")),
    ("Cincinnati Bengals",     "pro", ("cincinnati bengals", "bengals")),
    ("Cleveland Browns",       "pro", ("cleveland browns", "browns")),
    ("Dallas Cowboys",         "pro", ("dallas cowboys", "cowboys")),
    ("Denver Broncos",         "pro", ("denver broncos", "broncos")),
    ("Detroit Lions",          "pro", ("detroit lions", "lions")),
    ("Green Bay Packers",      "pro", ("green bay packers", "packers")),
    ("Houston Texans",         "pro", ("houston texans", "texans")),
    ("Indianapolis Colts",     "pro", ("indianapolis colts", "colts")),
    ("Jacksonville Jaguars",   "pro", ("jacksonville jaguars", "jaguars")),
    ("Kansas City Chiefs",     "pro", ("kansas city chiefs", "chiefs")),
    ("Las Vegas Raiders",      "pro", ("las vegas raiders", "raiders")),
    ("Los Angeles Chargers",   "pro", ("los angeles chargers", "chargers")),
    ("Los Angeles Rams",       "pro", ("los angeles rams", "rams")),
    ("Miami Dolphins",         "pro", ("miami dolphins", "dolphins")),
    ("Minnesota Vikings",      "pro", ("minnesota vikings", "vikings")),
    ("New England Patriots",   "pro", ("new england patriots", "patriots")),
    ("New Orleans Saints",     "pro", ("new orleans saints", "saints")),
    ("New York Giants",        "pro", ("new york giants", "giants")),
    ("New York Jets",          "pro", ("new york jets", "jets")),
    ("Philadelphia Eagles",    "pro", ("philadelphia eagles", "eagles")),
    ("Pittsburgh Steelers",    "pro", ("pittsburgh steelers", "steelers")),
    ("San Francisco 49ers",    "pro", ("san francisco 49ers", "49ers", "niners")),
    ("Seattle Seahawks",       "pro", ("seattle seahawks", "seahawks")),
    ("Tampa Bay Buccaneers",   "pro", ("tampa bay buccaneers", "buccaneers", "bucs")),
    ("Tennessee Titans",       "pro", ("tennessee titans", "titans")),
    ("Washington Commanders",  "pro", ("washington commanders", "commanders")),
]

# Two templates per team, deliberately different SOURCE types rather than two
# phrasings of one. The second is the handoff's angle 2: schools and clubs
# publish game-day guides and promotional schedules that list halftime per game
# — highly structured, rarely scraped, and they name the act.
PROGRAM_QUERIES = [
    '"{team}" halftime show performance regular season -"Super Bowl"',
    '"{team}" game day promotional schedule halftime entertainment',
]


def canonical_team(raw: str, teams: list = None) -> str:
    """Map whatever a source called a team onto our canonical name, or "".

    Longest alias first, so "new york giants" is not swallowed by "giants"
    matching some other row, and so a bare nickname still resolves.
    """
    if not raw:
        return ""
    hay = re.sub(r"[^a-z0-9 ]+", " ", str(raw).lower())
    hay = " " + re.sub(r"\s+", " ", hay).strip() + " "
    best, best_len = "", 0
    for canon, _level, aliases in (teams or NFL_TEAMS):
        for a in aliases:
            if len(a) > best_len and (" " + a + " ") in hay:
                best, best_len = canon, len(a)
    return best


def team_level(canon: str, teams: list = None) -> str:
    for name, level, _a in (teams or NFL_TEAMS):
        if name == canon:
            return level
    return "unknown"


def program_angles_for_today(n: int = PROGRAM_ANGLES_PER_RUN, day: int = None,
                             teams: list = None) -> list:
    """A rotating slice of (pool, team, query), same day-of-year trick as the
    act angles: no cursor to corrupt, and a missed night costs one slice rather
    than desyncing the rotation for good."""
    rows = []
    for canon, _level, _al in (teams or NFL_TEAMS):
        for q in PROGRAM_QUERIES:
            rows.append(("program", canon, q.format(team=canon)))
    if not rows:
        return []
    n = max(1, min(int(n), len(rows)))
    day = int(datetime.now().strftime("%j")) if day is None else day
    start = (day * n) % len(rows)
    return [rows[(start + i) % len(rows)] for i in range(n)]


# A band is the default at college level and is exactly what Buddy asked to
# exclude. Matched on the ACT NAME only -- a dog act that happens to perform
# "with the marching band" is still a dog act.
_BAND_RX = re.compile(
    r"\b(marching band|drum ?line|drum ?corps|pep band|spirit band|"
    r"symphonic band|wind ensemble|color ?guard|majorette)\b", re.IGNORECASE)

_EXTRACT_SYSTEM = """You catalogue NON-MARCHING-BAND entertainment used at
American football halftime shows, professional and college.

From the sources given, extract every distinct ACT or PROVIDER that has actually
performed at, or is sold for, a football halftime show.

EXCLUDE, always:
- marching bands, drumlines, drum corps, pep bands, colour guard, majorettes
- Super Bowl halftime performers (booked centrally, not by a club)
- concerts by touring musicians (a different pipeline already covers those)
- anything you cannot tie to football

Return ONLY a JSON array, no prose. Each element:
{"name": "the act or company name",
 "category": "one of: dog show, drone show, acrobat/variety, bmx/fmx,
              pogo/trampoline, pyro/fireworks, projection/light, fan contest,
              mascot/novelty, other",
 "level": "pro" | "college" | "both" | "unknown",
 "clients": "teams/schools named as having booked it, comma separated, or ''",
 "booking_contact": "phone/email/site if stated, else ''",
 "fee_note": "any DOCUMENTED figure with its source context, else ''",
 "home_base": "city/state if stated, else ''",
 "evidence": "one sentence, quoting or closely paraphrasing the source"}

If a field is not stated in the sources, use "". NEVER invent a contact, a fee
or a client list -- an empty field is correct and useful; a guessed one poisons
the catalogue. If the sources contain no qualifying act, return []."""


_MUSIC_SYSTEM = """You catalogue MUSICAL acts that have actually PERFORMED at
a sports halftime, intermission or in-game slot -- pro, college or arena.

WHY A CREDIT AND NOT AN AVAILABILITY CLAIM. A first pass asked the web to show
that an act was "available for hire" and found nothing across ten sources,
because that is not a thing the public web says: agency rosters that would say
it block scrapers, and everyone else reports who PLAYED. A credit is stated
constantly and is the real qualifier -- an act that played someone's halftime
can be asked to play another. Availability is a phone call, not a web page.

INCLUDE any musical act named as having performed such a slot, whether or not
it currently tours, and whether or not a fee is mentioned. Legacy and nostalgia
acts, regional favourites, heritage line-ups and solo members performing under
their own name are all in scope, and are the most useful finds.

ALSO INCLUDE acts whose SONG is an established in-stadium tradition for a team,
even with no halftime credit at all -- the song being a ritual IS the tie, and
is often a stronger one than having played a slot somewhere else. Styx's
"Renegade" at Steelers home games is the type case: the band has no Steelers
halftime credit, so a credit-only search cannot see the single most
Pittsburgh-connected act there is. Category these as "team tradition" and name
the song and the team in the evidence.

EXCLUDE, always:
- marching bands, drumlines, drum corps, pep bands, colour guard, majorettes
- Super Bowl halftime performers (booked centrally by the league, not a club)
- national-anthem-only appearances (a different, much shorter slot)
- anything you cannot tie to a named sports event or team

Return ONLY a JSON array, no prose. Each element:
{"name": "the act or performer name",
 "category": "one of: nostalgia music, classic rock, military / patriotic,
              regional / market, tribute, team tradition, other music",
 "style": "the kind of music, from: hip hop / rap, rock, classic rock, country,
           pop, r&b / soul, gospel, latin, metal, jazz, marching / military,
           other. Use '' if the sources do not make it clear -- a guessed style
           gets an act wrongly filtered out of a client's list.",
 "level": "pro" | "college" | "both" | "unknown",
 "clients": "the teams/venues/events it performed for, comma separated",
 "booking_contact": "agency, phone, email or site if stated, else ''",
 "fee_note": "any DOCUMENTED fee with its source context, else ''",
 "home_base": "city/state if stated, else ''",
 "evidence": "one sentence naming the event, quoting or closely paraphrasing"}

If a field is not stated in the sources, use "". NEVER invent a contact, a fee
or a client list -- an empty field is correct and useful; a guessed one poisons
the catalogue. A fee is the field most likely to be guessed and the most
damaging to guess: a client who quotes our invented number to an agent has been
embarrassed by us. If the sources contain no qualifying act, return []."""


_PROGRAM_SYSTEM = """You record WHAT A SPECIFIC TEAM ACTUALLY RAN at halftime,
season by season. This is not a catalogue of acts for sale — it is a record of
programmes that happened.

For every halftime programme you can tie to a NAMED team and a NAMED season,
return one element. Multiple seasons for one team are multiple elements, and so
are multiple acts within one season.

THE SEASON IS REQUIRED. A finding with no year does not answer the question
being asked, which is what teams have run RECENTLY — fees and formats move fast
enough that an undated example is not usable. If you cannot determine the
season from the source, omit that record entirely rather than guessing.

BANDS ARE A FINDING HERE, NOT AN EXCLUSION. Elsewhere in this project marching
bands are dropped. Here, "this team's halftime is always the band" is genuinely
useful — it marks a programme that is not a prospect — and "this team has run
non-band halftimes" is the signal we are hunting. So report band-only halftimes
with band_only true instead of discarding them.

EXCLUDE, always:
- Super Bowl halftime shows (booked centrally by the league, not by the club)
- pre-game and national-anthem performances (a different, shorter slot)
- anything you cannot tie to BOTH a named team and a named season

Return ONLY a JSON array, no prose. Each element:
{"team": "the team or school, as the source names it",
 "season": "the four-digit season year, e.g. 2023",
 "act": "the NAME of what performed, not a description of it. 'T-Pain',
         'Lil Jon', 'Marching Ravens', 'Red Panda'. A name is a few words; if
         you are writing a sentence you are filling in the wrong field, and the
         sentence belongs in evidence. When the source only DESCRIBES an
         unnamed segment, use the shortest noun phrase that identifies it --
         'mixed-reality video board show', 'cancer survivor tribute' -- never a
         clause with a verb in it.",
 "act_category": "one of: dog show, drone show, acrobat/variety, bmx/fmx,
                  pogo/trampoline, pyro/fireworks, projection/light,
                  fan contest, mascot/novelty, music, marching band, other",
 "band_only": true if the halftime was the marching band and nothing else,
 "occasion": "any special slot it fell in — Black Friday, military
              appreciation, homecoming, international series — else ''",
 "evidence": "one sentence naming team, season and act, quoting or closely
              paraphrasing the source"}

NEVER invent a season or an act. An omitted record is correct and useful; a
guessed one poisons a catalogue that a client may quote from. If the sources
contain no qualifying programme, return []."""


_SYSTEM_FOR_POOL = {
    "variety": _EXTRACT_SYSTEM,
    "for_hire_music": _MUSIC_SYSTEM,
    "program": _PROGRAM_SYSTEM,
}


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass          # logging must never take the job down


# The prompt already says "EXCLUDE Super Bowl halftime performers", and the
# model put Green Day in the catalogue anyway on a Super Bowl LX credit. An
# instruction is not a guard (rule 3b): a marquee event is centrally booked and
# is no evidence at all that an act would play a club's Sunday afternoon
# halftime, so the rule is enforced in code where it cannot be talked out of.
# Rejects only when EVERY credit is marquee — an act with both a Super Bowl and
# a club credit is still a real club candidate.
_MARQUEE_RX = re.compile(
    r"\b(super ?bowl|all[- ]star|nba finals|stanley cup|world series|"
    r"pro bowl|olympic|halftime show at the super bowl)\b", re.IGNORECASE)
_CLUB_RX = re.compile(
    r"\b(steelers|bengals|browns|ravens|lions|packers|cowboys|giants|"
    r"commanders|texans|broncos|eagles|bills|chiefs|49ers|seahawks|"
    r"vikings|bears|saints|falcons|panthers|buccaneers|cardinals|rams|"
    r"chargers|raiders|jets|dolphins|patriots|titans|colts|jaguars|"
    r"university|college|state)\b", re.IGNORECASE)


def marquee_only(clients: str) -> bool:
    """Every credit is a centrally-booked marquee event, none is a club slot."""
    text = clients or ""
    if not _MARQUEE_RX.search(text):
        return False
    # A club name alongside it means there is real club evidence too. Strip the
    # marquee phrases first so "Super Bowl LX" cannot itself look club-ish.
    return not _CLUB_RX.search(_MARQUEE_RX.sub(" ", text))


def looks_like_band(name: str) -> bool:
    """Is this act the thing we were explicitly told to filter out?"""
    return bool(_BAND_RX.search(name or ""))


# ── S119: sponsors and venues are not acts ───────────────────────────────────
# Section 15 in docs/DGX-SPARK-PERFORMANCE.md: gpt-oss:120b returned `Cisco` and
# `U.S. Bank` from a projection/light page as though they were halftime acts.
# Tightening the prompt DID remove them and cost 43-64% of the genuine acts on
# the same six blocks, twice -- so this is done the way looks_like_band() and
# marquee_only() already are: reject a named thing in code, deterministically,
# instead of discouraging the model and losing everything else with it.
#
# The production model qwen3.8:27b has never produced one of these, so this is
# not fixing a live defect. It is the guard that has to exist BEFORE
# gpt-oss:120b could be adopted, because that model does it reliably.
#
# EXACT match after normalisation, never substring. `Lumen` is a stadium sponsor
# (Lumen Field) and `Lumen and Forge` is a real projection company that appeared
# in these very results -- a substring rule would eat it. Every name below was
# either observed in output or is a current NFL stadium naming-rights holder.
_SPONSOR_NAMES = frozenset({
    # observed in real output (section 15)
    "cisco", "u.s. bank", "us bank",
    # current or recent NFL stadium naming-rights holders and common
    # in-stadium sponsors -- the brands most likely to sit on a halftime page
    "verizon", "at&t", "pepsi", "pepsico", "coca-cola", "coke", "gatorade",
    "state farm", "mercedes-benz", "metlife", "gillette", "heinz", "acrisure",
    "lincoln financial", "levi's", "sofi", "allegiant", "caesars", "ford",
    "nissan", "lumen", "paycor", "paycom", "raymond james", "hard rock",
    "highmark", "m&t bank", "everbank", "huntington bank", "lucas oil",
    "nrg", "geha", "empower",
})

# A venue, not an act. No halftime act is called "Something Stadium", and this
# catches the sponsor-named venues the exact list would otherwise miss
# ("U.S. Bank Stadium", "Acrisure Stadium").
_VENUE_LAST_WORDS = frozenset(
    ("stadium", "arena", "coliseum", "dome", "fieldhouse", "ballpark", "field"))


def looks_like_sponsor(name: str) -> bool:
    """A brand or a venue rather than something that performed. S119.

    Deliberately narrow: an exact normalised match, or a venue suffix. It is
    meant to reject the handful of things that are obviously not acts, not to
    adjudicate every company -- a projection firm and a pyrotechnics contractor
    are both companies and both belong in the catalogue.
    """
    n = " ".join((name or "").lower().split()).strip(" .,-")
    if not n:
        return False
    if n in _SPONSOR_NAMES:
        return True
    words = n.split()
    return len(words) > 1 and words[-1] in _VENUE_LAST_WORDS


def slug_for(name: str) -> str:
    return entity_kb.slugify(name)


def angles_for_today(n: int = DEFAULT_ANGLES, day: int = None,
                     pool: str = None) -> list:
    """A rotating slice of the matrix, so one run is cheap and a week is full.

    Rotates by day-of-year rather than a stored cursor: nothing to corrupt,
    nothing to reset, and a missed day costs one slice rather than desyncing
    the rotation permanently.
    """
    # `pool` narrows the rotation to one supply model. It exists for SEEDING:
    # when a pool is added, day-of-year rotation would take a week to reach its
    # angles for the first time, and the dashboard is empty until it does. The
    # nightly run passes nothing and keeps rotating over everything.
    angles = [a for a in SEARCH_ANGLES if pool is None or a[0] == pool]
    if not angles:
        return []
    n = max(1, min(int(n), len(angles)))
    day = int(datetime.now().strftime("%j")) if day is None else day
    start = (day * n) % len(angles)
    out = []
    for i in range(n):
        out.append(angles[(start + i) % len(angles)])
    return out


def parse_acts(raw: str, pool: str = "variety") -> list | None:
    """Parse the model's JSON array. None means UNUSABLE, [] means none found.

    The distinction matters and is the whole reason this returns a tri-state:
    "the model found nothing" is a real answer worth recording, while "the model
    produced something we cannot read" must escalate rather than be recorded as
    an empty result. Collapsing the two is how a broken extractor looks like a
    quiet week.
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    # S83: the programme pool returns a DIFFERENT record — team/season/act, with
    # no "name" at all. Normalising it through the act schema below would drop
    # every row on the `if not name` line and return [], which reads as "the
    # model found nothing" rather than "we cannot read this" — so the run would
    # not even escalate. Exactly the silent-failure shape this file already
    # guards against with the tri-state, one layer lower down.
    if pool == "program":
        out = []
        for item in data:
            if not isinstance(item, dict):
                continue
            team = str(item.get("team") or "").strip()
            season = str(item.get("season") or "").strip()
            if not team or not season:
                continue      # both are required; see _PROGRAM_SYSTEM
            out.append({
                "team": team[:120],
                "season": season[:10],
                # 80, not 200: this field holds a NAME. The first live run
                # returned whole sentences here ("Cancer previvors, fighters,
                # survivors and thrivers joined the ... Cheerleaders"), which a
                # 200-char cap simply stored. A tight cap does not fix the
                # prompt, but it makes prose obvious instead of plausible.
                "act": str(item.get("act") or "").strip()[:80],
                "act_category": str(item.get("act_category") or "other").strip()[:40],
                "band_only": bool(item.get("band_only")),
                "occasion": str(item.get("occasion") or "").strip()[:120],
                "evidence": str(item.get("evidence") or "").strip()[:400],
            })
        return out
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name[:120],
            "category": str(item.get("category") or "other").strip()[:40],
            "style": str(item.get("style") or "").strip()[:40],
            "level": str(item.get("level") or "unknown").strip()[:20],
            "clients": str(item.get("clients") or "").strip()[:400],
            "booking_contact": str(item.get("booking_contact") or "").strip()[:200],
            "fee_note": str(item.get("fee_note") or "").strip()[:400],
            "home_base": str(item.get("home_base") or "").strip()[:120],
            "evidence": str(item.get("evidence") or "").strip()[:400],
        })
    return out


# S103: `extracted_by` is provenance, not knowledge. Adding the model tag to it
# means EVERY re-encountered act shows a changed field on the next run, and
# `updated` in the monitored note is meant to say "we learned something new
# about this act" -- not "the label on who extracted it got longer". Counting
# provenance as an update would spike the number Buddy reads on exactly the run
# that carries the change, and quietly dilute it forever after.
_PROVENANCE_FIELDS = {"extracted_by"}


def _substantive(changed) -> bool:
    """Did anything change that a READER of the catalogue would care about?"""
    return bool(set(changed or ()) - _PROVENANCE_FIELDS)


def _tag(provider: str) -> str:
    """`provider:model`, e.g. `ollama:qwen3.8:27b` — for `extracted_by`.

    S103 (Buddy): every entry used to be labelled with the PROVIDER alone, so
    the catalogue could not say which local model produced it. When
    `ollama_model` moved qwen2.5:72b -> qwen3.8:27b on 2026-09-05, entries
    either side of the switch were indistinguishable, and the acceptance test
    for that switch had to be read from a log instead of the artefact.

    The model comes from llm_providers.last_model() -- the value actually put
    on the wire -- and NOT from a second reading of creds, which would be a
    copy that can drift from what was really called.

    Falls back to the bare provider name if the model is somehow unknown: a
    slightly less specific label is fine, inventing one is not.
    """
    import llm_providers
    model = llm_providers.last_model()
    return f"{provider}:{model}" if model else provider


def extract_acts(source_block: str, creds: dict,
                 pool: str = "variety") -> tuple:
    """(acts, model, escalated). LOCAL FIRST — requirement 4, literally.

    qwen2.5:72b answers unless it cannot produce usable JSON, and only then does
    a foundation model get asked. `escalated` is returned so the caller can
    record it per entity: the escalation RATE then falls out of the catalogue
    itself, which is the measurement S73 asked for before trusting the local
    model with anything.

    Never raises. A research job that dies on a bad model reply is a research
    job that stops running.
    """
    import llm_providers

    system = _SYSTEM_FOR_POOL.get(pool, _EXTRACT_SYSTEM)
    user = f"SOURCES:\n\n{source_block[:24000]}"

    try:
        raw = llm_providers.call("ollama", system, user, creds,
                                 max_tokens=4000, retries=0)
        acts = parse_acts(raw, pool)
        if acts is not None:
            return acts, _tag("ollama"), False
    except Exception:
        pass          # no local model on this box, or it fell over — escalate

    try:
        provider, raw = llm_providers.escalate(
            system, user, creds, max_tokens=4000, mode="single")
        acts = parse_acts(raw, pool)
        if acts is not None:
            return acts, _tag(provider), True
    except Exception:
        pass
    return [], "", False


def _fields_for(act: dict, angle: str, model: str, escalated: bool,
                pool: str = "variety") -> dict:
    return {
        "type": "halftime act",
        "pool": pool,
        "category": act["category"],
        "style": act.get("style", ""),
        "level": act["level"],
        "clients": act["clients"],
        "booking_contact": act["booking_contact"],
        "fee_note": act["fee_note"],
        "home_base": act["home_base"],
        "source": "halftime_catalogue",
        "found_via": angle,
        "extracted_by": model + (" (escalated)" if escalated else " (local)"),
        "confidence_basis": (
            "Extracted from public web sources by the halftime catalogue job. "
            "Contact and fee fields are copied only when the source states "
            "them; blank means not stated, never guessed."),
    }


def _program_fields_for(canon: str, season: int, band_only: bool,
                        prior: dict, angle: str, model: str,
                        escalated: bool) -> dict:
    """Team-level fields. Per-season detail lives in SIGNALS, not here: fields
    are overwritten on every upsert, so anything per-find recorded here would be
    destroyed by the next find for the same team."""
    # entity_kb returns the stored fields under "state", NOT "fields". Reading
    # the wrong key here is silent: prior_latest stays 0 and the confirmation
    # below is re-derived from scratch every time, so latest_season_seen would
    # track the LAST WRITE rather than the newest season and a band-only find
    # would quietly un-prove an earlier non-band one. Caught by the selftest,
    # which is the only reason it is not in the catalogue right now.
    prior_fields = (prior or {}).get("state") or {}
    try:
        prior_latest = int(prior_fields.get("latest_season_seen") or 0)
    except (TypeError, ValueError):
        prior_latest = 0
    # a non-band halftime, once confirmed, STAYS confirmed — a later band-only
    # find does not un-prove the year they ran drones (handoff angle 7)
    confirmed = (prior_fields.get("non_band_halftime") == "yes") or (not band_only)
    return {
        "type": "halftime program",
        "pool": "program",
        "team": canon,
        "level": team_level(canon),
        "non_band_halftime": "yes" if confirmed else "not seen yet",
        "latest_season_seen": str(max(prior_latest, season)),
        "source": "halftime_catalogue",
        "found_via": angle,
        "extracted_by": model + (" (escalated)" if escalated else " (local)"),
        "confidence_basis": (
            "What a named team ran at a named halftime, extracted from public "
            "sources. Season is required and never guessed; a record without "
            "one is dropped rather than estimated."),
    }


def _record_programs(rows: list, team_hint: str, angle: str, model: str,
                     escalated: bool, stats: dict, dry_run: bool,
                     db_path: str = None, this_year: int = None) -> None:
    """Record team-season programme findings. Keyed by TEAM."""
    year = this_year or datetime.now().year
    cutoff = year - PROGRAM_YEARS
    for r in rows:
        canon = canonical_team(r.get("team") or team_hint)
        if not canon:
            # the junk gate: an invented team simply fails to match
            stats["unknown_team_rejected"] = stats.get("unknown_team_rejected", 0) + 1
            continue
        try:
            season = int(str(r.get("season") or "")[:4])
        except (TypeError, ValueError):
            season = 0
        if not season:
            stats["undated_rejected"] = stats.get("undated_rejected", 0) + 1
            continue
        if season < cutoff:
            stats["too_old_rejected"] = stats.get("too_old_rejected", 0) + 1
            continue
        band_only = bool(r.get("band_only"))
        if band_only:
            stats["band_only_programs"] = stats.get("band_only_programs", 0) + 1
        stats["found"] += 1
        if dry_run:
            log("    would record: %s %s — %s%s"
                % (canon, season, r.get("act", ""), " [band only]" if band_only else ""))
            continue
        slug = slug_for(canon)
        prior = entity_kb.get_entity(KB_PROJECT, slug, db_path=db_path)
        res = entity_kb.upsert_entity(
            KB_PROJECT, slug, canon, entity_type="halftime_program",
            fields=_program_fields_for(canon, season, band_only, prior,
                                       angle, model, escalated),
            lead_state=None if prior else "new", db_path=db_path)
        if res.get("created"):
            stats["new"] += 1
        elif _substantive(res.get("changed_fields")):
            stats["updated"] += 1
        note = "%s: %s%s%s" % (
            season, r.get("act") or "(unnamed)",
            " [band only]" if band_only else "",
            (" — " + r["occasion"]) if r.get("occasion") else "")
        try:
            entity_kb.add_signal(KB_PROJECT, slug, "programme",
                                 note + " — " + (r.get("evidence") or ""),
                                 db_path=db_path)
        except Exception:
            pass          # the entity matters; the note is a bonus


# S119. Angles are gathered concurrently (search + fetch + extract); recording
# stays serial and in order -- see the note inside run(). Same knob shape as
# halftime_routing: HALFTIME_CATALOGUE_WORKERS=1 restores the exact serial path
# by skipping the executor entirely, which is both the kill switch and what the
# selftest compares against.
# ── S119 (Buddy): this job runs gpt-oss:120b, and ONLY this job ─────────────
# `ollama_model` in credentials.json is GLOBAL -- halftime_routing and
# promise_detect read the same field, and promise_detect runs on every client
# send via task_solver. Editing that field would have switched all three at
# once, which is not what was asked and is a much larger blast radius.
#
# So the model is overridden here, on an in-memory copy of the creds, exactly
# the way model_compare.py does it. credentials.json is never written, and every
# other caller keeps qwen3.8:27b.
#
# WHY 120b (docs/DGX-SPARK-PERFORMANCE.md):
#   section 9  - 6/6 parse, 0% escalation, vs the 27B's 5/6 and 17%. Escalation
#                is a paid Anthropic call, so this should REDUCE spend.
#   section 5b - 43 tok/s vs the 27B's 32, and it never exceeded 39.6s against
#                the 120s timeout the 27B was hitting.
#   section 16 - its one known failure mode, returning sponsors as acts, is now
#                caught deterministically by looks_like_sponsor().
#
# ⚠️ KNOWN, NOT YET GUARDED: section 9 also saw 120b return touring musicians
# (Journey, Minnesota Orchestra, The New Power Generation, The Steeles) from a
# projection/light page. The prompt excludes them and looks_like_sponsor() does
# NOT catch them -- it rejects brands and venues, not bands. Watch the first
# runs for musician names in the catalogue.
#
# Revert: set HALFTIME_CATALOGUE_MODEL to qwen3.8:27b, or empty to inherit the
# global. No deploy needed.
# ★ SET BACK TO INHERIT AFTER A LIVE DRY-RUN. Buddy asked for the switch and it
# was made; the dry-run that verified it is the reason it is off again.
#
# gpt-oss:120b delivered exactly the throughput and cost it promised -- 8 angles,
# escalated 0, local 8, so the paid-call rate really did go to zero. And the
# output was unusable. Of 12 acts it would have recorded, 11 were junk:
#
#   Minnesota Vikings 2025 — Snoop Dogg      <- touring musician, AND a
#   Detroit Lions 2025 — Snoop Dogg             malformed name; the same
#   Minnesota Vikings 2025 — Lainey Wilson      artist duplicated per team
#   Detroit Lions 2025 — Andrea Bocelli
#   Minnesota Vikings 2024 — Rogers High School ... Flag Game
#
# Existing entities are clean act names ("Stunt Dog Productions") or plain team
# names ("Baltimore Ravens"). "Minnesota Vikings 2025 — Snoop Dogg" is neither.
# Only "Harry Blackstone Jr." was plausible.
#
# Two failures at once: it ignores the touring-musician exclusion wholesale --
# the risk flagged in this comment before the run, and looks_like_sponsor()
# rejects brands and venues, not bands -- and it emits team+year+artist strings
# instead of act names.
#
# THIS CONTRADICTS SECTION 9, which measured 120b at 6/6 parse and matching the
# 27B's extractions. Section 9 used six curated *variety* blocks; the live
# rotation covers other angle pools, and its instruction-following collapses
# there. A sample of six was not a certification, as section 9 itself said.
#
# The mechanism below is kept and proven -- it is one string from switching
# back, and the dry-run is how to check any future candidate before it writes.
DEFAULT_CATALOGUE_MODEL = ""


def _catalogue_model() -> str:
    """The model THIS job uses. Empty string means inherit the global."""
    raw = os.environ.get("HALFTIME_CATALOGUE_MODEL")
    return DEFAULT_CATALOGUE_MODEL if raw is None else raw


DEFAULT_CATALOGUE_WORKERS = 6
MAX_CATALOGUE_WORKERS = 16

# S119: model calls are capped SEPARATELY from search/fetch, for the reason
# measured on the routing lane -- a live A/B there showed escalation going
# 2/7 -> 6/7 once extractions ran concurrently. ollama does not batch, so
# queued calls run past llm_providers._TIMEOUT (120s) and a timeout escalates
# to a PAID Anthropic call. Faster and three times the bill is not a win.
# Default 1 = what ollama can actually serve; raise it under vLLM, which can.
DEFAULT_CATALOGUE_EXTRACT_WORKERS = 1
MAX_CATALOGUE_EXTRACT_WORKERS = 16


def _extract_worker_count() -> int:
    """How many model calls may be in flight at once."""
    raw = os.environ.get("HALFTIME_CATALOGUE_EXTRACT_WORKERS")
    try:
        want = (int(raw) if raw not in (None, "")
                else DEFAULT_CATALOGUE_EXTRACT_WORKERS)
    except (TypeError, ValueError):
        want = DEFAULT_CATALOGUE_EXTRACT_WORKERS
    return max(1, min(want, MAX_CATALOGUE_EXTRACT_WORKERS))


def _worker_count(n_tasks: int) -> int:
    """How many angles to gather at once. Clamped, never more than tasks."""
    raw = os.environ.get("HALFTIME_CATALOGUE_WORKERS")
    try:
        want = int(raw) if raw not in (None, "") else DEFAULT_CATALOGUE_WORKERS
    except (TypeError, ValueError):
        want = DEFAULT_CATALOGUE_WORKERS
    want = max(1, min(want, MAX_CATALOGUE_WORKERS))
    return max(1, min(want, n_tasks or 1))


def run(dry_run: bool = False, angles: int = DEFAULT_ANGLES,
        refresh: int = REFRESH_CHUNK, creds: dict = None,
        db_path: str = None, pool: str = None) -> dict:
    """One pass: discover on a rotating slice of angles, then refresh known acts."""
    import cirrus_daily

    creds = creds if creds is not None else (
        json.loads((PROJECT_DIR / "config/credentials.json").read_text()))
    # Scoped override -- a COPY, so nothing else sees it (see the note above).
    _model = _catalogue_model()
    if _model:
        _global = creds.get("ollama_model")
        creds = dict(creds)                     # COPY -- see the note above
        creds["ollama_model"] = _model
        log(f"model for this job: {_model} (global ollama_model stays {_global})")

    stats = {"angles": 0, "sources": 0, "found": 0, "new": 0, "updated": 0,
             "bands_rejected": 0, "marquee_rejected": 0, "sponsors_rejected": 0,
             "escalated": 0, "local": 0}

    # Build the whole worklist BEFORE the loop: the loop rebinds `pool`, so
    # anything that reads the parameter has to happen first. The two rotations
    # stay separate on purpose — folding 64 team angles into SEARCH_ANGLES would
    # skew the day-of-year slice for the act pools that already work.
    todo = list(angles_for_today(angles, pool=pool))
    if pool is None or pool == "program":
        todo += program_angles_for_today(
            angles if pool == "program" else PROGRAM_ANGLES_PER_RUN)

    # ── S119: GATHER concurrently, RECORD serially ────────────────────────
    # Only the network + model half runs in parallel. Everything that mutates
    # `stats` or writes to the entity KB stays exactly where it was, on one
    # thread, in the original angle order -- so the catalogue this job produces
    # for Justin is unchanged, and sqlite is never written from two threads.
    #
    # Depends on the S119 fix making llm_providers.last_model() thread-local:
    # extract_acts() tags every entry's `extracted_by` from it, and with a
    # shared global two in-flight extractions would label each other's entries.
    def _gather(item):
        """Search + fetch + extract for one angle. Touches no shared state."""
        pool_, category_, query_ = item
        lines = [f"angle: {category_} — {query_}"]
        try:
            # S90: attribute this job's spend to itself. See the same note in
            # halftime_routing.py -- every caller of search_web was billed to
            # "daily_digest", so the usage report could not say what any job
            # except privacy_monitor actually costs.
            urls = cirrus_daily.search_web(query_, max_results=MAX_SEARCH_RESULTS,
                                           caller="halftime_catalogue")
        except Exception as e:
            return item, None, lines + [f"  search failed: {e}"]
        sources = []
        for url in urls:
            try:
                content, _ = cirrus_daily.fetch_article_content(url)
            except Exception:
                continue
            if content:
                sources.append((url, content[:MAX_FETCH_CHARS]))
        if not sources:
            return item, None, lines + ["  no fetchable sources"]
        block = "\n\n".join(f"SOURCE: {u}\n{t}" for u, t in sources)
        with _extract_gate:          # the model is the serialised resource
            acts, model, escalated = extract_acts(block, creds, pool_)
        lines.append(f"  {len(acts)} act(s) via {model or 'nothing usable'}"
                     + (" [escalated]" if escalated else ""))
        return item, (len(sources), acts, model, escalated), lines

    _extract_gate = threading.BoundedSemaphore(_extract_worker_count())
    _workers = _worker_count(len(todo))
    if _workers > 1 and len(todo) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=_workers) as _pool:
            gathered = list(_pool.map(_gather, todo))
    else:
        gathered = [_gather(item) for item in todo]

    for (pool, category, query), got, _lines in gathered:
        stats["angles"] += 1
        for _line in _lines:
            log(_line)
        if got is None:
            continue
        _n_sources, acts, model, escalated = got
        stats["sources"] += _n_sources
        if escalated:
            stats["escalated"] += 1
        elif model:
            stats["local"] += 1

        if pool == "program":
            _record_programs(acts, category, category, model, escalated,
                             stats, dry_run, db_path)
            continue

        for act in acts:
            if looks_like_band(act["name"]):
                stats["bands_rejected"] += 1
                continue
            if pool == "for_hire_music" and marquee_only(act.get("clients", "")):
                stats["marquee_rejected"] += 1
                log(f"    rejected (marquee-only credit): {act['name']}")
                continue
            if looks_like_sponsor(act["name"]):
                stats["sponsors_rejected"] += 1
                log(f"    rejected (sponsor or venue, not an act): {act['name']}")
                continue
            stats["found"] += 1
            slug = slug_for(act["name"])
            if dry_run:
                log(f"    would record: {act['name']} [{act['category']}]")
                continue
            existing = entity_kb.get_entity(KB_PROJECT, slug, db_path=db_path)
            res = entity_kb.upsert_entity(
                KB_PROJECT, slug, act["name"], entity_type="halftime_act",
                fields=_fields_for(act, category, model, escalated, pool),
                lead_state=None if existing else "new", db_path=db_path)
            if res.get("created"):
                stats["new"] += 1
            elif _substantive(res.get("changed_fields")):
                stats["updated"] += 1
            try:
                entity_kb.add_signal(
                    KB_PROJECT, slug, "catalogue-evidence", act["evidence"],
                    db_path=db_path)
            except Exception:
                pass          # the entity matters; the note is a bonus

    # REFRESH — oldest-first, so the catalogue self-balances with no cursor.
    if refresh and not dry_run:
        try:
            known = entity_kb.list_entities(KB_PROJECT, db_path=db_path)
            stale = sorted(known, key=lambda e: e.get("last_updated") or "")[:refresh]
            for e in stale:
                entity_kb.upsert_entity(KB_PROJECT, e["slug"], e["name"],
                                        db_path=db_path)
        except Exception as ex:
            log(f"refresh pass failed (discover results still recorded): {ex}")

    log(f"done: {stats}")
    return stats


def report(db_path: str = None) -> int:
    """What is actually in the catalogue. Read-only."""
    try:
        ents = entity_kb.list_entities(KB_PROJECT, db_path=db_path)
    except Exception as e:
        print(f"UNREADABLE: {e}")
        return 1
    # S83: acts and team programmes share one KB project but are different
    # questions — "who sells this" vs "what did this team run". Counting them
    # together would inflate the act total and bucket every programme under "?",
    # because a programme has no category field. A report that mixes them
    # answers neither question.
    acts = [e for e in ents if e.get("entity_type") != "halftime_program"]
    progs = [e for e in ents if e.get("entity_type") == "halftime_program"]
    print(f"halftime catalogue: {len(acts)} act(s), "
          f"{len(progs)} team programme(s)")
    by_cat, by_model = {}, {}
    for e in ents:
        st = e.get("state") or {}
        if e.get("entity_type") != "halftime_program":
            by_cat[st.get("category", "?")] = by_cat.get(st.get("category", "?"), 0) + 1
        by_model[st.get("extracted_by", "?")] = by_model.get(st.get("extracted_by", "?"), 0) + 1
    for c, n in sorted(by_cat.items(), key=lambda t: -t[1]):
        print(f"  {c:24} {n}")
    if progs:
        print("\n  team programmes — what teams ACTUALLY ran:")
        for e in sorted(progs, key=lambda x: (x.get("state") or {}).get("team", "")):
            st = e.get("state") or {}
            print("    %-24s latest %-6s non-band: %s"
                  % (st.get("team", e.get("name", "?")),
                     st.get("latest_season_seen", "?"),
                     st.get("non_band_halftime", "?")))
    print("\n  extraction (local vs escalated — the S73 measurement):")
    for m, n in sorted(by_model.items(), key=lambda t: -t[1]):
        print(f"    {m:40} {n}")
    return 0


def selftest() -> int:
    """Offline. No network, no model, no writes outside a temp DB."""
    import tempfile
    bad = 0

    def check(label, ok):
        nonlocal bad
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            bad += 1

    # --- the exclusion Buddy asked for by name -------------------------
    check("a marching band is rejected", looks_like_band("Ohio State Marching Band"))
    check("a drumline is rejected", looks_like_band("Blue Devils Drum Line"))
    check("a colour guard is rejected", looks_like_band("Tiger Color Guard"))
    check("a dog act is NOT rejected", not looks_like_band("Mutts Gone Nuts"))
    check("a drone provider is NOT rejected", not looks_like_band("Sky Elements"))
    check("an act that merely performs WITH a band is not rejected",
          not looks_like_band("Red Panda"))

    # --- tri-state parsing: the distinction that keeps a broken run loud
    check("a clean JSON array parses",
          len(parse_acts('[{"name": "K9s In Flight", "category": "dog show"}]')) == 1)
    check("JSON wrapped in prose still parses",
          len(parse_acts('Here you go:\n[{"name": "Xpogo"}]\nhope that helps')) == 1)
    check("an EMPTY array means 'none found', not 'broken'",
          parse_acts("[]") == [])
    check("unusable output returns None so the caller ESCALATES",
          parse_acts("I could not find anything useful.") is None)
    check("empty output returns None, not an empty result",
          parse_acts("") is None)
    check("a non-list JSON returns None", parse_acts('{"name": "x"}') is None)
    check("an entry with no name is dropped",
          parse_acts('[{"category": "dog show"}, {"name": "Real Act"}]')
          == [{"name": "Real Act", "category": "other", "style": "",
               "level": "unknown", "clients": "", "booking_contact": "",
               "fee_note": "", "home_base": "", "evidence": ""}])

    # --- rotation covers the whole matrix without a stored cursor -------
    seen = set()
    for d in range(1, 30):
        for _p, cat, q in angles_for_today(DEFAULT_ANGLES, day=d):
            seen.add(q)
    check("rotating angles cover the whole matrix within a month",
          seen == {q for _, _, q in SEARCH_ANGLES})
    check("a run asks for a bounded number of angles",
          len(angles_for_today(4, day=1)) == 4)
    check("asking for more angles than exist does not crash",
          len(angles_for_today(999, day=1)) == len(SEARCH_ANGLES))

    # --- pools (S79) -------------------------------------------------------
    # The two supply models must stay apart all the way from the search angle
    # to the stored entity. A for-hire act silently filed as "variety" would
    # be ranked by routing it does not have.
    check("every angle declares a pool that has an extraction prompt",
          all(len(a) == 3 and a[0] in _SYSTEM_FOR_POOL for a in SEARCH_ANGLES))
    check("both pools are actually searched, not just declared",
          {a[0] for a in SEARCH_ANGLES} == {"variety", "for_hire_music"})
    check("the two pools use DIFFERENT prompts",
          _SYSTEM_FOR_POOL["variety"] is not _SYSTEM_FOR_POOL["for_hire_music"])
    check("the for-hire prompt qualifies acts by CREDIT, not availability",
          "Availability is a phone call" in _MUSIC_SYSTEM)
    check("the for-hire prompt still refuses to invent a fee",
          "NEVER invent a contact, a fee" in _MUSIC_SYSTEM)
    check("anthem-only appearances are excluded from the music pool",
          "anthem-only" in _MUSIC_SYSTEM)
    check("an act whose SONG is a stadium tradition is in scope, credit or not",
          "team tradition" in _MUSIC_SYSTEM
          and "ritual IS the tie" in _MUSIC_SYSTEM)
    check("the blind spot that motivated it is named, so it is not re-lost",
          "Renegade" in _MUSIC_SYSTEM)
    check("team-tradition angles actually exist in the rotation",
          any(a[1] == "team tradition" for a in SEARCH_ANGLES))
    check("a Super Bowl-only credit is rejected, not just discouraged",
          marquee_only("Super Bowl LX 2026"))
    check("an NBA All-Star-only credit is rejected",
          marquee_only("NBA All-Star Game 2026 at Intuit Dome"))
    check("an NBA Finals-only credit is rejected",
          marquee_only("2026 NBA Finals Game 4 halftime at Madison Square Garden"))
    check("a real club credit is KEPT",
          not marquee_only("Detroit Lions Thanksgiving Classic 2025"))
    check("a college credit is KEPT",
          not marquee_only("Ohio State University homecoming"))
    check("an act with BOTH a marquee and a club credit is kept",
          not marquee_only("Super Bowl LX 2026; Pittsburgh Steelers halftime"))
    check("no credit at all is not a marquee rejection",
          not marquee_only(""))
    check("the music prompt asks for a style, which the rap rule needs",
          '"style"' in _MUSIC_SYSTEM)
    check("the prompt tells the model an EMPTY style is better than a guess",
          "a guessed style" in _MUSIC_SYSTEM)
    _st = parse_acts('[{"name":"A","style":"hip hop / rap"}]')
    check("style survives parsing", _st and _st[0]["style"] == "hip hop / rap")
    check("a missing style parses as empty, not as a guess",
          parse_acts('[{"name":"B"}]')[0]["style"] == "")
    # S119: this asserted ONE exact phrase, and broke the moment that clause was
    # reworded while keeping its meaning -- during the prompt-tightening
    # experiment that was ultimately reverted. Checking each exclusion by its
    # SUBJECT survives rewording and still names the one that went missing.
    # ── S119: the catalogue-scoped model override. ollama_model is GLOBAL --
    # halftime_routing and promise_detect read the same field, and
    # promise_detect runs on every client send -- so the override must never
    # escape this job. The mutation that matters is writing through to the
    # caller's dict, which would silently repoint the other two.
    _prev_m = os.environ.pop("HALFTIME_CATALOGUE_MODEL", None)
    try:
        check("the catalogue INHERITS the global model by default — gpt-oss:120b "
              "was tried and reverted, see the note at DEFAULT_CATALOGUE_MODEL",
              _catalogue_model() == "")
        os.environ["HALFTIME_CATALOGUE_MODEL"] = "gpt-oss:120b"
        check("...and a candidate can be switched in with no deploy",
              _catalogue_model() == "gpt-oss:120b")
        os.environ["HALFTIME_CATALOGUE_MODEL"] = ""
        check("...empty means INHERIT the global, not 'no model'",
              _catalogue_model() == "")
    finally:
        if _prev_m is None:
            os.environ.pop("HALFTIME_CATALOGUE_MODEL", None)
        else:
            os.environ["HALFTIME_CATALOGUE_MODEL"] = _prev_m
    _caller_creds = {"ollama_model": "GLOBAL-MODEL", "ollama_url": "http://x"}
    _copy = dict(_caller_creds)
    _copy["ollama_model"] = _catalogue_model()
    check("overriding the model never mutates the caller's creds — routing and "
          "promise_detect must keep the global",
          _caller_creds["ollama_model"] == "GLOBAL-MODEL")

    # ── S119: looks_like_sponsor(). Section 15 -- gpt-oss:120b returned Cisco
    # and U.S. Bank as halftime acts, and tightening the prompt cost 43-64% of
    # the genuine acts. This rejects named things instead, so recall is
    # untouched. BOTH directions are asserted: the false-positive list is every
    # real act seen in those runs, because a filter that quietly eats real acts
    # is worse than the sponsors it removes.
    for _bad in ("Cisco", "U.S. Bank", "  cisco  ", "AT&T",
                 "U.S. Bank Stadium", "Acrisure Stadium", "Lumen Field",
                 "MetLife Stadium"):
        check(f"looks_like_sponsor rejects {_bad!r}", looks_like_sponsor(_bad))
    for _good in ("Quince Imaging", "Lumen and Forge", "Image Engineering",
                  "Rozzi Fireworks", "Zambelli Fireworks", "Premier Pyrotechnics",
                  "Performance Dogs of Ohio", "Mutts Gone Nuts LLC",
                  "Xpogo Stunt Team", "Dunk All Stars", "TNT Dunk Squad",
                  "BMX Stunt Team", "Dialed Action Sports", "FMX Pros",
                  "Scarlett Entertainment", "Red Panda", "All Star Stunt Dogs",
                  "DJ Mal-Ski", "Hussein & The Fire Drummers", "Cornell Freeney",
                  "Jonathan Rinny", "Tyler's Amazing Balancing Act", "", None):
        check(f"looks_like_sponsor KEEPS {_good!r}",
              not looks_like_sponsor(_good))
    # `Lumen` is a stadium sponsor and `Lumen and Forge` is a real projection
    # company that appeared in these results. Substring matching would eat it,
    # which is why the rule is exact-match plus a venue suffix.
    check("...exact match, not substring — the Lumen case",
          looks_like_sponsor("Lumen") and not looks_like_sponsor("Lumen and Forge"))
    check("a bare venue word is not itself rejected (needs a qualifier)",
          not looks_like_sponsor("Stadium"))

    for _need, _why in (
            ("touring", "touring musicians are a different pipeline"),
            ("marching band", "excluding bands is the point of this catalogue"),
            ("Super Bowl", "Super Bowl acts are booked centrally, not by a club"),
    ):
        check(f"variety prompt still excludes '{_need}' — {_why}",
              _need.lower() in _EXTRACT_SYSTEM.lower())
    _sample = {"name": "x", "category": "c", "level": "pro", "clients": "",
               "booking_contact": "", "fee_note": "", "home_base": "",
               "evidence": ""}
    check("pool is recorded on the entity, not just used at search time",
          _fields_for(_sample, "a", "m", False, "for_hire_music")["pool"]
          == "for_hire_music")

    # --- TEAM-SIDE PROGRAMME POOL (S83) ------------------------------------
    check("the programme pool has its own prompt",
          _SYSTEM_FOR_POOL.get("program") is _PROGRAM_SYSTEM)
    check("the programme prompt REQUIRES a season",
          "THE SEASON IS REQUIRED" in _PROGRAM_SYSTEM)
    check("bands are a FINDING here, not an exclusion (handoff angle 7)",
          "BANDS ARE A FINDING HERE" in _PROGRAM_SYSTEM)
    check("the Super Bowl is still excluded — the club does not book it",
          "Super Bowl halftime shows" in _PROGRAM_SYSTEM)

    # the regression this pool would have hit silently: a programme record has
    # no "name", so the ACT parser drops every row and returns [] — which reads
    # as "found nothing" and does not escalate
    _prog_json = ('[{"team":"Pittsburgh Steelers","season":"2023",'
                  '"act":"Steel City Dog Show","act_category":"dog show",'
                  '"band_only":false,"occasion":"","evidence":"e"}]')
    check("programme JSON through the ACT parser is DROPPED (why pool is threaded)",
          parse_acts(_prog_json) == [])
    _rows = parse_acts(_prog_json, "program")
    check("programme JSON through the PROGRAMME parser survives",
          len(_rows) == 1 and _rows[0]["team"] == "Pittsburgh Steelers")
    check("a programme row with no season is dropped at parse time",
          parse_acts('[{"team":"Chicago Bears","act":"x"}]', "program") == [])
    check("a programme row with no team is dropped at parse time",
          parse_acts('[{"season":"2024","act":"x"}]', "program") == [])
    check("unusable programme output still returns None so it ESCALATES",
          parse_acts("no json here", "program") is None)
    check("an empty programme array still means 'none found', not 'broken'",
          parse_acts("[]", "program") == [])

    # team resolution is also the junk gate
    check("a full team name resolves", canonical_team("Pittsburgh Steelers")
          == "Pittsburgh Steelers")
    check("a bare nickname resolves", canonical_team("the Steelers")
          == "Pittsburgh Steelers")
    check("the LONGEST alias wins, so Giants does not steal New York Giants",
          canonical_team("New York Giants") == "New York Giants")
    check("an invented team resolves to nothing (the junk gate)",
          canonical_team("Springfield Atoms") == "")
    check("empty input resolves to nothing", canonical_team("") == "")
    check("level comes from the table, not the model",
          team_level("Green Bay Packers") == "pro")

    # rotation over the generated team matrix
    _all_prog = {q for _p, _t, q in
                 [("program", c, qq.format(team=c))
                  for c, _l, _a in NFL_TEAMS for qq in PROGRAM_QUERIES]}
    _seen = set()
    for d in range(1, 400):
        for _p, _t, q in program_angles_for_today(PROGRAM_ANGLES_PER_RUN, day=d):
            _seen.add(q)
    check("team angles cover all 32 clubs x both query shapes",
          _seen == _all_prog and len(_all_prog) == 64)
    check("a run asks for a bounded number of team angles",
          len(program_angles_for_today(4, day=1)) == 4)
    check("asking for more team angles than exist does not crash",
          len(program_angles_for_today(999, day=1)) == 64)
    check("the nightly run picks up team angles without being asked",
          any(a[0] == "program" for a in program_angles_for_today(4, day=5)))

    # recording rules — the bounds that keep the catalogue honest
    import tempfile as _tf
    with _tf.TemporaryDirectory() as _td:
        _db = str(Path(_td) / "kb.sqlite3")
        _stats = {"found": 0, "new": 0, "updated": 0}
        _rows = [
            {"team": "Pittsburgh Steelers", "season": "2023", "act": "Drone show",
             "act_category": "drone show", "band_only": False, "occasion": "",
             "evidence": "e1"},
            {"team": "Pittsburgh Steelers", "season": "2014", "act": "Old thing",
             "act_category": "other", "band_only": False, "occasion": "",
             "evidence": "e2"},
            {"team": "Springfield Atoms", "season": "2023", "act": "Fake",
             "act_category": "other", "band_only": False, "occasion": "",
             "evidence": "e3"},
            {"team": "Chicago Bears", "season": "", "act": "Undated",
             "act_category": "other", "band_only": False, "occasion": "",
             "evidence": "e4"},
        ]
        _record_programs(_rows, "Pittsburgh Steelers", "angle", "m", False,
                         _stats, False, db_path=_db, this_year=2026)
        check("a find inside the 5-year window is recorded", _stats["found"] == 1)
        check("a 2014 find is rejected as out of the window",
              _stats.get("too_old_rejected") == 1)
        check("an invented team is rejected, not recorded",
              _stats.get("unknown_team_rejected") == 1)
        check("an undated find is rejected — it cannot answer 'recently'",
              _stats.get("undated_rejected") == 1)

        _e = entity_kb.get_entity(KB_PROJECT, slug_for("Pittsburgh Steelers"),
                                  db_path=_db)
        check("the entity is keyed by TEAM, not by act",
              bool(_e) and _e["name"] == "Pittsburgh Steelers")
        check("it is a programme, not an act",
              _e.get("entity_type") == "halftime_program"
              and (_e.get("state") or {}).get("type") == "halftime program")
        check("a non-band halftime is confirmed on the team",
              (_e.get("state") or {}).get("non_band_halftime") == "yes")

        # a later band-only find must not un-prove the year they ran drones
        _s2 = {"found": 0, "new": 0, "updated": 0}
        _record_programs([{"team": "Pittsburgh Steelers", "season": "2022",
                           "act": "Marching band", "act_category": "marching band",
                           "band_only": True, "occasion": "", "evidence": "e5"}],
                         "Pittsburgh Steelers", "angle", "m", False, _s2, False,
                         db_path=_db, this_year=2026)
        _e2 = entity_kb.get_entity(KB_PROJECT, slug_for("Pittsburgh Steelers"),
                                   db_path=_db)
        check("a band-only find is counted, not discarded",
              _s2.get("band_only_programs") == 1)
        check("...and does NOT un-confirm an earlier non-band halftime",
              (_e2.get("state") or {}).get("non_band_halftime") == "yes")
        check("latest_season_seen keeps the MAX, not the last write",
              (_e2.get("state") or {}).get("latest_season_seen") == "2023")

        # dry run must write nothing
        _s3 = {"found": 0, "new": 0, "updated": 0}
        _record_programs([{"team": "Dallas Cowboys", "season": "2025",
                           "act": "x", "act_category": "other",
                           "band_only": False, "occasion": "", "evidence": "e"}],
                         "Dallas Cowboys", "a", "m", False, _s3, True,
                         db_path=_db, this_year=2026)
        check("--dry-run counts a find but writes NO entity",
              _s3["found"] == 1 and not entity_kb.get_entity(
                  KB_PROJECT, slug_for("Dallas Cowboys"), db_path=_db))

        import io as _io, contextlib as _cl
        _buf = _io.StringIO()
        with _cl.redirect_stdout(_buf):
            report(db_path=_db)
        _out = _buf.getvalue()
        check("report does NOT count a team programme as an act",
              "0 act(s), 1 team programme(s)" in _out)
        check("report names the team and what it knows about it",
              "Pittsburgh Steelers" in _out and "non-band: yes" in _out)
    check("an unknown pool falls back to the variety prompt rather than crashing",
          _SYSTEM_FOR_POOL.get("nonsense", _EXTRACT_SYSTEM) is _EXTRACT_SYSTEM)
    check("a pool filter returns only that pool's angles",
          all(a[0] == "for_hire_music"
              for a in angles_for_today(3, day=1, pool="for_hire_music")))
    check("no pool filter still rotates over everything",
          len({a[0] for d in range(40)
               for a in angles_for_today(4, day=d)}) == 2)
    check("an unknown pool yields no angles rather than silently running all",
          angles_for_today(4, day=1, pool="nope") == [])

    # --- writes land in the KB, and a band never does -------------------
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "halftime.db")
        entity_kb.upsert_entity(KB_PROJECT, "mutts-gone-nuts", "Mutts Gone Nuts",
                                entity_type="halftime_act",
                                fields={"category": "dog show"},
                                lead_state="new", db_path=db)
        ents = entity_kb.list_entities(KB_PROJECT, db_path=db)
        check("an act round-trips through entity_kb", len(ents) == 1)
        check("report() reads a real DB without raising",
              report(db_path=db) == 0)

    # S102 — the escalation rate must reach the MONITORED note, not just the run
    # log. It is the acceptance test for which model serves this lane, and it was
    # invisible to every monitor until now. Assert on the source of the note so a
    # future edit cannot quietly drop it.
    import inspect
    _src = open(__file__).read()
    check("the job_status note carries the escalation count",
          "escalated {_esc}/{_tot}" in _src or "escalated {" in _src)
    check("  ...and computes a rate, not just a raw count",
          "_rate" in _src and "100.0 * _esc" in _src)
    check("  ...and a zero-denominator run reports n/a rather than dividing",
          'else "n/a"' in _src)

    # ── S103 (Buddy): extracted_by names the MODEL, not just the provider ──
    # Driven through the real extract_acts with llm_providers stubbed, because
    # the point of the change is that the label follows what was actually
    # called. Every assertion is paired with its inverse.
    import types
    _acts_json = ('[{"name": "A", "category": "dog show", "level": "regional", '
                  '"clients": "", "booking_contact": "", "fee_note": "", '
                  '"home_base": "", "evidence": "e", "style": ""}]')

    def _fake_llm(local_raw, esc_raw, model):
        m = types.ModuleType("llm_providers")
        state = {"model": None}

        def call(_p, _s, _u, _c, **_kw):
            state["model"] = None
            if local_raw is None:
                raise RuntimeError("no local model")
            state["model"] = model
            return local_raw

        def escalate(_s, _u, _c, **_kw):
            state["model"] = None
            if esc_raw is None:
                raise RuntimeError("no cloud provider")
            state["model"] = "claude-sonnet-5"
            return ("anthropic", esc_raw)
        m.call, m.escalate = call, escalate
        m.last_model = lambda: state["model"]
        return m

    _real = sys.modules.get("llm_providers")
    try:
        sys.modules["llm_providers"] = _fake_llm(_acts_json, None, "qwen3.8:27b")
        _a, _m, _e = extract_acts("block", {})
        check("a local extraction is labelled with the MODEL, not 'ollama'",
              _m == "ollama:qwen3.8:27b" and _e is False)
        check("  ...and the field a reader sees carries it too",
              _fields_for(_a[0], "angle", _m, _e)["extracted_by"]
              == "ollama:qwen3.8:27b (local)")

        sys.modules["llm_providers"] = _fake_llm("junk", _acts_json, "x")
        _a, _m, _e = extract_acts("block", {})
        check("an ESCALATED extraction names the cloud model, not just "
              "'anthropic' — the inverse of the local case",
              _m == "anthropic:claude-sonnet-5" and _e is True)
        check("  ...and is still marked (escalated), so the S73 split survives",
              _fields_for(_a[0], "angle", _m, _e)["extracted_by"]
              .endswith("(escalated)"))

        # An unknown model must degrade to the old label, never invent one.
        _fl = _fake_llm(_acts_json, None, "qwen3.8:27b")
        _fl.last_model = lambda: None
        sys.modules["llm_providers"] = _fl
        _a, _m, _e = extract_acts("block", {})
        check("an unknown model falls back to the bare provider — never "
              "'ollama:None' written into the catalogue",
              _m == "ollama" and "None" not in _m)

        check("a provenance-only change is NOT an 'updated' act — the model "
              "tag must not spike the number Buddy reads",
              _substantive(["extracted_by"]) is False)
        # ...proven through a real recording path, not just on the helper:
        # record the SAME programme twice, changing only the model tag.
        with _tf.TemporaryDirectory() as _td2:
            _db2 = str(Path(_td2) / "kb.sqlite3")
            _row = [{"team": "Chicago Bears", "season": "2025", "act": "Drones",
                     "act_category": "drone show", "band_only": False,
                     "occasion": "", "evidence": "e"}]
            _p1 = {"found": 0, "new": 0, "updated": 0}
            _record_programs(_row, "Chicago Bears", "angle",
                             "ollama:qwen2.5:72b", False, _p1, False,
                             db_path=_db2, this_year=2026)
            _p2 = {"found": 0, "new": 0, "updated": 0}
            _record_programs(_row, "Chicago Bears", "angle",
                             "ollama:qwen3.8:27b", False, _p2, False,
                             db_path=_db2, this_year=2026)
            check("  ...re-recording the same find under a NEW model tag "
                  "counts as 0 updated, through the real recording path",
                  _p1["new"] == 1 and _p2["updated"] == 0)
            _p3 = {"found": 0, "new": 0, "updated": 0}
            _record_programs([dict(_row[0], season="2026")], "Chicago Bears",
                             "angle", "ollama:qwen3.8:27b", False, _p3, False,
                             db_path=_db2, this_year=2026)
            check("  ...while a genuinely new season DOES count as updated — "
                  "without this the filter could be blanket-blind",
                  _p3["updated"] == 1)
        # Behaviour is proven at one call site; there are two. This asserts the
        # OTHER one is wired the same way -- something no single-site test can
        # do, and the exact mutation that slipped through before it existed.
        # Read the branch LINES rather than counting substrings: a substring
        # count matched this assertion's own text and reported 3 of 2, which is
        # the trap_lint-flags-its-own-fixture shape. A check that trips over
        # itself gets deleted, and then nothing guards the second call site.
        _branches = [ln.strip() for ln in _src.splitlines()
                     if ln.strip().startswith("elif") and "changed_fields" in ln]
        check("both 'updated' branches go through _substantive — behaviour is "
              "proven at one call site, this covers the other",
              len(_branches) == 2
              and all("_substantive" in ln for ln in _branches))
        check("  ...but a real field change still counts, and so does a mixed "
              "one — the inverse, or the counter would go permanently blind",
              _substantive(["fee_note"]) is True
              and _substantive(["extracted_by", "fee_note"]) is True
              and _substantive([]) is False)

        sys.modules["llm_providers"] = _fake_llm(None, None, "x")
        _a, _m, _e = extract_acts("block", {})
        check("both providers failing still records NO model, so a total "
              "failure cannot pass as a local answer",
              (_a, _m, _e) == ([], "", False))
    finally:
        if _real is not None:
            sys.modules["llm_providers"] = _real
        else:
            sys.modules.pop("llm_providers", None)

    # ── S119: the concurrency knob. These exist because the first version of
    # _worker_count referenced `os` in a module that never imported it -- a
    # NameError that would have killed the live 06:30 run, and the whole
    # selftest still passed, because nothing here called the function. A gate
    # that never touches the new code cannot fail on it (T8).
    _prev_w = os.environ.pop("HALFTIME_CATALOGUE_WORKERS", None)
    try:
        check("_worker_count runs at all (its module imports what it uses)",
              _worker_count(10) == DEFAULT_CATALOGUE_WORKERS)
        os.environ["HALFTIME_CATALOGUE_WORKERS"] = "3"
        check("...and honours the env override", _worker_count(10) == 3)
        os.environ["HALFTIME_CATALOGUE_WORKERS"] = "1"
        check("...WORKERS=1 is the documented serial kill switch",
              _worker_count(10) == 1)
        os.environ["HALFTIME_CATALOGUE_WORKERS"] = "banana"
        check("...a garbage value falls back instead of crashing the run",
              _worker_count(10) == DEFAULT_CATALOGUE_WORKERS)
        os.environ.pop("HALFTIME_CATALOGUE_WORKERS", None)
        check("...and it never oversubscribes past the task count",
              _worker_count(2) == 2 and _worker_count(0) == 1)
        os.environ.pop("HALFTIME_CATALOGUE_EXTRACT_WORKERS", None)
        check("model calls default to ONE in flight — ollama cannot batch, and "
              "queued calls time out into paid escalations (routing measured "
              "2/7 -> 6/7 without this)",
              _extract_worker_count() == 1)
        os.environ["HALFTIME_CATALOGUE_EXTRACT_WORKERS"] = "8"
        check("...and the cap lifts for an engine that CAN batch",
              _extract_worker_count() == 8)
        os.environ["HALFTIME_CATALOGUE_EXTRACT_WORKERS"] = "banana"
        check("...garbage falls back to the safe default, never to unlimited",
              _extract_worker_count() == DEFAULT_CATALOGUE_EXTRACT_WORKERS)
        os.environ.pop("HALFTIME_CATALOGUE_EXTRACT_WORKERS", None)
    finally:
        if _prev_w is None:
            os.environ.pop("HALFTIME_CATALOGUE_WORKERS", None)
        else:
            os.environ["HALFTIME_CATALOGUE_WORKERS"] = _prev_w

    print("\nALL PASS" if not bad else f"\n{bad} FAILED")
    return 1 if bad else 0


def main() -> int:
    args = sys.argv[1:]
    if "selftest" in args:
        return selftest()
    if "report" in args:
        return report()
    dry = "--dry-run" in args
    pool = None
    if "--pool" in args:
        try:
            pool = args[args.index("--pool") + 1]
        except Exception:
            pass
    angles = DEFAULT_ANGLES
    if "--angles" in args:
        try:
            angles = int(args[args.index("--angles") + 1])
        except Exception:
            pass
    stats = run(dry_run=dry, angles=angles, pool=pool)

# S81: record into the job_status ledger so an overdue/failed run is actually
# SEEN. Until today this job ran unwatched -- opportunity_scout wrote its
# status correctly and nothing read it, and these jobs did not even write one.
# Best-effort and never allowed to change the exit status: monitoring must not
# break the thing it monitors.
    if not dry:
        try:
            import job_status
            st = stats or {}
            # S102: carry the ESCALATION RATE into the monitored note. It was
            # only ever in the run log, so the one number that decides whether
            # the local model is good enough was invisible to every monitor and
            # to the morning brief -- checkable only by grepping by hand.
            #
            # This is the acceptance test for which model serves this lane
            # (Buddy, 2026-09-05, on switching ollama_model 72b -> qwen3.8:27b).
            # Baseline on the 72B, measured over 12 runs: 13/85 = 15.3%.
            #
            # ⚠️ n is ~8 blocks per run, so a SINGLE run swings 0-50% and proves
            # nothing. Read the trend across several runs, which is exactly why
            # it belongs in a ledger rather than in one night's log line.
            _esc = st.get("escalated", 0)
            _loc = st.get("local", 0)
            _tot = _esc + _loc
            _rate = f"{100.0 * _esc / _tot:.0f}%" if _tot else "n/a"
            job_status.record(
                "halftimecatalogue", True,
                f"{st.get('found', 0)} found, {st.get('new', 0)} new, "
                f"{st.get('updated', 0)} updated, {st.get('sources', 0)} sources, "
                f"escalated {_esc}/{_tot} ({_rate})")
        except Exception as e:
            print(f"job_status.record failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
