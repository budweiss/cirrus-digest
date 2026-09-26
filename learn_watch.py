#!/usr/bin/env python3
"""learn_watch.py -- a daily "what can we learn" read of Medium + Substack (S308).

Buddy, 2026-09-26: review ALL Medium and Substack entries daily -- not only the
keyword-matched ones the digest keeps -- for anything that could improve CIRRUS
and CUMULUS or inform STRATUS (Cowork, Codex, agent harnesses, instruction .md
files, Mac Studio, DGX Spark, Ollama / local models, recursive self-improvement),
and extract it with LOCAL models.

  feeds    every Medium/Substack feed (by address) in config/sources.json and
           the CIRRUS overlay sources.local.json, plus learn-watch/feeds.json
           (Medium tag feeds and the Substacks accepted in S310).
  window   posts from the last 36 h; each missed day adds 24 h (S310, Buddy).
  text     the RSS content. Medium article PAGES answer 403 from Cloudflare to
           any server-side client -- measured S308, 8 of 8, with or without the
           stored cookies -- but Medium's RSS is not blocked, so a tag-feed post's
           full text comes from its author/publication feed (9 of 18 full; the
           rest are member-only teasers, read as teasers and marked so).
  extract  media_pipeline 'analyze' on the CUMULUS worker (local Qwen on C2) in
           claims mode: every claim carries a verbatim quote checked against the
           text, so nothing the model invents is published.
  output   learn-watch/findings/learn-watch-YYYY-MM-DD.md (everything), an email
           to Buddy with the top 15 when anything was learned, claims.jsonl (read
           by stratus/stratus_monthly.py), and job_status 'learnwatch'.

NOT an input to the dev loop, on purpose: S81 found article-derived build
proposals were mostly noise ("Install M5 Ultra Mac Studio"). This is reading
material for Buddy and for sessions; STRATUS-tagged items are the ones the
STRATUS research log wants.

    python3 learn_watch.py [--dry-run] [--limit N]
    python3 learn_watch.py selftest
"""
import html
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
SOURCES_PATH = HERE / "config" / "sources.json"
OVERLAY_PATH = HERE / "config" / "sources.local.json"   # Telegram-approved, CIRRUS only
FEEDS_PATH = HERE / "learn-watch" / "feeds.json"
SEEN_PATH = HERE / "learn-watch" / "seen.json"
CLAIMS_LOG = HERE / "learn-watch" / "claims.jsonl"   # machine-readable; stratus_monthly reads it
OUT_DIR = HERE / "learn-watch" / "findings"
CREDS_PATH = HERE / "config" / "credentials.json"
COOKIES_PATH = HERE / "config" / "cookies.json"     # Buddy's sessions, synced from his Mac
TO_ADDR = "Buddy.Weiss@outlook.com"

LIMIT = 100          # posts analysed per run -- above a 36 h window's measured
                     # volume, so the window, not the limit, decides what is read
WINDOW_H = 36        # Buddy, S310: read the last 36 h of posts...
OVERLAP_H = 12       # ...and when a day is skipped, add 24 h per missed day. Both
                     # are one rule: since = min(now - 36 h, last success - 12 h).
MAX_AGE_DAYS = 14    # a hard ceiling on that window after a long outage
EMAIL_TOP = 15       # Buddy, S310: the email carries the top 15; the file has all
BROWSER_LIMIT = 40   # member-only pages opened per run (~5-8 s each)
BROWSER_PAUSE = 3.0
TEASER = 1500        # below this many characters we only had a teaser
UNREADABLE = 300     # below this there is nothing to read -- a title, a byline.
                     # S308: a 35-char "post" scored "nothing for us" is a lie.
MAX_CHARS = 60000
FEED_PAUSE = 1.0
SEEN_KEEP = 5000

INSTRUCTIONS = (
    "You read articles for a small team that runs its own AI servers, looking ONLY for "
    "lessons that could improve those servers or how the team runs them. Our stack: "
    "CIRRUS, a Mac Studio M4 Max (64 GB) running Ollama and scheduled Python jobs; "
    "CUMULUS, two NVIDIA DGX Spark GB10 nodes (128 GB unified memory each, 200GbE link) "
    "serving GPT-OSS 120B in Ollama and Qwen 27B FP8 in vLLM; agent harnesses: Claude "
    "Code / Cowork, OpenAI Codex, the Claude Agent SDK; agent instruction files "
    "(CLAUDE.md, AGENTS.md, skills, memory .md files); a self-improving dev loop that "
    "builds, tests and learns from its own runs; and STRATUS, a larger production "
    "server we are still sizing (more Sparks, a DGX Station, clustering, storage, "
    "networking). Relevant: agent harness and instruction-file practice, local model "
    "serving and quantization, measured hardware results, evaluation and "
    "self-improvement loops, production sizing. NOT relevant: general AI news, funding, "
    "opinion without a method, product marketing, tutorials for beginners. Distinguish "
    "the author's claims from established facts; do not assume we lack something."
)

