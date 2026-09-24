#!/usr/bin/env python3
"""halftime_dashboard.py — build the Steelers halftime candidate dashboard.

S79. Justin's reply (halftime/REQUIREMENTS.md) asked for a dashboard rather
than a brief: every home game visible, a few options against each, and games
with nothing said so explicitly.

THE SHAPE. A nightly job writes ONE SNAPSHOT; the page renders that snapshot
and nothing else talks to anything. The data changes nightly, not per request,
so a live service would buy uptime risk and no freshness. It also makes
"what changed since you last looked" a diff of two files rather than a feature
somebody has to remember to build.

TWO POOLS, KEPT APART (R27). A TOURING act is a candidate for the one game its
routing passes near. A FOR-HIRE act has no routing at all and is available for
every game. Merge them and the same handful of for-hire names fill all nine
games identically and bury the routing signal that is the point of the tool.

COVERAGE IS AN OUTPUT, NOT A LOG LINE. It is what lets the page tell three
different empties apart -- "we swept and found nothing", "we have not swept
yet", and "the sweep broke". A dashboard that renders all three as a blank cell
tells a booker we checked when we did not. It is also the only honest answer to
"can we bypass Pollstar": a coverage count is evidence, an assurance is not.

Python 3.9-safe (CIRRUS): no PEP-604 unions.
"""
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import entity_kb

KB_PROJECT = "halftime_acts"
PROJECT_DIR = Path(__file__).resolve().parent
OUT_DIR = PROJECT_DIR / "out" / "halftime"
ROUTING_PATH = OUT_DIR / "routing.json"

SEASON = 2026
TEAM = "Pittsburgh Steelers"
VENUE = "Acrisure Stadium"

# 2026 home slate. `date` is None where the league has not set it (Week 16 is
# unflexed). A guessed date on a client dashboard is worse than an absent one --
# the booker plans around it. Absent, with the reason, is honest and still useful.
#
# 2026-09-23, Justin (halftime/REQUIREMENTS-2026-09-23.md): the Paris game is
# REMOVED (R30) -- it had been listed as "not a venue date" for completeness,
# and he asked for it gone. Carolina needs no act (R31). Three dates now carry
# his own brief (R36-R38), which is the `theme`; `brief` is his wording.
HOME_GAMES = [
    {"week": 1,  "date": "2026-09-13", "opponent": "Atlanta Falcons",
     "kick_et": "13:00", "slot": "day"},
    {"week": 3,  "date": "2026-09-27", "opponent": "Cincinnati Bengals",
     "kick_et": "13:00", "slot": "day"},
    {"week": 5,  "date": "2026-10-11", "opponent": "Indianapolis Colts",
     "kick_et": "13:00", "slot": "day"},
    {"week": 8,  "date": "2026-11-01", "opponent": "Cleveland Browns",
     "kick_et": "13:00", "slot": "day",
     "target": "Salute to Service", "theme": "salute_to_service",
     "brief": "A clear connection to the theme — military bands, country "
              "artists, or performers with songs or ties to the military, the "
              "country or American pride. Not unrelated acts just because they "
              "are available."},
    {"week": 12, "date": "2026-11-27", "opponent": "Denver Broncos",
     "kick_et": "15:00", "slot": "Black Friday national (Prime Video)",
     "target": "Heritage Game", "theme": "heritage",
     "brief": "1933 uniforms and a broader celebration of Pittsburgh. "
              "Pittsburgh / local connection is a major filter. The primetime "
              "window could make a larger act worth considering."},
    {"week": 13, "date": "2026-12-06", "opponent": "Houston Texans",
     "kick_et": "20:20", "slot": "Sunday Night Football (NBC)",
     "target": "Alumni Weekend — in-game stage acts, '90s theme",
     "theme": "alumni_90s",
     "brief": "No traditional halftime act. Supporting / stage acts during the "
              "game who could perform a couple of songs — House of Pain is the "
              "type."},
    {"week": 15, "date": "2026-12-20", "opponent": "Baltimore Ravens",
     "kick_et": "13:00", "slot": "day",
     "target": "Rivalry game — warrants an act"},
    {"week": 16, "date": None, "opponent": "Carolina Panthers",
     "kick_et": None, "slot": "flex — not yet set", "mode": "no_act",
     "note": "No act needed (your note, 23 Sep)."},
]

# The brief each theme stands for, in the words the page uses for its sorting.
THEME_LABEL = {"salute_to_service": "tie to Salute to Service",
               "heritage": "Pittsburgh / local connection",
               "alumni_90s": "'90s act"}

# R35 (Justin, 2026-09-23): his list of artists who are known Steelers fans or
# have a connection to the team. CLIENT DATA, his names -- a signal on any card
# where the act turns up, and on a themed date its own list. `aka` is the other
# name the same act arrives under; `style` only where it is not in doubt, and
# blank otherwise, because a wrong style is worse than none (S141).
STEELERS_CONNECTED = [
    {"name": "Bret Michaels", "style": "rock"},
    {"name": "Charles Wesley Godwin", "style": "country"},
    {"name": "Christina Aguilera", "style": "pop"},
    {"name": "Dan Smyers", "style": "country"},
    {"name": "Gabby Barrett", "style": "country"},
    {"name": "Garth Brooks", "style": "country"},
    {"name": "GloRilla", "style": "hip hop / rap"},
    {"name": "Daya", "aka": ["Grace Tandon"], "style": "pop"},
    {"name": "Hank Williams Jr.", "style": "country"},
    {"name": "Harry Mack", "style": "hip hop / rap"},
    {"name": "Jack Harlow", "style": "hip hop / rap"},
    {"name": "Jackie Evancho", "style": "classical / orchestral"},
    {"name": "Martina McBride", "style": "country"},
    {"name": "Noah Kahan", "style": ""},
    {"name": "Pat Monahan", "aka": ["Train"], "style": ""},
    {"name": "Chip Esten", "aka": ["Charles Esten"], "style": "country"},
    {"name": "Smokey Robinson", "style": "r&b / soul"},
    {"name": "Snoop Dogg", "style": "hip hop / rap"},
    {"name": "Trace Adkins", "style": "country"},
    {"name": "Warren Zeiders", "style": "country"},
    {"name": "Wiz Khalifa", "style": "hip hop / rap"},
    {"name": "Dan + Shay", "style": "country", "group": True},
    {"name": "Styx", "style": "classic rock", "group": True},
    {"name": "The Clarks", "style": "rock", "group": True},
    {"name": "Rusted Root", "style": "rock", "group": True},
    {"name": "Donnie Iris & The Cruisers", "aka": ["Donnie Iris"],
     "style": "classic rock", "group": True},
    {"name": "Wild Cherry", "style": "", "group": True},
    {"name": "The Gathering Field", "style": "rock", "group": True},
]

# Justin's own calls on specific acts for specific dates (2026-09-23). Shown on
# the card as HIS note, never reworded into a finding of ours. `ruled_out`
# moves the act into a collapsed list with the reason -- not deleted.
CLIENT_NOTES = [
    {"act": "Trans-Siberian Orchestra", "week": 15, "ruled_out": True,
     "note": "Your note (23 Sep): looks viable at first, but their performance "
             "schedule that day makes it unrealistic."},
    {"act": "Styx", "week": 15,
     "note": "Your note (23 Sep): interesting — though the 25th anniversary of "
             "Renegade next year may make 2027 the stronger opportunity."},
]

POOLS = ("touring", "for_hire")
# The for-hire label is deliberately NOT "available to book". Discovery is by
# CREDIT -- an act named as having played a sports slot -- which is a SUPERSET
# of the acts Justin meant. Eminem and Wu-Tang have halftime credits and are
# not for-hire nostalgia bookings. Separating "still touring at scale" from
# "plays when called" needs the routing data, which is step 4: the touring
# sweep is not only the other pool, it is the discriminator that makes THIS
# pool correct. Until then the honest label is what we actually know.
POOL_LABEL = {"touring": "Routing through",
              "for_hire": "Has played a sports slot",
              "fans": "Your Steelers-connected artists"}
# R5: "a few options for each game" -- a few, not the whole roster. Repeating
# every credit-list act under all nine games rendered 99 near-identical cards
# and buried the ranking that makes the column worth reading. The full roster
# is listed ONCE at the foot of the page instead.
PER_GAME_SHOWN = 3

POOL_SUB = {
    "touring": "Acts with an announced show within 3 days of kickoff and "
               "inside the 200-mile radius — they are already in the area.",
    "for_hire": "A credit list, not a verified availability list — every act "
                "here has performed a halftime or in-game slot somewhere. "
                "Whether a given one still takes one-off bookings is the phone "
                "call, and is not claimed here.",
    "fans": "From your list of Steelers fans and connections, filtered by "
            "this date's brief. Acts already in a column above are left "
            "there, marked.",
}

# Coverage states. The whole point of R4 is that these are three different
# answers and the page must never render them the same way.
NOT_SWEPT = "not_swept"
SWEPT = "swept"
FAILED = "failed"
# A fourth state, added because the selftest caught the page telling the Paris
# game "none available — we searched and nothing cleared the bar", which is a
# lie twice over: nobody searched, and nothing was ever going to. R4 is about
# not flattening distinct answers into one blank cell, and this file had done
# exactly that to itself.
NOT_APPLICABLE = "n/a"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def game_id(game: Dict) -> str:
    return "wk{:02d}-{}".format(
        game["week"], game["opponent"].split()[-1].lower())


def today_et() -> str:
    """Today in Pittsburgh. A game is still upcoming on its own day."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def is_completed(game: Dict, today: Optional[str] = None) -> bool:
    """R28: played games leave the main view. Undated games never are."""
    return bool(game.get("date")) and game["date"] < (today or today_et())


def upcoming_games(today: Optional[str] = None) -> List[Dict]:
    """The slate still worth sweeping: not played, and an act is wanted."""
    return [g for g in HOME_GAMES
            if not is_completed(g, today) and g.get("mode") != "no_act"]


# ── R35: Justin's Steelers-connected list ───────────────────────────────────

def _fan_key(name: str) -> str:
    """Letters and digits only, so "Dan + Shay" / "Dan & Shay" and "Hank
    Williams, Jr." / "Hank Williams Jr." meet."""
    return "".join(ch for ch in canonical_name(name) if ch.isalnum())


def _fan_index() -> Dict[str, Dict]:
    idx = {}
    for entry in STEELERS_CONNECTED:
        for n in [entry["name"]] + list(entry.get("aka") or []):
            idx.setdefault(_fan_key(n), entry)
    return idx


def fan_entry(name: str) -> Optional[Dict]:
    """The list entry this act matches, or None. Whole-name match only."""
    return _fan_index().get(_fan_key(name)) if name else None


def fan_acts() -> List[Dict]:
    """The list itself, shaped like act cards."""
    out = []
    for entry in STEELERS_CONNECTED:
        act = {"name": entry["name"], "also_known_as": list(entry.get("aka") or []),
               "fields": {"style": entry.get("style", ""),
                          "category": "steelers-connected"}}
        act["badges"] = badges_for(act)
        act["reach"] = {"state": "unknown", "why": ""}
        out.append(act)
    return out


# ── R36-R38: does this act fit the date's brief ─────────────────────────────
# Three answers, not two. For Salute to Service and the Heritage Game the brief
# asks for a CLEAR tie, so no evidence of one means not surfaced -- Justin: "We
# shouldn't surface unrelated mainstream artists simply because they're
# available." For the '90s date the evidence is a recorded era, and most acts
# have none yet: that is UNKNOWN, and the page must not say "not a '90s act"
# about an act whose decade nobody has looked up.
FIT, NO_FIT, UNKNOWN_FIT = "fit", "no", "unknown"


