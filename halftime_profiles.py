#!/usr/bin/env python3
"""halftime_profiles.py — who is this act, and what did they actually do?
(Phase 3: R32, R33, and the era R38's '90s date needs)

Justin, 2026-09-23:
  * "For any recommended act that may not be immediately recognizable, include
    a short 1-2 sentence description explaining who they are and, more
    importantly, why they would be relevant or interesting for our fans."
  * "For acts with prior sports experience, include what they actually did and
    a link to video when available. There is a meaningful difference between
    performing a stadium halftime and making a pregame appearance."

WHAT THIS WRITES, per act (out/halftime/profiles.json):
  who      1-2 sentences. Wikipedia's summary when the page is clearly about a
           musical act; otherwise the local model's summary of search results.
  era      the decade they broke through -- only with a quoted source sentence
           that contains a year in that decade.
  credits  [{team, event, year, role, quote, video}] -- role from a fixed
           vocabulary (halftime show / pregame / national anthem / in-game /
           postgame concert / other), each with the sentence it came from.
  video    only when YouTube's own oEmbed lookup resolves the link AND its
           title names the act.

EVERY MODEL CLAIM CARRIES A QUOTE, and the quote must appear in the text the
model was given. A claim whose quote is not there is dropped, not softened.
"Why they matter to your fans" is NOT written here: the dashboard composes it
from signals it already holds (his list, Pittsburgh roots, the date's brief,
credits, routing), so no model writes a selling line about a real person.

Local model only (vLLM, then ollama). No paid escalation: a profile is worth
having, not worth paying for twice; a failed act is retried the next night.
Runs at the end of the nightly catalogue job. Sends nothing. Python 3.9-safe.
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_DIR = Path(__file__).resolve().parent
OUT_PATH = PROJECT_DIR / "out" / "halftime" / "profiles.json"
RECHECK_DAYS = 30
MAX_PER_RUN = 40
# 2 (S274): film pages refused, "song played" is not a performance, inactive
# acts flagged -- every first-run profile is redone.
RECORD_VERSION = 2
SOURCE_CHARS = 12000
UA = "cowork-halftime-research/1.0 (cirrustask@gmail.com)"

ROLES = ("halftime show", "pregame", "national anthem", "in-game / stage",
         "postgame concert", "song played at games", "other")
_MUSIC = ("band", "singer", "musician", "rapper", "group", "orchestra", "duo",
          "trio", "dj", "composer", "songwriter", "ensemble", "performer",
          "vocalist", "guitarist", "drummer", "pianist", "violinist", "choir",
          "hip hop", "country music", "rock", "tribute")

_SYSTEM = """You read sources about ONE music act and return facts about it.

Return ONLY a JSON object, no prose:
{"who": "1-2 plain sentences: who the act is (genre, where from, best known
         for). '' if the sources do not say.",
 "who_quote": "one sentence copied EXACTLY from the sources that supports who",
 "era": "the decade the act broke through, like 1990s. '' if not stated.",
 "era_quote": "one sentence copied EXACTLY from the sources, containing the
               year that shows it",
 "credits": [{"team": "the sports team or event",
              "event": "short description, e.g. halftime show vs Browns",
              "year": "four-digit year or ''",
              "role": "one of: halftime show, pregame, national anthem,
                       in-game / stage, postgame concert,
                       song played at games, other",
              "quote": "one sentence copied EXACTLY from the sources"}]}

Rules:
- ONLY facts the sources state about THIS act. Nothing from memory.
- Every quote must be copied character for character from the sources.
- A pregame appearance is NOT a halftime show; an anthem is NOT a halftime
  show. Use the role the source actually describes.
- If the act's RECORDING was played -- entrance music, a stadium anthem, a
  song during timeouts -- and the act did not perform, the role is
  "song played at games". That is not a performance.
- If the sources are about a different act with a similar name, return
  {"who": "", "who_quote": "", "era": "", "era_quote": "", "credits": []}."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _key(name: str) -> str:
    import halftime_routing
    return halftime_routing.canonical_key(name)


def _norm(text: str) -> str:
    return " ".join((text or "").lower().replace("’", "'").split())


