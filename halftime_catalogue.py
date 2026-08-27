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
import re
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


_SYSTEM_FOR_POOL = {
    "variety": _EXTRACT_SYSTEM,
    "for_hire_music": _MUSIC_SYSTEM,
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


def parse_acts(raw: str) -> list | None:
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
        acts = parse_acts(raw)
        if acts is not None:
            return acts, "ollama", False
    except Exception:
        pass          # no local model on this box, or it fell over — escalate

    try:
        provider, raw = llm_providers.escalate(
            system, user, creds, max_tokens=4000, mode="single")
        acts = parse_acts(raw)
        if acts is not None:
            return acts, provider, True
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


def run(dry_run: bool = False, angles: int = DEFAULT_ANGLES,
        refresh: int = REFRESH_CHUNK, creds: dict = None,
        db_path: str = None, pool: str = None) -> dict:
    """One pass: discover on a rotating slice of angles, then refresh known acts."""
    import cirrus_daily

    creds = creds if creds is not None else (
        json.loads((PROJECT_DIR / "config/credentials.json").read_text()))

    stats = {"angles": 0, "sources": 0, "found": 0, "new": 0, "updated": 0,
             "bands_rejected": 0, "marquee_rejected": 0,
             "escalated": 0, "local": 0}

    for pool, category, query in angles_for_today(angles, pool=pool):
        stats["angles"] += 1
        log(f"angle: {category} — {query}")
        try:
            urls = cirrus_daily.search_web(query, max_results=MAX_SEARCH_RESULTS)
        except Exception as e:
            log(f"  search failed: {e}")
            continue

        sources = []
        for url in urls:
            try:
                content, _ = cirrus_daily.fetch_article_content(url)
            except Exception:
                continue
            if content:
                sources.append((url, content[:MAX_FETCH_CHARS]))
        if not sources:
            log("  no fetchable sources")
            continue
        stats["sources"] += len(sources)

        block = "\n\n".join(f"SOURCE: {u}\n{t}" for u, t in sources)
        acts, model, escalated = extract_acts(block, creds, pool)
        if escalated:
            stats["escalated"] += 1
        elif model:
            stats["local"] += 1
        log(f"  {len(acts)} act(s) via {model or 'nothing usable'}"
            + (" [escalated]" if escalated else ""))

        for act in acts:
            if looks_like_band(act["name"]):
                stats["bands_rejected"] += 1
                continue
            if pool == "for_hire_music" and marquee_only(act.get("clients", "")):
                stats["marquee_rejected"] += 1
                log(f"    rejected (marquee-only credit): {act['name']}")
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
            elif res.get("changed_fields"):
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
    print(f"halftime act catalogue: {len(ents)} act(s)")
    by_cat, by_model = {}, {}
    for e in ents:
        st = e.get("state") or {}
        by_cat[st.get("category", "?")] = by_cat.get(st.get("category", "?"), 0) + 1
        by_model[st.get("extracted_by", "?")] = by_model.get(st.get("extracted_by", "?"), 0) + 1
    for c, n in sorted(by_cat.items(), key=lambda t: -t[1]):
        print(f"  {c:24} {n}")
    print("\n  extraction (local vs escalated — the S73 measurement):")
    for m, n in sorted(by_model.items(), key=lambda t: -t[1]):
        print(f"    {m:28} {n}")
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
    check("the variety prompt still excludes touring concerts",
          "concerts by touring musicians" in _EXTRACT_SYSTEM)
    _sample = {"name": "x", "category": "c", "level": "pro", "clients": "",
               "booking_contact": "", "fee_note": "", "home_base": "",
               "evidence": ""}
    check("pool is recorded on the entity, not just used at search time",
          _fields_for(_sample, "a", "m", False, "for_hire_music")["pool"]
          == "for_hire_music")
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
            job_status.record(
                "halftimecatalogue", True,
                f"{st.get('found', 0)} found, {st.get('new', 0)} new, "
                f"{st.get('updated', 0)} updated, {st.get('sources', 0)} sources")
        except Exception as e:
            print(f"job_status.record failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