def theme_fit(act: Dict, theme: Optional[str]) -> tuple:
    """(FIT | NO_FIT | UNKNOWN_FIT, why)."""
    if not theme:
        return FIT, ""
    fields = act.get("fields") or {}
    kinds = {b["kind"] for b in act.get("badges", [])}
    style = (fields.get("style") or "").lower()
    cat = (fields.get("category") or "").lower()
    if theme == "salute_to_service":
        if "military" in cat or "patriotic" in cat or "military" in style:
            return FIT, "Military / patriotic act."
        if style == "country":
            return FIT, "Country — inside your Salute to Service brief."
        return NO_FIT, ""
    if theme == "heritage":
        if "market" in kinds:
            return FIT, "Pittsburgh / PA tie ({}).".format(
                fields.get("home_base", ""))
        if "fan" in kinds:
            return FIT, "On your Steelers-connected list."
        reach = act.get("reach") or {}
        if reach.get("state") == "our_market":
            return FIT, reach.get("why", "")
        return NO_FIT, ""
    if theme == "alumni_90s":
        era = (fields.get("era") or "").strip()
        if not era:
            return UNKNOWN_FIT, ""
        if era.startswith("199"):
            return FIT, "Broke through in the {}.".format(era)
        return NO_FIT, ""
    return FIT, ""


# ── badges ──────────────────────────────────────────────────────────────────
# Deliberately NOT a composite score. Justin's reader books acts for a living
# and wants the search narrowed, not the decision made (R14); a single number
# would hide exactly the reasoning they would want to argue with. Each badge is
# an independent, checkable claim, and the page sorts by whichever one matters
# for the game in front of you.

# MILITARY BANDS STAY (Buddy, 2026-08-26). The catalogue's band filter rejects
# marching bands, drumlines, colour guard and majorettes by name, and does NOT
# catch "122nd Army Band" or "US Navy Country Current Band". That was raised as
# a possible gap and the answer was to keep them: for a Salute to Service date
# a service band is close to what a club actually books, and both of these hold
# real NFL halftime credits. Do not "complete" the band filter to catch them.

_PA_HINTS = ("pittsburgh", "pennsylvania", ", pa", " pa ", "butler",
             "wexford", "allegheny", "steel city")


def badges_for(act: Dict) -> List[Dict]:
    """Independent claims about one act. Each carries the evidence for itself,
    so a badge can be argued with rather than trusted."""
    out = []
    fields = act.get("fields") or {}
    home = (fields.get("home_base") or "").lower()
    cat = (fields.get("category") or "").lower()
    clients = (fields.get("clients") or "")
    fee = (fields.get("fee_note") or "").strip()

    fan = fan_entry(act.get("name", ""))
    if fan:
        out.append({"kind": "fan", "label": "Steelers-connected (your list)",
                    "why": "{} is on your list of Steelers fans / "
                           "connections.".format(fan["name"])})
    if any(h in home for h in _PA_HINTS):
        out.append({"kind": "market", "label": "Pittsburgh / PA tie",
                    "why": fields.get("home_base", "")})
    if "military" in cat or "patriotic" in cat:
        out.append({"kind": "patriotic", "label": "Military / patriotic",
                    "why": fields.get("category", "")})
    if "nostalgia" in cat or "classic rock" in cat or "tribute" in cat:
        out.append({"kind": "nostalgia", "label": "Nostalgia fit (45+)",
                    "why": fields.get("category", "")})
    if fee:
        out.append({"kind": "price", "label": "Documented fee", "why": fee})
    if clients:
        out.append({"kind": "credit", "label": "Has sports credits",
                    "why": clients})
    return out


def rank_for_game(game: Dict, acts: List[Dict]) -> List[Dict]:
    """Order candidates for ONE game.

    The game's target decides what leads. For 11/1 that is the military tie;
    everywhere else it is the Pittsburgh tie, then the 45+ nostalgia fit that
    Justin named as the core of his crowd.

    A patriotic-only act is DEMOTED on a game with no patriotic ask. Without
    that, ranking fell through to alphabetical order and the 122nd Army Band
    led every single card — including a Week 1 game in September, which is the
    sort of suggestion that tells a booker the list is not really ranked.

    Nothing is filtered out by ranking; only the top few are shown, and the
    full list is on the page.
    """
    # S270: keyed on the THEME. It used to read "military" out of the target
    # wording, and Justin's own name for the date -- "Salute to Service" --
    # contains neither word, so renaming the label would have silently dropped
    # the patriotic lead.
    wants_patriotic = game.get("theme") == "salute_to_service"
    lead = "patriotic" if wants_patriotic else "market"

    def key(act):
        kinds = {b["kind"] for b in act.get("badges", [])}
        # R35: his Steelers-connected list is a Pittsburgh tie for ranking.
        if "fan" in kinds:
            kinds = kinds | {"market"}
        reach = REACH_ORDER.get((act.get("reach") or {}).get("state"), 2)
        # An act whose ONLY claim is the patriotic one is a Salute to Service
        # booking; it should not head a September afternoon game.
        patriotic_only = ("patriotic" in kinds
                          and not (kinds - {"patriotic", "credit"}))
        return (0 if lead in kinds else 1,
                reach,
                1 if (patriotic_only and not wants_patriotic) else 0,
                0 if "market" in kinds else 1,
                0 if "nostalgia" in kinds else 1,
                0 if "credit" in kinds else 1,
                0 if "price" in kinds else 1,
                act.get("name", ""))

    return sorted(acts, key=key)


# ── the same act, twice ─────────────────────────────────────────────────────
# The catalogue keys on a slug of the name, so "Eminem" and "Eminem (featuring
# Jack White)", or "Nathaniel Buttram" and "First Class Nathaniel Buttram",
# survive as separate entities. On a research catalogue that is untidy; on a
# page a booker reads it is worse than untidy, because the same person
# appearing twice under two names is the kind of thing that makes someone stop
# trusting the whole list.
#
# The collapse is deliberately CONSERVATIVE — a trailing parenthetical and a
# leading service rank, nothing else. Over-merging would silently delete a real
# act, which is the more expensive mistake: a duplicate is visible and
# embarrassing, a wrongly-merged act is invisible and gone.
# "musician" first: the Navy rate is "Musician First Class", and the loop strips
# in this order, so "musician" has to go before "first class" can match (S270 --
# the same sailor was on the 11/1 card twice).
_RANKS = ("musician", "first class", "staff sgt.", "staff sergeant", "sgt.", "sergeant",
          "master sgt.", "sfc", "mu1", "petty officer", "cpl.", "lt.",
          "the honorable")
_PAREN = re.compile(r"\s*\([^)]*\)\s*$")


def canonical_name(name: str) -> str:
    """A conservative merge key. Never merges on a substring match."""
    out = _PAREN.sub("", (name or "").strip()).strip()
    low = out.lower()
    for rank in _RANKS:
        if low.startswith(rank + " "):
            out = out[len(rank) + 1:].strip()
            low = out.lower()
    if low.startswith("the "):
        out = out[4:].strip()
    return " ".join(out.lower().split())


def dedupe_acts(acts: List[Dict]) -> List[Dict]:
    """Collapse name variants, keeping the entry that carries the most. The
    dropped variants are recorded on the survivor rather than vanishing."""
    by_key = {}
    for act in acts:
        key = canonical_name(act.get("name", ""))
        if not key:
            continue
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = dict(act, also_known_as=[])
            continue
        filled = lambda a: sum(
            1 for v in (a.get("fields") or {}).values() if str(v).strip())
        keep, drop = (prev, act) if filled(prev) >= filled(act) else (act, prev)
        merged = dict(keep)
        merged["also_known_as"] = sorted(
            set((prev.get("also_known_as") or []))
            | ({drop.get("name")} - {keep.get("name")}))
        by_key[key] = merged
    return sorted(by_key.values(), key=lambda a: a.get("name", ""))



# ── how reachable is this act, really ───────────────────────────────────────
# The first build offered Green Day, Eminem, Wu-Tang and Ludacris against
# Steelers home games. Every one has a genuine sports credit, so the catalogue
# was right and the PRESENTATION was wrong: a booker reading those as options
# concludes we do not understand his market, which is precisely the complaint
# Justin already made.
#
# The credit itself says which it is, and the distinction is not a judgement
# call:
#   * a MARQUEE credit (Super Bowl, All-Star, Finals) is a centrally-booked
#     event and no evidence at all about a club's Sunday afternoon slot;
#   * a club credit in the act's OWN home market is a hometown booking, which
#     is the league's dominant pattern — evidence that Detroit booked Detroit,
#     not that the act travels;
#   * a club credit in OUR market is the strongest thing available.
#
# Nothing is deleted. Each act carries its reachability and the reason, and the
# weak ones sort to the bottom instead of leading the card.

_MARQUEE = ("super bowl", "all-star", "all star", "nba finals", "stanley cup",
            "world series", "pro bowl", "olympic")
_OURS = ("steelers", "pittsburgh", "acrisure", "heinz field", "penguins",
         "pirates")

REACH_ORDER = {"our_market": 0, "travels": 1, "unknown": 2,
               "home_market": 3, "marquee_only": 4}


def _city_words(home_base: str) -> set:
    """City tokens from a home base, ignoring the state abbreviation."""
    head = (home_base or "").split(",")[0]
    return {w.lower() for w in head.split() if len(w) > 3}


def reachability(act: Dict) -> Dict:
    fields = act.get("fields") or {}
    clients = (fields.get("clients") or "").strip()
    low = clients.lower()
    cat = (fields.get("category") or "").lower()

    if not clients:
        return {"state": "unknown", "why": "No credit recorded yet."}
    if any(o in low for o in _OURS):
        # A TEAM-TRADITION act's tie is its SONG, not a performance. Styx came
        # into the catalogue with clients="Pittsburgh Steelers" because
        # "Renegade" is the Acrisure ritual — and reading that field as a
        # credit made the page say Styx had PLAYED for the Steelers, which is
        # false. The tie is real and worth ranking on; the claim about it has
        # to be true.
        if "team tradition" in cat:
            return {"state": "our_market",
                    "why": "Their music is an established tradition at a "
                           "Pittsburgh team — the tie is the song, not a "
                           "past booking."}
        return {"state": "our_market",
                "why": "Has played for a Pittsburgh team — the strongest "
                       "evidence available."}

    marquee = any(m in low for m in _MARQUEE)
    if marquee:
        return {"state": "marquee_only",
                "why": "Credit is a centrally-booked marquee event ({}). That "
                       "says nothing about whether they would play a club's "
                       "regular-season halftime.".format(clients[:60])}

    # A service band or a heritage act travels by definition; a hometown
    # headliner booked by its own club does not.
    travels = ("military" in cat or "patriotic" in cat or "tribute" in cat
               or "team tradition" in cat)
    if not travels and _city_words(fields.get("home_base", "")) & set(low.split()):
        return {"state": "home_market",
                "why": "Played their OWN market's club ({}). That is the "
                       "league's hometown pattern — evidence the club booked "
                       "local, not that the act travels.".format(clients[:50])}
    return {"state": "travels",
            "why": "Club credit outside their home market."}


# ── R12: the rap rule ───────────────────────────────────────────────────────
# Justin: "I'm not sure a rapper would work in our market/game unless they are
# from Pittsburgh like wiz or are a nostalgia play like snoop." That is a RULE,
# not a taste to weigh, so it gates rather than scores.
#
# Two things it must do that a plain filter would not:
#   1. SHOW ITS WORK. The nostalgia tier of the agent roster Justin sent is
#      almost entirely hip-hop, so "admitted" needs to say which door the act
#      came through, or the rule looks like it is not running.
#   2. NEVER SILENTLY DROP. A held act is listed with the reason, not removed.
#      Hiding it is the same sin as a silent truncation: the reader cannot tell
#      "your rule excluded three acts" from "there were only eight".
#
# It also FAILS OPEN. An act whose style the sources never stated is admitted,
# because holding one on a guess removes a real option from a client's list —
# the expensive direction of a wrong answer here.

_HIPHOP_STYLE = ("hip hop", "hip-hop", "rap")
_HIPHOP_TEXT = ("rapper", "hip hop", "hip-hop", "rap group", "rap trio",
                "hip hop group", "rap duo")
_NOSTALGIA_CATS = ("nostalgia", "tribute", "classic rock")


