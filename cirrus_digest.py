#!/usr/bin/env python3
"""
CIRRUS AI Digest — Project 1
Fetches podcast RSS feeds (transcribed with Whisper),
summarizes with a local Ollama model, saves a weekly digest.

Email newsletter fetching moved to cirrus_daily.py (runs daily instead of
weekly). CONFIG["email"]["days_back"] is still used below by
fetch_podcasts() for its own lookback window.
"""

import json
import os
import re
import subprocess
import tempfile
import requests
import feedparser
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────

CONFIG_PATH = Path.home() / "projects/cirrus-digest/config/sources.json"

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

PODCASTS    = CONFIG["podcasts"]
DIGEST_CFG  = CONFIG["digest"]

OUTPUT_DIR  = Path(DIGEST_CFG["output_dir"])
LOG_DIR     = Path(DIGEST_CFG["log_dir"])
MODEL       = DIGEST_CFG["ollama_model"]
OLLAMA_HOST = DIGEST_CFG["ollama_host"]
MAX_ARTICLE = DIGEST_CFG["max_article_length"]
MAX_EPISODE = DIGEST_CFG["max_episode_length"]

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

def ollama_summarize(prompt, timeout=120):
    """Send a prompt to local Ollama and return the response."""
    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": MODEL, "prompt": prompt, "stream": False,
                  "options": {"num_ctx": 8192}},
            timeout=timeout
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception as e:
        return f"[Summarization error: {e}]"

# ── Whisper Transcription ─────────────────────────────────────────────────────

WHISPER_BIN = "/Users/buddy/Library/Python/3.9/bin/whisper"
WHISPER_MODEL = "small"  # small = fast + accurate enough; upgrade to medium/large if needed

def download_audio(url, dest_path):
    """Download podcast audio file."""
    log(f"  Downloading audio: {url[:60]}...")
    try:
        resp = requests.get(url, stream=True, timeout=120)
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except Exception as e:
        log(f"  Audio download failed: {e}")
        return False

def transcribe_audio(audio_path):
    """Transcribe audio file using local Whisper."""
    log(f"  Transcribing with Whisper ({WHISPER_MODEL})...")
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                [WHISPER_BIN, str(audio_path),
                 "--model", WHISPER_MODEL,
                 "--output_format", "txt",
                 "--output_dir", tmpdir,
                 "--language", "en",
                 "--fp16", "False"],
                capture_output=True, text=True, timeout=1800  # 30 min max
            )
            if result.returncode != 0:
                log(f"  Whisper error: {result.stderr[:200]}")
                return None

            # Find output txt file
            txt_files = list(Path(tmpdir).glob("*.txt"))
            if txt_files:
                transcript = txt_files[0].read_text().strip()
                log(f"  Transcription complete: {len(transcript)} chars")
                return transcript
    except subprocess.TimeoutExpired:
        log("  Whisper timed out — episode too long")
    except Exception as e:
        log(f"  Transcription error: {e}")
    return None

# ── Podcast RSS Fetcher ───────────────────────────────────────────────────────

def fetch_podcasts():
    """Fetch latest episodes from podcast RSS feeds, transcribe audio with Whisper."""
    results = []
    since = datetime.now() - timedelta(days=CONFIG["email"]["days_back"])

    for podcast in PODCASTS:
        log(f"Fetching podcast: {podcast['name']}")
        try:
            feed = feedparser.parse(podcast["rss"])
            for entry in feed.entries[:3]:  # last 3 episodes max
                published = datetime(*entry.published_parsed[:6]) if hasattr(entry, "published_parsed") and entry.published_parsed else datetime.now()
                if published < since:
                    continue

                title = entry.get("title", "Untitled Episode")
                log(f"  Episode: {title[:60]}")

                # Try to get audio URL for transcription
                audio_url = None
                for enclosure in entry.get("enclosures", []):
                    if "audio" in enclosure.get("type", ""):
                        audio_url = enclosure.get("href") or enclosure.get("url")
                        break

                content = None

                # Attempt Whisper transcription
                if audio_url:
                    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                        tmp_path = Path(tmp.name)
                    try:
                        if download_audio(audio_url, tmp_path):
                            transcript = transcribe_audio(tmp_path)
                            if transcript:
                                # Truncate to max length. Stopgap: bumped 3x -> 10x
                                # (~20000 chars) to cover more of long episodes
                                # (e.g. 90+ min All-In) within the 8192-token
                                # Ollama context. Proper fix is chunked
                                # map-reduce summarization (see Session 13/14 recap).
                                cap = MAX_EPISODE * 10
                                content = transcript[:cap] if len(transcript) > cap else transcript
                                content = f"[TRANSCRIBED]\n{content}"
                    finally:
                        tmp_path.unlink(missing_ok=True)

                # Fall back to show notes if transcription failed
                if not content:
                    log("  Falling back to show notes")
                    content = clean_text(
                        entry.get("summary", entry.get("description", "")),
                        MAX_EPISODE
                    )
                    content = f"[SHOW NOTES ONLY]\n{content}"

                results.append({
                    "source": podcast["name"],
                    "subject": title,
                    "content": content,
                    "type": "podcast"
                })

        except Exception as e:
            log(f"Podcast fetch error ({podcast['name']}): {e}")

    log(f"Podcasts: found {len(results)} recent episodes")
    return results

# ── Summarizer ────────────────────────────────────────────────────────────────

