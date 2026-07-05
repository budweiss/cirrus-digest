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
import unicodedata
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

# ── Subject-level skip patterns (transactional / non-digest email) ────────────
# Applied before keyword scan — keeps receipts, billing, etc. out regardless
# of sender allowlist. Add new patterns here; all comparisons are lowercased.
_OMIT_SUBJECT_PATTERNS = [
    "receipt",
    "invoice",
    "payment confirmation",
    "order confirmation",
    "billing",
    "your subscription to",
    "thank you for your purchase",
    "thank you for subscribing",
    "subscription renewal",
    "account statement",
    "monthly statement",
    "your statement",
    "statement of account",
]

# ── Paywall detection ─────────────────────────────────────────────────────────
# Substrings searched (case-insensitive) in fetched page text.
_PAYWALL_PHRASES = [
    "member-only story",
    "this story is only available to members",
    "subscribe to read",
    "subscribe to continue reading",
    "this post is for paying subscribers",
    "unlock this article",
    "subscribe to unlock",
    "paid subscribers only",
    "become a paying subscriber",
    "upgrade to read",
    "upgrade your subscription",
    "you've reached your limit",
    "this content is for subscribers",
    "sign in to read the rest",
    "read the full story",
]

PAYWALL_LOG_PATH = LOG_DIR / "paywalls.log"

def log_paywall_hit(url: str, sender: str, subject: str):
    """Append a paywall hit to the dedicated paywall log for Buddy to review.
    Also flags the domain for automatic cookie refresh if it's on the watchlist.
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{ts}] PAYWALL | URL: {url}\n          Sender: {sender}\n          Subject: {subject}\n"
    with open(PAYWALL_LOG_PATH, "a") as f:
        f.write(entry)
    log(f"    ⚠️  PAYWALL — logged to paywalls.log: {url[:70]}")
    flag_cookie_refresh(url)


# ── Site Cookie Injection ──────────────────────────────────────────────────────
# Stores per-domain browser session cookies so CIRRUS can fetch paywalled
# content from sites Buddy subscribes to.
#
# File: ~/projects/cirrus-digest/config/cookies.json  (chmod 600, never deploy)
# Format:
#   {
#     "medium.com":    { "uid": "...", "sid": "...", "sz": "..." },
#     "theatlantic.com": { "piano_d": "...", "xbc": "..." }
#   }
#
# Domain matching uses suffix logic: stored key "medium.com" also matches
# "towardsdatascience.medium.com" and any other medium.com subdomain.

COOKIES_PATH     = Path.home() / "projects/cirrus-digest/config/cookies.json"
WATCHLIST_PATH   = Path.home() / "projects/cirrus-digest/config/cookie_watchlist.json"
REFRESH_FLAG     = Path.home() / "projects/cirrus-digest/logs/cookie_refresh.needed"
_SITE_COOKIES: dict = {}
_SITE_COOKIES_LOADED = False
_COOKIE_WATCHLIST: list = []


def _load_cookie_watchlist():
    """Load the list of domains to watch for paywall hits."""
    global _COOKIE_WATCHLIST
    if WATCHLIST_PATH.exists():
        try:
            with open(WATCHLIST_PATH) as f:
                data = json.load(f)
            _COOKIE_WATCHLIST = [d.lower() for d in data.get("watch", [])]
        except Exception as e:
            log(f"Could not load cookie_watchlist.json: {e}")


def flag_cookie_refresh(url: str):
    """If URL's domain is on the watchlist, append it to the refresh flag file.

    The MacBook sync_cookies.sh polls this file every 30 minutes and
    automatically extracts fresh cookies from Safari for flagged domains.
    """
    if not _COOKIE_WATCHLIST:
        return
    try:
        domain = urlparse(url).netloc.lower().lstrip("www.")
        # Check if any watchlist entry matches this domain
        for watched in _COOKIE_WATCHLIST:
            if domain == watched or domain.endswith("." + watched):
                log(f"    🔔 Flagging {watched} for cookie refresh (MacBook will sync next cycle)")
                REFRESH_FLAG.parent.mkdir(parents=True, exist_ok=True)
                # Append domain to flag file (deduplicated at sync time)
                with open(REFRESH_FLAG, "a") as f:
                    f.write(f"{watched}\n")
                return
    except Exception as e:
        log(f"    flag_cookie_refresh error: {e}")


def _load_site_cookies():
    """Load cookies.json once on first use. Silently skips if file not found."""
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
    """Return stored cookies matching the URL's domain, or {} if none.

    Strips www. prefix and uses suffix matching so "medium.com" covers
    both "medium.com" and "towardsdatascience.medium.com".
    """
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
    """Search DuckDuckGo HTML and return top result URLs (no API key required).

    DuckDuckGo wraps result links in a redirect:
      //duckduckgo.com/l/?uddg=<encoded_url>&...
    We decode the `uddg` parameter to get the actual destination URL.
    """
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


def extract_named_references(text: str) -> list[str]:
    """Ask qwen to identify specific named external sources in text.

    Returns a list of 0-3 search query strings for: named research papers,
    GitHub repos, blog posts, specific AI models, or named datasets.
    Only returns results for clearly named, specific resources — not vague
    references like "a recent study" or "researchers found."
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
- "the DSPy framework" → ["DSPy framework Stanford"]

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


