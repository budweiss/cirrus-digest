#!/usr/bin/env python3
"""halftime_fees.py — a published booking-fee range for each act the page shows.
(Phase 5: R40, the cost x viability view)

Justin, 2026-09-23: "a visual of acts across a cost spectrum x viability, with
a budget ($25K) that narrows the universe to realistic options."

The dashboard prices an act from the best basis it has, in this order:
documented figure > his agent's roster > a PUBLISHED range (this file) > room
scale; anything else is "unpriced". This job supplies the third.

THE SOURCE. One agency site, celebritytalent.net, publishes a structured
"Min Fee Range - U.S. Dates" for performers, plus an "example fee to book X is
in the starting range of $A-$B" sentence. Speaker bureaus (Gotham, All American
Speakers) were measured and REFUSED: their figure is a speaking fee, not a
performance fee -- Bret Michaels reads $100K-$125K there to speak.

Calibrated before use (S277), against the all-in figures Justin's agent quoted
for the same acts: same band for 4 of 8 (Montell Jordan, Tone Loc, Vanilla Ice,
Yung Gravy), 1.5-3x higher for 2 (Andra Day, Flo Rida), lower for 1 (Flavor
Flav), and 27x for Backstreet Boys. A usable stand-in, weaker than a quote --
which is why the roster outranks it.

IDENTITY IS THE RISK, so a range is kept only when: the site's own search
returns exactly ONE act whose URL name equals ours (letters and digits only),
the page lists it as a Performer, and the fee sentence names the act. Anything
else is recorded with the reason and priced as nothing.

No model, no paid search: two plain GETs per act, a second apart, with a UA
that says who we are (robots.txt allows /sampletalent/). Runs after the
profiles at the end of the nightly catalogue job. Sends nothing. 3.9-safe.
"""
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_DIR = Path(__file__).resolve().parent
OUT_PATH = PROJECT_DIR / "out" / "halftime" / "fees.json"
SITE = "https://www.celebritytalent.net"
SEARCH_URL = SITE + "/sampletalent/index.php?"
RECHECK_DAYS = 30
MAX_PER_RUN = 80
RECORD_VERSION = 1
PAUSE_S = 1.0
UA = "cowork-halftime-research/1.0 (cirrustask@gmail.com)"

_LINK = re.compile(r'href="(' + re.escape(SITE) +
                   r'/sampletalent/(\d+)/([^/"]+)/?)"')
_RANGE = r"\$([\d,]+)\s*-\s*\$([\d,]+)"
_FIELD = re.compile(r"Min Fee Range - U\.S\. Dates\s*(?:" + _RANGE + r")?")
_EXAMPLE = re.compile(r"An example fee to book (.{1,80}?) is in the starting "
                      r"range of\s*(?:" + _RANGE + r")?")
_TYPE = re.compile(r"Categories Type (.{0,40}?) Style ")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _key(name: str) -> str:
    import halftime_routing
    return halftime_routing.canonical_key(name)


def ident(name: str) -> str:
    """Letters and digits of the name, 'the' and a trailing parenthetical
    dropped: 'The Rolling Stones' / 'rolling-stones', 'Dan + Shay' /
    'dan-and-shay', 'Sugarhill Gang' / 'sugar-hill-gang' all meet."""
    n = re.sub(r"\s*\([^)]*\)\s*$", "", (name or "").strip().lower())
    n = n.replace("&", " and ").replace("+", " and ")
    n = re.sub(r"[^a-z0-9]+", " ", n).strip()
    if n.startswith("the "):
        n = n[4:]
    return n.replace(" ", "")


def page_text(raw: str) -> str:
    raw = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw,
                 flags=re.S | re.I)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", raw)).split())


def match(name: str, search_html: str) -> tuple:
    """(url, '') for exactly one result whose URL name is ours, else
    (None, reason)."""
    want = ident(name)
    urls = sorted({u for u, _id, slug in _LINK.findall(search_html or "")
                   if ident(slug.replace("-", " ")) == want})
    if not urls:
        return None, "not listed"
    if len(urls) > 1:
        return None, "{} listings share the name".format(len(urls))
    return urls[0], ""


