#!/usr/bin/env python3
"""halftime_itinerary.py — where is this act ON game day? (Phase 4, R34)

Justin, 2026-09-23: "An artist being geographically nearby isn't enough if they
are performing elsewhere that day and realistically couldn't make our game."
His example is Trans-Siberian Orchestra on 12/20: in the area on the 22nd, so
the routing sweep offered them, but their own schedule that day rules it out.

The routing sweep asks "who is announced near Acrisure around this date?" --
metro by metro. It cannot see a show 400 miles away on the day itself, because
it never searches there. This module asks the other question, artist by
artist: what is the act's WHOLE announced itinerary, and where are they on game
day and either side of it?

WHO IS CHECKED. Every act in the routing column for a game still to come, and
every act on Justin's Steelers-connected list -- the second half of R35: a fan
act announced near one of his dates joins that game's touring column.

FIVE ANSWERS, and the last one is the one that matters most:
    conflict     a show somewhere else on game day
    local        a show in Pittsburgh that day -- a double-up to ask about
    tight        a show far away the day before or after -- a travel day
    clear        itinerary found, nothing within a day of kickoff
    not_checked  no itinerary found -- NEVER shown as "clear"
Tour dates are only partially announced months out, so "clear" always says
how many dates it saw; it is evidence, not a promise.

COST. One Brave search per artist ($5/1k) plus fetches and a local model call.
Each artist is re-checked at most every RECHECK_DAYS, so after the first pass a
night costs a handful of searches.

Runs nightly at the end of the routing sweep (halftime_routing.main), and on
demand through job_runner. Sends nothing to anyone. Python 3.9-safe.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_DIR = Path(__file__).resolve().parent
OUT_PATH = PROJECT_DIR / "out" / "halftime" / "itinerary.json"
ROUTING_PATH = PROJECT_DIR / "out" / "halftime" / "routing.json"

RECHECK_DAYS = 7
MAX_PER_RUN = 60
SEARCH_RESULTS = 4
MAX_EVENTS_KEPT = 120

CONFLICT, LOCAL, TIGHT, CLEAR, NOT_CHECKED = (
    "conflict", "local", "tight", "clear", "not_checked")

# Suburbs whose rooms a booker files under Pittsburgh (Star Lake is in
# Burgettstown). Anything unrecognised is "elsewhere", said as the city name.
_PITTSBURGH_ALIASES = ("pittsburgh", "burgettstown", "homestead", "millvale",
                       "munhall", "oakmont", "canonsburg")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _key(name: str) -> str:
    import halftime_routing
    return halftime_routing.canonical_key(name)


def metro_of(event: Dict) -> Optional[tuple]:
    """(metro name, miles) if the show is inside the routing radius, else None."""
    import halftime_routing
    place = "{} {}".format(event.get("city") or "", event.get("venue") or "") \
        .lower()
    if any(a in place for a in _PITTSBURGH_ALIASES):
        return ("Pittsburgh, PA", 0)
    for metro, miles in halftime_routing.METROS:
        if metro.split(",")[0].lower() in place:
            return (metro, miles)
    return None


def _where(event: Dict) -> str:
    return (event.get("city") or event.get("venue") or "an unnamed venue")


def verdict(game: Dict, record: Optional[Dict]) -> Dict:
    """What the itinerary says about ONE game. Always carries its evidence."""
    date = game.get("date")
    if not record or record.get("error") or not record.get("events"):
        return {"state": NOT_CHECKED, "label": "Game-day schedule not checked",
                "why": "No announced itinerary was found for this act, so we "
                       "cannot say where they are that day. Not a finding "
                       "that they are free."}
    if not date:
        return {"state": NOT_CHECKED, "label": "Game date not set",
                "why": "The league has not set this date."}
    g = datetime.strptime(date, "%Y-%m-%d")
    same, near = [], []
    for ev in record["events"]:
        try:
            gap = (datetime.strptime(ev.get("date", ""), "%Y-%m-%d") - g).days
        except ValueError:
            continue
        if gap == 0:
            same.append(ev)
        elif abs(gap) == 1:
            near.append(dict(ev, gap=gap))
    checked = record.get("checked_at", "")[:10]
    for ev in same:
        m = metro_of(ev)
        if m and m[1] == 0:
            return {"state": LOCAL, "label": "Plays Pittsburgh that day",
                    "why": "{} at {} on game day — a double-up to ask about, "
                           "not a conflict to assume. Itinerary checked {}."
                           .format(_where(ev), ev.get("venue") or "a venue",
                                   checked)}
    if same:
        ev = same[0]
        m = metro_of(ev)
        return {"state": CONFLICT,
                "label": "Playing {} on game day".format(_where(ev)),
                "why": "{}{} on {}. Their own show that day makes our slot "
                       "unrealistic. Itinerary checked {}.".format(
                           ev.get("venue") + ", " if ev.get("venue") else "",
                           _where(ev) + (" ({} mi)".format(m[1]) if m else ""),
                           date, checked)}
    far = [ev for ev in near if not metro_of(ev)]
    if far:
        ev = far[0]
        return {"state": TIGHT,
                "label": "{} the day {}".format(
                    _where(ev), "before" if ev["gap"] < 0 else "after"),
                "why": "Plays {} on {} — a travel day either side of our game. "
                       "Possible, but ask. Itinerary checked {}.".format(
                           _where(ev), ev.get("date"), checked)}
    dates = sorted(ev.get("date", "") for ev in record["events"])
    return {"state": CLEAR, "label": "No show within a day of kickoff",
            "why": "{} announced date(s) checked, {} to {}; nothing within a "
                   "day of {}. Only ANNOUNCED dates — tours fill in as the "
                   "season nears. Checked {}.".format(
                       len(dates), dates[0], dates[-1], date, checked)}


def near_events(game: Dict, record: Optional[Dict]) -> List[Dict]:
    """Shows inside the routing window AND radius -- shaped like routing.json
    events, so the dashboard can put a fan-list act in the touring column."""
    import halftime_routing
    if not record or not game.get("date"):
        return []
    out = []
    for ev in record.get("events") or []:
        gap = halftime_routing.gap_days(ev.get("date", ""), game["date"])
        m = metro_of(ev)
        if gap is None or abs(gap) > halftime_routing.WINDOW_DAYS or not m:
            continue
        out.append({"artist": record.get("name"), "date": ev.get("date"),
                    "venue": ev.get("venue", ""), "city": ev.get("city", ""),
                    "metro": m[0], "miles": m[1], "gap": gap,
                    "style": ev.get("style", ""), "source": "itinerary"})
    return out


# ── who to check ────────────────────────────────────────────────────────────

def artists_to_check(routing: Dict, today: Optional[str] = None) -> List[Dict]:
    """Routing acts for games still to come, then Justin's list. One entry per
    act (by canonical name), nearest game first."""
    import halftime_dashboard as hd
    upcoming = {hd.game_id(g): g for g in hd.upcoming_games(today)}
    seen, out = set(), []

    def add(name, aka=()):
        key = _key(name)
        if name and key not in seen:
            seen.add(key)
            out.append({"name": name, "key": key, "aka": list(aka)})

    games = sorted(((gid, g) for gid, g in upcoming.items() if g.get("date")),
                   key=lambda x: x[1]["date"])
    for gid, _g in games:
        for ev in ((routing.get("games") or {}).get(gid) or {}).get("events") \
                or []:
            add(ev.get("artist", ""))
    for entry in hd.STEELERS_CONNECTED:
        add(entry["name"], entry.get("aka") or ())
    return out


def _season_window(today: Optional[str] = None) -> tuple:
    """Keep dates from a week before today to a month after the last game."""
    import halftime_dashboard as hd
    start = datetime.strptime(today or hd.today_et(), "%Y-%m-%d") \
        - timedelta(days=7)
    last = max(g["date"] for g in hd.HOME_GAMES if g.get("date"))
    end = datetime.strptime(last, "%Y-%m-%d") + timedelta(days=31)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def check_artist(artist: Dict, searcher, fetcher, extractor,
                 today: Optional[str] = None) -> Dict:
    """One act's announced itinerary. Only rows naming THIS act are kept --
    a tour page also lists support acts, and a support act's dates are not
    the headliner's."""
    import halftime_routing
    import halftime_dashboard as hd
    season = hd.SEASON
    query = '"{}" tour dates {}'.format(artist["name"], season)
    rec = {"name": artist["name"], "checked_at": _now(), "query": query,
           "sources": 0, "events": [], "error": None}
    try:
        urls = searcher(query) or []
    except Exception as e:
        rec["error"] = "search failed: {}".format(type(e).__name__)
        return rec
    blocks = []
    for url in urls[:SEARCH_RESULTS]:
        try:
            text = fetcher(url)
        except Exception:
            continue
        if text:
            blocks.append("SOURCE: {}\n{}".format(
                url, text[:halftime_routing.MAX_FETCH_CHARS]))
    rec["sources"] = len(blocks)
    if not blocks:
        rec["error"] = "no fetchable source"
        return rec
    found = extractor("\n\n".join(blocks))
    if found is None:
        rec["error"] = "extraction unusable"
        return rec
    names = {artist["key"]} | {_key(a) for a in artist.get("aka") or []}
    lo, hi = _season_window(today)
    keep = {}
    for ev in found:
        if _key(ev.get("artist", "")) not in names:
            continue
        d = ev.get("date", "")
        if not (lo <= d <= hi):
            continue
        keep[(d, (ev.get("city") or "").lower())] = {
            "date": d, "city": ev.get("city", ""), "venue": ev.get("venue", ""),
            "style": ev.get("style", "")}
    rec["events"] = sorted(keep.values(), key=lambda e: e["date"]) \
        [:MAX_EVENTS_KEPT]
    return rec


