#!/usr/bin/env python3
"""
CIRRUS Daily Web Digest
Fetches Medium and Substack RSS feeds from the past 24 hours,
summarizes with a local Ollama model, saves a daily digest.
"""

import imaplib
import email
import json
import re
import requests
import feedparser
from datetime import datetime, timedelta
from email.header import decode_header
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────

CONFIG_PATH = Path.home() / "projects/cirrus-digest/config/sources.json"

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

WEB_SOURCES = CONFIG["web_sources"]
EMAIL_CFG   = CONFIG["email"]
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

# URL patterns that are never worth following (trackers, social, nav, images)
_SKIP_URL_PATTERNS = [
    "unsubscribe", "mailto:", "twitter.com", "x.com", "facebook.com",
    "linkedin.com", "instagram.com", "youtube.com", "t.co/", "click.",
    "track.", "open.", "beacon.", ".gif", ".jpg", ".png", ".svg",
    "privacy", "terms", "manage-subscription", "list-unsubscribe",
    "/tag/", "/category/", "/author/", "/feed", "?utm_",
]

def is_article_url(url: str) -> bool:
    """Return True if a URL looks like an article (not a tracker/social/nav link)."""
    if not url.startswith("http"):
        return False
    url_lower = url.lower()
    if any(pat in url_lower for pat in _SKIP_URL_PATTERNS):
        return False
    path = urlparse(url).path.rstrip("/")
    return len(path) > 5  # must have a non-trivial path, not just a homepage