def _dollars(s: str) -> int:
    return int(s.replace(",", ""))


def parse_page(name: str, raw: str) -> Dict:
    """{low, high, quote} or {reason}."""
    text = page_text(raw)
    typ = _TYPE.search(text)
    if not typ or "performer" not in typ.group(1).lower():
        return {"reason": "not listed as a performer"}
    ex = _EXAMPLE.search(text)
    if not ex or ident(ex.group(1)) != ident(name):
        return {"reason": "fee sentence names a different act"}
    field = _FIELD.search(text)
    got = (field.group(1), field.group(2)) if field and field.group(1) \
        else (ex.group(2), ex.group(3)) if ex.group(2) else None
    if not got:
        return {"reason": "no figure published (\"please contact\")"}
    low, high = _dollars(got[0]), _dollars(got[1])
    if not 0 < low <= high:
        return {"reason": "range unreadable"}
    quote = text[ex.start():text.find(".", ex.end()) + 1] \
        if ex.group(2) else "Min Fee Range - U.S. Dates ${:,}-${:,}".format(
            low, high)
    return {"low": low, "high": high, "quote": quote}


def _get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(2_000_000).decode("utf-8", "replace")


def price_act(name: str, getter=_get, pause: float = PAUSE_S) -> Dict:
    rec = {"name": name, "checked_at": _now(), "v": RECORD_VERSION,
           "low": None, "high": None, "url": "", "quote": "", "reason": "",
           "error": None}
    try:
        try:
            found = getter(SEARCH_URL + urllib.parse.urlencode(
                {"term": name, "search": ""}))
        except urllib.error.HTTPError as e:
            # S277: the site 404s some queries outright ("Psychedelic Porn
            # Crumpets") -- no page for that name, not a failed lookup.
            if e.code != 404:
                raise
            found = ""
        url, why = match(name, found)
        if not url:
            rec["reason"] = why
            return rec
        time.sleep(pause)
        got = parse_page(name, getter(url))
    except Exception as e:
        rec["error"] = type(e).__name__
        return rec
    rec["url"] = url
    if "reason" in got:
        rec["reason"] = got["reason"]
    else:
        rec.update(low=got["low"], high=got["high"], quote=got["quote"])
    return rec


def acts_to_price(snapshot: Dict) -> List[Dict]:
    """Every act the page lists for a game still to come, the foot lists, and
    his agent's roster (so the page can show where a published range and a
    real quote disagree)."""
    import halftime_dashboard as hd
    seen, out = set(), []

    def add(name):
        k = _key(name or "")
        if k and k not in seen:
            seen.add(k)
            out.append({"name": name, "key": k})
    for pool in ("roster", "roster_held", "fans"):
        for a in snapshot.get(pool) or []:
            add(a.get("name"))
    for g in snapshot.get("games") or []:
        if g.get("completed"):
            continue
        for lst in (g.get("candidates") or {}).values():
            for a in lst:
                add(a.get("name"))
        for aside in (g.get("set_aside") or {}).values():
            for lst in aside.values():
                for a in lst:
                    add(a.get("name"))
    for e in hd.agent_fees().values():
        add(e["name"])
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
        getter=None, today: Optional[str] = None, limit: int = MAX_PER_RUN,
        pause: float = PAUSE_S) -> Dict:
    import halftime_dashboard as hd
    import halftime_routing
    today = today or hd.today_et()
    out = Path(out_path) if out_path else OUT_PATH
    snap = load(snapshot_path or hd.OUT_DIR / "snapshot.json")
    prior = load(out).get("acts") or {}
    todo = [a for a in acts_to_price(snap)
            if not _fresh(prior.get(a["key"]), today)][:limit]
    acts = dict(prior)
    for i, a in enumerate(todo):
        if i:
            time.sleep(pause)
        acts[a["key"]] = price_act(a["name"], getter or _get, pause)
    out.parent.mkdir(parents=True, exist_ok=True)
    halftime_routing._write_atomic(out, json.dumps(
        {"generated_at": _now(), "source": SITE, "acts": acts}, indent=2))
    done = [acts[a["key"]] for a in todo]
    return {"checked": len(todo),
            "priced": sum(1 for r in done if r["low"]),
            "not_listed": sum(1 for r in done if r["reason"] == "not listed"),
            "errors": sum(1 for r in done if r["error"]),
            "known": len(acts),
            "known_priced": sum(1 for r in acts.values() if r.get("low"))}


