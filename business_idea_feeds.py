"""business_idea_feeds.py — discover, trial, and retire content sources (S66).

Buddy's ask: don't stay limited to the feeds we already read. Find new ones,
try them for a day or two, keep the ones that produce something useful and
drop the ones that don't.

The whole point is that this decides itself on EVIDENCE rather than on
anyone's guess about which publication looks promising:

  propose  -> the council suggests real feeds, given what we're hunting for
  verify   -> the URL must actually return a parseable feed with recent items,
              checked live -- an LLM-suggested RSS URL is frequently wrong or
              long dead, so nothing is added on the model's say-so alone
  trial    -> added to config/business_idea_trial_feeds.json and scanned like
              any other source
  judge    -> after TRIAL_DAYS, business_idea_scan.source_productivity() says
              whether it ever produced a candidate. Kept feeds are promoted,
              barren ones retired, and retired URLs are remembered so the
              council can't re-propose the same dead feed next month.

Usage:
  python3 business_idea_feeds.py discover [--n 5]   # propose + verify + trial
  python3 business_idea_feeds.py judge              # promote/retire on evidence
  python3 business_idea_feeds.py list
  python3 business_idea_feeds.py selftest
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from business_idea_scan import (CAPABILITIES, MISSION, PROJECT_DIR,
                                all_sources, source_productivity)

TRIAL_PATH = PROJECT_DIR / "config/business_idea_trial_feeds.json"
TRIAL_DAYS = 3          # long enough for a daily feed to publish something
MIN_ITEMS_TO_JUDGE = 5  # don't retire a feed that has barely been sampled


def _load() -> dict:
    try:
        d = json.loads(TRIAL_PATH.read_text())
    except Exception:
        d = {}
    d.setdefault("trial", [])      # under evaluation
    d.setdefault("promoted", [])   # earned a permanent place
    d.setdefault("retired", [])    # tried, produced nothing -- never re-add
    return d


def _save(d: dict) -> None:
    try:
        TRIAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        TRIAL_PATH.write_text(json.dumps(d, indent=1, sort_keys=True))
    except Exception:
        pass


def active_feeds() -> list:
    """Trial + promoted feeds, for business_idea_scan.all_sources()."""
    d = _load()
    return [{"name": f["name"], "rss": f["rss"]} for f in d["trial"] + d["promoted"]]


# S66: the first live discovery run had all 6 suggestions fail verification
# (4x HTTP 404, 1 unparseable, 1 stale). The models know which publications
# EXIST but reliably guess their feed PATH wrong -- so stop asking for feed
# URLs. Ask for the publication's homepage instead and resolve the real feed
# from the site itself: RSS autodiscovery first (the <link rel="alternate">
# tag publishers put there for exactly this), then the handful of
# conventional paths.
_COMMON_FEED_PATHS = ("/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml",
                      "/index.xml", "/feed/", "/blog/feed")


def find_feed_url(site: str, timeout: int = 15) -> str:
    """Resolve a site's real feed URL, or '' if it has none."""
    import re as _re
    from urllib.parse import urljoin
    try:
        import requests
    except Exception:
        return ""
    site = site.strip()
    if not site.startswith("http"):
        site = "https://" + site
    headers = {"User-Agent": "CIRRUS-digest/1.0"}
    try:
        r = requests.get(site, timeout=timeout, headers=headers)
        if r.status_code == 200:
            # RSS autodiscovery -- the authoritative answer when present.
            for m in _re.finditer(r"<link[^>]+>", r.text[:200000], _re.IGNORECASE):
                tag = m.group(0)
                if "alternate" in tag.lower() and _re.search(
                        r"application/(rss|atom)\+xml", tag, _re.IGNORECASE):
                    href = _re.search(r'href=["\']([^"\']+)["\']', tag, _re.IGNORECASE)
                    if href:
                        return urljoin(site, href.group(1))
    except Exception:
        pass
    for path in _COMMON_FEED_PATHS:
        cand = urljoin(site, path)
        ok, _detail = verify_feed(cand, timeout=timeout)
        if ok:
            return cand
    return ""


def verify_feed(rss: str, timeout: int = 20) -> tuple:
    """(ok, detail). A suggested RSS URL is wrong or dead often enough that
    nothing may be added without a live check."""
    try:
        import feedparser
        import requests
        r = requests.get(rss, timeout=timeout,
                         headers={"User-Agent": "CIRRUS-digest/1.0"})
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        feed = feedparser.parse(r.text)
        if not feed.entries:
            return False, "parsed but no entries"
        recent = 0
        cutoff = datetime.now() - timedelta(days=45)
        for e in feed.entries[:15]:
            pp = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
            if pp and datetime(*pp[:6]) >= cutoff:
                recent += 1
        if not recent:
            return False, "no items in the last 45 days (stale feed)"
        return True, f"{len(feed.entries)} entries, {recent} recent"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:60]}"