def fetch_article_content(url: str, timeout: int = 8) -> str:
    """GET a URL and extract its main readable text.
    Returns '' on failure or if extracted content is too short to be useful.
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        # Strip chrome elements
        for tag in soup(["nav", "footer", "header", "script", "style",
                         "aside", "form", "button"]):
            tag.decompose()
        # Try common article containers in priority order
        for selector in [
            "article",
            "[class*='post-content']", "[class*='article-body']",
            "[class*='entry-content']", "[class*='post-body']",
            "[class*='article-content']", "main",
        ]:
            el = soup.select_one(selector)
            if el:
                text = clean_text(el.get_text(), MAX_ARTICLE)
                if len(text) > 200:
                    return text
        # Fallback: join all substantial paragraphs
        paras = [p.get_text() for p in soup.find_all("p") if len(p.get_text()) > 50]
        if paras:
            text = re.sub(r"\s+", " ", " ".join(paras)).strip()
            return text[:MAX_ARTICLE] if len(text) > 200 else ""
    except Exception:
        pass
    return ""

def ollama_summarize(prompt):
    """Send a prompt to local Ollama and return the response."""
    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": MODEL, "prompt": prompt, "stream": False,
                  "options": {"num_ctx": 8192}},
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

                # RSS feeds often only include a short teaser. If the content
                # is under 400 chars, try fetching the full article page.
                entry_url = entry.get("link", "")
                if entry_url and len(content) < 400 and is_article_url(entry_url):
                    log(f"    Short RSS snippet ({len(content)} chars), fetching full article...")
                    fetched = fetch_article_content(entry_url)
                    if len(fetched) > len(content):
                        content = fetched
                        log(f"    → fetched {len(content)} chars")

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

# ── Email Fetcher ─────────────────────────────────────────────────────────────

EMAIL_STATE_PATH = Path.home() / "projects/cirrus-digest/config/email_state.json"

def load_omit_senders():
    """Load sender substrings to always skip (junk, CIRRUS's own outgoing
    emails, etc.) from config/email_omit.txt.

    One entry per line; blank lines and lines starting with # are ignored.
    """
    omit_path = Path.home() / "projects/cirrus-digest/config/email_omit.txt"
    omit = []
    try:
        with open(omit_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    omit.append(line.lower())
    except FileNotFoundError:
        pass
    return omit

def load_email_state():
    """Load per-account IMAP tracking state (last processed UID + mailbox
    UIDVALIDITY) from config/email_state.json.

    Returns {} if the file is missing or unreadable - in that case every
    account falls back to treating last_uid as 0, i.e. the full
    daily_days_back window is scanned (same as before this tracking existed).
    """
    try:
        with open(EMAIL_STATE_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log(f"  Warning: could not read email_state.json ({e}), starting fresh")
        return {}

def save_email_state(state):
    """Persist per-account IMAP tracking state to config/email_state.json."""
    try:
        EMAIL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(EMAIL_STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log(f"  Warning: could not save email_state.json: {e}")

def fetch_emails(credentials):
    """Connect to each configured IMAP account and fetch newsletters that
    haven't been processed yet.

    For each account, only messages with a UID greater than the last
    processed UID (stored in config/email_state.json) are fetched, so the
    same email is never summarized twice across daily runs. The
    daily_days_back window is still used to bound the IMAP search (and as a
    fallback on the first run, or if the mailbox's UIDVALIDITY changes -
    which means previously stored UIDs may no longer refer to the same
    messages).

    An email is included if EITHER:
    - its sender matches one of EMAIL_CFG["senders"], OR
    - its subject or body contains one of EMAIL_CFG["keywords"]

    ...unless its sender matches an entry in config/email_omit.txt, in which
    case it's always skipped (e.g. CIRRUS's own outgoing digest emails, or
    junk senders added over time).
    """
    results = []
    senders_lower = [s.lower() for s in EMAIL_CFG["senders"]]
    keywords_lower = [k.lower() for k in EMAIL_CFG["keywords"]]
    omit_senders = load_omit_senders()
    state = load_email_state()

    days_back = EMAIL_CFG.get("daily_days_back", 3)
    since_date = (datetime.now() - timedelta(days=days_back)).strftime("%d-%b-%Y")

    for account in EMAIL_CFG.get("accounts", []):
        if not account.get("enabled", True):
            log(f"  Skipping {account.get('label', account.get('address'))}: disabled in config")
            continue

        label = account.get("label", account["address"])
        password = credentials.get(account["credential_key"])
        if not password:
            log(f"  Skipping {label}: no '{account['credential_key']}' in credentials.json")
            continue

        log(f"Connecting to {label} ({account['address']})...")
        found = 0
        try:
            mail = imaplib.IMAP4_SSL(account["imap_server"], account.get("imap_port", 993))
            mail.login(account["address"], password)
            mail.select("inbox")

            # UIDVALIDITY changes if the mailbox is recreated/migrated, which
            # means previously stored UIDs may now point at different
            # messages. Detect that and fall back to the date-window scan.
            uidvalidity = None
            try:
                typ, data = mail.status("inbox", "(UIDVALIDITY)")
                if typ == "OK" and data and data[0]:
                    m = re.search(rb"UIDVALIDITY (\d+)", data[0])
                    if m:
                        uidvalidity = int(m.group(1))
            except Exception:
                pass

            acct_state = state.get(label, {})
            last_uid = acct_state.get("last_uid", 0)
            if uidvalidity is not None and acct_state.get("uidvalidity") != uidvalidity:
                if acct_state:
                    log(f"  {label}: mailbox UIDVALIDITY changed - rescanning last {days_back} day(s)")
                last_uid = 0

            _, msg_ids = mail.uid("search", None, f'SINCE {since_date}')
            uids = [int(u) for u in msg_ids[0].split()]
            new_uids = [u for u in uids if u > last_uid]
            skipped = len(uids) - len(new_uids)

            for uid in new_uids:
                _, msg_data = mail.uid("fetch", str(uid), "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])

                # Decode sender and subject
                from_raw = msg.get("From", "")
                from_lower = from_raw.lower()

                # Always skip omitted senders, regardless of keyword/sender match
                if any(o in from_lower for o in omit_senders):
                    continue

                subj_raw, enc = decode_header(msg.get("Subject", ""))[0]
                subject = subj_raw.decode(enc or "utf-8") if isinstance(subj_raw, bytes) else subj_raw

                # Extract the raw (uncleaned) body so we can do a cheap
                # keyword pre-check before running the full HTML clean below.
                raw_body = ""
                raw_is_html = False
                if msg.is_multipart():
                    for part in msg.walk():
                        ct = part.get_content_type()
                        if ct == "text/plain":
                            raw_body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                            raw_is_html = False
                            break
                        elif ct == "text/html" and not raw_body:
                            raw_body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                            raw_is_html = True
                else:
                    raw_body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
                    raw_is_html = msg.get_content_type() == "text/html"

                # Match if sender is on the allowlist...
                sender_match = any(s in from_lower for s in senders_lower)

                if not sender_match:
                    # ...otherwise only keep it if a keyword shows up in the
                    # subject or the first 100 lines of the body. This skips
                    # the full HTML clean (and the email entirely) for
                    # newsletters that clearly aren't relevant, and avoids
                    # matching on stray keyword mentions buried in footers.
                    #
                    # IMPORTANT: use whole-word (\b) matching for all keywords.
                    # Simple substring ("ai" in text) causes massive false positives:
                    # "ai" matches "email", "paid", "trail", etc.
                    # "model" matches "payment model", "business model", etc.
                    preview_raw = "\n".join(raw_body.splitlines()[:100])
                    preview_text = clean_text(preview_raw) if raw_is_html else preview_raw
                    combined = (subject + " " + preview_text).lower()

                    def whole_word_match(keyword: str, text: str) -> bool:
                        return bool(re.search(
                            r'\b' + re.escape(keyword.lower()) + r'\b',
                            text,
                            re.IGNORECASE
                        ))

                    keyword_match = any(
                        whole_word_match(k, combined) for k in keywords_lower
                    )
                    if not keyword_match:
                        continue

                body = clean_text(raw_body, MAX_ARTICLE)

                # If the email body is short (teaser-style newsletter), try
                # following article links in the email to get the full content.
                if raw_is_html and len(body) < 800:
                    soup_links = BeautifulSoup(raw_body, "html.parser")
                    hrefs = [a.get("href", "") for a in soup_links.find_all("a", href=True)]
                    article_urls = [u for u in hrefs if is_article_url(u)][:3]
                    for article_url in article_urls:
                        log(f"    Following article link: {article_url[:70]}")
                        fetched = fetch_article_content(article_url)
                        if len(fetched) > len(body):
                            body = fetched
                            log(f"    → fetched {len(body)} chars from linked article")
                            break

                match_type = "sender" if sender_match else "keyword"
                log(f"  Found ({match_type}): {subject[:60]} | From: {from_raw[:40]}")

                try:
                    published = parsedate_to_datetime(msg.get("Date", "")).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    published = datetime.now().strftime("%Y-%m-%d %H:%M")

                results.append({
                    "source": f"{from_raw} ({label})",
                    "subject": subject,
                    "content": body,
                    "type": "email",
                    "published": published
                })
                found += 1

            mail.logout()

            if uids:
                state[label] = {"last_uid": max(uids), "uidvalidity": uidvalidity}

            log(f"  {label}: found {found} relevant email(s) "
                f"({len(new_uids)} new, {skipped} already processed)")

        except Exception as e:
            log(f"Email fetch error ({label}): {e}")

    save_email_state(state)
    log(f"Email: found {len(results)} relevant newsletter(s) total")
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

Write a concise 2-4 sentence summary. If this topic was covered in past digests (see RELEVANT PAST KNOWLEDGE above), note what's new or different.

Only add a "→ CIRRUS NOTE:" bullet if this content mentions something CONCRETELY actionable for CIRRUS itself — for example: a specific Ollama model to pull by name, a specific Python package to install, a specific RSS feed or newsletter URL worth adding to sources.json, or a specific code change to make. The note must describe a discrete action CIRRUS can execute.
DO NOT add a CIRRUS NOTE for: general AI trend observations, content descriptions, podcast themes, vague suggestions like "consider monitoring more sources", or source attribution lines. Most items should have NO CIRRUS NOTE — only add one when there is a specific, named action."""

    return ollama_summarize(prompt)