# Deterministic area tags, matched on title + quote. Cheap, testable, and the
# model never has to be trusted to label its own output.
AREAS = [
    ("Agent harness & instruction files",
     ("cowork", "claude code", "codex", "harness", "agents.md", "claude.md", "skill",
      "subagent", "sub-agent", "mcp", "system prompt", "memory file", "markdown",
      "agent sdk", "context engineering")),
    ("Local models & serving",
     ("ollama", "vllm", "llama.cpp", "gguf", "quantiz", "fp8", "nvfp4", "local model",
      "local llm", "qwen", "gpt-oss", "gemma", "tokens per second", "tok/s",
      "context window", "speculative", "kv cache", "inference")),
    ("Hardware",
     ("mac studio", "dgx spark", "gb10", "m4 max", "m5", "unified memory",
      "memory bandwidth", "gpu", "nvlink", "rdma", "connectx", "200gbe", "thermal")),
    ("Self-improvement & evals",
     ("recursive", "self-improv", "feedback loop", "eval", "benchmark", "reflection",
      "learn from", "regression test", "selftest")),
    ("STRATUS (production sizing)",
     ("cluster", "production", "datacenter", "data center", "rack", "dgx station",
      "gb300", "scale out", "scale-out", "nas", "storage", "400g", "switch",
      "multi-node", "two-node")),
]


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def platform_of(source):
    """medium / substack / None, by the feed's ADDRESS as well as its label.
    S310: the Telegram overlay files Substacks as "blog" (The Innermost Loop),
    so a label-only filter never read them."""
    if source.get("type") in ("medium", "substack"):
        return source["type"]
    host = urlparse(source.get("rss", "")).netloc.lower()
    if host == "medium.com" or host.endswith(".medium.com"):
        return "medium"
    if host.endswith(".substack.com"):
        return "substack"
    return None


def load_feeds(sources_path=SOURCES_PATH, feeds_path=FEEDS_PATH, overlay_path=OVERLAY_PATH):
    listed = load_json(sources_path, {}).get("web_sources", []) + \
        [s for s in load_json(overlay_path, []) if isinstance(s, dict)]
    feeds, urls = [], set()
    for s in listed:
        kind = platform_of(s)
        if kind and s.get("rss") and s["rss"] not in urls:
            urls.add(s["rss"])
            feeds.append({"name": s.get("name", s["rss"]), "rss": s["rss"], "kind": kind})
    for f in load_json(feeds_path, {}).get("feeds", []):
        if f.get("rss") and f["rss"] not in urls:
            urls.add(f["rss"])
            feeds.append({"name": f["name"], "rss": f["rss"], "kind": f.get("kind", "medium-tag")})
    return feeds


def window_start(now, last_success):
    """36 h back, widened by 24 h for every day a run was missed."""
    since = now - timedelta(hours=WINDOW_H)
    if last_success:
        since = min(since, last_success - timedelta(hours=OVERLAP_H))
    return max(since, now - timedelta(days=MAX_AGE_DAYS))


def plain(markup):
    """HTML -> text. Crude on purpose: the model reads it, and quotes are checked
    against exactly this text, so what matters is that it is the SAME text."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", markup or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</h\d>|</li>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def mostly_latin(text):
    """False for a post mostly in another script. S308's first run spent C2 time
    on Bengali, Korean and Chinese posts and published quotes Buddy cannot read."""
    letters = [ch for ch in text[:4000] if ch.isalpha()]
    return not letters or sum(ch.isascii() for ch in letters) / len(letters) >= 0.6


def entry_text(entry):
    body = (entry.get("content") or [{}])[0].get("value", "") or entry.get("summary", "")
    return plain(body)


def post_key(link):
    """Medium post id when there is one (the same post appears under several
    tags and under its author feed), else the link without its query."""
    m = re.search(r"-([0-9a-f]{10,12})$", urlparse(link).path)
    return m.group(1) if m else link.split("?")[0].rstrip("/")


def source_feed(link):
    """The author or publication feed a Medium post lives in."""
    u = urlparse(link)
    parts = [p for p in u.path.split("/") if p]
    if u.netloc in ("medium.com", "www.medium.com") and parts:
        return "https://medium.com/feed/" + parts[0]
    return "https://%s/feed" % u.netloc


def full_text(entry, kind, parse):
    """RSS text; for a Medium tag-feed teaser, the author feed's copy if longer."""
    text = entry_text(entry)
    if kind == "medium-tag" and len(text) < TEASER:
        key = post_key(entry.get("link", ""))
        try:
            for x in parse(source_feed(entry.get("link", ""))).entries:
                if key and key == post_key(x.get("link", "")):
                    better = entry_text(x)
                    if len(better) > len(text):
                        text = better
                    break
        except Exception:
            pass
    return text


