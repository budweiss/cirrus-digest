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
from urllib.parse import urlparse

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


# ── Site Cookie Injection ──────────────────────────────────────────────────────
# Shared with cirrus_daily.py — same cookies.json file, same domain-matching
# logic. Used here for reference source fetching in podcast enrichment.

COOKIES_PATH = Path.home() / "projects/cirrus-digest/config/cookies.json"
_SITE_COOKIES: dict = {}
_SITE_COOKIES_LOADED = False


def _load_site_cookies():
    global _SITE_COOKIES, _SITE_COOKIES_LOADED
    if _SITE_COOKIES_LOADED:
        return
    _SITE_COOKIES_LOADED = True
    if not COOKIES_PATH.exists():
        return
    try:
        with open(COOKIES_PATH) as f:
            _SITE_COOKIES = json.load(f)
        log(f"Loaded cookies for {len(_SITE_COOKIES)} domain(s): {', '.join(_SITE_COOKIES)}")
    except Exception as e:
        log(f"Could not load cookies.json: {e}")


def get_cookies_for_url(url: str) -> dict:
    """Return stored cookies matching the URL's domain, or {} if none."""
    _load_site_cookies()
    if not _SITE_COOKIES:
        return {}
    try:
        domain = urlparse(url).netloc.lower().lstrip("www.")
        for cookie_domain, cookies in _SITE_COOKIES.items():
            cd = cookie_domain.lower().lstrip("www.")
            if domain == cd or domain.endswith("." + cd):
                return cookies
    except Exception:
        pass
    return {}


# ── Reference Search & Enrichment ─────────────────────────────────────────────

