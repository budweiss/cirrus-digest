#!/usr/bin/env python3
"""
CIRRUS Daily Web Digest
Fetches Medium and Substack RSS feeds from the past 24 hours,
summarizes with a local Ollama model, saves a daily digest.
"""

import json
import re
import requests
import feedparser
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────

CONFIG_PATH = Path.home() / "projects/cirrus-digest/config/sources.json"

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

WEB_SOURCES = CONFIG["web_sources"]
DIGEST_CFG  = CONFIG["digest"]

OUTPUT_DIR  = Path(DIGEST_CFG["output_dir"])
LOG_DIR     = Path(DIGEST_CFG["log_dir"])
MODEL       = DIGEST_CFG["ollama_model"]
OLLAMA_HOST = DIGEST_CFG["ollama_host"]
MAX_ARTICLE = DIGEST_CFG["max_article_length"]

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Helpers ──────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")

def clean_text(text, max_len=None):
    """Strip HTML tags and excess whitespace."""
    soup = BeautifulSoup(text, "html.parser")
    clean = re.sub(r'\s+', ' ', soup.get_text()).strip()
    if max_len and len(clean) > max_len:
        clean = clean[:max_len] + "..."
    return clean

def ollama_summarize(prompt):
    """Send a prompt to local Ollama and return the response."""
    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": MODEL, "prompt": prompt, "stream": False},
            timeout=120
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception as e:
        return f"[Summarization error: {e}]"

# ── Web RSS Fetcher ───────────────────────────────────────────────────────────

def fetch_web_sources():
    """Fetch articles from Medium and Substack RSS feeds published in the last 24 hours."""
    results = []
    since = datetime.now() - timedelta(hours=24)

    for source in WEB_SOURCES:
        log(f"Fetching: {source['name']}")
        try:
            feed = feedparser.parse(source["rss"])
            count = 0
            for entry in feed.entries:
                # Parse publish date
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    published = datetime(*entry.published_parsed[:6])
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    published = datetime(*entry.updated_parsed[:6])
                else:
                    continue  # skip if no date

                if published < since:
                    continue

                content = clean_text(
                    entry.get("content", [{}])[0].get("value", "")
                    or entry.get("summary", "")
                    or entry.get("description", ""),
                    MAX_ARTICLE
                )

                if not content:
                    continue

                results.append({
                    "source": source["name"],
                    "type": source["type"],
                    "subject": entry.get("title", "Untitled"),
                    "url": entry.get("link", ""),
                    "content": content,
                    "published": published.strftime("%Y-%m-%d %H:%M")
                })
                count += 1
                log(f"  Article: {entry.get('title', '')[:60]}")

            if count == 0:
                log(f"  No new articles in the last 24 hours")

        except Exception as e:
            log(f"  Fetch error ({source['name']}): {e}")

    log(f"Web sources: found {len(results)} new articles")
    return results

# ── Summarizer ────────────────────────────────────────────────────────────────

def summarize_item(item):
    """Summarize a single article, enriched with RAG context from past digests."""
    # Try to get relevant past knowledge
    rag_context = ""
    try:
        from cirrus_rag import build_context
        query = f"{item['subject']} {item['content'][:200]}"
        rag_context = build_context(query)
    except Exception as e:
        log(f"  RAG context unavailable: {e}")

    prompt = f"""You are CIRRUS, an AI assistant monitoring developments in artificial intelligence and technology.

Summarize the following article for a daily digest. Focus on:
- Key AI developments, tools, or techniques mentioned
- Anything relevant to running local AI models (Ollama, LLMs, Mac Studio setup)
- Any actionable recommendations or insights
- Notable trends

{rag_context}

Source: {item['source']}
Title: {item['subject']}
Published: {item['published']}

Content:
{item['content']}

Write a concise 2-4 sentence summary. If this topic was covered in past digests (see RELEVANT PAST KNOWLEDGE above), note what's new or different. End with one bullet point labeled "→ CIRRUS NOTE:" if anything is directly relevant to improving this AI system."""

    return ollama_summarize(prompt)

# ── Digest Writer ─────────────────────────────────────────────────────────────

def write_digest(items, summaries):
    """Write the daily digest file."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = OUTPUT_DIR / f"daily-{date_str}.md"

    # Group by source type
    medium_items = [(i, s) for i, s in zip(items, summaries) if i["type"] == "medium"]
    substack_items = [(i, s) for i, s in zip(items, summaries) if i["type"] == "substack"]

    with open(filename, "w") as f:
        f.write(f"# CIRRUS Daily Web Digest — {date_str}\n\n")
        f.write(f"Generated by CIRRUS using `{MODEL}`\n")
        f.write(f"Articles processed: {len(items)} ({len(medium_items)} Medium, {len(substack_items)} Substack)\n\n")
        f.write("---\n\n")

        if medium_items:
            f.write("## 📝 Medium\n\n")
            for item, summary in medium_items:
                f.write(f"### {item['subject']}\n")
                f.write(f"*{item['source']} — {item['published']}*\n\n")
                if item['url']:
                    f.write(f"[Read article]({item['url']})\n\n")
                f.write(f"{summary}\n\n")
                f.write("---\n\n")

        if substack_items:
            f.write("## 📬 Substack\n\n")
            for item, summary in substack_items:
                f.write(f"### {item['subject']}\n")
                f.write(f"*{item['source']} — {item['published']}*\n\n")
                if item['url']:
                    f.write(f"[Read article]({item['url']})\n\n")
                f.write(f"{summary}\n\n")
                f.write("---\n\n")

        f.write(f"*End of daily digest — {date_str}*\n")

    log(f"Daily digest saved: {filename}")
    return filename

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("=== CIRRUS Daily Web Digest Starting ===")

    items = fetch_web_sources()

    if not items:
        log("No new articles in the last 24 hours. Exiting.")
        return

    log(f"Summarizing {len(items)} articles with {MODEL}...")
    summaries = []
    for i, item in enumerate(items, 1):
        log(f"  [{i}/{len(items)}] {item['subject'][:50]}")
        summaries.append(summarize_item(item))

    digest_file = write_digest(items, summaries)

    log("=== Daily Digest Complete ===")
    log(f"Read it with: cat {digest_file}")

if __name__ == "__main__":
    main()

    # Post-run: index new digest, extract action items, check disk space, send email
    try:
        from cirrus_rag import index_digest
        from extract_actions import extract_from_latest
        from space_monitor import run_monitor
        from send_digest import main as send_email
        import sys
        log("Updating RAG knowledge base...")
        latest_digest = sorted(OUTPUT_DIR.glob("daily-*.md"), reverse=True)
        if latest_digest:
            index_digest(latest_digest[0])
        log("Extracting action items...")
        extract_from_latest("daily")
        log("Running space monitor...")
        run_monitor()
        log("Sending daily digest email...")
        sys.argv = ["send_digest.py", "daily"]
        send_email()
    except Exception as e:
        log(f"Post-run step error: {e}")