def propose(n: int, creds: dict) -> list:
    """Ask the council for feeds we don't already read. Returns raw
    suggestions -- callers must verify before trusting any of them."""
    d = _load()
    # include_trials=False -- we add those from the store below, and asking
    # all_sources() for them here would recurse back into this module.
    known = {s["rss"] for s in all_sources(include_trials=False)}
    known |= {f["rss"] for f in d["trial"] + d["promoted"] + d["retired"]}
    avoid = "\n".join(f"- {u}" for u in sorted(known)[:60]) or "(none yet)"
    prompt = (
        f"{MISSION}\n\n{CAPABILITIES}\n\n"
        f"We already read, or have already tried and rejected, these feeds:\n{avoid}\n\n"
        f"Suggest {n} DIFFERENT RSS/Atom feeds likely to describe real, "
        f"operating small businesses and their revenue models -- founder "
        f"write-ups, teardowns, indie/bootstrapped business coverage, niche "
        f"trade publications. Favor sources that publish concrete numbers and "
        f"specifics over general tech or AI news, and prefer ones a large "
        f"audience is NOT already mining for ideas.\n"
        f"Give each publication's HOMEPAGE URL -- do NOT try to give the feed "
        f"path, we resolve that ourselves. Only suggest publications you are "
        f"confident actually exist at that domain.\n"
        f'Respond as JSON only: {{"feeds": [{{"name": "...", "site": "https://...", '
        f'"why": "one sentence"}}]}}'
    )
    try:
        import ensemble
        _m, text = ensemble.best_answer(
            "You suggest content sources. You give real, working feed URLs and "
            "omit anything you are unsure of rather than inventing a plausible path.",
            prompt, creds, max_tokens=1200, task="business-idea-feed-discovery",
            mode="council")
        t = (text or "").strip()
        if t.startswith("```"):
            t = t.strip("`")
            if t.lower().startswith("json"):
                t = t[4:]
        return (json.loads(t) or {}).get("feeds", []) or []
    except Exception as e:
        print(f"  proposal failed: {e}")
        return []


def discover(n: int = 5) -> dict:
    creds = {}
    try:
        creds = json.loads((PROJECT_DIR / "config/credentials.json").read_text())
    except Exception:
        pass
    d = _load()
    known = {s["rss"] for s in all_sources(include_trials=False)} | {
        f["rss"] for f in d["trial"] + d["promoted"] + d["retired"]}
    added, rejected = [], []
    for f in propose(n, creds):
        name = (f.get("name") or "").strip()
        site = (f.get("site") or f.get("rss") or "").strip()
        if not name or not site:
            continue
        # Resolve the real feed from the site rather than trusting a guessed
        # path; accept a direct feed URL too, if one was given anyway.
        rss = site if verify_feed(site)[0] else find_feed_url(site)
        if not rss:
            rejected.append(f"{name}: no discoverable feed at {site}")
            d["retired"].append({"name": name, "rss": site,
                                 "retired": datetime.now().strftime("%Y-%m-%d"),
                                 "reason": "no discoverable feed"})
            continue
        if rss in known:
            rejected.append(f"{name}: already covered")
            continue
        ok, detail = verify_feed(rss)
        if not ok:
            rejected.append(f"{name}: {detail}")
            # Remember dead suggestions so the council can't re-propose them.
            d["retired"].append({"name": name, "rss": rss,
                                 "retired": datetime.now().strftime("%Y-%m-%d"),
                                 "reason": f"failed verification ({detail})"})
            continue
        d["trial"].append({"name": name, "rss": rss, "why": f.get("why", ""),
                           "started": datetime.now().strftime("%Y-%m-%d")})
        known.add(rss)
        added.append(f"{name} ({detail})")
    _save(d)
    return {"added_to_trial": added, "rejected": rejected}


