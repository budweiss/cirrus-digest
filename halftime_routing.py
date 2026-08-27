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
 "city": "city, state if stated, else ''"}

Rules:
- Only shows with a REAL, STATED date. If a listing gives no date, or gives a
  range or a month with no day, SKIP it. A guessed date is worse than a missing
  one here: it puts an act next to a game it is not near, and a client plans
  around that.
- Only MUSIC performances. Skip theatre, comedy, sports, festivals with no
  named act, and "tickets on sale" pages with no show date.
- Use the four-digit year that the source states. Do not assume the current
  year.
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
        out.append({"artist": artist[:120], "date": date,
                    "venue": str(item.get("venue") or "").strip()[:120],
                    "city": str(item.get("city") or "").strip()[:80]})
    return out


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


def sweep_game(game: Dict, creds: Dict, searcher=None, fetcher=None,
               extractor=None) -> Dict:
    """One game, every metro. Returns events + a coverage record per metro.

    The injectable searcher/fetcher/extractor exist so the selftest can drive
    the whole path offline — T32: a test must never reach the live web or a
    real config.
    """
    # Import the network stack ONLY if a real one is actually needed. Importing
    # it unconditionally made the module unusable with injected dependencies —
    # which is to say, untestable offline on a box without `requests`. A
    # default should not be a requirement.
    if searcher is None or fetcher is None:
        import cirrus_daily
        searcher = searcher or (
            lambda q: cirrus_daily.search_web(q, max_results=MAX_SEARCH_RESULTS))
        fetcher = fetcher or (lambda u: cirrus_daily.fetch_article_content(u)[0])
    extractor = extractor or (lambda block: _extract(block, creds))

    events, coverage = [], []
    for metro, miles, query in queries_for(game):
        rec = {"metro": metro, "miles": miles, "query": query,
               "swept_at": _now(), "sources": 0, "found": 0, "error": None}
        try:
            urls = searcher(query)
        except Exception as e:
            rec["error"] = "search failed: {}".format(e)[:200]
            coverage.append(rec)
            continue
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
            coverage.append(rec)
            continue
        found = extractor("\n\n".join(blocks))
        if found is None:
            rec["error"] = "extraction unusable"
            coverage.append(rec)
            continue
        near = [dict(e, metro=metro, miles=miles,
                     gap=gap_days(e["date"], game["date"]))
                for e in found if near_game(e["date"], game["date"])]
        rec["found"] = len(near)
        coverage.append(rec)
        events.extend(near)
        log("  {} — {} source(s), {} of {} show(s) inside the window".format(
            metro, len(blocks), len(near), len(found)))
    return {"events": events, "coverage": coverage}


def _extract(block: str, creds: Dict):
    import llm_providers
    user = "LISTINGS:\n\n{}".format(block[:24000])
    try:
        raw = llm_providers.call("ollama", _EXTRACT_SYSTEM, user, creds,
                                 max_tokens=4000, retries=0)
        got = parse_events(raw)
        if got is not None:
            return got
    except Exception:
        pass
    try:
        _provider, raw = llm_providers.escalate(
            _EXTRACT_SYSTEM, user, creds, max_tokens=4000, mode="single")
        return parse_events(raw)
    except Exception:
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


def run(games: Optional[List[Dict]] = None, only_targets: bool = False,
        creds: Optional[Dict] = None, out_path: Optional[Path] = None) -> Dict:
    import halftime_dashboard
    games = games if games is not None else halftime_dashboard.HOME_GAMES
    creds = creds if creds is not None else json.loads(
        (PROJECT_DIR / "config/credentials.json").read_text())

    todo = [g for g in games
            if g.get("date") and g.get("at_venue", True)
            and (not only_targets or g.get("target"))]
    result = {"generated_at": _now(), "window_days": WINDOW_DAYS, "games": {}}
    for game in todo:
        gid = halftime_dashboard.game_id(game)
        log("game {} — {} vs {}".format(gid, game["date"], game["opponent"]))
        result["games"][gid] = sweep_game(game, creds)
    out = Path(out_path) if out_path else OUT_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(out, json.dumps(result, indent=2))
    total = sum(len(v["events"]) for v in result["games"].values())
    log("done: {} game(s) swept, {} show(s) in window".format(
        len(todo), total))
    return {"games_swept": len(todo), "events": total, "out": str(out)}


def selftest() -> int:
    failures = []

    def check(label, ok):
        print(("  PASS  " if ok else "  FAIL  ") + label)
        if not ok:
            failures.append(label)

    check("a clean array parses",
          parse_events('[{"artist":"A","date":"2026-11-01"}]')
          == [{"artist": "A", "date": "2026-11-01", "venue": "", "city": ""}])
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

    print()
    if failures:
        print("FAILURES: {}".format(len(failures)))
        return 1
    print("ALL PASS")
    return 0


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

# S81: record into the job_status ledger so an overdue/failed run is actually
# SEEN. Until today this job ran unwatched -- opportunity_scout wrote its
# status correctly and nothing read it, and these jobs did not even write one.
# Best-effort and never allowed to change the exit status: monitoring must not
# break the thing it monitors.
    try:
        import job_status
        job_status.record(
            "halftimerouting", True,
            f"{res.get('events', 0)} event(s) across "
            f"{res.get('games_swept', 0)} game(s)")
    except Exception as e:
        print(f"job_status.record failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