def quoted(quote: str, source: str) -> bool:
    """Is the quote really in the source? Whitespace/case-insensitive, and
    long enough to mean something."""
    q = _norm(quote)
    return len(q) >= 20 and q in _norm(source)


def is_music(text: str) -> bool:
    return any(re.search(r"\b" + re.escape(m) + r"s?\b", text) for m in _MUSIC)


def inactive_quote(extract: str) -> str:
    """Wikipedia's first sentence when it speaks of the act in the PAST tense
    ("... was an English singer", "... were an American punk rock band") --
    a death or a break-up. '' otherwise. A booking list must not offer them."""
    first = first_sentences(extract, n=1, limit=400)
    past = re.search(r"\b(was|were) (an?|the)\b", first)
    present = re.search(r"\b(is|are) (an?|the)\b", first)
    if past and (not present or past.start() < present.start()):
        return first
    return ""


def first_sentences(text: str, n: int = 2, limit: int = 320) -> str:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", (text or "").strip())
    out = " ".join(parts[:n]).strip()
    return out if len(out) <= limit else out[:limit].rsplit(" ", 1)[0] + "…"


# ── sources ─────────────────────────────────────────────────────────────────

def _get_json(url: str, timeout: int = 15) -> Dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def wiki_lookup(name: str, getter=_get_json) -> Optional[Dict]:
    """The act's Wikipedia summary, or None. Refused unless the page is
    clearly about a musical act AND its title is this act's name -- "Styx"
    alone finds the river of the dead, and "Sparta" finds a city."""
    try:
        hits = getter("https://en.wikipedia.org/w/api.php?" + urllib.parse
                      .urlencode({"action": "opensearch", "search": name,
                                  "limit": 5, "namespace": 0,
                                  "format": "json"}))
    except Exception:
        return None
    titles = hits[1] if isinstance(hits, list) and len(hits) > 1 else []
    want = _key(name)
    for title in titles:
        base = re.sub(r"\s*\([^)]*\)\s*$", "", title)
        if _key(base) != want:
            continue
        try:
            summ = getter("https://en.wikipedia.org/api/rest_v1/page/summary/"
                          + urllib.parse.quote(title.replace(" ", "_")))
        except Exception:
            continue
        # S274: judged on Wikipedia's SHORT description ("American rock band"
        # vs "2015 film"), whole words. The first cut searched the whole
        # extract by substring, and "Creed" came back as the Rocky spin-off
        # film because "Rocky" contains "rock".
        desc = _norm(summ.get("description", ""))
        if summ.get("type") == "standard" and is_music(desc):
            return {"title": summ.get("title"), "extract":
                    summ.get("extract", ""), "url": ((summ.get(
                        "content_urls") or {}).get("desktop") or {}).get(
                            "page", "")}
    return None


def youtube_title(url: str, getter=_get_json) -> Optional[str]:
    """The video's title per YouTube itself, or None if it does not resolve."""
    if not re.match(r"^https://(www\.)?(youtube\.com/watch\?v=|youtu\.be/)"
                    r"[A-Za-z0-9_-]{6,}", url or ""):
        return None
    try:
        return getter("https://www.youtube.com/oembed?" + urllib.parse
                      .urlencode({"url": url, "format": "json"})).get("title")
    except Exception:
        return None


def video_for(name: str, credit: Dict, searcher, getter=_get_json) -> Optional[Dict]:
    """A video of THIS credit, or None. Kept only if YouTube resolves it and
    the title names the act -- a search result is not evidence on its own."""
    query = '"{}" {} {} video'.format(name, credit.get("team", ""),
                                      credit.get("role", ""))
    try:
        urls = searcher(query) or []
    except Exception:
        return None
    words = [w for w in re.findall(r"[a-z0-9]+", _key(name)) if len(w) > 2]
    for url in urls:
        title = youtube_title(url, getter)
        if title and words and all(w in _norm(title) for w in words):
            return {"url": url, "title": title}
    return None


# ── the model ───────────────────────────────────────────────────────────────