def infer_style(act: Dict) -> tuple:
    """(style, source). Only used when the catalogue has no stated style —
    acts recorded before the field existed. Returns ('', 'unknown') rather
    than guessing, and the caller treats unknown as admitted."""
    fields = act.get("fields") or {}
    stated = (fields.get("style") or "").strip().lower()
    if stated:
        return stated, "stated"
    blob = " ".join(str(fields.get(k) or "") for k in
                    ("category", "clients", "home_base")).lower()
    for sig in _HIPHOP_TEXT:
        if sig in blob:
            return "hip hop / rap", "inferred"
    return "", "unknown"


def rule_verdict(act: Dict) -> Dict:
    """Apply Justin's rule to one act. Always returns a verdict with a reason,
    so the page can explain itself either way."""
    fields = act.get("fields") or {}
    style, source = infer_style(act)
    kinds = {b["kind"] for b in act.get("badges", [])}
    is_hiphop = any(h in style for h in _HIPHOP_STYLE)

    if not is_hiphop:
        return {"state": "admitted", "rule": None, "why": "", "style": style,
                "style_source": source}
    if "market" in kinds:
        return {"state": "admitted", "rule": "rap rule",
                "why": "Pittsburgh / PA tie — clears your rule the way Wiz does.",
                "style": style, "style_source": source}
    # S270: an act Justin himself listed as Steelers-connected (GloRilla, Jack
    # Harlow, Harry Mack) is not held by his own rule. Treated like the
    # Pittsburgh door, and the card says so, so the call can be argued with.
    if "fan" in kinds:
        return {"state": "admitted", "rule": "rap rule",
                "why": "On your own Steelers-connected list — treated like a "
                       "Pittsburgh tie.",
                "style": style, "style_source": source}
    cat = (fields.get("category") or "").lower()
    if any(n in cat for n in _NOSTALGIA_CATS) or "nostalgia" in kinds:
        return {"state": "admitted", "rule": "rap rule",
                "why": "Nostalgia play — clears your rule the way Snoop does.",
                "style": style, "style_source": source}
    return {"state": "held", "rule": "rap rule",
            "why": "Hip-hop with no Pittsburgh tie and no nostalgia angle. "
                   "Held by your rule, not removed — say the word and it "
                   "comes back.",
            "style": style, "style_source": source}


def apply_rules(acts: List[Dict]) -> tuple:
    """(admitted, held). Both are returned; nothing is thrown away."""
    admitted, held = [], []
    for act in acts:
        act = dict(act)
        act["verdict"] = rule_verdict(act)
        (held if act["verdict"]["state"] == "held" else admitted).append(act)
    return admitted, held


# ── snapshot ────────────────────────────────────────────────────────────────

def _load_acts(pool: str, db_path: Optional[str] = None) -> List[Dict]:
    """Acts in one pool, as the catalogue recorded them."""
    try:
        rows = entity_kb.list_entities(KB_PROJECT, db_path=db_path)
    except Exception:
        return []
    out = []
    for row in rows:
        fields = row.get("fields") or row.get("state") or {}
        if isinstance(fields, str):
            try:
                fields = json.loads(fields)
            except Exception:
                fields = {}
        if (fields.get("pool") or "variety") != _kb_pool(pool):
            continue
        act = {"slug": row.get("slug"), "name": row.get("name"),
               "fields": fields, "last_updated": row.get("last_updated")}
        act["badges"] = badges_for(act)
        act["reach"] = reachability(act)
        out.append(act)
    return dedupe_acts(out)


def _kb_pool(pool: str) -> str:
    """Dashboard pool name -> the value the catalogue writes."""
    return "for_hire_music" if pool == "for_hire" else "touring"


def _load_routing(path: Optional[Path] = None) -> Dict:
    """The routing sweep's output, or {} if it has never run.

    An ABSENT file and an EMPTY sweep are different answers and must stay
    different all the way to the page: no file means nobody looked.
    """
    path = Path(path) if path else ROUTING_PATH
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def split_for_game(game: Dict, acts: List[Dict]) -> tuple:
    """(shown, set_aside) for one game's ranked list. Nothing is dropped:
    set_aside holds the acts with no tie to the brief, the ones whose fit is
    not yet known, and the ones Justin ruled out himself -- each with its
    reason, so an empty main list can always be explained."""
    notes = {canonical_name(n["act"]): n for n in CLIENT_NOTES
             if n.get("week") == game.get("week")}
    shown = []
    aside = {"no_fit": [], "unknown": [], "ruled_out": []}
    for act in acts:
        act = dict(act)
        note = notes.get(canonical_name(act.get("name", "")))
        if note:
            act["client_note"] = note["note"]
            if note.get("ruled_out"):
                aside["ruled_out"].append(act)
                continue
        state, why = theme_fit(act, game.get("theme"))
        if state == FIT:
            if why:
                act["theme_fit"] = why
            shown.append(act)
        else:
            aside["no_fit" if state == NO_FIT else "unknown"].append(act)
    return shown, aside


def _empty_aside() -> Dict:
    return {"no_fit": [], "unknown": [], "ruled_out": []}


def build_snapshot(db_path: Optional[str] = None,
                   games: Optional[List[Dict]] = None,
                   routing_path: Optional[Path] = None,
                   today: Optional[str] = None) -> Dict:
    """Everything the page needs, in one file."""
    games = games if games is not None else HOME_GAMES
    today = today or today_et()
    for_hire = _load_acts("for_hire", db_path=db_path)
    routing = _load_routing(routing_path)
    # Cross-reference key: an act in BOTH pools is the strongest lead there is.
    import halftime_routing
    credited_names = {halftime_routing.canonical_key(a["name"])
                      for a in for_hire}

    roster, roster_held = apply_rules(for_hire)
    snap = {"season": SEASON, "team": TEAM, "venue": VENUE,
            "generated_at": _now(), "today": today, "games": [],
            "acts_total": len(for_hire),
            # The whole credit list and Justin's list, listed ONCE at the foot.
            # Built here rather than gathered back out of the games, because a
            # themed or played game no longer carries every act.
            "roster": sorted(roster, key=lambda a: a.get("name", "")),
            "roster_held": roster_held,
            "fans": fan_acts()}

    for game in games:
        gid = game_id(game)
        entry = dict(game)
        entry["game_id"] = gid
        entry["at_venue"] = game.get("at_venue", True)
        entry["completed"] = is_completed(game, today)
        entry["set_aside"] = {"for_hire": _empty_aside(),
                              "touring": _empty_aside(),
                              "fans": _empty_aside()}
        entry["candidates"] = {"for_hire": [], "touring": [], "fans": []}
        entry["held"] = {"for_hire": [], "touring": []}

        # R28 / R31: a played game and a no-act game carry no candidates. They
        # are rendered as what they are, not as an empty search.
        if entry["completed"] or game.get("mode") == "no_act":
            why = ("Played." if entry["completed"]
                   else game.get("note") or "No act needed.")
            na = {"state": NOT_APPLICABLE, "swept_at": None, "sources": None,
                  "found": 0, "note": why}
            entry["coverage"] = {"for_hire": na, "touring": dict(na)}
            snap["games"].append(entry)
            continue

        # FOR-HIRE: no routing, so the same pool applies to every game. Coverage
        # is the catalogue's own state, which is genuinely known.
        if entry["at_venue"]:
            fh, fh_held = apply_rules(rank_for_game(game, for_hire))
            fh_cov = {"state": SWEPT, "swept_at": _now(),
                      "sources": "halftime_catalogue (nightly)",
                      "found": len(fh),
                      "note": "Acts with no tour to track — available for "
                              "any date."}
        else:
            fh, fh_held = [], []
            fh_cov = {"state": NOT_APPLICABLE, "swept_at": None,
                      "sources": None, "found": 0,
                      "note": "Staged in Paris — a different entertainment "
                              "programme, so no candidates are offered."}

        # TOURING: not built yet. Saying so is the requirement (R4); pretending
        # a blank cell means "nothing out there" is the failure it guards.
        touring = []
        if entry["at_venue"]:
            swept = (routing.get("games") or {}).get(gid)
            if not swept:
                tr_cov = {"state": NOT_SWEPT, "swept_at": None,
                          "sources": None, "found": 0,
                          "note": "No routing sweep has run for this date. "
                                  "This is NOT a finding of 'no acts "
                                  "available'."}
            else:
                rows = swept.get("coverage") or []
                errs = [r for r in rows if r.get("error")]
                touring = _as_candidates(
                    swept.get("events") or [], credited_names)
                if rows and len(errs) == len(rows):
                    tr_cov = {"state": FAILED,
                              "swept_at": rows[0].get("swept_at"),
                              "sources": "{} metro(s)".format(len(rows)),
                              "found": 0,
                              "note": "Every source failed: {}".format(
                                  errs[0].get("error", ""))[:180]}
                else:
                    tr_cov = {
                        "state": SWEPT,
                        "swept_at": rows[0].get("swept_at") if rows else None,
                        "sources": "{} metro(s) within {} mi".format(
                            len(rows), max((r.get("miles") or 0)
                                           for r in rows) if rows else 0),
                        "found": len(touring),
                        "note": "Announced shows within {} days of kickoff. "
                                "{} of {} metro searches returned nothing."
                                .format(routing.get("window_days", 3),
                                        len([r for r in rows
                                             if not r.get("found")]),
                                        len(rows))}
        else:
            tr_cov = {"state": NOT_APPLICABLE, "swept_at": None,
                      "sources": None, "found": 0,
                      "note": "Staged in Paris — routing to Acrisure does not "
                              "apply."}

        fh, entry["set_aside"]["for_hire"] = split_for_game(game, fh)
        touring, entry["set_aside"]["touring"] = split_for_game(game, touring)
        # R35: on a date with a brief, Justin's own list is a column of its own,
        # filtered by the same brief. Acts already in another column for this
        # game are left there (they carry the badge) rather than listed twice.
        fans = []
        if game.get("theme") and entry["at_venue"]:
            elsewhere = {canonical_name(a["name"]) for a in fh + touring}
            fans, entry["set_aside"]["fans"] = split_for_game(
                game, [a for a in rank_for_game(game, snap["fans"])
                       if canonical_name(a["name"]) not in elsewhere])
        entry["candidates"] = {"for_hire": fh, "touring": touring,
                               "fans": fans}
        entry["held"] = {"for_hire": fh_held, "touring": []}
        entry["coverage"] = {"for_hire": fh_cov, "touring": tr_cov}
        snap["games"].append(entry)
    return snap



# ── R13: what does well in this market ──────────────────────────────────────
# Justin asked for this directly. The constraint that shapes it is his OTHER
# note: he books acts for a living, and our first summary "feels out of touch"
# because it told him things he already knew. So every finding here carries a
# NUMBER or a COMPARISON, and anything we cannot support is listed as a gap
# rather than padded into a claim. An analysis that restates the obvious is
# worse than a short one.

# Figures we hold with a source. Kept as data, not prose, so the page cannot
# drift from what the research files actually say.
_ROSTER_NOSTALGIA = [("Rob Base", 15000), ("Treach (Naughty by Nature)", 17500),
                     ("Sugarhill Gang", 21000), ("Montell Jordan", 27000),
                     ("Rev Run (Run-DMC)", 30000)]
_ROSTER_HEADLINE = [("Paul Russell", 50000), ("Vanilla Ice", 75000),
                    ("Yung Gravy", 75000), ("Flavor Flav", 75000),
                    ("Backstreet Boys", 75000), ("Lil Jon", 100000),
                    ("Flo Rida", 100000), ("Nate Smith", 100000),
                    ("Ernest", 100000), ("Shaboozey", 150000)]
# From public university contract records — see halftime/NON-BAND-HALFTIME-
# ENTERTAINMENT.md. No NFL club figure exists publicly.
_VARIETY_BENCHMARK = ("Mutts Gone Nuts", 3650, "a Steelers halftime, 6 minutes")

