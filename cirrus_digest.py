#!/usr/bin/env python3
"""
CIRRUS AI Digest — Project 1
Fetches AI newsletters from Yahoo email and podcast RSS feeds,
summarizes with a local Ollama model, saves a weekly digest.
"""

import imaplib
import email
import json
import os
import re
import subprocess
import tempfile
import requests
import feedparser
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from email.header import decode_header
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────

CONFIG_PATH = Path.home() / "projects/cirrus-digest/config/sources.json"

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

EMAIL_CFG   = CONFIG["email"]
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

# ── Email Fetcher ─────────────────────────────────────────────────────────────

def fetch_emails(password):
    """Connect to Yahoo IMAP and fetch newsletters from the past week."""
    log("Connecting to Yahoo Mail...")
    results = []

    try:
        mail = imaplib.IMAP4_SSL(EMAIL_CFG["imap_server"], EMAIL_CFG["imap_port"])
        mail.login(EMAIL_CFG["account"], password)
        mail.select("inbox")

        since_date = (datetime.now() - timedelta(days=EMAIL_CFG["days_back"])).strftime("%d-%b-%Y")
        _, msg_ids = mail.search(None, f'SINCE {since_date}')

        senders_lower = [s.lower() for s in EMAIL_CFG["senders"]]
        keywords_lower = [k.lower() for k in EMAIL_CFG["keywords"]]

        for msg_id in msg_ids[0].split():
            _, msg_data = mail.fetch(msg_id, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])

            # Decode sender and subject
            from_raw = msg.get("From", "")
            subj_raw, enc = decode_header(msg.get("Subject", ""))[0]
            subject = subj_raw.decode(enc or "utf-8") if isinstance(subj_raw, bytes) else subj_raw

            # Filter by sender
            if not any(s in from_raw.lower() for s in senders_lower):
                continue

            log(f"  Found: {subject[:60]} | From: {from_raw[:40]}")

            # Extract body
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    ct = part.get_content_type()
                    if ct == "text/plain":
                        body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                        break
                    elif ct == "text/html" and not body:
                        body = clean_text(
                            part.get_payload(decode=True).decode("utf-8", errors="ignore"),
                            MAX_ARTICLE
                        )
            else:
                body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

            body = clean_text(body, MAX_ARTICLE)

            results.append({
                "source": from_raw,
                "subject": subject,
                "content": body,
                "type": "email"
            })

        mail.logout()
        log(f"Email: found {len(results)} relevant newsletters")

    except Exception as e:
        log(f"Email fetch error: {e}")

    return results

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
                                # Truncate to max length
                                content = transcript[:MAX_EPISODE * 3] if len(transcript) > MAX_EPISODE * 3 else transcript
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

Write a concise 3-5 sentence summary. If this topic was covered in past digests (see RELEVANT PAST KNOWLEDGE above), note what's new or different. End with one bullet point labeled "→ CIRRUS NOTE:" if anything is directly relevant to improving this AI system."""

    return ollama_summarize(prompt)

def generate_meta_recommendations(summaries):
    """Ask the model to reflect on improvements to the digest process itself."""
    combined = "\n\n".join(summaries[:5])  # use first 5 summaries
    prompt = f"""You are CIRRUS, reviewing your own weekly AI digest process.

Based on these summaries from this week's digest:

{combined}

Suggest 2-3 specific improvements to how CIRRUS monitors, fetches, or summarizes AI content.
Consider: better sources, smarter filtering, new tools mentioned in the content, or process improvements.
Be specific and actionable. Format as a numbered list."""

    return ollama_summarize(prompt)

# ── Digest Writer ─────────────────────────────────────────────────────────────

def write_digest(items, summaries, meta):
    """Write the final digest file."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = OUTPUT_DIR / f"digest-{date_str}.md"

    with open(filename, "w") as f:
        f.write(f"# CIRRUS Weekly AI Digest — {date_str}\n\n")
        f.write(f"Generated by CIRRUS using `{MODEL}`\n")
        f.write(f"Sources processed: {len(items)} ({sum(1 for i in items if i['type']=='email')} emails, {sum(1 for i in items if i['type']=='podcast')} podcast episodes)\n\n")
        f.write("---\n\n")

        # Newsletters
        email_items = [(i, s) for i, s in zip(items, summaries) if i["type"] == "email"]
        if email_items:
            f.write("## 📰 Newsletters\n\n")
            for item, summary in email_items:
                f.write(f"### {item['subject']}\n")
                f.write(f"*From: {item['source']}*\n\n")
                f.write(f"{summary}\n\n")
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

    # Load Yahoo password from credentials file
    creds_path = Path.home() / "projects/cirrus-digest/config/credentials.json"
    with open(creds_path) as f:
        password = json.load(f)["yahoo_password"]

    # Fetch content
    email_items  = fetch_emails(password)
    podcast_items = fetch_podcasts()
    all_items = email_items + podcast_items

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
    meta = generate_meta_recommendations(summaries)

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