def session_domain(url):
    """Whose synced session opens this post: medium.com, substack.com, or None.
    A Substack on its own domain (latent.space) does not get the substack.com
    session, so it stays on its public preview."""
    host = urlparse(url).netloc.lower()
    if host == "medium.com" or host.endswith(".medium.com"):
        return "medium.com"
    if host.endswith(".substack.com"):
        return "substack.com"
    return None


class Browser:
    """Member-only posts, read through a real headless Chromium carrying Buddy's
    own synced session (config/cookies.json; values are never printed).

    S310, measured on CIRRUS: a plain request to a Medium post gets Cloudflare's
    "Just a moment" page (403); the same post in Chromium with Buddy's cookies
    returned 9,481 characters of article against a 1,221-character teaser
    without them. Started once per run, only when a teaser needs it."""

    def __init__(self, cookies_path=COOKIES_PATH, limit=BROWSER_LIMIT, pause=BROWSER_PAUSE):
        self.jar = load_json(cookies_path, {})
        self.limit, self.pause = limit, pause
        self.opened = self.challenged = 0
        self._pw = self._browser = None
        self._contexts = {}

    def _context(self, domain):
        if domain not in self._contexts:
            if self._browser is None:
                from playwright.sync_api import sync_playwright
                self._pw = sync_playwright().start()
                self._browser = self._pw.chromium.launch(headless=True)
            ctx = self._browser.new_context(user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"))
            ctx.add_cookies([{"name": k, "value": v, "domain": "." + domain, "path": "/"}
                             for k, v in (self.jar.get(domain) or {}).items() if isinstance(v, str)])
            self._contexts[domain] = ctx
        return self._contexts[domain]

    def text(self, url):
        domain = session_domain(url)
        if not domain or not self.jar.get(domain) or self.opened >= self.limit:
            return ""
        if self.opened and self.pause:
            time.sleep(self.pause)
        self.opened += 1
        page = self._context(domain).new_page()
        try:
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            if page.title().startswith("Just a moment"):
                self.challenged += 1        # the session needs refreshing on the Mac
                return ""
            node = page.locator("article")
            return plain(node.first.inner_text()) if node.count() else ""
        except Exception as e:
            log("  browser: %s on %s" % (type(e).__name__, url[:70]))
            return ""
        finally:
            page.close()

    def close(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass


def feed_unreadable(feed):
    status = feed.get("status") or 0
    return status >= 400 or (bool(feed.get("bozo")) and not feed.get("entries"))


def why_unreadable(feed):
    """The cause, not "HTTP ?": S308's first dry run hid a CIRRUS DNS outage
    behind six identical "HTTP ?" lines."""
    if feed.get("status"):
        return "HTTP %s" % feed.get("status")
    exc = feed.get("bozo_exception")
    return "%s: %s" % (type(exc).__name__, str(exc)[:70]) if exc else "no response"


def collect(feeds, seen, since, parse, pause=0.0):
    """Unseen posts from every feed, newest first, one per post. Pure given parse."""
    posts, feed_errors = {}, []
    for i, f in enumerate(feeds):
        if i and pause:
            time.sleep(pause)
        try:
            feed = parse(f["rss"])
        except Exception as e:
            feed_errors.append("%s: %s" % (f["name"], type(e).__name__))
            continue
        if feed_unreadable(feed):
            feed_errors.append("%s: %s" % (f["name"], why_unreadable(feed)))
            continue
        for e in feed.get("entries", []):
            link = e.get("link", "")
            p = e.get("published_parsed") or e.get("updated_parsed")
            if not link or not p:
                continue
            published = datetime(*p[:6])
            key = post_key(link)
            if key in seen or key in posts or published < since:
                continue
            posts[key] = {"key": key, "url": link.split("?")[0], "title": e.get("title", "").strip(),
                          "source": f["name"], "kind": f["kind"],
                          "published": published.strftime("%Y-%m-%d"), "entry": e}
    return sorted(posts.values(), key=lambda p: p["published"], reverse=True), feed_errors


def areas_for(text):
    t = text.lower()
    found = [name for name, words in AREAS if any(w in t for w in words)]
    return found or ["Other"]


def capped(text, limit=MAX_CHARS):
    """Cut long posts WITH a marker, so a cut is never mistaken for the whole (T40)."""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n[truncated at %d of %d characters]" % (limit, len(text))


def default_analyze(text, meta):
    import media_pipeline as media
    return media.call("analyze", text=capped(text), instructions=INSTRUCTIONS,
                      domain="articles-infra", claims=True, metadata=meta)


def _read_posts(posts, parse, analyze, browser, results, errors, seen, seen_set,
                last_ok, seen_path, dry_run):
    for post in posts:
        text = full_text(post.pop("entry"), post["kind"], parse)
        if len(text) < TEASER:
            member = browser.text(post["url"])
            if len(member) > len(text):
                text, post["via_browser"] = member, True
        post["teaser"] = len(text) < TEASER
        post["unread"] = len(text) < UNREADABLE
        post["foreign"] = not post["unread"] and not mostly_latin(post["title"] + " " + text)
        meta = {k: post[k] for k in ("title", "url", "source", "published")}
        if post["unread"] or post["foreign"]:
            # member-only / unavailable, or not in a language Buddy reads:
            # listed or counted for Buddy, never scored
            post.update(claims=[], failed=False)
            results.append(post)
            seen.append(post["key"])
            seen_set.add(post["key"])
            log("  %s | %s | %s" % (post["source"][:18], post["title"][:50],
                                    "not English, skipped" if post["foreign"] else "could not read"))
            continue
        try:
            claims = analyze("%s\n\n%s" % (post["title"], text), meta) if text else []
        except (RuntimeError, OSError) as e:
            # the worker or the link is down: NOT seen, so tomorrow retries it
            errors.append("%s: %s" % (post["title"][:50], str(e)[:80]))
            continue
        except Exception as e:
            # a deterministic failure on this post: seen, so it cannot block the queue
            errors.append("%s: %s: %s" % (post["title"][:50], type(e).__name__, str(e)[:60]))
            claims = None
        for c in claims or []:
            c["areas"] = areas_for(post["title"] + " " + c.get("quote", ""))
        post["claims"] = claims or []
        post["failed"] = claims is None
        results.append(post)
        seen.append(post["key"])
        seen_set.add(post["key"])
        log("  %s | %s | %s" % (post["source"][:18], post["title"][:50],
                                ("%d claim(s)" % len(post["claims"])) if post["claims"]
                                else ("failed" if claims is None else "nothing for us")))
        if not dry_run:
            seen_path.parent.mkdir(parents=True, exist_ok=True)
            seen_path.write_text(json.dumps({"keys": seen[-SEEN_KEEP:], "last_success": last_ok}, indent=1))


def run(dry_run=False, limit=LIMIT, feeds=None, parse=None, analyze=None,
        seen_path=None, out_dir=None, now=None, pause=FEED_PAUSE, claims_log=None,
        browser=None):
    """Injectable throughout, so the selftest touches no network and no live file (T32)."""
    if parse is None:
        import feedparser
        parse = feedparser.parse
    feeds = load_feeds() if feeds is None else feeds
    analyze = analyze or default_analyze
    seen_path = Path(seen_path or SEEN_PATH)
    out_dir = Path(out_dir or OUT_DIR)
    now = now or datetime.now()

    state = load_json(seen_path, {})
    seen = state.get("keys", [])
    seen_set = set(seen)
    last_ok = state.get("last_success")
    since = window_start(now, datetime.fromisoformat(last_ok) if last_ok else None)
    posts, feed_errors = collect(feeds, seen_set, since, parse, pause)
    log("%d feed(s), %d unseen post(s) since %s (%.0f h), %d feed error(s)"
        % (len(feeds), len(posts), since.strftime("%m-%d %H:%M"),
           (now - since).total_seconds() / 3600, len(feed_errors)))

    results, errors = [], list(feed_errors)
    browser = browser if browser is not None else Browser()
    try:
        _read_posts(posts[:limit], parse, analyze, browser, results, errors, seen, seen_set,
                    last_ok, seen_path, dry_run)
    finally:
        browser.close()
    if browser.challenged:
        errors.append("browser: %d member page(s) answered with a Cloudflare challenge -- "
                      "the synced session may need refreshing on the Mac" % browser.challenged)
    day = now.strftime("%Y-%m-%d")
    body = render(results, day)
    stats_window = (now - since).total_seconds() / 3600
    if not dry_run:
        if results:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / ("learn-watch-%s.md" % day)).write_text(body)
            with open(claims_log or CLAIMS_LOG, "a") as fh:
                for r in results:
                    for c in r["claims"]:
                        fh.write(json.dumps({"date": day, "title": r["title"], "url": r["url"],
                                             "source": r["source"], "quote": c.get("quote", ""),
                                             "how_to_test": c.get("how_to_test", ""),
                                             "areas": c["areas"]}) + "\n")
        # the window's anchor moves only when the run actually worked, so a
        # worker outage widens tomorrow's window instead of losing a day
        analysed_ok = not posts or any(not r.get("failed") for r in results)
        if analysed_ok:
            seen_path.parent.mkdir(parents=True, exist_ok=True)
            seen_path.write_text(json.dumps({"keys": seen[-SEEN_KEEP:],
                                             "last_success": now.isoformat(timespec="seconds")}, indent=1))
    return {"feeds": len(feeds), "feed_errors": len(feed_errors), "unseen": len(posts),
            "window_h": round(stats_window), "results": results,
            "processed": len(results), "claims": sum(len(r["claims"]) for r in results),
            "with_claims": sum(1 for r in results if r["claims"]),
            "teasers": sum(1 for r in results if r["teaser"] and not r.get("unread")),
            "unread": sum(1 for r in results if r.get("unread")),
            "via_browser": sum(1 for r in results if r.get("via_browser")),
            "foreign": sum(1 for r in results if r.get("foreign")),
            "errors": errors, "body": body}


def render(results, day):
    """Findings grouped by area; one line per post that taught us nothing."""
    by_area = {}
    for r in results:
        for c in r["claims"]:
            by_area.setdefault(c["areas"][0], []).append((r, c))
    n = sum(len(r["claims"]) for r in results)
    lines = ["# Server learnings %s" % day, "",
             "%d post(s) read, %d with something for us, %d claim(s). Every quote below "
             "was checked word for word against the post. Claims are the author's, "
             "not tested by us." % (len(results), sum(1 for r in results if r["claims"]), n), ""]
    order = [a for a, _ in AREAS] + ["Other"]
    for area in order:
        if area not in by_area:
            continue
        lines += ["## %s" % area, ""]
        for r, c in by_area[area]:
            lines += ["- **%s** — %s, %s%s  " % (r["title"], r["source"], r["published"],
                                                 " (teaser only)" if r["teaser"] else ""),
                      "  %s  " % r["url"],
                      "  > %s  " % c.get("quote", "").replace("\n", " "),
                      "  %s" % c.get("how_to_test", ""),
                      "  Areas: %s" % ", ".join(c["areas"]), ""]
    unread = [r for r in results if r.get("unread")]
    worth = [r for r in unread if areas_for(r["title"]) != ["Other"]]
    if worth:
        lines += ["## Could not read — titles worth opening yourself (%d)" % len(worth), "",
                  "Not in any readable feed, and the member session could not open them "
                  "(no session for that site, or it needs refreshing on the Mac).", ""]
        lines += ["- %s — %s  \n  %s" % (r["title"][:110], r["source"], r["url"]) for r in worth]
        lines += [""]
    if len(unread) > len(worth):
        lines += ["%d other post(s) could not be read and their titles are off-topic." % (len(unread) - len(worth)), ""]
    foreign = sum(1 for r in results if r.get("foreign"))
    if foreign:
        lines += ["%d post(s) not in English were skipped." % foreign, ""]
    rest = [r for r in results if not r["claims"] and not r.get("unread") and not r.get("foreign")]
    if rest:
        lines += ["## Read, nothing for us (%d)" % len(rest), ""]
        lines += ["- %s — %s%s%s" % (r["title"][:90], r["source"],
                                    " (teaser only)" if r["teaser"] else "",
                                    " (ANALYSIS FAILED)" if r.get("failed") else "")
                  for r in rest]
    return "\n".join(lines) + "\n"


def top_claims(results, n=EMAIL_TOP):
    """The email's picks: round-robin across posts, richest post first, so one
    long article cannot fill the email. Returns [(post, claim)]."""
    posts = sorted((r for r in results if r["claims"]),
                   key=lambda r: (r["teaser"], -len(r["claims"])))
    picked, depth = [], 0
    while len(picked) < n and any(len(r["claims"]) > depth for r in posts):
        picked += [(r, r["claims"][depth]) for r in posts if len(r["claims"]) > depth]
        depth += 1
    return picked[:n]


def email_body(results, day, n=EMAIL_TOP):
    """Buddy, S310: cap the email at the top 15. Everything is in the file."""
    picked = top_claims(results, n)
    total = sum(len(r["claims"]) for r in results)
    lines = ["Top %d of %d lesson(s) from %d post(s) read in the last window. All %d are in "
             "learn-watch/findings/learn-watch-%s.md on CIRRUS. Quotes are verified word for "
             "word; claims are the authors', not tested by us."
             % (len(picked), total, len(results), total, day), ""]
    order = [a for a, _ in AREAS] + ["Other"]
    for area in order:
        mine = [(r, c) for r, c in picked if c["areas"][0] == area]
        if not mine:
            continue
        lines += ["== %s ==" % area, ""]
        for r, c in mine:
            lines += ["* %s (%s)%s" % (r["title"], r["source"], " [teaser]" if r["teaser"] else ""),
                      "  %s" % r["url"],
                      "  \"%s\"" % c.get("quote", "").replace("\n", " ")[:400],
                      "  %s" % c.get("how_to_test", "")[:300], ""]
    member = [r for r in results if r.get("unread") and areas_for(r["title"]) != ["Other"]][:5]
    if member:
        lines += ["== Member-only, on-topic: open with your subscription ==", ""]
        lines += ["* %s (%s)\n  %s" % (r["title"][:110], r["source"], r["url"]) for r in member]
    return "\n".join(lines) + "\n"


def is_healthy(stats):
    """Unhealthy when half or more feeds are unreadable (the ytwatch lesson, S307),
    or when there was work and every analysis failed."""
    feeds_down = stats["feed_errors"] and stats["feed_errors"] * 2 >= max(stats["feeds"], 1)
    analysis_down = (stats["unseen"] > 0 and stats["processed"] == 0
                     and len(stats["errors"]) > stats["feed_errors"])
    return not feeds_down and not analysis_down


def send(subject, body):
    """True only when the mail went (T113: read the result of a sender)."""
    try:
        creds = json.loads(CREDS_PATH.read_text())
        from entity_kb_weekly_digest import _send_mail
        return bool(_send_mail(creds.get("outlook_email", ""), creds.get("outlook_password", ""),
                               TO_ADDR, "", subject, body))
    except Exception as e:
        log("send failed: %s" % type(e).__name__)
        return False


def main():
    args = sys.argv[1:]
    if args[:1] == ["selftest"] or "--selftest" in args:
        return 0 if selftest() else 1
    limit = LIMIT
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    dry = "--dry-run" in args
    stats = run(dry_run=dry, limit=limit)
    for e in stats["errors"]:
        log("  ERROR %s" % e)
    note = "%d post(s), %d claim(s), %d teaser-only" % (stats["processed"], stats["claims"], stats["teasers"])
    if stats["errors"]:
        note += ", %d error(s): %s" % (len(stats["errors"]), "; ".join(stats["errors"][:3]))
    healthy = is_healthy(stats)
    if dry:
        print(stats["body"])
        log("DRY RUN: nothing sent, nothing marked seen. %s" % note)
        return 0
    if stats["claims"]:
        subject = "Server learnings: %d from %d post(s) (%s)" % (
            stats["claims"], stats["with_claims"], datetime.now().strftime("%b %d"))
        sent = send(subject, email_body(stats["results"], datetime.now().strftime("%Y-%m-%d")))
        note += ", email %s" % ("sent" if sent else "FAILED")
        healthy = healthy and sent
    try:
        import job_status
        job_status.record("learnwatch", healthy, note)
    except Exception as e:
        log("job_status.record failed: %s" % e)
    log(note)
    return 0 if healthy else 1


# ── selftest ────────────────────────────────────────────────────────────────
def selftest():
    import tempfile
    ok = True

    def ck(name, cond):
        nonlocal ok
        print("  [%s] %s" % ("OK " if cond else "FAIL", name))
        ok = ok and cond

    ck("key: a Medium post keeps its id across tag and author links",
       post_key("https://medium.com/tag/x/p/slug-ab12cd34ef56?source=rss") == "ab12cd34ef56"
       and post_key("https://medium.com/@me/slug-ab12cd34ef56") == "ab12cd34ef56")
    ck("key: a Substack link drops its query",
       post_key("https://x.substack.com/p/post?utm=1") == "https://x.substack.com/p/post")
    ck("feed: a publication post maps to the publication feed",
       source_feed("https://medium.com/my-aiml/slug-ab12cd34ef56") == "https://medium.com/feed/my-aiml")
    ck("feed: an @author post maps to the author feed",
       source_feed("https://medium.com/@me/slug-ab12cd34ef56") == "https://medium.com/feed/@me")
    ck("feed: a custom domain maps to its own /feed",
       source_feed("https://pub.towardsai.net/slug-ab12cd34ef56") == "https://pub.towardsai.net/feed")
    ck("plain: tags stripped, entities decoded",
       plain("<p>vLLM &amp; <b>FP8</b></p><script>x()</script>") == "vLLM & FP8")
    ck("session: Medium post -> medium.com session",
       session_domain("https://medium.com/@a/x-ab12cd34ef56") == "medium.com")
    ck("session: a *.substack.com post -> substack.com session",
       session_domain("https://kaitchup.substack.com/p/x") == "substack.com")
    ck("session: a custom-domain Substack gets none", session_domain("https://www.latent.space/p/x") is None)
    ck("areas: DGX Spark is hardware", "Hardware" in areas_for("Two DGX Spark nodes"))
    ck("areas: a CLAUDE.md tip is harness", areas_for("keep CLAUDE.md short")[0].startswith("Agent harness"))
    ck("areas: nothing matched is Other", areas_for("a recipe for bread") == ["Other"])
    ck("latin: English passes", mostly_latin("Serving Qwen on DGX Spark with vLLM"))
    ck("latin: Indonesian (Latin script) passes", mostly_latin("Membangun RAG sederhana dari nol"))
    ck("latin: Bengali does not", not mostly_latin("AI Model Quantization: পার্ট ৩ কোয়ান্টাইজেশন মডেলের আকার কমায়"))
    ck("latin: Korean does not", not mostly_latin("클로드 코드 에이전틱 코딩 실전 가이드"))
    n0 = datetime(2026, 9, 27, 1, 15)
    ck("window: no history -> 36 h", window_start(n0, None) == n0 - timedelta(hours=36))
    ck("window: yesterday's run -> still 36 h",
       window_start(n0, n0 - timedelta(hours=24)) == n0 - timedelta(hours=36))
    ck("window: one skipped day -> 60 h (+24)",
       window_start(n0, n0 - timedelta(hours=48)) == n0 - timedelta(hours=60))
    ck("window: a month down -> capped at MAX_AGE_DAYS",
       window_start(n0, n0 - timedelta(days=30)) == n0 - timedelta(days=MAX_AGE_DAYS))
    ck("platform: an overlay Substack labelled blog is Substack",
       platform_of({"type": "blog", "rss": "https://theinnermostloop.substack.com/feed"}) == "substack")
    ck("platform: a real blog stays out",
       platform_of({"type": "blog", "rss": "https://simonwillison.net/atom/everything/"}) is None)
    fake = [{"title": "T%d" % i, "source": "S", "url": "u", "teaser": False, "published": "d",
             "claims": [{"quote": "q%d-%d" % (i, j), "how_to_test": "t", "areas": ["Hardware"]}
                        for j in range(k)]} for i, k in enumerate([9, 4, 3, 2, 1, 1])]
    tc = top_claims(fake)
    ck("email: capped at EMAIL_TOP", len(tc) == EMAIL_TOP)
    ck("email: round-robin -- every post with a claim gets one before any gets two",
       {r["title"] for r, _ in tc[:6]} == {"T0", "T1", "T2", "T3", "T4", "T5"})
    ck("email: body says how many were left in the file",
       "Top 15 of 20" in email_body(fake, "2026-09-26"))
    ck("capped: short text unchanged", capped("abc", 10) == "abc")
    ck("capped: a cut says so", capped("x" * 20, 10).endswith("[truncated at 10 of 20 characters]"))

    now = datetime(2026, 9, 26, 12, 0)
    fresh = (2026, 9, 25, 8, 0, 0, 0, 0, 0)
    old = (2026, 8, 1, 8, 0, 0, 0, 0, 0)
    long_body = "Serving Qwen on two DGX Spark nodes with vLLM FP8 doubled throughput. " * 40

    def entry(link, title, body, when=fresh):
        return {"link": link, "title": title, "published_parsed": when,
                "content": [{"value": body}]}

    class F(dict):
        __getattr__ = dict.get

    class FakeBrowser:
        """T32: the real Browser would open Chromium with Buddy's live cookies."""
        def __init__(self, pages=None, challenge=False):
            self.pages, self.challenge = pages or {}, challenge
            self.opened = self.challenged = 0
            self.closed = False

        def text(self, url):
            self.opened += 1
            if self.challenge:
                self.challenged += 1
                return ""
            return self.pages.get(url, "")

        def close(self):
            self.closed = True
    tag_teaser = entry("https://medium.com/@a/spark-ab12cd34ef56?src=tag", "Spark tips", "short teaser")
    author_full = entry("https://medium.com/@a/spark-ab12cd34ef56", "Spark tips", long_body)
    feeds_map = {
        "https://medium.com/feed/tag/dgx-spark": F(status=200, entries=[tag_teaser]),
        "https://medium.com/feed/@a": F(status=200, entries=[author_full]),
        "https://x.substack.com/feed": F(status=200, entries=[
            entry("https://x.substack.com/p/a", "Harness notes", long_body),
            entry("https://x.substack.com/p/old", "Old post", long_body, old)]),
        "https://dead.medium.com/feed": F(status=404, entries=[], bozo=1),
    }
    parse = lambda url: feeds_map[url]
    feeds = [{"name": "tag/dgx-spark", "rss": "https://medium.com/feed/tag/dgx-spark", "kind": "medium-tag"},
             {"name": "X", "rss": "https://x.substack.com/feed", "kind": "substack"},
             {"name": "Dead", "rss": "https://dead.medium.com/feed", "kind": "medium"}]
    seen_calls = []

    def analyze(text, meta):
        seen_calls.append((meta["title"], len(text)))
        if meta["title"] == "Spark tips":
            return [{"quote": "Serving Qwen on two DGX Spark nodes with vLLM FP8 doubled throughput.",
                     "how_to_test": "Proposed test, not performed: bench it"}]
        return []

    with tempfile.TemporaryDirectory() as td:
        sp, od, cl = Path(td) / "seen.json", Path(td) / "out", Path(td) / "claims.jsonl"
        s = run(browser=FakeBrowser(), feeds=feeds, parse=parse, analyze=analyze, seen_path=sp, out_dir=od, now=now,
                pause=0, claims_log=cl)
        ck("run: claims logged as JSONL for the STRATUS review",
           [json.loads(x)["title"] for x in cl.read_text().splitlines()] == ["Spark tips"])
        ck("run: a working run records its time as the window anchor",
           load_json(sp, {}).get("last_success") == now.isoformat(timespec="seconds"))
        ck("run: the dead feed is an error, not a quiet feed", s["feed_errors"] == 1)
        ck("run: posts older than the 36 h window are skipped", s["unseen"] == 2)
        ck("run: a tag teaser is read from its author feed in full",
           any(t == "Spark tips" and n > TEASER for t, n in seen_calls))
        ck("run: claims are tagged with areas", s["claims"] == 1 and "Hardware" in s["body"])
        ck("run: a post with nothing for us is listed as read", "Harness notes" in s["body"])
        ck("run: findings file written", len(list(od.glob("learn-watch-*.md"))) == 1)
        s2 = run(browser=FakeBrowser(), feeds=feeds, parse=parse, analyze=analyze, seen_path=sp, out_dir=od, now=now,
                 pause=0, claims_log=cl)
        ck("run: nothing is read twice", s2["processed"] == 0)

        def down(text, meta):
            raise RuntimeError("Cumulus_media_worker_failed")
        sp2 = Path(td) / "seen2.json"
        s3 = run(browser=FakeBrowser(), feeds=feeds, parse=parse, analyze=down, seen_path=sp2, out_dir=od, now=now,
                 pause=0, claims_log=Path(td) / "c2.jsonl")
        ck("run: a worker outage does NOT move the window anchor (tomorrow widens)",
           load_json(sp2, {}).get("last_success") is None)
        ck("run: a worker outage marks nothing seen (retried tomorrow)",
           load_json(sp2, {}).get("keys", []) == [] and s3["processed"] == 0)
        ck("health: every analysis failing is unhealthy", not is_healthy(s3))
        ck("health: the normal run is healthy", is_healthy(s))
        ck("health: half the feeds down is unhealthy",
           not is_healthy({"feeds": 4, "feed_errors": 2, "unseen": 0, "processed": 0, "errors": ["a", "b"]}))
        s4 = run(dry_run=True, browser=FakeBrowser(), feeds=feeds, parse=parse, analyze=analyze,
                 seen_path=Path(td) / "seen3.json", out_dir=Path(td) / "out3", now=now, pause=0)
        tiny = {"https://y.substack.com/feed": F(status=200, entries=[
            entry("https://y.substack.com/p/t", "Ollama tool calls with Qwen", "35 chars of teaser only")]),
            "https://z.substack.com/feed": F(bozo=1, entries=[], bozo_exception=OSError("nodename nor servname"))}
        calls_before = len(seen_calls)
        s5 = run(browser=FakeBrowser(), feeds=[{"name": "Y", "rss": "https://y.substack.com/feed", "kind": "substack"},
                        {"name": "Z", "rss": "https://z.substack.com/feed", "kind": "substack"}],
                 parse=lambda u: tiny[u], analyze=analyze, seen_path=Path(td) / "seen5.json",
                 out_dir=Path(td) / "out5", now=now, pause=0, claims_log=Path(td) / "c5.jsonl")
        ck("unread: a post with no text is never sent to the model", len(seen_calls) == calls_before)
        ck("unread: ...and is listed for Buddy when its title is on-topic",
           s5["unread"] == 1 and "worth opening yourself" in s5["body"] and "Ollama tool calls" in s5["body"])
        ck("feed error: names the cause, not 'HTTP ?'", "nodename" in s5["errors"][0])
        member_url = "https://y.substack.com/p/t"
        fb = FakeBrowser({member_url: long_body})
        calls_before = len(seen_calls)
        s6 = run(browser=fb, feeds=[{"name": "Y", "rss": "https://y.substack.com/feed", "kind": "substack"}],
                 parse=lambda u: tiny[u], analyze=analyze, seen_path=Path(td) / "seen6.json",
                 out_dir=Path(td) / "out6", now=now, pause=0, claims_log=Path(td) / "c6.jsonl")
        ck("browser: a member-only teaser is read in full through the session",
           s6["via_browser"] == 1 and len(seen_calls) == calls_before + 1
           and seen_calls[-1][1] > TEASER)
        ck("browser: always closed", fb.closed)
        s7 = run(browser=FakeBrowser(challenge=True),
                 feeds=[{"name": "Y", "rss": "https://y.substack.com/feed", "kind": "substack"}],
                 parse=lambda u: tiny[u], analyze=analyze, seen_path=Path(td) / "seen7.json",
                 out_dir=Path(td) / "out7", now=now, pause=0, claims_log=Path(td) / "c7.jsonl")
        ck("browser: a Cloudflare challenge is reported, not silent",
           any("Cloudflare challenge" in e for e in s7["errors"]))
        ck("dry run: nothing written", not (Path(td) / "seen3.json").exists()
           and not (Path(td) / "out3").exists() and s4["processed"] == 2)
    print("selftest:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    sys.exit(main())