def enrich_with_references(body: str, sender: str, subject: str) -> str:
    """Extract named references from body, search + fetch each, append as context.

    Runs after link-following. Adds fetched source content to the body so
    qwen summarizes with the original referenced material, not just a mention.

    Caps at 2 references per item to keep latency reasonable.
    Returns the enriched body string (unchanged if no references found).
    """
    if len(body) < 300:
        return body

    log(f"    Extracting named references...")
    refs = extract_named_references(body)
    if not refs:
        log(f"    No named references found.")
        return body

    log(f"    References to search: {refs}")
    appended = []

    for ref in refs[:2]:
        log(f"    → Searching: {ref}")
        urls = search_web(ref)
        if not urls:
            log(f"      No results for: {ref}")
            continue

        fetched_content = ""
        fetched_url = ""
        for url in urls[:2]:
            if not is_article_url(url):
                continue
            log(f"      Fetching: {url[:70]}")
            content, paywalled = fetch_article_content(url)
            status = "paywalled" if paywalled else (
                "ok" if len(content) > 200 else "failed")
            record_link_visit(url, status, f"ref: {ref}", len(content))
            if paywalled:
                log_paywall_hit(url, sender, f"[ref] {subject}")
            if len(content) > 200:
                fetched_content = content[:2000]
                fetched_url = url
                log(f"      ✓ {len(content):,} chars from: {url[:60]}")
                break

        if fetched_content:
            appended.append(
                f"\n\n--- Referenced Source: {ref} ---\n"
                f"URL: {fetched_url}\n\n"
                f"{fetched_content}"
            )

    if appended:
        log(f"    Enriched body with {len(appended)} referenced source(s).")
        return body + "".join(appended)
    return body


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

def strip_unicode_format(text: str) -> str:
    """Strip Unicode formatting/invisible characters (category Cf).
    Spammers inject these between letters to bypass keyword filters —
    e.g. 'Me͏t͏aI' visually reads as 'MetaI' but the invisible
    U+034F chars create false \b word boundaries, making \bAI\b match.
    """
    return ''.join(c for c in text if unicodedata.category(c) != 'Cf')

def whole_word_match(keyword: str, text: str) -> bool:
    """Whole-word keyword match (\\b) — avoids 'ai' matching 'email' etc."""
    return bool(re.search(
        r'\b' + re.escape(keyword.lower()) + r'\b', text, re.IGNORECASE
    ))