def _fresh(rec: Optional[Dict], today: str) -> bool:
    if not rec or rec.get("error"):
        return False
    try:
        at = datetime.strptime(rec.get("checked_at", "")[:10], "%Y-%m-%d")
    except ValueError:
        return False
    return (datetime.strptime(today, "%Y-%m-%d") - at).days < RECHECK_DAYS


def load(path: Optional[Path] = None) -> Dict:
    try:
        return json.loads(Path(path or OUT_PATH).read_text())
    except Exception:
        return {}


def _workers(creds: Dict) -> int:
    """vLLM batches, so several extractions at once; ollama does not (S119:
    concurrency there turned timeouts into PAID escalations). 1 unless the
    batching endpoint is configured."""
    try:
        want = int(os.environ.get("HALFTIME_ITINERARY_WORKERS", "") or 0)
    except ValueError:
        want = 0
    if want <= 0:
        want = 8 if creds.get("vllm_url") else 1
    return max(1, min(want, 16))


def run(creds: Optional[Dict] = None, out_path: Optional[Path] = None,
        routing_path: Optional[Path] = None, searcher=None, fetcher=None,
        extractor=None, today: Optional[str] = None,
        limit: int = MAX_PER_RUN) -> Dict:
    """Check every act that is due. Injectable end to end for the selftest."""
    import halftime_dashboard as hd
    import halftime_routing
    today = today or hd.today_et()
    out = Path(out_path) if out_path else OUT_PATH
    prior = load(out).get("artists") or {}
    routing = load(routing_path or ROUTING_PATH)
    if creds is None and (searcher is None or fetcher is None
                          or extractor is None):
        creds = json.loads((PROJECT_DIR / "config/credentials.json")
                           .read_text())
    creds = creds or {}
    if searcher is None or fetcher is None:
        import cirrus_daily
        searcher = searcher or (lambda q: cirrus_daily.search_web(
            q, max_results=SEARCH_RESULTS, caller="halftime_itinerary"))
        fetcher = fetcher or (lambda u: cirrus_daily.fetch_article_content(u)[0])
    stats = {}
    extractor = extractor or (
        lambda block: halftime_routing._extract(block, creds, stats))

    todo = [a for a in artists_to_check(routing, today)
            if not _fresh(prior.get(a["key"]), today)][:limit]
    artists = dict(prior)
    if todo:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=_workers(creds)) as pool:
            recs = list(pool.map(
                lambda a: check_artist(a, searcher, fetcher, extractor, today),
                todo))
        for a, rec in zip(todo, recs):
            artists[a["key"]] = rec
    result = {"generated_at": _now(), "recheck_days": RECHECK_DAYS,
              "artists": artists}
    out.parent.mkdir(parents=True, exist_ok=True)
    halftime_routing._write_atomic(out, json.dumps(result, indent=2))
    errors = sum(1 for a in todo if artists[a["key"]].get("error"))
    return {"checked": len(todo), "errors": errors,
            "with_dates": sum(1 for a in todo if artists[a["key"]]["events"]),
            "known": len(artists), "llm": stats, "out": str(out)}