# ── selftest ────────────────────────────────────────────────────────────────

_SEARCH_FIXTURE = """
<a href="https://www.celebritytalent.net/sampletalent/23621/the-clarks/">x</a>
<a href="https://www.celebritytalent.net/sampletalent/6829/the-killers/">x</a>
<a href="https://www.celebritytalent.net/sampletalent/3/sugar-hill-gang/">x</a>
<a href="https://www.celebritytalent.net/sampletalent/7/ernest/">x</a>
<a href="https://www.celebritytalent.net/sampletalent/8/ernest/">x</a>
<a href="https://evil.example/sampletalent/9/the-clarks/">x</a>
"""


def _page(name, field="$25,000-$39,999", example="$25,000-$39,999",
          typ="Performer"):
    return ("<html><script>var a='Min Fee Range - U.S. Dates $1-$2';</script>"
            "<p>An example fee to book {n} is in the starting range of {e}. "
            "However, any recent popularity change would cause a price "
            "fluctuation.</p><div>Categories Type {t} Style Rock Range Rock "
            "Min Fee Range - U.S. Dates {f} Min Fee Range - Intl. Dates</div>"
            "</html>").format(n=name, e=example, t=typ, f=field)


def selftest() -> int:
    import tempfile
    failures = []

    def check(label, ok):
        print("  {} {}".format("ok  " if ok else "FAIL", label))
        if not ok:
            failures.append(label)

    print("halftime_fees selftest")
    check("names meet across 'the', '+', '&' and hyphens",
          ident("The Rolling Stones") == ident("rolling stones")
          and ident("Dan + Shay") == ident("dan and shay")
          and ident("Sugarhill Gang") == ident("sugar hill gang")
          and ident("Treach (Naughty by Nature)") == "treach")
    check("a single exact listing is matched",
          match("The Clarks", _SEARCH_FIXTURE)[0]
          == "https://www.celebritytalent.net/sampletalent/23621/the-clarks/")
    check("spacing differences still match (Sugarhill / sugar-hill)",
          match("Sugarhill Gang", _SEARCH_FIXTURE)[0].endswith(
              "/3/sugar-hill-gang/"))
    check("two listings with our name are refused, not guessed",
          match("Ernest", _SEARCH_FIXTURE) == (None,
                                               "2 listings share the name"))
    check("a near name is not a match (Clark != The Clarks)",
          match("Clark", _SEARCH_FIXTURE) == (None, "not listed"))
    check("a link to another site is never followed",
          all("evil" not in (match(n, _SEARCH_FIXTURE)[0] or "")
              for n in ("The Clarks",)))

    got = parse_page("The Clarks", _page("The Clarks"))
    check("the U.S. field is read", (got.get("low"), got.get("high"))
          == (25000, 39999))
    check("the evidence quote is the site's own sentence",
          got.get("quote", "").startswith(
              "An example fee to book The Clarks is in the starting range "
              "of $25,000-$39,999."))
    got = parse_page("Baha Men", _page("Baha Men",
                                       field="Please Contact For Fee"))
    check("'please contact' in the field falls back to the example sentence",
          (got.get("low"), got.get("high")) == (25000, 39999))
    got = parse_page("Rolling Stones", _page(
        "Rolling Stones", field="Please Contact For Fee", example=""))
    check("no figure anywhere -> a reason, no price",
          "low" not in got and "please contact" in got["reason"])
    check("a script's text is not read as the fee",
          parse_page("X", _page("X", field="Please Contact For Fee",
                                example="")).get("low") is None)
    check("a speaker listing is refused",
          parse_page("Bret Michaels", _page("Bret Michaels",
                                            typ="Speaker"))
          == {"reason": "not listed as a performer"})
    check("a page whose fee sentence names someone else is refused",
          parse_page("The Clarks", _page("The Killers")).get("reason")
          == "fee sentence names a different act")
    check("a backwards range is refused",
          parse_page("X", _page("X", field="$50,000-$10,000")).get("reason")
          == "range unreadable")

    calls = []

    def getter(url):
        calls.append(url)
        if "index.php" in url:
            return _SEARCH_FIXTURE
        if "23621" in url:
            return _page("The Clarks")
        raise OSError("down")
    rec = price_act("The Clarks", getter, pause=0)
    check("price_act: search, one fetch, a sourced range",
          rec["low"] == 25000 and rec["url"].endswith("/the-clarks/")
          and len(calls) == 2 and "term=The+Clarks" in calls[0])
    rec = price_act("Nobody At All", getter, pause=0)
    check("price_act: not listed -> no price, the reason kept",
          rec["low"] is None and rec["reason"] == "not listed")
    rec = price_act("Sugarhill Gang", getter, pause=0)
    check("price_act: a network failure is an error (retried), not a price",
          rec["low"] is None and rec["error"] == "OSError")

    def g404(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    rec = price_act("Psychedelic Porn Crumpets", g404, pause=0)
    check("price_act: a search the site 404s is 'not listed', not an error",
          rec["reason"] == "not listed" and rec["error"] is None)

    def g500(url):
        raise urllib.error.HTTPError(url, 500, "Server Error", {}, None)
    check("price_act: ...but a server error is still an error (retried)",
          price_act("X", g500, pause=0)["error"] == "HTTPError")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        snap = {"roster": [{"name": "The Clarks"}],
                "roster_held": [], "fans": [{"name": "Clarks"}],
                "games": [{"completed": True,
                           "candidates": {"touring": [{"name": "Old Act"}]}},
                          {"candidates": {"touring": [{"name": "Nobody"}]},
                           "set_aside": {"touring": {"conflict": [
                               {"name": "Sugarhill Gang"}]}}}]}
        names = [a["name"] for a in acts_to_price(snap)]
        check("acts_to_price: shown + set-aside + agent roster, once each; "
              "played games skipped",
              names[:3] == ["The Clarks", "Nobody", "Sugarhill Gang"]
              and "Old Act" not in names and "Rob Base" in names
              and names.count("The Clarks") == 1)
        (tmp / "snap.json").write_text(json.dumps(snap))
        out = tmp / "fees.json"
        calls.clear()
        res = run(snapshot_path=tmp / "snap.json", out_path=out,
                  getter=getter, today="2026-09-24", limit=3, pause=0)
        data = load(out)
        check("run: capped, written, counted",
              res["checked"] == 3 and res["priced"] == 1
              and data["acts"]["clarks"]["low"] == 25000
              and data["source"] == SITE)
        calls.clear()
        res = run(snapshot_path=tmp / "snap.json", out_path=out,
                  getter=getter, today="2026-09-25", limit=3, pause=0)
        check("run: a fresh record is not re-fetched; an errored one is",
              "term=The+Clarks" not in " ".join(calls)
              and "term=Sugarhill+Gang" in " ".join(calls))
        calls.clear()
        run(snapshot_path=tmp / "snap.json", out_path=out, getter=getter,
            today="2026-10-30", limit=1, pause=0)
        check("run: a record older than {} days is re-checked".format(
            RECHECK_DAYS), "term=The+Clarks" in " ".join(calls))

    print()
    print("{} failure(s)".format(len(failures)) if failures else "ALL PASS")
    return 1 if failures else 0


USAGE = "usage: halftime_fees.py [selftest]   (no argument = run)"


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