def matches_keywords(text: str) -> bool:
    """True if text contains any configured digest keyword (whole-word).

    Used to filter EVERY digest item — RSS articles and emails alike — so
    off-topic content never reaches the digest, regardless of source.
    Returns True if no keywords are configured (filter disabled).
    """
    keywords = EMAIL_CFG.get("keywords", [])
    if not keywords:
        return True
    cleaned = strip_unicode_format(text.lower())
    return any(whole_word_match(k, cleaned) for k in keywords)

# ── Link visit tracking ───────────────────────────────────────────────────────
# Every external URL CIRRUS fetches during a run is recorded here and
# reported in a "Links Visited" section at the end of the daily digest,
# including paywall hits (sites needing cookie/login access).

_VISITED_LINKS: list = []

def record_link_visit(url: str, status: str, context: str = "", chars: int = 0):
    """Record a fetched URL. status: 'ok' | 'paywalled' | 'failed'."""
    _VISITED_LINKS.append({
        "url": url,
        "status": status,
        "context": context[:80],
        "chars": chars,
    })

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

def fetch_article_content(url: str, timeout: int = 30) -> tuple[str, bool]:
    """GET a URL and extract its main readable text.

    Returns (content, is_paywalled).
    content  — extracted article text, '' on failure or too-short content.
    is_paywalled — True if a paywall page was detected (content will be partial).

    Timeout defaults to 30s — internet is free at 7am, no rush needed.
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
        page_text_lower = resp.text.lower()

        # Paywall check before full parse
        is_paywalled = any(phrase in page_text_lower for phrase in _PAYWALL_PHRASES)

        soup = BeautifulSoup(resp.text, "html.parser")

        # Strip chrome elements
        for tag in soup(["nav", "footer", "header", "script", "style",
                         "aside", "form", "button", "iframe"]):
            tag.decompose()

        # Try common article containers in priority order
        # Medium uses <article>, Substack uses .post-content or article
        for selector in [
            "article",
            "[class*='post-content']",
            "[class*='article-body']",
            "[class*='entry-content']",
            "[class*='post-body']",
            "[class*='article-content']",
            "[class*='body-markup']",   # Substack
            "[class*='markup']",        # Substack fallback
            "main",
        ]:
            el = soup.select_one(selector)
            if el:
                text = clean_text(el.get_text(), MAX_ARTICLE)
                if len(text) > 200:
                    return text, is_paywalled

        # Fallback: join all substantial paragraphs
        paras = [p.get_text() for p in soup.find_all("p") if len(p.get_text()) > 50]
        if paras:
            text = re.sub(r"\s+", " ", " ".join(paras)).strip()
            if len(text) > 200:
                return text[:MAX_ARTICLE], is_paywalled

    except requests.exceptions.Timeout:
        log(f"    Fetch timed out: {url[:70]}")
    except requests.exceptions.HTTPError as e:
        log(f"    Fetch HTTP error {e.response.status_code}: {url[:70]}")
    except Exception as e:
        log(f"    Fetch error: {e}")

    return "", False


def score_article_url(url: str, subject_words: list[str]) -> int:
    """Score a URL for article quality. Higher = better candidate to follow.
    Used to pick the best link from a newsletter email.
    """
    score = 0
    url_lower = url.lower()
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    path = parsed.path.lower()

    # Prefer known quality article domains
    quality_domains = [
        "medium.com", "substack.com", "towards", "hackernoon.com",
        "towardsdatascience.com", "levelup.gitconnected.com",
        "betterprogramming.pub", "pub.towardsai.net", "newsletter",
        "blog.", "news.", "techcrunch", "venturebeat", "wired.com",
        "arstechnica", "theverge.com",
    ]
    for d in quality_domains:
        if d in domain or d in path:
            score += 10
            break

    # Longer, more specific paths are more likely to be articles
    path_depth = path.rstrip("/").count("/")
    score += min(path_depth * 2, 8)

    # URL contains words from the subject → strong signal it's the main article
    for word in subject_words:
        if len(word) > 4 and word in url_lower:
            score += 5

    # Penalise likely list/home/tag pages
    for fragment in ["/tag/", "/category/", "/author/", "/topics/", "/page/"]:
        if fragment in path:
            score -= 10

    return score

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

                # RSS feeds often only include a short teaser. Always try
                # fetching the full article page for richer content.
                entry_url = entry.get("link", "")
                if entry_url and is_article_url(entry_url):
                    log(f"    Fetching full article ({len(content)} chars in RSS)...")
                    fetched, paywalled = fetch_article_content(entry_url)
                    status = "paywalled" if paywalled else (
                        "ok" if len(fetched) > 200 else "failed")
                    record_link_visit(entry_url, status,
                                      entry.get("title", ""), len(fetched))
                    if paywalled:
                        log_paywall_hit(entry_url, source["name"], entry.get("title", ""))
                    if len(fetched) > len(content):
                        content = fetched
                        log(f"    ✓ Fetched {len(content):,} chars")

                # Keyword filter — EVERY item must match a configured keyword
                # in its title or content, or it's dropped from the digest.
                if not matches_keywords(
                        entry.get("title", "") + " " + content[:3000]):
                    log(f"  Skipping (no keyword match): {entry.get('title', '')[:60]}")
                    continue

                # Reference enrichment: search for named papers/models/repos
                # mentioned in the article and append their content as context.
                content = enrich_with_references(
                    content, source["name"], entry.get("title", "")
                )

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
            # timeout=60 prevents a single slow/stalled IMAP operation from
            # hanging the entire daily run indefinitely (observed 2026-07-05:
            # run froze >10 min inside mail.uid("fetch") with no timeout).
            mail = imaplib.IMAP4_SSL(account["imap_server"], account.get("imap_port", 993),
                                     timeout=60)
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
              # Per-email guard: one malformed email (bad encoding, huge
              # attachment, fetch error) must not abort the whole account —
              # previously "unknown encoding: unknown-8bit" killed the loop
              # and dropped every remaining email that day.
              try:
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

                # Skip transactional emails by subject (module-level constant).
                if any(pat in subject.lower() for pat in _OMIT_SUBJECT_PATTERNS):
                    log(f"  Skipping transactional email: {subject[:60]}")
                    continue

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

                # Sender allowlist match only affects HOW MUCH of the body we
                # scan for keywords — it no longer bypasses the keyword filter.
                # EVERY email must match a keyword to make the digest:
                #   - allowlisted sender: keyword anywhere in subject or body
                #     (trusted source, so we search the whole email)
                #   - unknown sender: keyword in subject or first 30 lines only
                #     (short window avoids false positives from generic
                #     newsletters that mention AI once in a sidebar or footer)
                #
                # whole_word_match() uses \b matching (substring "ai" would
                # match "email"/"paid") and matches_keywords() strips Unicode
                # formatting chars spammers inject to fake word boundaries.
                sender_match = any(s in from_lower for s in senders_lower)

                scan_raw = raw_body if sender_match else "\n".join(raw_body.splitlines()[:30])
                scan_text = clean_text(scan_raw) if raw_is_html else scan_raw
                if not matches_keywords(subject + " " + scan_text):
                    if sender_match:
                        log(f"  Skipping (trusted sender, no keyword match): {subject[:60]}")
                    continue

                body = clean_text(raw_body, MAX_ARTICLE)

                # ── Article link-following ────────────────────────────────────
                # Always try to fetch the full article from links in the email.
                # Newsletter emails are almost always teasers — following the
                # link gives qwen the real content for a better summary.
                # Uses URL scoring to pick the best candidate link.
                # Logs paywall hits to paywalls.log for Buddy to review.
                fetched_url = ""
                if raw_is_html:
                    soup_links = BeautifulSoup(raw_body, "html.parser")
                    hrefs = [a.get("href", "") for a in soup_links.find_all("a", href=True)]
                    candidate_urls = [u for u in hrefs if is_article_url(u)]

                    # Score and rank candidates; take top 3 to try
                    subject_words = re.findall(r"[a-z]{4,}", subject.lower())
                    ranked = sorted(
                        set(candidate_urls),
                        key=lambda u: score_article_url(u, subject_words),
                        reverse=True
                    )[:3]

                    for article_url in ranked:
                        log(f"    → Following: {article_url[:80]}")
                        fetched, paywalled = fetch_article_content(article_url)
                        status = "paywalled" if paywalled else (
                            "ok" if len(fetched) > 200 else "failed")
                        record_link_visit(article_url, status, subject, len(fetched))
                        if paywalled:
                            log_paywall_hit(article_url, from_raw, subject)
                            # Use what was fetched (partial content is still
                            # better than the email teaser) but keep looking
                            # for a non-paywalled link
                            if len(fetched) > len(body):
                                body = fetched
                                fetched_url = article_url
                            continue
                        if len(fetched) > len(body):
                            body = fetched
                            fetched_url = article_url
                            log(f"    ✓ Fetched {len(body):,} chars")
                            break
                    else:
                        if body:
                            log(f"    Using email body ({len(body):,} chars) — no better article found")

                # Reference enrichment: find named papers/repos/models in body,
                # search the web for each, fetch and append as additional context
                # so qwen summarizes with the original source material.
                body = enrich_with_references(body, from_raw, subject)

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
                    "published": published,
                    "fetched_url": fetched_url
                })
                found += 1
              except Exception as e:
                log(f"  Skipping email UID {uid} (error: {e})")

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

When AI coding tools are compared or discussed (e.g. Claude Code, Codex, Cursor, Copilot, Aider, Devin), highlight the nuanced differences between them — specifically around task clarity requirements, how much context or specification each tool needs, agent management style (autonomous vs. guided), and how they handle multi-file or multi-step tasks. Do not flatten them into "all are similar" — the differences matter for choosing the right tool for the right job.

When open-source AI model adoption is mentioned, emphasize the trend of businesses and developers moving toward running models locally (Ollama, llama.cpp, LM Studio, vLLM) to avoid proprietary API dependency, data privacy concerns, and ongoing cost. If a specific open-source model is named, note its size, architecture, and benchmark performance if mentioned.

{rag_context}

Source: {item['source']}
Title: {item['subject']}
Published: {item['published']}

Content:
{item['content']}

Write a concise 2-4 sentence summary. If this topic was covered in past digests (see RELEVANT PAST KNOWLEDGE above), note what's new or different.

If the content names specific external resources — papers, GitHub repos, blog posts, AI models, datasets, or tools — add a line at the very end:
Referenced: [name 1], [name 2], ...
IMPORTANT: Only add this line if you can name something specific. If nothing is named, do not write this line at all — not "Referenced: None", not "Referenced: N/A", not "Referenced: nothing". Simply end your response without it.

Only add a "→ CIRRUS NOTE:" bullet if this content mentions something CONCRETELY actionable for CIRRUS itself — for example: a specific Ollama model to pull by name (with its model string), a specific Python package to install, a specific RSS feed or newsletter URL worth adding to sources.json, or a specific code change to make. For open-source models, if one is named and seems worth tracking locally, note it by exact model name. For AI tool comparisons, only add a CIRRUS NOTE if there is a specific workflow recommendation worth logging.
DO NOT add a CIRRUS NOTE for: general AI trend observations, content descriptions, podcast themes, vague suggestions like "consider monitoring more sources", or source attribution lines. Most items should have NO CIRRUS NOTE — only add one when there is a specific, named action."""

    summary = ollama_summarize(prompt)
    # Strip any "Referenced: None/N/A/nothing" lines qwen produces despite instructions
    summary = re.sub(
        r'\nReferenced:\s*(None|none|N\/A|n\/a|nothing|no specific.*|-)?\s*$',
        '', summary, flags=re.IGNORECASE
    ).rstrip()
    return summary