def search_web(query: str, max_results: int = 3) -> list[str]:
    """Search DuckDuckGo HTML and return top result URLs (no API key required)."""
    try:
        encoded = requests.utils.quote(query)
        search_url = f"https://html.duckduckgo.com/html/?q={encoded}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
        }
        resp = requests.get(search_url, headers=headers, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        urls = []
        for a in soup.select("a.result__a"):
            href = a.get("href", "")
            if "uddg=" in href:
                raw = href.split("uddg=")[1].split("&")[0]
                actual = requests.utils.unquote(raw)
                if actual.startswith("http") and "duckduckgo.com" not in actual:
                    urls.append(actual)
                    if len(urls) >= max_results:
                        break
        log(f"    Web search '{query[:50]}' → {len(urls)} result(s)")
        return urls
    except Exception as e:
        log(f"    Web search error: {e}")
        return []


def fetch_ref_content(url: str, timeout: int = 30) -> str:
    """Fetch and extract readable text from a URL for reference enrichment.

    Uses stored cookies for subscribed sites so paywalled reference sources
    (papers on member-only blogs, etc.) are also reachable.
    Returns empty string on any failure.
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        site_cookies = get_cookies_for_url(url)
        if site_cookies:
            log(f"    🍪 Using stored cookies for: {urlparse(url).netloc}")
        resp = requests.get(url, headers=headers, cookies=site_cookies,
                            timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["nav", "footer", "header", "script", "style",
                         "aside", "form", "button", "iframe"]):
            tag.decompose()
        for selector in [
            "article", "[class*='post-content']", "[class*='article-body']",
            "[class*='entry-content']", "[class*='post-body']",
            "[class*='article-content']", "main",
        ]:
            el = soup.select_one(selector)
            if el:
                text = re.sub(r"\s+", " ", el.get_text()).strip()
                if len(text) > 200:
                    return text[:MAX_ARTICLE]
        paras = [p.get_text() for p in soup.find_all("p") if len(p.get_text()) > 50]
        if paras:
            return re.sub(r"\s+", " ", " ".join(paras)).strip()[:MAX_ARTICLE]
    except requests.exceptions.Timeout:
        log(f"    Fetch timed out: {url[:70]}")
    except Exception as e:
        log(f"    Fetch error: {e}")
    return ""


def extract_named_references(text: str) -> list[str]:
    """Ask qwen to identify specific named external sources mentioned in text.

    Returns 0-3 search query strings for named papers, repos, blog posts,
    AI models, or datasets. Skips vague references like "a recent study."
    """
    if len(text) < 300:
        return []

    snippet = text[:3000]
    prompt = f"""Read the following content and identify any specific named external resources that are referenced — such as named research papers, GitHub repositories, blog posts, specific AI models by name, or named datasets.

Return ONLY a JSON array of search query strings (max 3) suitable for a web search to find each resource. If nothing specific is named, return an empty array [].

Examples of what to extract:
- "the Attention is All You Need paper" → ["Attention is All You Need transformer paper"]
- "Mistral's new 7B model" → ["Mistral 7B model release"]
- "the LangChain blog post on agents" → ["LangChain blog agents 2025"]
- "llama.cpp on GitHub" → ["llama.cpp GitHub repository"]

Do NOT include vague references like "a recent study", "researchers found", "according to experts", or company homepages. Only specific, named resources worth fetching.

Content:
{snippet}

Return only the JSON array on a single line, nothing else:"""

    try:
        result = ollama_summarize(prompt, timeout=45)
        match = re.search(r'\[.*?\]', result, re.DOTALL)
        if match:
            refs = json.loads(match.group())
            if isinstance(refs, list):
                clean = [str(r).strip() for r in refs if isinstance(r, str) and len(r.strip()) > 5]
                return clean[:3]
    except Exception as e:
        log(f"    Reference extraction error: {e}")
    return []


def enrich_with_references(content: str, source: str, title: str) -> str:
    """Find named references in content, search + fetch each, append as context.

    Used for both podcast transcripts and newsletter content. Caps at
    2 references per item. Returns enriched content (unchanged if none found).
    """
    if len(content) < 300:
        return content

    log(f"    Extracting named references from: {title[:50]}")
    refs = extract_named_references(content)
    if not refs:
        log(f"    No named references found.")
        return content

    log(f"    References to search: {refs}")
    appended = []

    for ref in refs[:2]:
        log(f"    → Searching: {ref}")
        urls = search_web(ref)
        if not urls:
            log(f"      No results for: {ref}")
            continue

        for url in urls[:2]:
            # Skip obviously non-article URLs
            parsed_path = url.lower()
            if any(x in parsed_path for x in [".jpg", ".png", ".gif", ".pdf",
                                               "twitter.com", "x.com", "linkedin.com",
                                               "facebook.com", "instagram.com"]):
                continue
            log(f"      Fetching: {url[:70]}")
            fetched = fetch_ref_content(url)
            if len(fetched) > 200:
                appended.append(
                    f"\n\n--- Referenced Source: {ref} ---\n"
                    f"URL: {url}\n\n"
                    f"{fetched[:2000]}"
                )
                log(f"      ✓ {len(fetched):,} chars from: {url[:60]}")
                break

    if appended:
        log(f"    Enriched with {len(appended)} referenced source(s).")
        return content + "".join(appended)
    return content


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
    """Summarize a single newsletter or podcast episode, enriched with RAG context
    and any referenced external sources fetched via web search."""
    # Reference enrichment: find named papers/repos/models mentioned in the
    # content, search for each, fetch and append source material so qwen
    # summarizes with the original referenced content, not just a mention.
    content = enrich_with_references(item["content"], item["source"], item["subject"])

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

When AI coding tools are compared or discussed (e.g. Claude Code, Codex, Cursor, Copilot, Aider, Devin), highlight the nuanced differences between them — specifically around task clarity requirements, how much context or specification each tool needs, agent management style (autonomous vs. guided), and how they handle multi-file or multi-step tasks. Do not flatten them into "all are similar" — the differences matter for choosing the right tool for the right job.

When open-source AI model adoption is mentioned, emphasize the trend of businesses and developers moving toward running models locally (Ollama, llama.cpp, LM Studio, vLLM) to avoid proprietary API dependency, data privacy concerns, and ongoing cost. If a specific open-source model is named, note its size, architecture, and benchmark performance if mentioned.

{rag_context}

Source: {item['source']}
Title: {item['subject']}

Content:
{content}

Write a concise 3-5 sentence summary. If this topic was covered in past digests (see RELEVANT PAST KNOWLEDGE above), note what's new or different.

If the content names specific external resources — papers, GitHub repos, blog posts, AI models, datasets, or tools — add a line at the very end formatted exactly as:
Referenced: [name 1], [name 2], ...
Omit this line entirely if nothing specific is named (do not write "Referenced: none").

Only add a "→ CIRRUS NOTE:" bullet if this content mentions something CONCRETELY actionable for CIRRUS itself — for example: a specific Ollama model to pull by name (with its model string), a specific Python package to install, a specific RSS feed or newsletter URL worth adding to sources.json, or a specific code change to make. For open-source models, if one is named and seems worth tracking locally, note it by exact model name. For AI tool comparisons, only add a CIRRUS NOTE if there is a specific workflow recommendation worth logging.
DO NOT add a CIRRUS NOTE for: general AI trend observations, content descriptions, podcast themes, vague suggestions like "consider monitoring more sources", or source attribution lines. Most items should have NO CIRRUS NOTE — only add one when there is a specific, named action."""

    return ollama_summarize(prompt)

def generate_learning_report(summaries, all_items):
    """Generate the Weekly Learning Report — a meta-analysis of what CIRRUS
    observed this week across ALL sources (daily digests + this week's podcasts).

    Reads the last 7 daily digest files from disk, combines them with this
    week's podcast summaries, then asks qwen to identify patterns, themes,
    and what to prioritize next week.

    Four sections:
      1. Top Themes This Week
      2. Emerging vs. Ongoing
      3. Watch Next Week
      4. Source Quality Note
    """
    # ── Gather daily digest content from the past 7 days ─────────────────────
    daily_excerpts = []
    for days_ago in range(1, 8):
        date = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        daily_file = OUTPUT_DIR / f"daily-{date}.md"
        if daily_file.exists():
            try:
                content = daily_file.read_text()
                # Trim to first 2,500 chars — enough context without overwhelming qwen
                daily_excerpts.append(f"=== Daily {date} ===\n{content[:2500]}")
                log(f"  Learning Report: loaded daily-{date}.md")
            except Exception as e:
                log(f"  Learning Report: could not read daily-{date}.md ({e})")

    if not daily_excerpts:
        log("  Learning Report: no daily digests found for the past 7 days")

    # Combine sources: daily digests (up to 5 days) + this week's summaries
    daily_block = "\n\n".join(daily_excerpts[:5]) or "(no daily digests available)"
    weekly_block = "\n\n".join(summaries[:10]) or "(no weekly summaries available)"

    # Source names for context
    source_names = list({item["source"] for item in all_items})
    sources_line = ", ".join(source_names[:10]) if source_names else "various"

    prompt = f"""You are CIRRUS, an AI assistant that has been monitoring AI developments all week across newsletters, podcasts, and RSS feeds.

DAILY DIGEST EXCERPTS (last 5 days):
{daily_block}

THIS WEEK'S PODCAST & SOURCE SUMMARIES:
{weekly_block}

Sources monitored this week: {sources_line}

Generate a **Weekly Learning Report** with exactly these four sections:

**1. TOP THEMES THIS WEEK**
3-5 bullet points. The most frequently appearing topics this week. Be specific — name actual tools, models, companies, and techniques. Do NOT use vague categories like "AI advancement" or "continued progress."

**2. EMERGING VS. ONGOING**
Emerging (new this week or rapidly accelerating): specific developments that appeared or became urgent THIS week.
Ongoing (steady presence from prior weeks): topics that have been consistently present across multiple weeks.

**3. WATCH NEXT WEEK**
2-3 specific items — products, companies, model releases, events, or regulatory actions — to pay close attention to in the coming 7 days, and a one-line reason why each matters.

**4. SOURCE QUALITY NOTE**
1-2 sentences. Which source types (newsletters, podcasts, RSS) delivered the most substantive AI content this week vs. which were mostly noise or off-topic. Be specific about source types, not vague praise.

Write for a developer running local AI models on Mac Studio who wants to stay at the leading edge of AI tooling and infrastructure. Be direct and specific throughout."""

    try:
        result = ollama_summarize(prompt, timeout=300)
        if result.startswith("[Summarization error:"):
            log(f"Learning Report error: {result}")
            return "*Weekly Learning Report unavailable (Ollama error — see digest.log).*"
        return result
    except Exception as e:
        log(f"Learning Report failed: {e}")
        return "*Weekly Learning Report unavailable (error — see digest.log).*"


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

def write_digest(items, summaries, meta, learning_report=None):
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

        # Weekly Learning Report (Sunday only — appears before Self-Improvement Notes)
        if learning_report:
            f.write("## 📊 Weekly Learning Report\n\n")
            f.write("*Meta-analysis of this week's AI developments across all CIRRUS sources:*\n\n")
            f.write(f"{learning_report}\n\n")
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

    # Weekly Learning Report (Sundays only — meta-analysis for Buddy)
    learning_report = None
    if datetime.now().weekday() == 6:  # 6 = Sunday
        log("Sunday detected — generating Weekly Learning Report...")
        try:
            learning_report = generate_learning_report(summaries, all_items)
            log("Weekly Learning Report generated.")
        except Exception as e:
            log(f"Weekly Learning Report crashed unexpectedly: {e}")
            learning_report = "*Weekly Learning Report unavailable (unexpected error — see digest.log).*"
    else:
        log("Skipping Weekly Learning Report (runs on Sundays only).")

    # Write digest
    digest_file = write_digest(all_items, summaries, meta, learning_report=learning_report)

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