def parse_object(raw: str) -> Optional[Dict]:
    if not raw:
        return None
    a, b = raw.find("{"), raw.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        obj = json.loads(raw[a:b + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def local_model(creds: Dict, stats: Dict):
    """vLLM then ollama. No paid escalation, on purpose (see module doc)."""
    import llm_providers

    def ask(user: str) -> Optional[Dict]:
        for provider in (("vllm",) if creds.get("vllm_url") else ()) + (
                "ollama",):
            try:
                got = parse_object(llm_providers.call(
                    provider, _SYSTEM, user, creds, max_tokens=6000,
                    retries=0))
            except Exception:
                got = None
            if got is not None:
                stats[provider] = stats.get(provider, 0) + 1
                return got
        stats["unusable"] = stats.get("unusable", 0) + 1
        return None
    return ask


def verify(obj: Dict, source: str) -> Dict:
    """Keep only what the sources back. Returns who / era / credits."""
    out = {"who": "", "era": "", "era_quote": "", "credits": []}
    if obj.get("who") and quoted(obj.get("who_quote", ""), source):
        out["who"] = first_sentences(obj["who"])
    era = (obj.get("era") or "").strip()
    m = re.fullmatch(r"(1[5-9]|20)(\d)0s", era)
    eq = obj.get("era_quote", "")
    if m and quoted(eq, source) and re.search(
            r"\b" + era[:3] + r"\d\b", eq):
        out["era"], out["era_quote"] = era, eq
    seen = set()
    for c in obj.get("credits") or []:
        if not isinstance(c, dict):
            continue
        key = ((c.get("role") or "").lower(), _norm(c.get("event") or
                                                    c.get("team") or ""),
               str(c.get("year") or ""))
        if key in seen:
            continue
        seen.add(key)
        role = (c.get("role") or "").strip().lower()
        if role in ROLES and quoted(c.get("quote", ""), source):
            year = str(c.get("year") or "")
            out["credits"].append({
                "team": str(c.get("team") or "")[:80],
                "event": str(c.get("event") or "")[:120],
                "year": year if re.fullmatch(r"(19|20)\d\d", year) else "",
                "role": role, "quote": c["quote"][:300]})
    return out


def profile_act(act: Dict, searcher, fetcher, ask, getter=_get_json) -> Dict:
    """One act. `act` = {name, clients}. Every step is injectable."""
    name = act["name"]
    rec = {"name": name, "checked_at": _now(), "v": RECORD_VERSION,
           "who": "", "who_source": "", "era": "", "era_quote": "",
           "credits": [], "sources": [], "error": None, "inactive": ""}
    wiki = wiki_lookup(name, getter)
    blocks = []
    if wiki:
        blocks.append("SOURCE: {}\n{}".format(wiki["url"], wiki["extract"]))
        rec["sources"].append(wiki["url"])
    clients = (act.get("clients") or "").strip()
    if clients or not wiki:
        q = ('"{}" {} halftime OR pregame OR anthem'.format(name, clients)
             if clients else '"{}" band OR singer OR musician'.format(name))
        try:
            urls = searcher(q) or []
        except Exception:
            urls = []
        for url in urls[:3]:
            try:
                text = fetcher(url)
            except Exception:
                continue
            if text:
                blocks.append("SOURCE: {}\n{}".format(url, text[:SOURCE_CHARS]))
                rec["sources"].append(url)
    if not blocks:
        rec["error"] = "no source"
        return rec
    source = "\n\n".join(blocks)
    obj = ask("ACT: {}\nKNOWN SPORTS CREDIT (unverified): {}\n\nSOURCES:\n\n{}"
              .format(name, clients or "none", source[:30000]))
    if obj is None and not wiki:
        rec["error"] = "extraction unusable"
        return rec
    got = verify(obj or {}, source)
    rec.update(era=got["era"], era_quote=got["era_quote"],
               credits=got["credits"])
    # Wikipedia's own words when there is a page; the model's only otherwise.
    if wiki:
        rec["who"], rec["who_source"] = first_sentences(wiki["extract"]), \
            wiki["url"]
        rec["inactive"] = inactive_quote(wiki["extract"])
    elif got["who"]:
        rec["who"], rec["who_source"] = got["who"], rec["sources"][0]
    for c in rec["credits"][:2]:
        c["video"] = video_for(name, c, searcher, getter)
    return rec


# ── who, and the run ────────────────────────────────────────────────────────

def acts_to_profile(snapshot: Dict) -> List[Dict]:
    """Acts the page SHOWS that are not on Justin's own list (he knows those):
    the credit list, then routing acts for games still to come."""
    import halftime_dashboard as hd
    fans = {_key(e["name"]) for e in hd.STEELERS_CONNECTED}
    seen, out = set(fans), []

    def add(name, clients=""):
        k = _key(name)
        if name and k not in seen:
            seen.add(k)
            out.append({"name": name, "key": k, "clients": clients})
    for a in snapshot.get("roster") or []:
        add(a.get("name"), (a.get("fields") or {}).get("clients", ""))
    for g in snapshot.get("games") or []:
        if g.get("completed"):
            continue
        for a in (g.get("candidates") or {}).get("touring") or []:
            add(a.get("name"))
    return out


def _fresh(rec: Optional[Dict], today: str) -> bool:
    if not rec or rec.get("error") or rec.get("v") != RECORD_VERSION:
        return False
    try:
        at = datetime.strptime(rec["checked_at"][:10], "%Y-%m-%d")
        return (datetime.strptime(today, "%Y-%m-%d") - at).days < RECHECK_DAYS
    except (KeyError, ValueError):
        return False


def load(path: Optional[Path] = None) -> Dict:
    try:
        return json.loads(Path(path or OUT_PATH).read_text())
    except Exception:
        return {}


def run(snapshot_path: Optional[Path] = None, out_path: Optional[Path] = None,
        searcher=None, fetcher=None, ask=None, getter=None,
        today: Optional[str] = None, limit: int = MAX_PER_RUN,
        creds: Optional[Dict] = None) -> Dict:
    import halftime_dashboard as hd
    import halftime_routing
    today = today or hd.today_et()
    out = Path(out_path) if out_path else OUT_PATH
    snap = load(snapshot_path or hd.OUT_DIR / "snapshot.json")
    prior = load(out).get("acts") or {}
    stats = {}
    if searcher is None or fetcher is None or ask is None:
        import cirrus_daily
        creds = creds or json.loads((PROJECT_DIR / "config/credentials.json")
                                    .read_text())
        searcher = searcher or (lambda q: cirrus_daily.search_web(
            q, max_results=4, caller="halftime_profiles"))
        fetcher = fetcher or (lambda u: cirrus_daily.fetch_article_content(
            u, max_chars=SOURCE_CHARS)[0])
        ask = ask or local_model(creds, stats)
    getter = getter or _get_json
    todo = [a for a in acts_to_profile(snap)
            if not _fresh(prior.get(a["key"]), today)][:limit]
    acts = dict(prior)
    for a in todo:
        acts[a["key"]] = profile_act(a, searcher, fetcher, ask, getter)
    out.parent.mkdir(parents=True, exist_ok=True)
    halftime_routing._write_atomic(out, json.dumps(
        {"generated_at": _now(), "acts": acts}, indent=2))
    done = [acts[a["key"]] for a in todo]
    return {"checked": len(todo),
            "with_who": sum(1 for r in done if r["who"]),
            "with_era": sum(1 for r in done if r["era"]),
            "credits": sum(len(r["credits"]) for r in done),
            "videos": sum(1 for r in done for c in r["credits"]
                          if c.get("video")),
            "errors": sum(1 for r in done if r["error"]),
            "llm": stats, "known": len(acts)}


# ── selftest ────────────────────────────────────────────────────────────────

def selftest() -> int:
    import tempfile
    failures = []

    def check(label, ok):
        print(("  PASS  " if ok else "  FAIL  ") + label)
        if not ok:
            failures.append(label)

    src = ("SOURCE: u\nHouse of Pain was an American hip hop group formed in "
           "Los Angeles in 1991. Their single Jump Around reached number 3 in "
           "1992. The group performed at halftime of the Cincinnati Bengals "
           "game against the Browns in 2023.")
    good = {"who": "An American hip hop group best known for Jump Around.",
            "who_quote": "House of Pain was an American hip hop group formed "
                         "in Los Angeles in 1991.",
            "era": "1990s", "era_quote": "Their single Jump Around reached "
                                         "number 3 in 1992.",
            "credits": [{"team": "Cincinnati Bengals", "event": "halftime vs "
                         "Browns", "year": "2023", "role": "halftime show",
                         "quote": "The group performed at halftime of the "
                                  "Cincinnati Bengals game against the Browns "
                                  "in 2023."}]}
    v = verify(good, src)
    check("a claim whose quote IS in the source is kept (who, era, credit)",
          v["who"] and v["era"] == "1990s" and len(v["credits"]) == 1)
    bad = dict(good, era_quote="They were huge in the nineties everywhere.",
               who_quote="An invented sentence that is not in the text.",
               credits=[dict(good["credits"][0], quote="They played the Super "
                                                        "Bowl in 2020 too.")])
    vb = verify(bad, src)
    check("a claim whose quote is NOT in the source is dropped, not softened",
          vb["who"] == "" and vb["era"] == "" and vb["credits"] == [])
    check("an era whose quote holds no year of that decade is dropped",
          verify(dict(good, era="1980s"), src)["era"] == "")
    check("a role outside the vocabulary is dropped (pregame != halftime is "
          "the whole point)",
          verify(dict(good, credits=[dict(good["credits"][0],
                                          role="appearance")]),
                 src)["credits"] == [])
    check("a short or empty quote proves nothing",
          not quoted("1992", src) and not quoted("", src))

    wiki_pages = {
        "Styx": ["Styx", ["Styx", "Styx (band)"]],
        "summ:Styx": {"type": "standard", "title": "Styx", "description":
                      "Greek mythology", "extract": "The river Styx..."},
        "summ:Styx_(band)": {"type": "standard", "title": "Styx",
                             "description": "American rock band",
                             "extract": "Styx is an American rock band.",
                             "content_urls": {"desktop": {
                                 "page": "https://en.wikipedia.org/wiki/Styx_(band)"}}},
        "Sparta": ["Sparta", ["Sparta"]],
        "summ:Sparta": {"type": "standard", "description": "city in Greece",
                        "extract": "Sparta was a city-state."}}

    def getter(url):
        if "opensearch" in url:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["search"][0]
            return wiki_pages.get(q, [q, []])
        if "/page/summary/" in url:
            return wiki_pages["summ:" + urllib.parse.unquote(url.rsplit("/", 1)[1])]
        if "oembed" in url:
            u = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["url"][0]
            if u.endswith("dead"):
                raise OSError("404")
            return {"title": "House of Pain - Jump Around LIVE at Bengals halftime"
                    if "good" in u else "Some other band live"}
        raise AssertionError(url)

    wiki_pages.update({
        "Creed": ["Creed", ["Creed (film)", "Creed (band)"]],
        "summ:Creed_(film)": {"type": "standard", "description":
                              "2015 film by Ryan Coogler", "extract":
                              "Creed is a 2015 American sports drama film, a "
                              "spin-off of the Rocky series."},
        "summ:Creed_(band)": {"type": "standard", "description":
                              "American rock band", "extract":
                              "Creed is an American rock band."}})
    check("Wikipedia: a FILM page is refused, the band chosen (Creed; "
          "'Rocky' is not 'rock')",
          "rock band" in (wiki_lookup("Creed", getter) or {}).get("extract", ""))
    check("music words are whole words", not is_music("rocky film")
          and is_music("american rock band") and is_music("country singers"))
    check("inactive: a past-tense first sentence is flagged (death)",
          inactive_quote('John Michael "Ozzy" Osbourne was an English singer.'))
    check("inactive: ...and a break-up",
          inactive_quote("The Ramones were an American punk rock band formed "
                         "in 1974. They played fast."))
    check("inactive: a present-tense act is not",
          inactive_quote("Styx is an American rock band. It was formed in "
                         "1972.") == "")
    _dup = verify(dict(good, credits=good["credits"] * 2), src)
    check("credits: the same credit twice is listed once",
          len(_dup["credits"]) == 1)
    _song = verify(dict(good, credits=[dict(good["credits"][0],
                                            role="song played at games")]), src)
    check("credits: 'song played at games' is its own role, not a performance",
          _song["credits"][0]["role"] == "song played at games")
    w = wiki_lookup("Styx", getter)
    check("Wikipedia: the BAND page is chosen, not the river",
          w and "rock band" in w["extract"])
    check("Wikipedia: a non-music page is refused (Sparta the city)",
          wiki_lookup("Sparta", getter) is None)
    cr = good["credits"][0]
    check("video: kept only when YouTube resolves it and the title names the act",
          (video_for("House of Pain", cr, lambda q: [
              "https://www.youtube.com/watch?v=deadxxx_dead",
              "https://www.youtube.com/watch?v=otherxx",
              "https://www.youtube.com/watch?v=goodxxxx"], getter) or {})
          .get("url", "").endswith("goodxxxx"))
    check("video: a non-YouTube link is never kept",
          video_for("House of Pain", cr, lambda q: [
              "https://example.com/watch?v=goodxxxx"], getter) is None)

    rec = profile_act({"name": "House of Pain", "clients": "Cincinnati Bengals"},
                      lambda q: ["https://www.youtube.com/watch?v=goodxxxx"],
                      lambda u: src, lambda user: good, getter)
    check("profile: who, era and a role-labelled credit, each sourced",
          rec["who"] and rec["era"] == "1990s"
          and rec["credits"][0]["role"] == "halftime show"
          and rec["who_source"])
    check("profile: the credit carries its verified video",
          (rec["credits"][0].get("video") or {}).get("title", "")
          .startswith("House of Pain"))
    styx = profile_act({"name": "Styx", "clients": ""}, lambda q: [],
                       lambda u: "", lambda user: {}, getter)
    check("profile: with a Wikipedia page, WHO is Wikipedia's own words",
          styx["who"] == "Styx is an American rock band."
          and "wikipedia.org" in styx["who_source"])
    none = profile_act({"name": "Nobody Knows", "clients": ""},
                       lambda q: [], lambda u: "", lambda user: None, getter)
    check("profile: nothing found is an error, never an invented description",
          none["error"] == "no source" and none["who"] == "")

    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "snapshot.json"
        snap.write_text(json.dumps({
            "roster": [{"name": "House of Pain",
                        "fields": {"clients": "Cincinnati Bengals"}},
                       {"name": "Styx", "fields": {"clients": ""}}],
            "games": [{"completed": False, "candidates": {"touring": [
                {"name": "Club Act"}]}},
                {"completed": True, "candidates": {"touring": [
                    {"name": "Played Game Act"}]}}]}))
        names = [a["name"] for a in acts_to_profile(load(snap))]
        check("who: the credit list and upcoming routing acts, NOT his own "
              "list (he knows them) and not played games",
              names == ["House of Pain", "Club Act"])
        out = Path(tmp) / "profiles.json"
        calls = []
        res = run(snapshot_path=snap, out_path=out,
                  searcher=lambda q: calls.append(q) or (
                      ["u"] if "House of Pain" in q else []),
                  fetcher=lambda u: src, ask=lambda user: good,
                  getter=getter, today="2026-09-24")
        check("run: each act profiled and written", res["checked"] == 2
              and len(load(out)["acts"]) == 2)
        data = load(out)
        for r in data["acts"].values():
            r["checked_at"] = "2026-09-24T00:00:00Z"   # pin: not the real clock
        out.write_text(json.dumps(data))
        calls.clear()
        res2 = run(snapshot_path=snap, out_path=out, searcher=lambda q:
                   calls.append(q) or [], fetcher=lambda u: src,
                   ask=lambda user: good, getter=getter, today="2026-10-01")
        # House of Pain succeeded; Club Act found no source. A good profile is
        # not re-searched for 30 days; a failed one is retried next run.
        check("run: a fresh profile is not re-searched (cost); a failed one "
              "is retried",
              res2["checked"] == 1
              and not any("House of Pain" in q for q in calls)
              and any("Club Act" in q for q in calls))
    check("an unknown argument is refused (T107)", main(["--selftest"]) == 2)
    print()
    if failures:
        print("FAILURES: {}".format(len(failures)))
        return 1
    print("ALL PASS")
    return 0


USAGE = "usage: halftime_profiles.py [selftest]   (no argument = run)"


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
