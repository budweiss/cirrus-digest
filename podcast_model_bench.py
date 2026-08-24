#!/usr/bin/env python3
"""podcast_model_bench.py — S75. Is llama3.2:3b good enough for podcasts?

WHY
---
cirrus_digest.py summarises podcast transcripts with PODCAST_MODEL, defaulting
to llama3.2:3b, while every other item uses qwen2.5:14b. The comment justifying
that says it is "far faster than qwen2.5:72b with minimal quality loss"
(proposal-2026-07-12-3, Session 35).

That claim was measured against the 72B, on hardware that predates CUMULUS.
CUMULUS now has qwen3-coder:30b — an MoE measured at 91 tok/s on 2026-08-23,
19x the dense 72B on the same box. So the premise ("the only alternative is
slow") may simply no longer hold, and the longest, densest content we ingest is
being handled by the smallest model in the stack.

stall_check.py's own docstring: "No detector replaces occasionally checking
whether an old claim is still true." This is that check.

METHOD
------
ONE real transcript, THREE models, the digest's OWN prompt — not a paraphrase,
so the output is what the digest would actually have produced. Timings are
wall-clock for the generate call only; transcription happens once and is shared.

  --transcribe   fetch the newest episode of a feed, Whisper it, save the text
  --summarize M  summarise the saved transcript with model M via local Ollama

Run --summarize on whichever box holds the model. The transcript file is the
unit of comparison; copy it between boxes so all three see identical input.

Usage:
  python3 podcast_model_bench.py --transcribe [--feed URL]
  python3 podcast_model_bench.py --summarize llama3.2:3b
"""
import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRANSCRIPT = HERE / "logs" / "bench_transcript.txt"
META = HERE / "logs" / "bench_transcript.meta.json"
OLLAMA = "http://127.0.0.1:11434"
DEFAULT_FEED = ("https://feeds.acast.com/public/shows/"
                "ai-news-strategy-daily-with-nate-b-jones")


def _log(m):
    print(m, flush=True)


def do_transcribe(feed_url):
    import feedparser
    _log(f"feed: {feed_url}")
    feed = feedparser.parse(feed_url)
    if not feed.entries:
        _log("!! no entries in feed")
        return 1
    entry = feed.entries[0]
    title = entry.get("title", "untitled")
    audio = None
    for enc in entry.get("enclosures", []):
        if "audio" in (enc.get("type") or ""):
            audio = enc.get("href") or enc.get("url")
            break
    if not audio:
        _log("!! newest entry has NO audio enclosure — nothing to transcribe.")
        return 1
    _log(f"episode: {title}")

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        path = Path(tmp.name)
    try:
        t0 = time.time()
        _log("downloading audio…")
        req = urllib.request.Request(audio, headers={"User-Agent": "CIRRUS-digest/1.0"})
        with urllib.request.urlopen(req, timeout=300) as r, open(path, "wb") as f:
            f.write(r.read())
        mb = path.stat().st_size / 1e6
        _log(f"  {mb:.1f} MB in {time.time()-t0:.0f}s")

        # Reuse the digest's own Whisper settings rather than inventing new ones.
        import cirrus_digest as D
        t0 = time.time()
        _log(f"transcribing with Whisper ({D.WHISPER_MODEL})… this is the slow part")
        text = D.transcribe_audio(path)
        if not text:
            _log("!! transcription failed")
            return 1
        secs = time.time() - t0
        TRANSCRIPT.parent.mkdir(parents=True, exist_ok=True)
        TRANSCRIPT.write_text(text)
        META.write_text(json.dumps({"title": title, "feed": feed_url,
                                    "chars": len(text),
                                    "transcribe_seconds": round(secs, 1)}, indent=2))
        _log(f"transcript: {len(text):,} chars in {secs:.0f}s -> {TRANSCRIPT}")
        return 0
    finally:
        path.unlink(missing_ok=True)


def do_summarize(model):
    if not TRANSCRIPT.exists():
        _log(f"!! no transcript at {TRANSCRIPT} — run --transcribe first, or copy "
             f"the file from the box that has it. NOT the same as 'nothing to do'.")
        return 1
    text = TRANSCRIPT.read_text()
    meta = {}
    try:
        meta = json.loads(META.read_text())
    except Exception:
        pass

    # Read the cap from config directly rather than importing cirrus_digest:
    # the summarize path needs ONE constant, and importing the module drags in
    # feedparser, which CUMULUS does not have. A benchmark that only runs on the
    # box that happens to have the digest's deps is not a comparison.
    cap = 20000
    try:
        cfg = json.loads((HERE / "config" / "sources.json").read_text())
        cap = int(cfg.get("digest", {}).get("max_episode_length", 2000)) * 10
    except Exception:
        pass
    content = text[:cap]
    item = {"content": f"[TRANSCRIBED]\n{content}",
            "source": meta.get("feed", "bench"),
            "subject": meta.get("title", "bench episode"),
            "type": "podcast",
            "published": ""}

    # Build the digest's REAL prompt, minus the network-bound enrichment steps
    # (reference fetching and RAG) so the comparison isolates the MODEL and is
    # reproducible. Both are identical across the three runs either way.
    if True:
        # cirrus_digest builds the prompt inline; reproduce its shape faithfully.
        prompt = (
            "You are CIRRUS, an AI assistant monitoring developments in "
            "artificial intelligence.\n\n"
            f"Summarize the following {item['type']} for a weekly digest. Focus on:\n"
            "- Key AI developments, tools, or techniques mentioned\n"
            "- Anything relevant to running local AI models (Ollama, LLMs, Mac Studio setup)\n"
            "- Any actionable recommendations or improvements worth considering\n"
            "- Notable trends or insights\n\n"
            "When a new AI model or tool is covered, include the concrete performance "
            "metrics the source provides — benchmark names and scores, tokens/sec, "
            "context window, parameter count, memory footprint, or pricing. Prefer "
            "numbers over adjectives like \"faster\" or \"powerful\". If the source "
            "provides no numbers, do not invent any.\n\n"
            f"Source: {item['source']}\nTitle: {item['subject']}\n\n"
            f"Content:\n{item['content']}\n\n"
            "Write a concise 3-5 sentence summary."
        )

    body = json.dumps({"model": model, "prompt": prompt, "stream": False,
                       "options": {"num_ctx": 8192}}).encode()
    req = urllib.request.Request(OLLAMA + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            d = json.loads(r.read().decode())
    except Exception as e:
        _log(f"!! {model}: FAILED — {str(e)[:200]}")
        return 1
    secs = time.time() - t0
    out = (d.get("response") or "").strip()
    ev = d.get("eval_count") or 0
    toks = ev / secs if secs else 0
    print("=" * 72)
    print(f"MODEL: {model}")
    print(f"  episode        : {meta.get('title','?')}")
    print(f"  transcript      : {len(text):,} chars (fed {len(content):,})")
    print(f"  wall clock      : {secs:.1f}s")
    print(f"  output tokens   : {ev}  ({toks:.1f} tok/s)")
    print(f"  summary chars   : {len(out)}")
    print("-" * 72)
    print(out if out else "(EMPTY RESPONSE — this is a failure, not a short answer)")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--transcribe", action="store_true")
    g.add_argument("--summarize", metavar="MODEL")
    ap.add_argument("--feed", default=DEFAULT_FEED)
    a = ap.parse_args()
    sys.exit(do_transcribe(a.feed) if a.transcribe else do_summarize(a.summarize))