_NFL_KEYS = ("steelers", "bengals", "giants", "commanders", "lions",
             "packers", "cowboys", "browns", "ravens", "texans", "broncos",
             "nfl")
_ARENA_KEYS = ("nba", "nhl", "all-star", "all star", "arena", "finals")


def _credit_split(snap: Dict) -> Dict:
    """Where the catalogue's credits actually sit. Computed, not asserted."""
    seen, nfl, arena = {}, [], []
    for a in (snap.get("roster") or []) + (snap.get("roster_held") or []):
        seen[a["name"]] = (a.get("fields") or {}).get("clients", "") or ""
    for name, clients in seen.items():
        low = clients.lower()
        if any(k in low for k in _ARENA_KEYS):
            arena.append(name)
        elif any(k in low for k in _NFL_KEYS):
            nfl.append(name)
    return {"nfl": sorted(nfl), "arena": sorted(arena), "total": len(seen)}


def market_analysis(snap: Dict) -> List[Dict]:
    findings = []
    nos = [f for _, f in _ROSTER_NOSTALGIA]
    hed = [f for _, f in _ROSTER_HEADLINE]
    vname, vfee, vwhere = _VARIETY_BENCHMARK

    findings.append({
        "title": "The supply splits into two price tiers, not a spectrum",
        "body": "The roster you sent breaks cleanly: {} nostalgia acts between "
                "${:,} and ${:,}, then {} acts from ${:,} to ${:,} with almost "
                "nothing in between. The gap sits right where a decision gets "
                "made — the question is which tier a date is worth, and there "
                "is no middle to compromise on.".format(
                    len(nos), min(nos), max(nos), len(hed), min(hed), max(hed)),
        "basis": "The booking agent's roster in your 26 Aug email.",
        "strength": "documented"})

    findings.append({
        "title": "One number anchors the top of that range to a real NFL halftime",
        "body": "Shaboozey appears on that roster at ${:,} and played the "
                "Lions' 2024 regular-season halftime. That is the only public "
                "link we have found between an NFL halftime booking and a "
                "price — every other club figure is private.".format(
                    dict(_ROSTER_HEADLINE)["Shaboozey"]),
        "basis": "Roster fee + the Lions' 2024 booking.",
        "strength": "documented"})

    findings.append({
        "title": "Non-musical entertainment is an order of magnitude cheaper",
        "body": "{} played {} for ${:,} — about a fifth of the cheapest "
                "musical act on your roster, and roughly 1/40th of the top of "
                "it. If a date needs something in the slot rather than "
                "someone specific, that is a different budget conversation."
                .format(vname, vwhere, vfee),
        "basis": "Public-university contract records; no NFL club figure is "
                 "public. See our non-band research file.",
        "strength": "documented"})

    split = _credit_split(snap)
    if split["nfl"] or split["arena"]:
        findings.append({
            "title": "NFL halftime credits and arena credits are different populations",
            "body": "Of {} acts the catalogue has found with a sports credit, "
                    "the arena and All-Star slots go to national names ({}), "
                    "while the NFL regular-season halftime credits skew local, "
                    "heritage and military ({}). The clubs and the arenas are "
                    "not competing for the same acts, which is why a national "
                    "name being 'available' says little about a Sunday "
                    "afternoon slot.".format(
                        split["total"], ", ".join(split["arena"][:4]) or "none yet",
                        ", ".join(split["nfl"][:5]) or "none yet"),
            "basis": "Computed from this catalogue — {} acts, refreshed nightly."
                     .format(split["total"]),
            "strength": "computed"})

    findings.append({
        "title": "In recent bookings the market tie holds where the age fit does not",
        "body": "Across the recent set — Bret Michaels for the Steelers "
                "(Butler County), Jack White for the Lions (Detroit), Post "
                "Malone for the Cowboys (Dallas area) — the hometown "
                "connection is present every time. The 45+ age fit is not: "
                "Post Malone skews younger and was still the pick. On this "
                "sample the market tie is doing more work than the era does, "
                "which matters when the two pull apart on a given date.",
        "basis": "Five recent club bookings — a small sample, and stated as one.",
        "strength": "small sample"})

    gaps = []
    for g in snap["games"]:
        if g.get("at_venue", True) and \
           g["coverage"]["touring"]["state"] == NOT_SWEPT:
            gaps.append(g["week"])
    findings.append({
        "title": "What this analysis cannot tell you yet",
        "body": "Nothing here measures DRAW or response, because we hold no "
                "attendance, ticket or reaction data — every claim above is "
                "about supply and price. Routing is unswept for {} of the "
                "home dates. The version of this worth acting on needs two "
                "things from you: which acts you have already used and how "
                "recently, and anything you track on how a halftime landed."
                .format(len(gaps)),
        "basis": "Stated so it is not mistaken for an absence of effect.",
        "strength": "gap"})
    return findings


def _as_candidates(events: List[Dict],
                   credited_names=()) -> List[Dict]:
    """A routing hit, shaped like an act card. One entry per ARTIST — the same
    act announced in two metros is one option, not two.

    `credited_names` are the acts already in the credit catalogue, so a routing
    hit that is ALSO a known halftime act can be marked as such. That
    intersection is the most useful thing on the page and neither column can
    show it alone.
    """
    import halftime_routing
    by_artist = {}
    for ev in events:
        key = canonical_name(ev.get("artist", ""))
        if not key:
            continue
        gap = ev.get("gap")
        when = ("same day" if gap == 0 else
                "{} day(s) {}".format(abs(gap), "before" if gap < 0 else "after")
                if isinstance(gap, int) else "date unclear")
        where = "{}{}".format(
            ev.get("venue") or ev.get("city") or ev.get("metro", ""),
            "" if not ev.get("miles") else " ({} mi)".format(ev["miles"]))
        cand = {"name": ev.get("artist"),
                "fields": {"clients": "", "category": "touring",
                           "home_base": ev.get("city", ""),
                           # S141: was hard-coded "" — so even after the sweep
                           # started extracting a style, the page would have
                           # thrown it away. Both halves had to change: the
                           # routing prompt did not ask, and this did not read.
                           "style": ev.get("style") or "",
                           "routing": "{} — {}, {}".format(
                               ev.get("date"), where, when)},
                "also_known_as": []}
        draw = halftime_routing.draw_signal(ev, credited_names)
        cand["draw"] = draw
        cand["badges"] = badges_for(cand)
        cand["badges"].append(
            {"kind": "routing", "label": when,
             "why": "{} at {}".format(ev.get("date"), where)})
        cand["badges"].append(
            {"kind": "draw" if draw["plausible"] else "draw-small",
             "label": draw["label"], "why": draw["why"]})
        cand["reach"] = {"state": "unknown",
                         "why": "Announced show near this date."}
        prev = by_artist.get(key)
        # Prefer the closest date, then the nearest metro.
        if prev is None or abs(ev.get("gap") or 99) < abs(
                prev.get("_gap") or 99):
            cand["_gap"] = ev.get("gap")
            by_artist[key] = cand
    # Draw first, date second. An arena act four days out is a better lead
    # than a 300-capacity club act on the day. Justin's Steelers-connected list
    # ahead of both (R35): "Dan + Shay playing the same venue two days before
    # our game, combined with Dan being a Steelers fan" is his own example of
    # the signal worth leading with.
    return sorted(by_artist.values(),
                  key=lambda c: (
                      0 if fan_entry(c["name"]) else 1,
                      halftime_routing.DRAW_ORDER.get(
                          (c.get("draw") or {}).get("tier"), 4),
                      abs(c.get("_gap") or 99), c["name"]))


# ── render ──────────────────────────────────────────────────────────────────

def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _empty_message(cov: Dict) -> str:
    """The three empties, worded so they cannot be mistaken for each other."""
    state = cov.get("state")
    if state == NOT_APPLICABLE:
        return ("<p class='empty na'><strong>Not applicable.</strong> "
                + _e(cov.get("note") or "") + "</p>")
    if state == NOT_SWEPT:
        return ("<p class='empty not-swept'><strong>Not searched yet.</strong> "
                + _e(cov.get("note") or "") + "</p>")
    if state == FAILED:
        return ("<p class='empty failed'><strong>Search failed.</strong> "
                + _e(cov.get("note") or "") + "</p>")
    return ("<p class='empty none'><strong>None available.</strong> "
            "We searched and nothing cleared the bar. "
            + _e(cov.get("note") or "") + "</p>")


def _act_card(act: Dict) -> str:
    fields = act.get("fields") or {}
    bits = ["<li class='act'>", "<div class='act-name'>",
            _e(act.get("name")), "</div>"]
    if act.get("client_note"):
        bits.append("<div class='client-note'>{}</div>".format(
            _e(act["client_note"])))
    if act.get("theme_fit"):
        bits.append("<div class='cleared'>Fits the brief: {}</div>".format(
            _e(act["theme_fit"])))
    d = act.get("draw") or {}
    if d.get("why"):
        bits.append("<div class='{}'>{}</div>".format(
            "cleared" if d.get("plausible") else "reach", _e(d["why"])))
    r = act.get("reach") or {}
    if r.get("state") in ("marquee_only", "home_market"):
        bits.append("<div class='reach'>{}</div>".format(_e(r.get("why"))))
    elif r.get("state") == "our_market":
        bits.append("<div class='cleared'>{}</div>".format(_e(r.get("why"))))
    if act.get("also_known_as"):
        bits.append("<div class='aka'>also listed as: {}</div>".format(
            _e(", ".join(act["also_known_as"]))))
    v = act.get("verdict") or {}
    if v.get("rule") and v.get("state") == "admitted":
        bits.append("<div class='cleared'>Clears your {}: {}</div>".format(
            _e(v["rule"]), _e(v["why"])))
    if act.get("badges"):
        bits.append("<div class='badges'>")
        for b in act["badges"]:
            bits.append("<span class='badge b-{}' title='{}'>{}</span>".format(
                _e(b["kind"]), _e(b["why"]), _e(b["label"])))
        bits.append("</div>")
    # S141: STYLE leads the rows. It is Justin's scoring criterion #3 -- "style
    # of music fit for a family/TV stadium crowd" -- and it was on the page
    # NOWHERE, for either pool: the snapshot carried 29 styles across 9
    # categories and the renderer had no row for them. The cost is worst on the
    # date he cares most about: the 1 November card, whose brief is a
    # military/patriotic tie for a 45+ daytime crowd, led with "Forbidden",
    # "With A Vengeance" and "The Amity Affliction" -- thrash and metalcore --
    # with nothing on the page to say so. First, because it is the field a
    # booker rejects on before reading anything else.
    for label, key in (("Style", "style"),
                       ("Playing", "routing"), ("Fee", "fee_note"),
                       ("Base", "home_base"),
                       ("Credits", "clients"),
                       ("Booking", "booking_contact")):
        val = (fields.get(key) or "").strip()
        if val:
            bits.append("<div class='row'><span class='k'>{}</span>"
                        "<span class='v'>{}</span></div>".format(
                            _e(label), _e(val)))
    bits.append("</li>")
    return "".join(bits)


def _coverage_line(cov: Dict) -> str:
    if cov.get("state") == NOT_APPLICABLE:
        return "<div class='cov cov-none'>not applicable to this game</div>"
    if cov.get("state") == NOT_SWEPT:
        return "<div class='cov cov-none'>no sweep has run</div>"
    return "<div class='cov'>{} · {} found · {}</div>".format(
        _e(cov.get("sources") or "—"), _e(cov.get("found", 0)),
        _e((cov.get("swept_at") or "")[:10]))


def _game_heading(g: Dict) -> str:
    meta = [g.get("slot")]
    if g.get("kick_et"):
        meta.append(g["kick_et"] + " ET")
    return ("<h2><span class='wk'>Wk {}</span> {} <span class='opp'>vs {}"
            "</span></h2><p class='meta'>{}</p>".format(
                _e(g["week"]), _e(g.get("date") or "date TBD"),
                _e(g["opponent"]), _e(" · ".join(m for m in meta if m))))