# ── selftest ────────────────────────────────────────────────────────────────

def selftest() -> int:
    import tempfile
    failures = []

    def check(label, ok):
        print(("  PASS  " if ok else "  FAIL  ") + label)
        if not ok:
            failures.append(label)

    g20 = {"week": 15, "date": "2026-12-20", "opponent": "Baltimore Ravens"}
    rec = lambda evs, **k: dict({"name": "X", "checked_at":
                                 "2026-09-23T00:00:00Z", "events": evs,
                                 "error": None}, **k)

    v = verdict(g20, rec([{"date": "2026-12-20", "city": "Chicago, IL",
                           "venue": "Allstate Arena"}]))
    check("a show elsewhere on game day is a CONFLICT, with city and date",
          v["state"] == CONFLICT and "Chicago" in v["label"]
          and "2026-12-20" in v["why"])
    check("a same-day show 135 mi away is still a conflict, with the miles",
          (lambda x: x["state"] == CONFLICT and "135 mi" in x["why"])(
              verdict(g20, rec([{"date": "2026-12-20",
                                 "city": "Cleveland, OH"}]))))
    check("a Pittsburgh show that day is a double-up, not a conflict",
          verdict(g20, rec([{"date": "2026-12-20", "city": "Pittsburgh, PA",
                             "venue": "PPG Paints Arena"}]))["state"] == LOCAL)
    check("Star Lake (Burgettstown) counts as Pittsburgh",
          metro_of({"city": "Burgettstown, PA"}) == ("Pittsburgh, PA", 0))
    check("a far show the day before is TIGHT",
          verdict(g20, rec([{"date": "2026-12-19",
                             "city": "Denver, CO"}]))["state"] == TIGHT)
    check("an in-radius show the day before is not a problem",
          verdict(g20, rec([{"date": "2026-12-19",
                             "city": "Cleveland, OH"}]))["state"] == CLEAR)
    c = verdict(g20, rec([{"date": "2026-11-02", "city": "Omaha, NE"},
                          {"date": "2026-12-28", "city": "Boise, ID"}]))
    check("nothing near the date is CLEAR, and says how many dates it saw",
          c["state"] == CLEAR and "2 announced" in c["why"]
          and "ANNOUNCED" in c["why"])
    check("NO itinerary is 'not checked' -- never 'clear'",
          verdict(g20, rec([]))["state"] == NOT_CHECKED
          and verdict(g20, None)["state"] == NOT_CHECKED
          and verdict(g20, rec([{"date": "2026-12-20"}],
                               error="no fetchable source"))["state"]
          == NOT_CHECKED)

    fake_page = "tour page"
    def fx(block):
        return [{"artist": "Trans-Siberian Orchestra", "date": "2026-12-20",
                 "city": "Chicago, IL", "venue": "Allstate Arena"},
                {"artist": "Trans-Siberian Orchestra", "date": "2026-12-20",
                 "city": "Chicago, IL", "venue": "Allstate Arena"},
                {"artist": "Opening Act", "date": "2026-12-21",
                 "city": "Pittsburgh, PA"},
                {"artist": "Trans-Siberian Orchestra", "date": "2025-12-20",
                 "city": "Old, OH"}]
    tso = {"name": "Trans-Siberian Orchestra",
           "key": _key("Trans-Siberian Orchestra"), "aka": []}
    r = check_artist(tso, lambda q: ["u1", "u2"], lambda u: fake_page, fx,
                     today="2026-09-23")
    check("an itinerary keeps THIS act's dates, once each",
          [e["date"] for e in r["events"]] == ["2026-12-20"])
    check("...drops a support act's row and last season's date",
          all(e["city"] != "Pittsburgh, PA" and e["date"] >= "2026"
              for e in r["events"]))
    check("the check itself rules TSO out on 12/20",
          verdict(g20, r)["state"] == CONFLICT)
    check("no source is an error, not an empty itinerary",
          check_artist(tso, lambda q: [], lambda u: "", fx)["error"]
          == "no fetchable source")
    check("an unusable model answer is an error, not 'no shows'",
          check_artist(tso, lambda q: ["u"], lambda u: "x",
                       lambda b: None)["error"] == "extraction unusable")

    fan = {"name": "Pat Monahan", "key": _key("Pat Monahan"),
           "aka": ["Train"]}
    rt = check_artist(fan, lambda q: ["u"], lambda u: "x", lambda b: [
        {"artist": "Train", "date": "2026-11-02", "city": "Pittsburgh, PA",
         "venue": "Stage AE"}], today="2026-09-23")
    check("an aka on his list is the same act (Train -> Pat Monahan)",
          len(rt["events"]) == 1)
    g1 = {"week": 8, "date": "2026-11-01"}
    ne = near_events(g1, rt)
    check("a fan act in the window and radius is shaped for the touring column",
          ne and ne[0]["artist"] == "Pat Monahan" and ne[0]["gap"] == 1
          and ne[0]["miles"] == 0)
    check("...and one outside the radius is not",
          near_events(g1, rec([{"date": "2026-11-01",
                                "city": "Denver, CO"}])) == [])

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "itinerary.json"
        routing = Path(tmp) / "routing.json"
        routing.write_text(json.dumps({"games": {
            "wk15-ravens": {"events": [
                {"artist": "Trans-Siberian Orchestra", "date": "2026-12-22"}]},
            # Week 1 is PLAYED as of the dates below; its act must not cost a
            # search.
            "wk01-falcons": {"events": [
                {"artist": "Played Game Act", "date": "2026-09-12"}]}}}))
        calls = []

        def searcher(q):
            calls.append(q)
            return ["u"]
        res = run(out_path=out, routing_path=routing, searcher=searcher,
                  fetcher=lambda u: "x", extractor=fx, today="2026-09-23",
                  creds={})
        # Pin the recorded check time. It is the REAL clock in production, and
        # a cache test on the real clock is a time bomb (it inverts in 2027).
        data = load(out)
        for _r in data["artists"].values():
            _r["checked_at"] = "2026-09-23T00:00:00Z"
        out.write_text(json.dumps(data))
        check("run: routing acts AND his whole list are checked",
              res["checked"] == 29 and len(calls) == 29)
        check("run: TSO's record is written",
              data["artists"][_key("Trans-Siberian Orchestra")]["events"])
        calls.clear()
        res2 = run(out_path=out, routing_path=routing, searcher=searcher,
                   fetcher=lambda u: "x", extractor=fx, today="2026-09-25",
                   creds={})
        check("run: a fresh record is not re-searched (cost)",
              res2["checked"] == 0 and calls == []
              and len(load(out)["artists"]) == 29)
        res3 = run(out_path=out, routing_path=routing, searcher=searcher,
                   fetcher=lambda u: "x", extractor=fx, today="2026-10-05",
                   creds={}, limit=5)
        check("run: a stale record is re-checked, within the per-run cap",
              res3["checked"] == 5)
        check("run: an act routed only to a PLAYED game is never searched",
              not any("Played Game Act" in q for q in calls + [
                  a["name"] for a in artists_to_check(load(routing),
                                                      "2026-09-23")]))

    check("an unknown argument is refused (T107)", main(["--selftest"]) == 2)
    print()
    if failures:
        print("FAILURES: {}".format(len(failures)))
        return 1
    print("ALL PASS")
    return 0


USAGE = "usage: halftime_itinerary.py [selftest]   (no argument = run)"


def main(argv: Optional[List[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["selftest"]:
        return selftest()
    if args:
        print(USAGE, file=sys.stderr)
        return 2
    print(json.dumps(run()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