# ── Digest Writer ─────────────────────────────────────────────────────────────

def write_digest(items, summaries):
    """Write the daily digest file."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = OUTPUT_DIR / f"daily-{date_str}.md"

    # Group by source type. Anything that isn't medium/substack/email
    # (blog, newsletter, etc.) goes in the catch-all web section — previously
    # these items were summarized but silently DROPPED from the digest.
    medium_items = [(i, s) for i, s in zip(items, summaries) if i["type"] == "medium"]
    substack_items = [(i, s) for i, s in zip(items, summaries) if i["type"] == "substack"]
    email_items = [(i, s) for i, s in zip(items, summaries) if i["type"] == "email"]
    web_items = [(i, s) for i, s in zip(items, summaries)
                 if i["type"] not in ("medium", "substack", "email")]

    with open(filename, "w") as f:
        f.write(f"# CIRRUS Daily Web Digest — {date_str}\n\n")
        f.write(f"Generated by CIRRUS using `{MODEL}`\n")
        f.write(f"Items processed: {len(items)} ({len(medium_items)} Medium, {len(substack_items)} Substack, {len(web_items)} Blog/News, {len(email_items)} Email)\n\n")
        f.write("---\n\n")

        if email_items:
            f.write("## 📰 Newsletters\n\n")
            for item, summary in email_items:
                f.write(f"### {item['subject']}\n")
                f.write(f"*From: {item['source']} — {item['published']}*\n\n")
                if item.get("fetched_url"):
                    f.write(f"🔗 *Full article fetched from:* {item['fetched_url']}\n\n")
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

        if web_items:
            f.write("## 🌐 Blogs & News\n\n")
            for item, summary in web_items:
                f.write(f"### {item['subject']}\n")
                f.write(f"*{item['source']} — {item['published']}*\n\n")
                if item.get('url'):
                    f.write(f"[Read article]({item['url']})\n\n")
                f.write(f"{summary}\n\n")
                f.write("---\n\n")

        # ── Links Visited section ─────────────────────────────────────────
        # Full transparency: every external URL CIRRUS fetched this run,
        # its outcome, and which sites need cookie/login access.
        if _VISITED_LINKS:
            emoji_map = {"ok": "✅", "paywalled": "🔒", "failed": "⚠️"}
            f.write("## 🔗 Links Visited This Run\n\n")
            for v in _VISITED_LINKS:
                em = emoji_map.get(v["status"], "•")
                size = f" ({v['chars']:,} chars)" if v["status"] == "ok" else ""
                ctx = f" — _{v['context']}_" if v["context"] else ""
                f.write(f"- {em} {v['url']}{size}{ctx}\n")

            paywalled_domains = sorted({
                urlparse(v["url"]).netloc.lstrip("www.")
                for v in _VISITED_LINKS if v["status"] == "paywalled"
            })
            if paywalled_domains:
                f.write("\n### 🔒 Access Needed\n\n")
                f.write("These sites returned paywalls — cookies are missing or expired:\n\n")
                for d in paywalled_domains:
                    f.write(f"- {d}\n")
                f.write("\nWatched domains are auto-flagged for cookie refresh from the MacBook. "
                        "Others need entries added to `cookies.json` "
                        "(Safari → Web Inspector → Storage → Cookies).\n")
            f.write("\n---\n\n")

        f.write(f"*End of daily digest — {date_str}*\n")

    log(f"Daily digest saved: {filename}")
    return filename

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("=== CIRRUS Daily Web Digest Starting ===")

    # Load cookie watchlist so paywall hits on watched domains get flagged
    # for automatic refresh by the MacBook sync_cookies.sh agent.
    _load_cookie_watchlist()
    if _COOKIE_WATCHLIST:
        log(f"Cookie watchlist loaded: {', '.join(_COOKIE_WATCHLIST)}")

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