def summarize_item(item):
    """Summarize a single newsletter or podcast episode, enriched with RAG context."""
    # Try to get relevant past knowledge
    rag_context = ""
    try:
        from cirrus_rag import build_context
        query = f"{item['subject']} {item['content'][:200]}"
        rag_context = build_context(query)
    except Exception as e:
        log(f"  RAG context unavailable: {e}")

    prompt = f"""You are CIRRUS, an AI assistant monitoring developments in artificial intelligence.

Summarize the following {item['type']} for a weekly digest. Focus on:
- Key AI developments, tools, or techniques mentioned
- Anything relevant to running local AI models (Ollama, LLMs, Mac Studio setup)
- Any actionable recommendations or improvements worth considering
- Notable trends or insights

{rag_context}

Source: {item['source']}
Title: {item['subject']}

Content:
{item['content']}

Write a concise 3-5 sentence summary. If this topic was covered in past digests (see RELEVANT PAST KNOWLEDGE above), note what's new or different.

Only add a "→ CIRRUS NOTE:" bullet if this content mentions something CONCRETELY actionable for CIRRUS itself — for example: a specific Ollama model to pull by name, a specific Python package to install, a specific RSS feed or newsletter URL worth adding to sources.json, or a specific code change to make. The note must describe a discrete action CIRRUS can execute.
DO NOT add a CIRRUS NOTE for: general AI trend observations, content descriptions, podcast themes, vague suggestions like "consider monitoring more sources", or source attribution lines. Most items should have NO CIRRUS NOTE — only add one when there is a specific, named action."""

    return ollama_summarize(prompt)

def generate_meta_recommendations(summaries):
    """Ask the model to reflect on improvements to the digest process itself.

    Wrapped with a longer timeout (300s) since the 72b model can take a while
    on this larger combined prompt, and with error handling so a failure here
    never blocks the digest from being written or emailed.
    """
    try:
        combined = "\n\n".join(summaries[:5])  # use first 5 summaries
        prompt = f"""You are CIRRUS, reviewing your own weekly AI digest process.

Based on these summaries from this week's digest:

{combined}

Suggest 2-3 specific improvements to how CIRRUS monitors, fetches, or summarizes AI content. Each suggestion must be a CONCRETE, EXECUTABLE action. Good examples:
- "Add the XYZ Newsletter RSS feed at https://... to sources.json"
- "Pull model llama3.2:3b via Ollama — faster for short summarization tasks"
- "Filter out emails from domain X in cirrus_daily.py — they are always off-topic"

Do NOT suggest things like "continue monitoring AI developments", "maintain interest in", or "consider exploring" — these are vague, not actions. If this week's content does not clearly suggest a concrete improvement, say so explicitly rather than inventing generic advice.
Format as a numbered list."""

        result = ollama_summarize(prompt, timeout=300)
        if result.startswith("[Summarization error:"):
            log(f"Self-improvement notes error: {result}")
            return "*Self-improvement notes unavailable this week (Ollama error — see digest.log).*"
        return result
    except Exception as e:
        log(f"Self-improvement notes failed: {e}")
        return "*Self-improvement notes unavailable this week (error — see digest.log).*"

# ── Digest Writer ─────────────────────────────────────────────────────────────

def write_digest(items, summaries, meta):
    """Write the final digest file."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = OUTPUT_DIR / f"digest-{date_str}.md"

    with open(filename, "w") as f:
        f.write(f"# CIRRUS Weekly AI Digest — {date_str}\n\n")
        f.write(f"Generated by CIRRUS using `{MODEL}`\n")
        f.write(f"Sources processed: {len(items)} podcast episode(s)\n\n")
        f.write("---\n\n")

        # Podcasts
        pod_items = [(i, s) for i, s in zip(items, summaries) if i["type"] == "podcast"]
        if pod_items:
            f.write("## 🎙️ Podcasts\n\n")
            for item, summary in pod_items:
                f.write(f"### {item['subject']}\n")
                f.write(f"*From: {item['source']}*\n\n")
                f.write(f"{summary}\n\n")
                f.write("---\n\n")

        # Meta recommendations
        f.write("## 🔄 CIRRUS Self-Improvement Notes\n\n")
        f.write("*Recommendations for improving this digest process:*\n\n")
        f.write(f"{meta}\n\n")
        f.write("---\n\n")
        f.write(f"*End of digest — {date_str}*\n")

    log(f"Digest saved: {filename}")
    return filename

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("=== CIRRUS Weekly Digest Starting ===")

    # Fetch content (email newsletters are now handled daily by cirrus_daily.py)
    all_items = fetch_podcasts()

    if not all_items:
        log("No content found this week. Exiting.")
        return

    # Summarize each item
    log(f"Summarizing {len(all_items)} items with {MODEL}...")
    summaries = []
    for i, item in enumerate(all_items, 1):
        log(f"  [{i}/{len(all_items)}] {item['subject'][:50]}")
        summaries.append(summarize_item(item))

    # Meta self-improvement recommendations
    log("Generating CIRRUS self-improvement notes...")
    try:
        meta = generate_meta_recommendations(summaries)
    except Exception as e:
        log(f"Self-improvement notes step crashed unexpectedly: {e}")
        meta = "*Self-improvement notes unavailable this week (unexpected error — see digest.log).*"

    # Write digest
    digest_file = write_digest(all_items, summaries, meta)

    log("=== Digest Complete ===")
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
        latest_digest = sorted(OUTPUT_DIR.glob("digest-*.md"), reverse=True)
        if latest_digest:
            index_digest(latest_digest[0])
        log("Extracting action items...")
        extract_from_latest("digest")
        log("Running space monitor...")
        run_monitor()
        log("Sending weekly digest email...")
        sys.argv = ["send_digest.py", "weekly"]
        send_email()
    except Exception as e:
        log(f"Post-run step error: {e}")