def _short_list(acts: List[Dict], detail) -> str:
    """Name plus one line -- for lists a booker scans, not reads."""
    out = ["<ul class='acts'>"]
    for a in acts:
        line = detail(a)
        out.append("<li class='act'><div class='act-name'>{}</div>{}</li>".format(
            _e(a.get("name")),
            "<div class='row'><span class='v'>{}</span></div>".format(_e(line))
            if line else ""))
    out.append("</ul>")
    return "".join(out)


def _style_and_place(a: Dict) -> str:
    f = a.get("fields") or {}
    return " · ".join(x for x in (f.get("style"), f.get("routing")) if x)


# R36-R38: what was set aside, and why -- collapsed, never deleted. An empty
# main list on a themed date is only honest if the reader can open this and
# see what was found and why it is not being recommended.
_NO_FIT_LABEL = {"salute_to_service": "no tie to Salute to Service on record",
                 "heritage": "no Pittsburgh / local connection on record",
                 "alumni_90s": "did not break through in the '90s"}


def _aside_html(aside: Dict, theme: Optional[str]) -> str:
    out = []
    if aside.get("ruled_out"):
        out.append("<details class='aside ruled'><summary>{} ruled out by you"
                   "</summary>{}</details>".format(
                       len(aside["ruled_out"]),
                       _short_list(aside["ruled_out"],
                                   lambda a: a.get("client_note", ""))))
    if aside.get("no_fit"):
        out.append("<details class='aside'><summary>{} set aside — {}"
                   "</summary>{}</details>".format(
                       len(aside["no_fit"]),
                       _e(_NO_FIT_LABEL.get(theme, "no tie to the brief")),
                       _short_list(aside["no_fit"], _style_and_place)))
    if aside.get("unknown"):
        out.append("<details class='aside'><summary>{} not checked yet — "
                   "decade not recorded, so not ruled out</summary>{}"
                   "</details>".format(
                       len(aside["unknown"]),
                       _short_list(aside["unknown"], _style_and_place)))
    return "".join(out)


def _held_html(held: List[Dict], lead: str) -> str:
    return ("<details class='held'><summary>{}</summary>{}</details>".format(
        _e(lead), _short_list(held, lambda h: (h.get("verdict") or {}).get(
            "why", ""))))


def _pool_panel(g: Dict, pool: str) -> str:
    cov = g["coverage"].get(pool)
    acts = list(g["candidates"].get(pool) or [])
    aside = (g.get("set_aside") or {}).get(pool) or _empty_aside()
    held = (g.get("held") or {}).get(pool) or []
    parts = ["<div class='pool pool-{}'>".format(_e(pool)),
             "<h3>{}</h3>".format(_e(POOL_LABEL[pool])),
             "<p class='poolsub'>{}</p>".format(_e(POOL_SUB[pool]))]
    if pool == "fans":
        parts.append("<div class='cov'>your list · {} fit this brief · where "
                     "they are playing is not checked yet</div>".format(
                         len(acts)))
    else:
        parts.append(_coverage_line(cov))
    # The credit pool has no per-game signal — the same acts rank the same way
    # on every date — so repeating the top three under all nine games said
    # nothing and put Bret Michaels, who Justin told us they have booked "many
    # times", at the top of six cards. Show it per game only where the game's
    # own target actually reorders it; elsewhere point at the one list.
    pointed = pool == "for_hire" and bool(acts) and not g.get("target")
    if pointed:
        parts.append("<p class='more'>{} acts in the credit list, and it "
                     "applies to every date equally — so it is listed once "
                     "below rather than repeated here.</p>".format(len(acts)))
    elif acts:
        shown = acts[:PER_GAME_SHOWN]
        parts.append("<ul class='acts'>")
        parts.extend(_act_card(a) for a in shown)
        parts.append("</ul>")
        rest = acts[len(shown):]
        if rest and pool == "for_hire":
            parts.append("<p class='more'>+{} more in the credit list below — "
                         "these {} rank highest for this game.</p>".format(
                             len(rest), len(shown)))
        elif rest:
            # S270: these used to be pointed at "the credit list below", which
            # never contains a routing hit -- the rest of this column was
            # simply unreachable. They open here instead.
            parts.append("<details class='rest'><summary>{} more for this "
                         "date</summary><ul class='acts'>{}</ul></details>"
                         .format(len(rest),
                                 "".join(_act_card(a) for a in rest)))
    elif any(aside.values()):
        n = sum(len(v) for v in aside.values())
        parts.append("<p class='empty none'><strong>Nothing here fits the "
                     "brief yet.</strong> {} found and set aside below, each "
                     "with the reason.</p>".format(n))
    else:
        parts.append(_empty_message(cov) if cov else
                     "<p class='empty none'><strong>None on your list fit "
                     "this brief.</strong></p>")
    parts.append(_aside_html(aside, g.get("theme")))
    if held:
        parts.append(_held_html(held, "{} held by your rules".format(len(held))))
    parts.append("</div>")
    return "".join(parts)


def render_html(snap: Dict) -> str:
    parts = [_HEAD.format(team=_e(snap["team"]), season=_e(snap["season"]))]
    parts.append(
        "<header><h1>{} · {} home slate</h1>"
        "<p class='sub'>{} · every home game, both supply pools. "
        "Generated {}.</p></header>".format(
            _e(snap["team"]), _e(snap["season"]), _e(snap["venue"]),
            _e(snap["generated_at"])))

    live = [g for g in snap["games"] if not g.get("completed")]
    done = [g for g in snap["games"] if g.get("completed")]

    targets = [g for g in live if g.get("target")]
    if targets:
        parts.append("<section class='targets'><h2>Your dates</h2><ul>")
        for g in targets:
            parts.append("<li><strong>{}</strong> vs {} — {}</li>".format(
                _e(g.get("date") or "date TBD"), _e(g["opponent"]),
                _e(g["target"])))
        parts.append("</ul></section>")

    for g in live:
        cls = "game" + (" is-target" if g.get("target") else "") + \
              ("" if g.get("at_venue", True) else " off-site") + \
              (" no-act" if g.get("mode") == "no_act" else "")
        parts.append("<section class='{}' id='{}'>".format(
            cls, _e(g.get("game_id", ""))))
        parts.append(_game_heading(g))
        if g.get("target"):
            parts.append("<p class='target'>{}</p>".format(_e(g["target"])))
        if g.get("brief"):
            parts.append("<p class='brief'><span class='k'>Your brief</span> "
                         "{}</p>".format(_e(g["brief"])))
        if g.get("note"):
            parts.append("<p class='note'>{}</p>".format(_e(g["note"])))
        # R31: no act wanted -- say so in one line, not two empty panels.
        if g.get("mode") == "no_act":
            parts.append("</section>")
            continue

        parts.append("<div class='pools'>")
        for pool in POOLS:
            parts.append(_pool_panel(g, pool))
        parts.append("</div>")
        # R35: on a date with a brief, Justin's own list gets a panel of its
        # own, under the two supply pools and filtered by the same brief.
        if g.get("theme") and g.get("at_venue", True):
            parts.append(_pool_panel(g, "fans"))
        parts.append("</section>")

    # R28: played games leave the main view. Phase 2 turns each one into a log
    # entry (what was done, final cost, Voice of the Fan rating).
    if done:
        parts.append("<section class='completed'><details><summary>"
                     "<h2>Completed games ({})</h2></summary><ul>".format(
                         len(done)))
        for g in done:
            parts.append("<li id='{}'><span class='wk'>Wk {}</span> {} vs {}"
                         "</li>".format(_e(g.get("game_id", "")),
                                        _e(g["week"]), _e(g.get("date")),
                                        _e(g["opponent"])))
        parts.append("</ul></details></section>")

    parts.append("<section class='analysis'><h2>What does well in this "
                 "market</h2>")
    for f in market_analysis(snap):
        parts.append(
            "<div class='finding f-{}'><h3>{}</h3><p>{}</p>"
            "<p class='basis'>{}</p></div>".format(
                _e(f["strength"].split()[0]), _e(f["title"]), _e(f["body"]),
                _e(f["basis"])))
    parts.append("</section>")

    roster = snap.get("roster") or []
    if roster:
        parts.append("<section class='roster'><h2>Credit list — "
                     "{} acts</h2>".format(len(roster)))
        parts.append("<p class='meta'>Every act the catalogue has found with a "
                     "sports-slot credit. Each game above shows the {} that "
                     "rank highest for it; this is the whole list.</p>".format(
                         PER_GAME_SHOWN))
        parts.append("<ul class='acts'>")
        parts.extend(_act_card(a) for a in roster)
        parts.append("</ul></section>")

    fans = snap.get("fans") or []
    if fans:
        parts.append("<section class='roster fans'><h2>Your Steelers-connected "
                     "artists — {}</h2>".format(len(fans)))
        parts.append("<p class='meta'>Your list, 23 Sep. Wherever one of these "
                     "turns up on a card above it is marked, and ranks with a "
                     "Pittsburgh tie. Where each is playing around your dates "
                     "is not checked yet — that check is in progress.</p>")
        parts.append("<ul class='acts'>")
        parts.extend(_act_card(a) for a in fans)
        parts.append("</ul></section>")

    parts.append(
        "<footer><p>Two pools, kept apart on purpose. <strong>Routing "
        "through</strong> is acts whose announced tour passes near the date — "
        "each one belongs to a single game. <strong>Has played a sports "
        "slot</strong> is a credit list, and applies to every date, which is "
        "why it is not mixed into the routing column.</p>"
        "<p><strong>Known gap.</strong> The credit list is a superset of the "
        "for-hire acts you asked about: an act that played a halftime once may "
        "be a heritage act who takes one-off bookings, or a stadium headliner "
        "who does not. Telling those apart needs current touring activity, "
        "which is the sweep still to be built — so it is stated here rather "
        "than guessed.</p>"
        "<p>Coverage is stated on every panel so an empty one can be read "
        "correctly: “none available” and “not searched yet” are different "
        "answers.</p></footer></body></html>")
    return "\n".join(parts)


