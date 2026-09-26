#!/usr/bin/env python3
"""learn_watch.py -- a daily "what can we learn" read of Medium + Substack (S308).

Buddy, 2026-09-26: review ALL Medium and Substack entries daily -- not only the
keyword-matched ones the digest keeps -- for anything that could improve CIRRUS
and CUMULUS or inform STRATUS (Cowork, Codex, agent harnesses, instruction .md
files, Mac Studio, DGX Spark, Ollama / local models, recursive self-improvement),
and extract it with LOCAL models.

  feeds    config/sources.json web_sources of type medium/substack, plus the
           topic feeds in learn-watch/feeds.json (Medium tag feeds).
  text     the RSS content. Medium article PAGES answer 403 from Cloudflare to
           any server-side client -- measured S308, 8 of 8, with or without the
           stored cookies -- but Medium's RSS is not blocked, so a tag-feed post's
           full text comes from its author/publication feed (9 of 18 full; the
           rest are member-only teasers, read as teasers and marked so).
  extract  media_pipeline 'analyze' on the CUMULUS worker (local Qwen on C2) in
           claims mode: every claim carries a verbatim quote checked against the
           text, so nothing the model invents is published.
  output   learn-watch/findings/learn-watch-YYYY-MM-DD.md, an email to Buddy
           when anything was learned, and job_status 'learnwatch'.

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
FEEDS_PATH = HERE / "learn-watch" / "feeds.json"
SEEN_PATH = HERE / "learn-watch" / "seen.json"
OUT_DIR = HERE / "learn-watch" / "findings"
CREDS_PATH = HERE / "config" / "credentials.json"
TO_ADDR = "Buddy.Weiss@outlook.com"

LIMIT = 40           # posts analysed per run; ~30/day measured S308, so the
                     # backlog drains and a busy day does not run into the morning
MAX_AGE_DAYS = 14    # older posts are skipped, not queued forever
TEASER = 1500        # below this many characters we only had a teaser
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


def load_feeds(sources_path=SOURCES_PATH, feeds_path=FEEDS_PATH):
    feeds = [{"name": s["name"], "rss": s["rss"], "kind": s["type"]}
             for s in load_json(sources_path, {}).get("web_sources", [])
             if s.get("type") in ("medium", "substack") and s.get("rss")]
    feeds += [{"name": f["name"], "rss": f["rss"], "kind": f.get("kind", "medium-tag")}
              for f in load_json(feeds_path, {}).get("feeds", []) if f.get("rss")]
    return feeds


def plain(markup):
    """HTML -> text. Crude on purpose: the model reads it, and quotes are checked
    against exactly this text, so what matters is that it is the SAME text."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", markup or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</h\d>|</li>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


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


def feed_unreadable(feed):
    status = feed.get("status") or 0
    return status >= 400 or (bool(feed.get("bozo")) and not feed.get("entries"))


def collect(feeds, seen, now, parse, pause=0.0):
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
            feed_errors.append("%s: HTTP %s" % (f["name"], feed.get("status", "?")))
            continue
        for e in feed.get("entries", []):
            link = e.get("link", "")
            p = e.get("published_parsed") or e.get("updated_parsed")
            if not link or not p:
                continue
            published = datetime(*p[:6])
            key = post_key(link)
            if key in seen or key in posts or now - published > timedelta(days=MAX_AGE_DAYS):
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