# ── Digest Writer ─────────────────────────────────────────────────────────────

def write_digest(items, summaries):
    """Write the daily digest file."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = OUTPUT_DIR / f"daily-{date_str}.md"

    # Group by source type
    medium_items = [(i, s) for i, s in zip(items, summaries) if i["type"] == "medium"]
    substack_items = [(i, s) for i, s in zip(items, summaries) if i["type"] == "substack"]
    email_items = [(i, s) for i, s in zip(items, summaries) if i["type"] == "email"]

    with open(filename, "w") as f:
        f.write(f"# CIRRUS Daily Web Digest — {date_str}\n\n")
        f.write(f"Generated by CIRRUS using `{MODEL}`\n")
        f.write(f"Items processed: {len(items)} ({len(medium_items)} Medium, {len(substack_items)} Substack, {len(email_items)} Email)\n\n")
        f.write("---\n\n")

        if email_items:
            f.write("## 📰 Newsletters\n\n")
            for item, summary in email_items:
                f.write(f"### {item['subject']}\n")
                f.write(f"*From: {item['source']} — {item['published']}*\n\n")
                f.write(f"{summary}\n\n")
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

    web_items = fetch_web_sources()

    email_items = []
    try:
        creds_path = Path.home() / "projects/cirrus-digest/config/credentials.json"
        with open(creds_path) as f:
            credentials = json.load(f)
        email_items = fetch_emails(credentials)
    except Exception as e:
        log(f"Email fetch skipped: {e}")

    items = web_items + email_items

    if not items:
        log("No new articles or emails found. Exiting.")
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