def judge() -> dict:
    """Promote trial feeds that produced a candidate; retire those that were
    sampled enough and produced none."""
    d = _load()
    prod = {name: (seen, adm) for name, seen, adm, _v in source_productivity()}
    cutoff = datetime.now() - timedelta(days=TRIAL_DAYS)
    promoted, retired, still_trialing = [], [], []
    for f in d["trial"]:
        try:
            started = datetime.strptime(f.get("started", ""), "%Y-%m-%d")
        except Exception:
            started = datetime.now()
        seen, adm = prod.get(f["name"], (0, 0))
        if started > cutoff:
            still_trialing.append(f"{f['name']} (day {(datetime.now()-started).days+1}/{TRIAL_DAYS})")
            f["_keep"] = True
        elif adm > 0:
            f["promoted"] = datetime.now().strftime("%Y-%m-%d")
            d["promoted"].append(f)
            promoted.append(f"{f['name']} ({adm} candidate(s) from {seen} item(s))")
        elif seen < MIN_ITEMS_TO_JUDGE:
            # Barely sampled -- a quiet feed deserves more time, not a verdict.
            still_trialing.append(f"{f['name']} (only {seen} item(s) seen, extending)")
            f["_keep"] = True
        else:
            f["retired"] = datetime.now().strftime("%Y-%m-%d")
            f["reason"] = f"0 candidates from {seen} items over {TRIAL_DAYS}d"
            d["retired"].append(f)
            retired.append(f"{f['name']} (0 from {seen})")
    d["trial"] = [f for f in d["trial"] if f.pop("_keep", False)]
    _save(d)
    return {"promoted": promoted, "retired": retired, "still_trialing": still_trialing}


def selftest() -> bool:
    import os
    import tempfile
    checks = []
    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        globals()["TRIAL_PATH"] = Path(tmp)
        Path(tmp).write_text(json.dumps({"trial": [], "promoted": [], "retired": []}))
        d = _load()
        checks.append(("empty store loads with all three buckets",
                       set(d) >= {"trial", "promoted", "retired"}))

        old = (datetime.now() - timedelta(days=TRIAL_DAYS + 1)).strftime("%Y-%m-%d")
        new = datetime.now().strftime("%Y-%m-%d")
        d["trial"] = [
            {"name": "Productive", "rss": "https://a/feed", "started": old},
            {"name": "Barren", "rss": "https://b/feed", "started": old},
            {"name": "TooNew", "rss": "https://c/feed", "started": new},
            {"name": "BarelySampled", "rss": "https://d/feed", "started": old},
        ]
        _save(d)
        globals()["source_productivity"] = lambda: [
            ("Productive", 20, 2, ""), ("Barren", 20, 0, ""),
            ("TooNew", 3, 0, ""), ("BarelySampled", 2, 0, ""),
        ]
        r = judge()
        checks.append(("a feed that produced candidates is promoted",
                       any("Productive" in x for x in r["promoted"])))
        checks.append(("a well-sampled barren feed is retired",
                       any("Barren" in x for x in r["retired"])))
        checks.append(("a feed still inside its trial window is left alone",
                       any("TooNew" in x for x in r["still_trialing"])))
        checks.append(("a barely-sampled feed gets more time, not a verdict",
                       any("BarelySampled" in x for x in r["still_trialing"])))
        after = _load()
        checks.append(("retired feeds are remembered so they can't be re-proposed",
                       any(f["name"] == "Barren" for f in after["retired"])))
        checks.append(("promoted + still-trialing feeds remain active",
                       {f["name"] for f in active_feeds()}
                       >= {"Productive", "TooNew", "BarelySampled"}))
        checks.append(("a retired feed is NOT active",
                       "Barren" not in {f["name"] for f in active_feeds()}))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    all_ok = all(ok for _, ok in checks)
    for desc, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
    return all_ok


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "selftest":
        sys.exit(0 if selftest() else 1)
    if cmd == "cycle":
        # The scheduled mode: judge every day (a trial that has run its
        # course gets promoted or retired promptly), but only hunt for new
        # feeds weekly -- discovery is a council call with a low yield, and
        # running it daily would mostly re-reject the same dead suggestions.
        out = {"judged": judge()}
        if datetime.now().weekday() == 6:  # Sunday
            out["discovered"] = discover(6)
        print(json.dumps(out, indent=2))
        try:
            import job_status
            job_status.record("businessideafeeds", True,
                              f"{len(out['judged']['promoted'])} promoted, "
                              f"{len(out['judged']['retired'])} retired, "
                              f"{len(out['judged']['still_trialing'])} on trial")
        except Exception:
            pass
    elif cmd == "discover":
        n = 5
        if "--n" in sys.argv:
            n = int(sys.argv[sys.argv.index("--n") + 1])
        print(json.dumps(discover(n), indent=2))
    elif cmd == "judge":
        print(json.dumps(judge(), indent=2))
    else:
        d = _load()
        for bucket in ("trial", "promoted", "retired"):
            print(f"{bucket.upper()} ({len(d[bucket])})")
            for f in d[bucket]:
                print(f"  - {f.get('name')} — {f.get('rss')}"
                      + (f"  [{f.get('reason')}]" if f.get("reason") else ""))