def run(dry_run=False, limit=LIMIT, feeds=None, parse=None, analyze=None,
        seen_path=None, out_dir=None, now=None, pause=FEED_PAUSE):
    """Injectable throughout, so the selftest touches no network and no live file (T32)."""
    if parse is None:
        import feedparser
        parse = feedparser.parse
    feeds = load_feeds() if feeds is None else feeds
    analyze = analyze or default_analyze
    seen_path = Path(seen_path or SEEN_PATH)
    out_dir = Path(out_dir or OUT_DIR)
    now = now or datetime.now()

    seen = load_json(seen_path, {}).get("keys", [])
    seen_set = set(seen)
    posts, feed_errors = collect(feeds, seen_set, now, parse, pause)
    log("%d feed(s), %d unseen post(s) within %d days, %d feed error(s)"
        % (len(feeds), len(posts), MAX_AGE_DAYS, len(feed_errors)))

    results, errors = [], list(feed_errors)
    for post in posts[:limit]:
        text = full_text(post.pop("entry"), post["kind"], parse)
        post["teaser"] = len(text) < TEASER
        meta = {k: post[k] for k in ("title", "url", "source", "published")}
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
            seen_path.write_text(json.dumps({"keys": seen[-SEEN_KEEP:]}, indent=1))

    day = now.strftime("%Y-%m-%d")
    body = render(results, day)
    if not dry_run and results:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / ("learn-watch-%s.md" % day)).write_text(body)
    return {"feeds": len(feeds), "feed_errors": len(feed_errors), "unseen": len(posts),
            "processed": len(results), "claims": sum(len(r["claims"]) for r in results),
            "with_claims": sum(1 for r in results if r["claims"]),
            "teasers": sum(1 for r in results if r["teaser"]),
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
    rest = [r for r in results if not r["claims"]]
    if rest:
        lines += ["## Read, nothing for us (%d)" % len(rest), ""]
        lines += ["- %s — %s%s%s" % (r["title"][:90], r["source"],
                                    " (teaser only)" if r["teaser"] else "",
                                    " (ANALYSIS FAILED)" if r.get("failed") else "")
                  for r in rest]
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
        sent = send(subject, stats["body"])
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
    ck("areas: DGX Spark is hardware", "Hardware" in areas_for("Two DGX Spark nodes"))
    ck("areas: a CLAUDE.md tip is harness", areas_for("keep CLAUDE.md short")[0].startswith("Agent harness"))
    ck("areas: nothing matched is Other", areas_for("a recipe for bread") == ["Other"])
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
        sp, od = Path(td) / "seen.json", Path(td) / "out"
        s = run(feeds=feeds, parse=parse, analyze=analyze, seen_path=sp, out_dir=od, now=now, pause=0)
        ck("run: the dead feed is an error, not a quiet feed", s["feed_errors"] == 1)
        ck("run: posts older than MAX_AGE_DAYS are skipped", s["unseen"] == 2)
        ck("run: a tag teaser is read from its author feed in full",
           any(t == "Spark tips" and n > TEASER for t, n in seen_calls))
        ck("run: claims are tagged with areas", s["claims"] == 1 and "Hardware" in s["body"])
        ck("run: a post with nothing for us is listed as read", "Harness notes" in s["body"])
        ck("run: findings file written", len(list(od.glob("learn-watch-*.md"))) == 1)
        s2 = run(feeds=feeds, parse=parse, analyze=analyze, seen_path=sp, out_dir=od, now=now, pause=0)
        ck("run: nothing is read twice", s2["processed"] == 0)

        def down(text, meta):
            raise RuntimeError("Cumulus_media_worker_failed")
        sp2 = Path(td) / "seen2.json"
        s3 = run(feeds=feeds, parse=parse, analyze=down, seen_path=sp2, out_dir=od, now=now, pause=0)
        ck("run: a worker outage marks nothing seen (retried tomorrow)",
           load_json(sp2, {}).get("keys", []) == [] and s3["processed"] == 0)
        ck("health: every analysis failing is unhealthy", not is_healthy(s3))
        ck("health: the normal run is healthy", is_healthy(s))
        ck("health: half the feeds down is unhealthy",
           not is_healthy({"feeds": 4, "feed_errors": 2, "unseen": 0, "processed": 0, "errors": ["a", "b"]}))
        s4 = run(dry_run=True, feeds=feeds, parse=parse, analyze=analyze,
                 seen_path=Path(td) / "seen3.json", out_dir=Path(td) / "out3", now=now, pause=0)
        ck("dry run: nothing written", not (Path(td) / "seen3.json").exists()
           and not (Path(td) / "out3").exists() and s4["processed"] == 2)
    print("selftest:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    sys.exit(main())