_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{team} {season} — halftime candidates</title>
<style>
:root {{ --bg:#0f1216; --card:#171c22; --edge:#232b34; --ink:#e8edf2;
         --dim:#93a1b0; --gold:#ffb612; --accent:#4da3ff; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:24px; background:var(--bg); color:var(--ink);
        font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
header {{ max-width:1100px; margin:0 auto 28px; }}
h1 {{ margin:0 0 6px; font-size:26px; letter-spacing:-.02em; }}
.sub {{ margin:0; color:var(--dim); font-size:14px; }}
section {{ max-width:1100px; margin:0 auto 18px; background:var(--card);
           border:1px solid var(--edge); border-radius:12px; padding:18px 20px; }}
section.is-target {{ border-color:var(--gold); }}
section.off-site {{ opacity:.72; }}
.targets {{ background:transparent; border-style:dashed; }}
.targets ul {{ margin:8px 0 0; padding-left:20px; }}
h2 {{ margin:0 0 4px; font-size:18px; font-weight:600; }}
.wk {{ display:inline-block; min-width:52px; color:var(--gold);
       font-variant-numeric:tabular-nums; }}
.opp {{ color:var(--dim); font-weight:400; }}
.meta {{ margin:0 0 10px; color:var(--dim); font-size:13px; }}
.target {{ margin:0 0 10px; color:var(--gold); font-size:13px; font-weight:600; }}
.note {{ margin:0 0 12px; color:var(--dim); font-size:13px; font-style:italic; }}
.pools {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
@media (max-width:760px) {{ .pools {{ grid-template-columns:1fr; }} }}
.pool {{ background:#12171c; border:1px solid var(--edge); border-radius:9px;
         padding:12px 14px; }}
h3 {{ margin:0 0 6px; font-size:13px; text-transform:uppercase;
      letter-spacing:.08em; color:var(--accent); }}
.poolsub {{ margin:0 0 8px; font-size:12px; color:#7d8794; }}
.cov {{ font-size:12px; color:var(--dim); margin-bottom:10px;
        font-variant-numeric:tabular-nums; }}
.cov-none {{ color:#7d8794; font-style:italic; }}
.acts {{ list-style:none; margin:0; padding:0; }}
.act {{ padding:10px 0; border-top:1px solid var(--edge); }}
.act:first-child {{ border-top:none; }}
.act-name {{ font-weight:600; }}
.badges {{ margin:6px 0 4px; display:flex; flex-wrap:wrap; gap:5px; }}
.badge {{ font-size:11px; padding:2px 7px; border-radius:99px;
          border:1px solid var(--edge); color:var(--dim); }}
.b-market {{ border-color:var(--gold); color:var(--gold); }}
.b-patriotic {{ border-color:#7fb3ff; color:#7fb3ff; }}
.b-nostalgia {{ border-color:#c9a3ff; color:#c9a3ff; }}
.b-price {{ border-color:#79d19a; color:#79d19a; }}\n.b-routing {{ border-color:#4da3ff; color:#4da3ff; }}\n.b-draw {{ border-color:#79d19a; color:#79d19a; }}\n.b-draw-small {{ border-color:#6f7b88; color:#6f7b88; }}
.row {{ font-size:13px; color:var(--dim); margin-top:2px; }}
.k {{ display:inline-block; min-width:64px; color:#6f7b88; }}
.empty {{ margin:6px 0 0; font-size:13px; color:var(--dim); }}
.empty.not-swept strong {{ color:#e0a03a; }}
.empty.none strong {{ color:#8fa0b0; }}
.empty.failed strong {{ color:#e06a6a; }}
.empty.na strong {{ color:#6f7b88; }}
.more {{ margin:8px 0 0; font-size:12px; color:#7d8794; }}
.cleared {{ margin:5px 0 2px; font-size:12px; color:#79d19a; }}
.held {{ margin-top:10px; font-size:13px; }}
.held summary {{ cursor:pointer; color:#e0a03a; font-size:12px; }}\n.finding {{ padding:12px 0; border-top:1px solid var(--edge); }}\n.finding:first-of-type {{ border-top:none; }}\n.finding h3 {{ margin:0 0 5px; color:var(--ink); font-size:15px;\n               text-transform:none; letter-spacing:0; }}\n.finding p {{ margin:0 0 4px; }}\n.basis {{ font-size:12px; color:#6f7b88; }}\n.aka {{ font-size:12px; color:#6f7b88; font-style:italic; }}\n.reach {{ margin:5px 0 2px; font-size:12px; color:#e0a03a; }}\n.f-small .basis, .f-gap .basis {{ color:#e0a03a; }}
.brief {{ margin:0 0 12px; font-size:13px; color:var(--ink); }}
.brief .k {{ color:var(--gold); min-width:0; margin-right:6px; }}
.client-note {{ margin:5px 0 2px; font-size:12px; color:var(--gold);
                font-style:italic; }}
.b-fan {{ border-color:var(--gold); color:var(--bg); background:var(--gold); }}
section.no-act {{ opacity:.72; }}
.pool-fans {{ margin-top:16px; }}
.aside, .rest {{ margin-top:10px; font-size:13px; }}
.aside summary, .rest summary {{ cursor:pointer; color:#7d8794; font-size:12px; }}
.aside.ruled summary {{ color:var(--gold); }}
.completed summary {{ cursor:pointer; }}
.completed summary h2 {{ display:inline; }}
.completed ul {{ margin:10px 0 0; padding-left:20px; color:var(--dim); }}
.roster .acts {{ columns:2; column-gap:26px; }}
@media (max-width:760px) {{ .roster .acts {{ columns:1; }} }}
.roster .act {{ break-inside:avoid; }}
footer {{ max-width:1100px; margin:26px auto 0; color:var(--dim);
          font-size:13px; border-top:1px solid var(--edge); padding-top:14px; }}
</style></head><body>"""


def build(out_dir: Optional[Path] = None,
          db_path: Optional[str] = None) -> Dict:
    out_dir = Path(out_dir) if out_dir else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    snap = build_snapshot(db_path=db_path)
    (out_dir / "snapshot.json").write_text(json.dumps(snap, indent=2))
    (out_dir / "index.html").write_text(render_html(snap))
    return {"games": len(snap["games"]), "acts": snap["acts_total"],
            "out": str(out_dir)}


# ── selftest ────────────────────────────────────────────────────────────────

def selftest() -> int:
    import tempfile
    failures = []

    def check(label, ok):
        print(("  PASS  " if ok else "  FAIL  ") + label)
        if not ok:
            failures.append(label)

    # S270: every snapshot below is built as of a FIXED day before the season.
    # Played games now leave the main view (R28), so a suite on the real
    # calendar would lose its Week 8 checks on 2 November -- a time bomb.
    _T = "2026-09-01"

    check("every home game has a stable id",
          len({game_id(g) for g in HOME_GAMES}) == len(HOME_GAMES))
    check("the whole home slate is present, not just the target dates",
          len(HOME_GAMES) == 8)
    check("the Paris game is GONE (Justin, 23 Sep: remove it)",
          not any("Saints" in g["opponent"] for g in HOME_GAMES))
    check("every date Justin gave a brief is flagged",
          {g["date"] for g in HOME_GAMES if g.get("target")}
          == {"2026-11-01", "2026-11-27", "2026-12-06", "2026-12-20"})
    check("each themed date carries his brief in words",
          all(g.get("brief") for g in HOME_GAMES if g.get("theme")))
    check("Carolina needs no act, and says so",
          next(g for g in HOME_GAMES if "Panthers" in g["opponent"])
          .get("mode") == "no_act")
    check("no game invents a date the league has not set",
          all(g.get("date") or g.get("note") for g in HOME_GAMES))

    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "kb.sqlite3")
        # T32/S97: build_snapshot() falls back to the LIVE ROUTING_PATH when no
        # routing_path is given, so these cases were reading whatever the last
        # real sweep wrote. "the touring pool is EMPTY" passed only for as long
        # as no sweep had ever run; the 2026-09-02 sweep (53 events across 7
        # games) made week 8 genuinely swept and turned it red. It went unseen
        # because the runner piped the suite into `tail` and exited with TAIL's
        # status (T64), reporting PASS for a red suite.
        # A path inside the tmpdir that is never created is the honest stand-in
        # for "no sweep has ever run" -- _load_routing returns {} for it.
        no_routing = Path(tmp) / "never-swept.json"
        entity_kb.upsert_entity(
            KB_PROJECT, "test-patriot", "Test Patriot Band",
            entity_type="halftime_act", db_path=db,
            fields={"pool": "for_hire_music", "category": "military / patriotic",
                    "home_base": "Pittsburgh, PA", "fee_note": "$20,000 all-in",
                    "clients": "Some Bowl", "booking_contact": ""})
        entity_kb.upsert_entity(
            KB_PROJECT, "test-variety", "Test Dog Show",
            entity_type="halftime_act", db_path=db,
            fields={"pool": "variety", "category": "dog show"})
        snap = build_snapshot(today=_T, db_path=db, routing_path=no_routing)

        check("a snapshot covers every game",
              len(snap["games"]) == len(HOME_GAMES))
        check("the variety pool does NOT leak into the for-hire dashboard",
              snap["acts_total"] == 1)

        wk8 = next(g for g in snap["games"] if g["week"] == 8)
        check("a for-hire act appears against a real home game",
              [a["name"] for a in wk8["candidates"]["for_hire"]]
              == ["Test Patriot Band"])
        check("the touring pool is EMPTY and says why, rather than looking searched",
              wk8["coverage"]["touring"]["state"] == NOT_SWEPT)
        check("for-hire acts are offered on every un-themed game, not just "
              "targets",
              all(g["candidates"]["for_hire"] for g in snap["games"]
                  if not g.get("theme") and g.get("mode") != "no_act"))
        # The off-site path is kept for the next international game, and tested
        # on a synthetic one now that Paris is off the real slate.
        _offsite = {"week": 7, "date": None, "opponent": "Test Saints",
                    "kick_et": None, "slot": "international",
                    "at_venue": False, "note": "Staged abroad."}
        osnap = build_snapshot(today=_T, db_path=db, routing_path=no_routing,
                               games=HOME_GAMES + [_offsite])
        check("an off-site game is not offered for-hire candidates",
              next(g for g in osnap["games"]
                   if g["week"] == 7)["candidates"]["for_hire"] == [])

        badges = {b["kind"] for b in wk8["candidates"]["for_hire"][0]["badges"]}
        check("badges are independent claims, not one score",
              {"patriotic", "market", "price", "credit"} <= badges)

        page = render_html(osnap)
        check("each kind of empty gets its OWN wording on the page",
              all(w in page for w in ("Not searched yet", "Not applicable"))
              and page.count("class='empty not-swept'") > 0
              and page.count("class='empty na'") > 0)
        check("an empty panel never renders as a bare blank",
              page.count("<div class='pool pool-touring'>") == sum(
                  1 for _g in osnap["games"] if _g.get("mode") != "no_act")
              and "class='empty" in page)
        check("an unsearched pool never reads as 'nothing out there'",
              "NOT a finding" in json.dumps(snap))
        check("every game reaches the page",
              all(_e(g["opponent"]) in page for g in snap["games"]))
        check("the target dates are called out on the page",
              "Your dates" in page)
        check("the credit pool does NOT claim the acts are available",
              "Available to book" not in page
              and "not a verified availability list" in page)
        check("the page states the known gap rather than hiding it",
              "Known gap" in page)
        check("no unescaped act name can inject markup",
              "<script>" not in render_html(_snap_with_name(snap, "<script>x")))

        res = build(out_dir=Path(tmp) / "out", db_path=db)
        # R5, pinned: a per-game column must stay a shortlist. This regressed
        # once already -- 11 acts under 9 games rendered 99 cards.
        many = json.loads(json.dumps(snap))
        for _g in many["games"]:
            if _g["candidates"]["for_hire"]:
                _a = _g["candidates"]["for_hire"][0]
                _g["candidates"]["for_hire"] = [
                    dict(_a, name="Act {}".format(i)) for i in range(9)]
        big = render_html(many)
        check("a game column shows a few options, not the whole roster",
              big.count("Act 0") <= len(many["games"]) + 1
              and "+6 more in the credit list" in big)
        check("the full roster still appears exactly once",
              big.count("Credit list —") == 1)

        # --- routing feeds the touring column
        # S270: on Week 5, an un-themed date. Week 8 is Salute to Service now,
        # and its brief would set these style-less test acts aside. ---------------------------
        import tempfile as _tf
        rt = Path(tmp) / "routing.json"
        gid8 = game_id(next(g for g in HOME_GAMES if g["week"] == 5))
        rt.write_text(json.dumps({
            "generated_at": "2026-08-26T00:00:00Z", "window_days": 3,
            "games": {gid8: {
                "events": [
                    {"artist": "Near Act", "date": "2026-10-10",
                     "venue": "Stage AE", "city": "Pittsburgh, PA",
                     "metro": "Pittsburgh, PA", "miles": 0, "gap": -1},
                    {"artist": "Near Act", "date": "2026-10-13",
                     "venue": "Rocket", "city": "Cleveland, OH",
                     "metro": "Cleveland, OH", "miles": 135, "gap": 2}],
                "coverage": [
                    {"metro": "Pittsburgh, PA", "miles": 0, "sources": 2,
                     "found": 1, "error": None,
                     "swept_at": "2026-08-26T00:00:00Z"},
                    {"metro": "Cleveland, OH", "miles": 135, "sources": 1,
                     "found": 1, "error": None,
                     "swept_at": "2026-08-26T00:00:00Z"}]}}}))
        rsnap = build_snapshot(today=_T, db_path=db, routing_path=rt)
        w8 = next(g for g in rsnap["games"] if g["week"] == 5)
        w1 = next(g for g in rsnap["games"] if g["week"] == 1)
        check("a swept game shows its routing hits",
              [c["name"] for c in w8["candidates"]["touring"]] == ["Near Act"])
        check("one artist in two metros is ONE option, not two",
              len(w8["candidates"]["touring"]) == 1)
        check("...and it keeps the CLOSEST date",
              "2026-10-10" in w8["candidates"]["touring"][0]["fields"]["routing"])
        check("a swept game reports coverage, not 'not searched'",
              w8["coverage"]["touring"]["state"] == SWEPT)
        check("an UNSWEPT game still says nobody looked",
              w1["coverage"]["touring"]["state"] == NOT_SWEPT)
        check("a sweep where every metro failed is FAILED, not empty",
              build_snapshot(today=_T, db_path=db, routing_path=_fail_routing(
                  Path(tmp), gid8))["games"][0] is not None
              and next(g for g in build_snapshot(
                  today=_T, db_path=db, routing_path=_fail_routing(Path(tmp), gid8)
              )["games"] if g["week"] == 5
              )["coverage"]["touring"]["state"] == FAILED)
        rpage3 = render_html(rsnap)
        check("the routing hit and its gap reach the page",
              "Near Act" in rpage3 and "1 day(s) before" in rpage3)

        # --- draw ordering in the touring column ------------------------
        rt2 = Path(tmp) / "routing-draw.json"
        rt2.write_text(json.dumps({
            "generated_at": "2026-08-26T00:00:00Z", "window_days": 3,
            "games": {gid8: {"events": [
                {"artist": "Club Act", "date": "2026-10-11",
                 "venue": "Rumba Cafe", "city": "Columbus, OH",
                 "metro": "Columbus, OH", "miles": 185, "gap": 0},
                {"artist": "Arena Act", "date": "2026-10-14",
                 "venue": "PPG Paints Arena", "city": "Pittsburgh, PA",
                 "metro": "Pittsburgh, PA", "miles": 0, "gap": 3},
                {"artist": "Test Patriot Band", "date": "2026-10-12",
                 "venue": "Rumba Cafe", "city": "Columbus, OH",
                 "metro": "Columbus, OH", "miles": 185, "gap": 1}],
                "coverage": [{"metro": "Pittsburgh, PA", "miles": 0,
                              "sources": 2, "found": 3, "error": None,
                              "swept_at": "2026-08-26T00:00:00Z"}]}}}))
        dsnap = build_snapshot(today=_T, db_path=db, routing_path=rt2)
        d8 = next(g for g in dsnap["games"] if g["week"] == 5)
        order = [c["name"] for c in d8["candidates"]["touring"]]
        check("an act in BOTH pools leads the touring column",
              order[0] == "Test Patriot Band")
        check("an arena act four days out beats a club act on the day",
              order.index("Arena Act") < order.index("Club Act"))
        check("the club act is still listed, not filtered away",
              "Club Act" in order)
        dpage = render_html(dsnap)
        # the repetition Justin's tone note made expensive
        tpage = render_html(snap)
        _names = [a["name"] for g_ in snap["games"]
                  for a in g_["candidates"]["for_hire"]]
        if _names:
            _lead = _names[0]
            # One per dated brief (each reorders the list for its own reason)
            # plus the roster -- never once per game, which was nine.
            check("a credit act is not repeated under every game",
                  tpage.count(">" + _e(_lead) + "<")
                  <= sum(1 for g_ in HOME_GAMES if g_.get("target")) + 1)
        check("a game with no target points at the one list instead",
              "applies to every date equally" in tpage)
        check("a TARGET game still shows its ranked credit acts",
              any(g_["candidates"]["for_hire"] for g_ in snap["games"]
                  if g_.get("target")))

        check("the cross-pool act says why it is top",
              "Both pools at once" in dpage)
        # Assert the LABEL and the reassurance, not the exact prose. This
        # check broke silently when halftime_routing reworded "well below
        # stadium draw" to add the sizes-the-show caveat, and it went unnoticed
        # because only the changed module's suite was re-run (T38: do not pin
        # wording a neighbouring module owns).
        check("the small room is explained, not silently ranked down",
              "Club-scale room" in dpage and "genuinely in the area" in dpage)

        # --- reachability: the credit-list problem -----------------------
        def _rc(name, clients, cat="other music", home=""):
            return {"name": name, "fields": {"clients": clients,
                                             "category": cat,
                                             "home_base": home}}

        check("a Pittsburgh club credit is the strongest state",
              reachability(_rc("B", "Pittsburgh Steelers"))["state"]
              == "our_market")
        check("a Super Bowl credit is NOT evidence of club availability",
              reachability(_rc("G", "Super Bowl LX 2026"))["state"]
              == "marquee_only")
        check("an NBA All-Star credit is treated the same way",
              reachability(_rc("L", "NBA All-Star Game 2026"))["state"]
              == "marquee_only")
        check("a hometown act playing its OWN club is demoted, with the reason",
              reachability(_rc("E", "Detroit Lions Thanksgiving Classic",
                               home="Detroit, MI"))["state"] == "home_market")
        check("...and the reason names the league's hometown pattern",
              "hometown pattern" in reachability(
                  _rc("E", "Detroit Lions", home="Detroit, MI"))["why"])
        check("a club credit outside the home market counts as travelling",
              reachability(_rc("X", "New York Giants",
                               home="Nashville, TN"))["state"] == "travels")
        check("a service band travels even in its own state",
              reachability(_rc("N", "Washington Commanders",
                               cat="military / patriotic",
                               home="Washington, DC"))["state"] == "travels")
        check("no credit reads as unknown, never as unreachable",
              reachability(_rc("Q", ""))["state"] == "unknown")
        styx = reachability(_rc("Styx", "Pittsburgh Steelers",
                                cat="team tradition"))
        check("a team-tradition act ranks as our-market",
              styx["state"] == "our_market")
        check("...but is NOT described as having played for the team",
              "played" not in styx["why"].lower()
              and "the tie is the song" in styx["why"])
        check("a genuine Pittsburgh performer IS described as having played",
              "Has played" in reachability(
                  _rc("BM", "Pittsburgh Steelers",
                      cat="nostalgia music"))["why"])

        reach_pool = [
            dict(_rc("Marquee Act", "Super Bowl LX 2026"), badges=[]),
            dict(_rc("Steelers Act", "Pittsburgh Steelers"), badges=[]),
        ]
        for a in reach_pool:
            a["reach"] = reachability(a)
        ordered = rank_for_game(HOME_GAMES[0], reach_pool)
        check("a marquee-only act does NOT outrank a Pittsburgh credit",
              ordered[0]["name"] == "Steelers Act")
        rpage2 = render_html(build_snapshot(today=_T, db_path=db,
                                            routing_path=no_routing))
        check("reachability is EXPLAINED on the page, not applied invisibly",
              "reach" in rpage2 or "strongest evidence available" in rpage2)

        # --- ranking ---------------------------------------------------
        def _ra(name, cat, home=""):
            a = {"name": name, "fields": {"category": cat, "home_base": home,
                                          "clients": "Some Team", "style": ""}}
            a["badges"] = badges_for(a)
            return a

        army = _ra("122nd Army Band", "military / patriotic")
        rocker2 = _ra("Zed Rock Act", "classic rock")
        local = _ra("Zeta Local Act", "regional / market", "Pittsburgh, PA")
        plain = _ra("Aaa Plain Act", "other music")
        pool = [army, rocker2, local, plain]

        sept = next(g for g in HOME_GAMES if g["week"] == 1)
        nov = next(g for g in HOME_GAMES if g["week"] == 8)
        check("on a patriotic date the military act LEADS",
              rank_for_game(nov, pool)[0]["name"] == "122nd Army Band")
        check("on an ordinary date it does NOT lead, despite sorting first "
              "alphabetically",
              rank_for_game(sept, pool)[0]["name"] != "122nd Army Band")
        check("on an ordinary date the Pittsburgh tie leads",
              rank_for_game(sept, pool)[0]["name"] == "Zeta Local Act")
        check("nostalgia fit outranks an unbadged act",
              rank_for_game(sept, pool).index(rocker2)
              < rank_for_game(sept, pool).index(plain))
        check("ranking never drops an act, it only orders them",
              len(rank_for_game(sept, pool)) == len(pool))

        # --- name variants ---------------------------------------------
        check("a trailing parenthetical is the same act",
              canonical_name("Eminem (featuring Jack White)")
              == canonical_name("Eminem"))
        check("a service rank is the same person",
              canonical_name("First Class Nathaniel Buttram")
              == canonical_name("Nathaniel Buttram"))
        check("the full Navy rate is the same person too",
              canonical_name("Musician First Class Nathaniel Buttram")
              == canonical_name("First Class Nathaniel Buttram"))
        check("a leading 'The' does not create a second act",
              canonical_name("The Roots") == canonical_name("Roots"))
        check("two genuinely different acts are NOT merged",
              canonical_name("Run-DMC") != canonical_name("Rev Run"))
        check("a substring is not enough to merge",
              canonical_name("Jack White") != canonical_name("Jack White Band"))
        dd = dedupe_acts([
            {"name": "Eminem", "fields": {"style": "hip hop / rap"}},
            {"name": "Eminem (featuring Jack White)",
             "fields": {"style": "hip hop / rap", "clients": "Lions",
                        "home_base": "Detroit"}}])
        # ── S141: style must actually REACH the page ────────────────────────
        # It was absent for both pools and for three separate reasons, each of
        # which alone was enough to hide it: the routing sweep never asked for
        # it, this file hard-coded "" when building a touring candidate, and
        # _act_card had no row for it at all. The snapshot carried 29 styles
        # across 9 categories and the rendered page showed none of them.
        _styled = _act_card({"name": "X", "fields": {"style": "classic rock",
                                                     "home_base": "Pittsburgh"}})
        check("style: a card RENDERS the style row (it had none, for either pool)",
              "classic rock" in _styled and ">Style<" in _styled)
        check("style: ...and it leads, because it is what a booker rejects on "
              "before reading anything else",
              _styled.index("Style") < _styled.index("Base"))
        check("style: an act with no style shows no empty row",
              "Style" not in _act_card({"name": "X",
                                        "fields": {"home_base": "Pittsburgh"}}))
        # ...and the same, one layer down, on the path that actually BUILDS a
        # touring candidate. The card check above passes even when this layer
        # throws the style away -- which is precisely the bug that was here, so
        # a check that cannot see it is not the check this needs.
        _ev = {"artist": "A", "date": "2026-11-01", "venue": "V",
               "city": "Pittsburgh, PA", "miles": 10, "style": "country"}
        _tc = _as_candidates([_ev], set())
        check("style: a TOURING candidate carries the sweep's style through",
              _tc and (_tc[0]["fields"] or {}).get("style") == "country")
        check("style: ...and reaches the rendered card",
              _tc and "country" in _act_card(_tc[0]))

        check("a duplicate collapses to one entry",
              len(dd) == 1)
        check("...keeping the richer record",
              (dd[0]["fields"] or {}).get("clients") == "Lions")
        check("...and the dropped variant is SHOWN, not silently lost",
              dd[0]["also_known_as"] == ["Eminem"])

        # --- R12 the rap rule -----------------------------------------
        def _act(name, style="", cat="other music", home="", badges=()):
            a = {"name": name, "fields": {"style": style, "category": cat,
                                          "home_base": home, "clients": ""}}
            a["badges"] = badges_for(a) if not badges else list(badges)
            return a

        wiz = _act("Wiz-like", "hip hop / rap", "regional / market",
                   "Pittsburgh, PA")
        snoop = _act("Snoop-like", "hip hop / rap", "nostalgia music")
        other = _act("Unconnected Rapper", "hip hop / rap", "other music")
        rocker = _act("Rock Act", "rock", "classic rock")
        silent = _act("Style Unknown", "", "other music")

        check("hip-hop with a Pittsburgh tie is ADMITTED",
              rule_verdict(wiz)["state"] == "admitted")
        check("...and the card says WHICH door it came through",
              "Pittsburgh" in rule_verdict(wiz)["why"])
        check("hip-hop as a nostalgia play is ADMITTED",
              rule_verdict(snoop)["state"] == "admitted"
              and "Nostalgia" in rule_verdict(snoop)["why"])
        check("hip-hop with neither is HELD",
              rule_verdict(other)["state"] == "held")
        check("a non-hip-hop act is untouched by the rule",
              rule_verdict(rocker)["state"] == "admitted"
              and rule_verdict(rocker)["rule"] is None)
        check("the rule FAILS OPEN on an unstated style, never on a guess",
              rule_verdict(silent)["state"] == "admitted"
              and rule_verdict(silent)["style_source"] == "unknown")
        adm, held = apply_rules([wiz, snoop, other, rocker])
        check("a held act is separated, never discarded",
              len(adm) == 3 and [h["name"] for h in held]
              == ["Unconnected Rapper"])

        ruled = json.loads(json.dumps(snap))
        for _g in ruled["games"]:
            if _g.get("at_venue", True):
                _g["candidates"]["for_hire"] = [dict(other, verdict=None)]
                _g["held"]["for_hire"] = [
                    dict(other, verdict=rule_verdict(other))]
        rpage = render_html(ruled)
        check("a held act is VISIBLE on the page with its reason",
              "held by your rules" in rpage
              and "no Pittsburgh tie" in rpage)

        # --- S270: Justin's 23 Sep brief --------------------------------
        def _th(name, style="", cat="other music", home="", era=""):
            a = {"name": name, "fields": {"style": style, "category": cat,
                                          "home_base": home, "clients": "",
                                          "era": era}}
            a["badges"] = badges_for(a)
            a["reach"] = reachability(a)
            return a

        check("brief: a military act fits Salute to Service",
              theme_fit(_th("B", cat="military / patriotic"),
                        "salute_to_service")[0] == FIT)
        check("brief: ...and so does a country act (his words)",
              theme_fit(_th("C", "country"), "salute_to_service")[0] == FIT)
        check("brief: an unrelated pop act is NOT surfaced for it",
              theme_fit(_th("P", "pop"), "salute_to_service")[0] == NO_FIT)
        check("brief: Heritage keeps a Pittsburgh tie",
              theme_fit(_th("L", home="Pittsburgh, PA"), "heritage")[0] == FIT)
        check("brief: Heritage keeps his Steelers-connected list",
              theme_fit(_th("The Clarks"), "heritage")[0] == FIT)
        check("brief: Heritage sets aside an act with no local tie",
              theme_fit(_th("Z", "rock", home="Nashville, TN"),
                        "heritage")[0] == NO_FIT)
        check("brief: a '90s act fits Alumni Weekend",
              theme_fit(_th("H", era="1990s"), "alumni_90s")[0] == FIT)
        check("brief: an '80s act does not",
              theme_fit(_th("E", era="1980s"), "alumni_90s")[0] == NO_FIT)
        check("brief: NO recorded era is unknown -- never 'not a 90s act'",
              theme_fit(_th("U"), "alumni_90s")[0] == UNKNOWN_FIT)
        check("brief: an un-themed date takes everyone",
              theme_fit(_th("P", "pop"), None)[0] == FIT)

        check("fans: 'Dan + Shay' is on his list",
              fan_entry("Dan + Shay") is not None)
        check("fans: ...and 'Dan & Shay' is the same act",
              fan_entry("Dan & Shay") is not None)
        check("fans: 'Train' reaches Pat Monahan through the aka",
              (fan_entry("Train") or {}).get("name") == "Pat Monahan")
        check("fans: a PART of a name is not a match",
              fan_entry("Dan") is None and fan_entry("Root") is None)
        check("fans: all 28 of his names are carried",
              len(STEELERS_CONNECTED) == 28)
        _fk = {b["kind"] for b in _th("Styx").get("badges", [])}
        check("fans: a listed act carries the badge", "fan" in _fk)
        _ft = _as_candidates([
            {"artist": "Arena Act", "date": "2026-10-10",
             "venue": "PPG Paints Arena", "city": "Pittsburgh, PA", "miles": 0,
             "gap": -1},
            {"artist": "Dan + Shay", "date": "2026-10-08",
             "venue": "Schottenstein Center", "city": "Columbus, OH",
             "miles": 185, "gap": -3}], set())
        check("fans: his listed act LEADS the touring column (his Dan + Shay "
              "example)", _ft[0]["name"] == "Dan + Shay")
        check("fans: a listed hip-hop act is not held by his own rap rule",
              rule_verdict(_th("GloRilla", "hip hop / rap"))["state"]
              == "admitted")

        _w15 = next(g for g in HOME_GAMES if g["week"] == 15)
        _w3 = next(g for g in HOME_GAMES if g["week"] == 3)
        _tso = _th("Trans-Siberian Orchestra")
        _shown, _aside = split_for_game(_w15, [_tso, _th("Styx")])
        check("notes: TSO is ruled out on 12/20, with HIS reason",
              [a["name"] for a in _aside["ruled_out"]]
              == ["Trans-Siberian Orchestra"]
              and "performance schedule" in _aside["ruled_out"][0]
              ["client_note"])
        check("notes: Styx stays, carrying his 2027 note",
              [a["name"] for a in _shown] == ["Styx"]
              and "2027" in _shown[0]["client_note"])
        check("notes: a note for 12/20 does not leak onto another date",
              split_for_game(_w3, [_tso])[1]["ruled_out"] == [])

        csnap = build_snapshot(today="2026-09-20", db_path=db,
                               routing_path=no_routing)
        _c1 = next(g for g in csnap["games"] if g["week"] == 1)
        check("completed: a played game is marked, with no candidates",
              _c1["completed"] and not any(_c1["candidates"].values()))
        check("completed: a game on its own day is still upcoming",
              not is_completed({"date": "2026-09-27"}, "2026-09-27"))
        check("completed: an undated game never is",
              not is_completed({"date": None}, "2030-01-01"))
        cpage = render_html(csnap)
        check("completed: it moves to the collapsed section at the foot",
              "Completed games (1)" in cpage
              and "id='wk01-falcons'" in cpage
              and cpage.index("Completed games") > cpage.index("Wk 3"))
        check("completed: ...and no longer has a full card in the main view",
              "<section class='game' id='wk01-falcons'>" not in cpage)
        check("no-act: Carolina renders its note and NO empty panels",
              "No act needed" in cpage and cpage.count(
                  "<div class='pool pool-touring'>") == sum(
                      1 for g in csnap["games"] if not g["completed"]
                      and g.get("mode") != "no_act"))
        check("sweep: the default slate skips played and no-act games",
              [g["week"] for g in upcoming_games("2026-09-20")]
              == [3, 5, 8, 12, 13, 15])

        # A themed date whose touring hits all miss the brief says so, and
        # the set-aside acts are one click away rather than gone.
        _gid8 = game_id(next(g for g in HOME_GAMES if g["week"] == 8))
        _rt8 = Path(tmp) / "routing-8.json"
        _rt8.write_text(json.dumps({"window_days": 3, "games": {_gid8: {
            "events": [{"artist": "Club Pop", "date": "2026-11-01",
                        "venue": "Rumba Cafe", "city": "Columbus, OH",
                        "miles": 185, "gap": 0, "style": "pop"}],
            "coverage": [{"metro": "Columbus, OH", "miles": 185,
                          "sources": 1, "found": 1, "error": None,
                          "swept_at": "2026-08-26T00:00:00Z"}]}}}))
        t8 = render_html(build_snapshot(today=_T, db_path=db,
                                        routing_path=_rt8))
        _w8sec = t8[t8.index("id='wk08-browns'"):t8.index("id='wk12-broncos'")]
        check("themed-empty: 11/1 says nothing fits the brief yet",
              "Nothing here fits the brief yet" in _w8sec)
        check("themed-empty: ...and the pop act is set aside WITH the reason",
              "no tie to Salute to Service on record" in _w8sec
              and "Club Pop" in _w8sec)
        check("themed-empty: ...never as 'None available'",
              "None available" not in _w8sec.split("pool-for_hire")[0])
        _fans = _w8sec[_w8sec.index("pool-fans"):] if "pool-fans" in _w8sec \
            else ""
        # Everything before the set-aside lists: the 3 shown plus the rest of
        # the acts that fit, which open in place.
        _fans_main = _fans.split("<details class='aside")[0]
        check("fans panel: a themed date shows his list, filtered to the brief",
              "Trace Adkins" in _fans_main and "Snoop Dogg" not in _fans_main
              and "Snoop Dogg" in _fans)

        # S270 fix: touring overflow used to point at "the credit list below",
        # which never holds a routing hit.
        _over = json.loads(json.dumps(rsnap))
        _g5 = next(g for g in _over["games"] if g["week"] == 5)
        _g5["candidates"]["touring"] = [
            dict(_g5["candidates"]["touring"][0], name="Tour {}".format(i))
            for i in range(5)]
        _op = render_html(_over)
        check("overflow: extra routing hits open in place, not 'in the "
              "credit list'",
              "2 more for this date" in _op and "Tour 4" in _op)

        # --- R13 market analysis --------------------------------------
        mkt = market_analysis(snap)
        check("the analysis has findings, each with a stated basis",
              len(mkt) >= 5 and all(f["basis"] for f in mkt))
        check("every finding carries a number or a comparison, not a truism",
              all(any(ch.isdigit() for ch in f["body"]) for f in mkt))
        check("the small-sample claim is LABELLED as one",
              any(f["strength"] == "small sample" for f in mkt))
        check("the analysis states what it cannot tell you",
              any(f["strength"] == "gap" for f in mkt))
        check("no finding restates what a booker already knows",
              not any("each team books" in f["body"].lower() for f in mkt))
        mpage = render_html(snap)
        check("the analysis reaches the page",
              "What does well in this market" in mpage)

        # T107: an unknown argument must never reach build(). build is
        # stubbed, so if this guard regresses the check fails instead of
        # writing the live page.
        _calls = []
        _real_build = globals()["build"]
        globals()["build"] = lambda *a, **k: _calls.append(1) or {}
        try:
            import io, contextlib
            with contextlib.redirect_stderr(io.StringIO()):
                _rc = main(["--selftest"])
        finally:
            globals()["build"] = _real_build
        check("an unknown argument is refused, and does NOT build the page",
              _rc == 2 and not _calls)

        check("build writes both the snapshot and the page",
              (Path(res["out"]) / "snapshot.json").exists()
              and (Path(res["out"]) / "index.html").exists())

    print()
    if failures:
        print("FAILURES: {}".format(len(failures)))
        return 1
    print("ALL PASS")
    return 0


def _fail_routing(tmp: Path, gid: str) -> Path:
    """A sweep where every metro errored — used by the selftest."""
    path = tmp / "routing-fail.json"
    path.write_text(json.dumps({
        "generated_at": "2026-08-26T00:00:00Z", "window_days": 3,
        "games": {gid: {"events": [], "coverage": [
            {"metro": "Pittsburgh, PA", "miles": 0, "sources": 0, "found": 0,
             "error": "no fetchable source",
             "swept_at": "2026-08-26T00:00:00Z"}]}}}))
    return path


def _snap_with_name(snap: Dict, name: str) -> Dict:
    """Every name the page renders, replaced -- S270 added set-aside lists, the
    fan panel and a snapshot-level roster, and a test that only renamed the
    old for-hire cards would pass without ever reaching them."""
    clone = json.loads(json.dumps(snap))
    for g in clone["games"]:
        for acts in list(g["candidates"].values()) + [
                v for aside in (g.get("set_aside") or {}).values()
                for v in aside.values()]:
            for a in acts:
                a["name"] = name
                a["client_note"] = name
    for a in (clone.get("roster") or []) + (clone.get("fans") or []):
        a["name"] = name
    return clone


USAGE = "usage: halftime_dashboard.py [selftest]   (no argument = build)"


def main(argv: Optional[List[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["selftest"]:
        return selftest()
    # S270 (T107 again): anything else used to fall through to build(), so a
    # checker typing `--selftest` on CUMULUS rewrote Justin's live page with
    # unreviewed code. The build is the no-argument path and nothing else.
    if args:
        print(USAGE, file=sys.stderr)
        return 2
    res = build()
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
