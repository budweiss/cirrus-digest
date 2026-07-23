#!/usr/bin/env python3
"""
PEDAGOGY Daily — literacy research digest for Alyssa
====================================================
4th-grade reading/writing/English teacher, Avonworth Elementary (PA).
Design: pedagogy/PEDAGOGY-SPEC.md (Cowork repo). Session 37 build.

Pipeline (runs 6am via com.cirrus.pedagogy, before the 7am AI digest so the
two never contend for Ollama):
  articles (RSS) + podcasts (RSS → Whisper transcribe) → Ollama summaries
  written FOR a 4th-grade teacher → technique spotlight (one evidence-based
  practice, rotating) → focus topics (Alyssa's REQUEST: queue from intake)
  → markdown digest → email (Phase A: Buddy reviews; Phase B: flip
  config recipient to Alyssa) → Telegram note to Buddy.

Send policy (Buddy chose daily cadence; literacy sources publish ~weekly):
  daily run sends ONLY if there is new content or an active focus topic;
  Friday always sends a roundup so the week is never silent.

Modes:
  python3 pedagogy_daily.py            normal run
  python3 pedagogy_daily.py --dry-run  build digest md only; no email,
                                       no telegram, no state writes
  python3 pedagogy_daily.py selftest   offline tests, no network
"""

import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import feedparser
import requests

try:
    import llm_providers            # multi-provider (Claude/Gemini/...) — dormant until keyed
except Exception:                   # never let a missing module break the 6am digest
    llm_providers = None

# ── Paths & config ────────────────────────────────────────────────────────────

PROJECT_DIR = Path.home() / "projects/cirrus-digest"
CONFIG_PATH = PROJECT_DIR / "config/sources-pedagogy.json"
CREDS_PATH  = PROJECT_DIR / "config/credentials.json"
TOPICS_PATH = PROJECT_DIR / "config/topics-pedagogy.json"
STATE_PATH  = PROJECT_DIR / "config/pedagogy_state.json"
LOG_PATH    = PROJECT_DIR / "logs/pedagogy.log"

WHISPER_BIN = "/Users/buddy/Library/Python/3.9/bin/whisper"
WHISPER_MODEL = "small"

MAX_ARTICLE_CHARS   = 6000
MAX_TRANSCRIPT_CHARS = 20000
MAX_EPISODES_PER_RUN = 2      # daily cadence: keep transcription time bounded
DAYS_BACK = 3

# ── Source discovery (dry-spell → ask a foundation model for new sources) ──────
DRY_STREAK_TRIGGER   = 3      # consecutive dry runs before we go looking for sources
DISCOVERY_COOLDOWN_D = 7      # don't run discovery more than ~weekly
MAX_NEW_SOURCES      = 6      # cap how many validated sources we add per discovery


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] pedagogy: {msg}"
    print(line)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def load_config():
    cfg = load_json(CONFIG_PATH)
    if not cfg:
        raise SystemExit(f"missing/invalid {CONFIG_PATH}")
    return cfg


# ── Ollama ────────────────────────────────────────────────────────────────────

def ollama(prompt, cfg, timeout=180, model=None):
    d = cfg.get("digest", {})
    host = d.get("ollama_host", "http://localhost:11434")
    mdl = model or d.get("ollama_model", "qwen2.5:14b")
    try:
        resp = requests.post(f"{host}/api/generate",
                             json={"model": mdl, "prompt": prompt,
                                   "stream": False,
                                   "options": {"num_ctx": 8192}},
                             timeout=timeout)
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception as e:
        return f"[Summarization error: {e}]"


TEACHER_PROMPT = """You are writing for Alyssa, an EXPERIENCED 4th-grade
reading/writing/English teacher in Pennsylvania — over 10 years in the
classroom. She knows the fundamentals cold; do NOT explain basic concepts,
definitions, or routine practices — beginner-level content is useless to her
and must be omitted. Summarize the following {kind} in 4-8 sentences.
Focus ONLY on what is genuinely new or useful to a veteran teacher of
9-10 year olds: fresh research findings, advanced nuance or live debates,
and emerging or innovative practices — including approaches educators
outside the US are using — that she could take advantage of. Be specific:
when applicable, include concrete detail and a brief, real classroom example
of how it looks in practice — not vague generalities. Skip publisher
promotion, host chatter, and anything aimed at administrators. If there is
truly nothing new or useful for an experienced elementary reading/writing
teacher, reply exactly: NOT RELEVANT

Title: {title}
Source: {source}

Content:
{content}"""


def summarize_for_teacher(kind, title, source, content, cfg):
    prompt = TEACHER_PROMPT.format(kind=kind, title=title, source=source,
                                   content=content)
    model = None
    if kind == "podcast episode":
        model = cfg.get("digest", {}).get("podcast_model", "llama3.2:3b")
    out = ollama(prompt, cfg, model=model)
    if model and (not out or out.startswith("[Summarization error")):
        out = ollama(prompt, cfg)  # fall back to main model
    return out


# ── Articles ──────────────────────────────────────────────────────────────────

def strip_html(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def fetch_article_body(url):
    """Best-effort article text. Falls back to '' (we still have the RSS
    summary)."""
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "CirrusPedagogy/1.0"})
        r.raise_for_status()
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, "html.parser")
            for t in soup(["script", "style", "nav", "header", "footer"]):
                t.decompose()
            return re.sub(r"\s+", " ", soup.get_text(" ")).strip()[:MAX_ARTICLE_CHARS]
        except ImportError:
            return strip_html(r.text)[:MAX_ARTICLE_CHARS]
    except Exception:
        return ""


def fetch_articles(cfg, state):
    since = datetime.now() - timedelta(days=DAYS_BACK)
    seen = set(state.get("seen_urls", []))
    out = []
    for src in cfg.get("rss", []):
        url = src.get("rss") or src.get("url", "")
        if not url or url.startswith("TBD"):
            continue
        log(f"feed: {src['name']}")
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:10]:
                link = e.get("link", "")
                if not link or link in seen:
                    continue
                pub = (datetime(*e.published_parsed[:6])
                       if e.get("published_parsed") else datetime.now())
                if pub < since:
                    continue
                body = fetch_article_body(link) or strip_html(
                    e.get("summary", ""))[:MAX_ARTICLE_CHARS]
                if len(body) < 200:
                    continue
                seen.add(link)
                out.append({"source": src["name"], "title": e.get("title", ""),
                            "link": link, "content": body})
        except Exception as ex:
            log(f"  feed error ({src['name']}): {ex}")
    state["seen_urls"] = list(seen)[-2000:]
    log(f"articles: {len(out)} new")
    return out


# ── Podcasts ──────────────────────────────────────────────────────────────────

def transcribe(audio_url):
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        # requests with a real UA — Buzzsprout 403s urllib's default agent
        with requests.get(audio_url, stream=True, timeout=120,
                          headers={"User-Agent": "Mozilla/5.0 (CirrusPedagogy)"}) as r:
            r.raise_for_status()
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
        with tempfile.TemporaryDirectory() as outdir:
            subprocess.run([WHISPER_BIN, str(tmp_path), "--model", WHISPER_MODEL,
                            "--output_format", "txt", "--output_dir", outdir,
                            "--fp16", "False"],
                           capture_output=True, timeout=3600)
            txts = list(Path(outdir).glob("*.txt"))
            if txts:
                return txts[0].read_text()[:MAX_TRANSCRIPT_CHARS]
    except Exception as e:
        log(f"  transcription failed: {e}")
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
    return ""


def fetch_podcasts(cfg, state):
    since = datetime.now() - timedelta(days=DAYS_BACK)
    seen = set(state.get("seen_episodes", []))
    out = []
    for pod in cfg.get("podcasts", []):
        url = pod.get("feed", "")
        if not url or url.startswith("TBD"):
            continue
        if len(out) >= MAX_EPISODES_PER_RUN:
            break
        log(f"podcast: {pod['name']}")
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:3]:
                guid = e.get("id", e.get("link", e.get("title", "")))
                if not guid or guid in seen:
                    continue
                pub = (datetime(*e.published_parsed[:6])
                       if e.get("published_parsed") else datetime.now())
                if pub < since:
                    continue
                title = e.get("title", "Untitled")
                log(f"  new episode: {title[:60]}")
                audio = ""
                for enc in e.get("enclosures", []):
                    if "audio" in enc.get("type", ""):
                        audio = enc.get("href") or enc.get("url", "")
                        break
                content = transcribe(audio) if audio else ""
                tag = "[TRANSCRIBED]" if content else "[SHOW NOTES ONLY]"
                if not content:
                    content = strip_html(e.get("summary", ""))[:MAX_ARTICLE_CHARS]
                seen.add(guid)
                out.append({"source": pod["name"], "title": title,
                            "content": f"{tag}\n{content}"})
                if len(out) >= MAX_EPISODES_PER_RUN:
                    break
        except Exception as ex:
            log(f"  podcast error ({pod['name']}): {ex}")
    state["seen_episodes"] = list(seen)[-1000:]
    log(f"podcasts: {len(out)} new episodes")
    return out


# ── Source discovery via foundation models (dry-spell recovery) ───────────────

DISCOVERY_SYSTEM = (
    "You are a research librarian building a literacy-instruction news feed for "
    "Alyssa, an experienced (10+ yr) 4th-grade reading/writing/English teacher in "
    "the US. You only recommend ACTIVE, high-quality sources that publish regularly.")


def _discovery_user_prompt(existing_names):
    have = "; ".join(sorted(n for n in existing_names if n))[:1500] or "(none yet)"
    return (
        "Recommend NEW sources we do NOT already have, covering evidence-based and "
        "emerging literacy instruction (reading, writing, English) useful to a veteran "
        "elementary teacher. Include high-quality options from ANYWHERE in the world "
        "(UK, Ireland, Australia, New Zealand, Canada, etc.), not just the US.\n\n"
        "Return ONLY a JSON array (no prose, no code fence) of up to 8 objects:\n"
        '  {"name":"...", "type":"blog|podcast|youtube", '
        '"url":"<direct RSS/Atom feed URL for a blog/podcast, OR the YouTube channel '
        'URL or UC… channel_id for youtube>", "region":"...", "why":"one line"}\n'
        "Rules: prefer sources with a real RSS/Atom feed; for youtube give the channel "
        "URL or UC… id; do NOT invent URLs — only include sources you are confident "
        "exist and are active; exclude anything already in this list:\n" + have)


_UC_RX = re.compile(r"(UC[0-9A-Za-z_-]{20,})")


def youtube_to_rss(url_or_id):
    """Best-effort YouTube channel -> RSS feed URL. Handles a bare UC… id, a
    /channel/UC… URL, or an existing feeds/videos.xml URL. Returns '' for
    @handle / c/ / user/ forms that need an online lookup we skip here."""
    s = (url_or_id or "").strip()
    if "feeds/videos.xml" in s:
        return s
    m = _UC_RX.search(s)
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}" if m else ""


def validate_feed(url):
    """True if the URL parses as a feed with at least one entry."""
    try:
        return bool(getattr(feedparser.parse(url), "entries", None))
    except Exception:
        return False


def _extract_json_array(text):
    """Pull JSON objects out of a model reply. Tolerant of code fences, leading
    prose, and TRUNCATION (max_tokens cut-off) — salvages each complete {...}
    object even when the enclosing array was never closed."""
    if not text:
        return []
    t = re.sub(r"```(?:json)?", "", text)      # drop code fences
    i = t.find("[")
    frag = t[i:] if i != -1 else t
    j = frag.rfind("]")
    if j != -1:                                 # try a clean full-array parse first
        try:
            data = json.loads(frag[:j + 1])
            if isinstance(data, list):
                return data
        except Exception:
            pass
    objs, depth, start = [], 0, None            # salvage object-by-object
    for k, ch in enumerate(frag):
        if ch == "{":
            if depth == 0:
                start = k
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    objs.append(json.loads(frag[start:k + 1]))
                except Exception:
                    pass
                start = None
    return objs


def existing_source_index(cfg):
    idx = set()
    for s in cfg.get("rss", []):
        idx.add((s.get("name", "")).strip().lower())
        idx.add((s.get("rss") or s.get("url", "")).strip().lower())
    for p in cfg.get("podcasts", []):
        idx.add((p.get("name", "")).strip().lower())
        idx.add((p.get("feed", "")).strip().lower())
    idx.discard("")
    return idx


def vet_candidates(cands, cfg, validate=True):
    """Normalize + validate + dedupe model candidates. Returns (accepted, rejected)."""
    have = existing_source_index(cfg)
    accepted, rejected = [], []
    for c in cands:
        if not isinstance(c, dict):
            continue
        name = (c.get("name") or "").strip()
        typ = (c.get("type") or "").strip().lower()
        url = (c.get("url") or c.get("rss") or c.get("feed") or "").strip()
        if not name or not url:
            continue
        feed = youtube_to_rss(url) if typ == "youtube" else url
        reason = ""
        if typ == "youtube" and not feed:
            reason = "youtube handle needs online lookup"
        elif name.lower() in have or feed.lower() in have:
            reason = "already have it"
        elif len(accepted) >= MAX_NEW_SOURCES:
            reason = "over per-run cap"
        elif validate and not validate_feed(feed):
            reason = "feed did not validate (no entries)"
        if reason:
            rejected.append({"name": name, "type": typ, "url": url, "reason": reason})
            continue
        accepted.append({"name": name, "type": typ or "blog", "feed": feed,
                         "region": (c.get("region") or "").strip(),
                         "why": (c.get("why") or "").strip()})
        have.add(name.lower()); have.add(feed.lower())
    return accepted, rejected


def add_sources(cfg, accepted):
    """Append accepted sources: podcasts -> podcasts[] (transcribe path);
    blogs + youtube -> rss[] (article path)."""
    for a in accepted:
        if a["type"] == "podcast":
            cfg.setdefault("podcasts", []).append({"name": a["name"], "feed": a["feed"]})
        else:
            cfg.setdefault("rss", []).append({"name": a["name"], "rss": a["feed"]})
    return cfg


def discover_sources(cfg, creds, state, dry=False):
    """Ask a foundation model for new literacy sources, validate their feeds, and
    (unless dry) add them to sources-pedagogy.json. Returns a human summary.
    NEVER raises — the 6am digest must not break because discovery failed."""
    try:
        if llm_providers is None:
            return "discovery skipped: llm_providers unavailable"
        avail = llm_providers.available(creds)
        if not avail:
            return "discovery skipped: no foundation-model key in credentials.json"
        names = ([s.get("name", "") for s in cfg.get("rss", [])]
                 + [p.get("name", "") for p in cfg.get("podcasts", [])])
        provider, reply = llm_providers.escalate(
            DISCOVERY_SYSTEM, _discovery_user_prompt(names), creds, max_tokens=4000)
        cands = _extract_json_array(reply)
        if not cands:
            log(f"  discovery: {provider} reply {len(reply or '')} chars, 0 parsed; "
                f"head={ (reply or '')[:200]!r}")
        accepted, rejected = vet_candidates(cands, cfg)
        log(f"discovery via {provider}: {len(cands)} proposed, {len(accepted)} "
            f"validated, {len(rejected)} rejected" + (" (DRY)" if dry else ""))
        for a in accepted:
            log(f"  + [{a['type']}] {a['name']} ({a['region']}) {a['feed']}")
        for r in rejected:
            log(f"  - {r['name']}: {r['reason']}")
        if accepted and not dry:
            add_sources(cfg, accepted)
            CONFIG_PATH.write_text(json.dumps(cfg, indent=1, ensure_ascii=False))
            state["last_discovery"] = datetime.now().strftime("%Y-%m-%d")
            state["dry_streak"] = 0
        if not accepted:
            return (f"discovery via {provider}: 0 new valid sources "
                    f"({len(cands)} proposed, all duplicate/invalid)")
        head = f"{'would add' if dry else 'ADDED'} {len(accepted)} source(s) via {provider}:"
        return "\n".join([head] + [f"• [{a['type']}] {a['name']} — "
                                   f"{a['region']}: {a['feed']}" for a in accepted])
    except Exception as e:
        log(f"discovery error (non-fatal): {e}")
        return f"discovery error: {e}"


# ── Technique spotlight ───────────────────────────────────────────────────────

SPOTLIGHT_PROMPT = """You are writing a short "Technique Spotlight" for Alyssa,
an EXPERIENCED 4th-grade reading/writing/English teacher (10+ years — assume
she knows the basics; do NOT include any beginner-level explanation). The
technique is: {technique}

Write 4 short sections in markdown (no top-level heading):
**What it is** — 1-2 sentences, plain language; she may already use it.
**The evidence** — 2-3 sentences on why researchers recommend it (name the
research base honestly; do not invent citations or statistics).
**The advanced angle** — 2-3 sentences: a refinement, extension, or emerging
variation that experienced teachers — including educators outside the US —
are using; be honest if the innovation is early-stage.
**Try it this week** — 3-4 concrete bullet steps for a 4th-grade classroom,
pitched at a veteran teacher (skip setup she'd find obvious), and include ONE
specific worked example (e.g. a sample prompt, short text, or student exchange).
Keep the whole thing under 350 words."""


def technique_spotlight(cfg, state):
    seeds = cfg.get("technique_seeds", [])
    if not seeds:
        return None, None
    used = state.get("spotlights_used", [])
    remaining = [s for s in seeds if s not in used]
    if not remaining:            # all used — start the rotation over
        remaining, used = seeds[:], []
    pick = remaining[0]
    text = ollama(SPOTLIGHT_PROMPT.format(technique=pick), cfg)
    if text.startswith("[Summarization error"):
        return None, None
    state["spotlights_used"] = used + [pick]
    return pick, text


# ── Focus topics (Alyssa's REQUEST: queue via intake) ────────────────────────

TOPIC_PROMPT = """Alyssa, an experienced 4th-grade reading/writing/English
teacher (10+ years), asked for research on: {topic}

Write a practical research brief in markdown (no top-level heading, under
450 words) pitched at a veteran teacher — skip the basics and any beginner
explanation, emphasize the latest evidence, points of active debate, and
emerging practices (including ways educators outside the US approach it):
what the evidence says, what works in a 4th-grade classroom, and 2-3 concrete
next steps she can take. Include at least one specific, detailed example (a
sample activity, short text, or piece of student work) so it is immediately
usable. Name the research base honestly; do not invent citations, statistics,
or program names. If the topic is outside reading/writing/English instruction,
say so briefly and give your best practical pointer."""


def cover_focus_topics(cfg, dry_run):
    data = load_json(TOPICS_PATH, {"topics": []})
    covered = []
    for t in data.get("topics", []):
        if t.get("status") != "active":
            continue
        log(f"focus topic: {t['topic'][:60]}")
        brief = ollama(TOPIC_PROMPT.format(topic=t["topic"]), cfg, timeout=240)
        if brief.startswith("[Summarization error"):
            continue
        covered.append({"topic": t["topic"], "requested_by":
                        t.get("requested_by", ""), "brief": brief})
        t["status"] = "covered"
        t["covered"] = datetime.now().strftime("%Y-%m-%d")
    if covered and not dry_run:
        TOPICS_PATH.write_text(json.dumps(data, indent=2))
    return covered


# ── Digest build ──────────────────────────────────────────────────────────────

def build_digest(date_str, summaries, pod_summaries, spotlight, topics, cfg,
                 is_friday):
    lines = [f"# Literacy Research Digest — {date_str}",
             "*Prepared for Alyssa — 4th-grade reading, writing & English*", ""]
    if topics:
        lines.append("## Your requested topics\n")
        for t in topics:
            lines.append(f"### {t['topic']}\n\n{t['brief']}\n")
    if spotlight:
        name, text = spotlight
        lines.append(f"## Technique spotlight: {name}\n\n{text}\n")
    if pod_summaries:
        lines.append("## Podcast recaps\n")
        for p in pod_summaries:
            lines.append(f"### {p['title']}\n*{p['source']}*\n\n{p['summary']}\n")
    if summaries:
        lines.append("## Worth your time\n")
        for a in summaries:
            lines.append(f"### [{a['title']}]({a['link']})\n*{a['source']}*\n\n"
                         f"{a['summary']}\n")
    if is_friday:
        week = []
        outdir = Path(cfg["digest"]["output_dir"])
        for i in range(1, 7):
            d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            f = outdir / f"daily-{d}.md"
            if f.exists():
                week.append(d)
        lines.append("## This week\n")
        lines.append("Friday roundup — digests also went out on: "
                     + (", ".join(week) if week else "no other days this week")
                     + ".\n")
    lines.append("---\n*Reply to this email with feedback, or send a new "
                 "email with subject \"REQUEST: your topic\" to queue a "
                 "research subject.*")
    return "\n".join(lines)


# ── Email + Telegram ──────────────────────────────────────────────────────────

def send_email(subject, body_md, cfg, creds):
    import smtplib
    d = cfg["digest"]
    to_addr = d["recipient"]
    cc = [a for a in d.get("cc", []) if a and a != to_addr]
    from_email = creds["outlook_email"]     # legacy-misnamed Gmail sender
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_addr
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg.attach(MIMEText(body_md, "plain"))
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=60) as s:
        s.ehlo(); s.starttls(); s.ehlo()
        s.login(from_email, creds["outlook_password"])
        s.sendmail(from_email, [to_addr] + cc, msg.as_string())
    log(f"emailed digest to {to_addr}" + (f" cc {cc}" if cc else ""))


def telegram(text, creds):
    try:
        token, chat = creds["telegram_bot_token"], creds["telegram_user_id"]
    except Exception:
        return
    for payload in ({"parse_mode": "Markdown"}, {}):
        try:
            data = json.dumps({"chat_id": int(chat), "text": text,
                               **payload}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage", data=data,
                headers={"Content-Type": "application/json",
                         "User-Agent": "CirrusPedagogy/1.0"})
            urllib.request.urlopen(req, timeout=30).read()
            return
        except Exception as e:
            log(f"telegram attempt failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main(dry_run=False, force=False):
    cfg = load_config()
    creds = load_json(CREDS_PATH, {})
    state = load_json(STATE_PATH, {})
    date_str = datetime.now().strftime("%Y-%m-%d")
    is_friday = datetime.now().weekday() == 4

    articles = fetch_articles(cfg, state)
    podcasts = fetch_podcasts(cfg, state)
    topics = cover_focus_topics(cfg, dry_run)

    # Dry-spell source discovery (Buddy 2026-07-23): if the FEEDS produce nothing
    # for several days running, ask a foundation model for fresh literacy sources
    # (blogs/podcasts/YouTube, anywhere in the world), validate their feeds, and add
    # them — so the pipeline finds material again instead of going quiet. Rate-limited
    # by a cooldown; never blocks the digest (discover_sources never raises).
    streak = 0 if (articles or podcasts) else int(state.get("dry_streak", 0)) + 1
    state["dry_streak"] = streak
    last_disc = state.get("last_discovery")
    cooldown_ok = True
    if last_disc:
        try:
            cooldown_ok = (datetime.now()
                           - datetime.strptime(last_disc, "%Y-%m-%d")).days >= DISCOVERY_COOLDOWN_D
        except Exception:
            cooldown_ok = True
    if streak >= DRY_STREAK_TRIGGER and cooldown_ok and not dry_run:
        summary = discover_sources(cfg, creds, state, dry=False)
        state["last_discovery"] = date_str          # respect cooldown even if 0 added
        log("source-discovery: " + summary.replace("\n", " | "))
        telegram(f"\U0001F50E *Pedagogy source discovery* (dry spell {streak} days):\n"
                 + summary, creds)

    # Cheap early-out ONLY when nothing was even fetched (saves summarize cost).
    # NOTE: a non-empty raw fetch is NOT sufficient to send — articles can all be
    # filtered out as NOT RELEVANT below. The real send decision is made after
    # filtering (see the hard guard) so we never mail an empty shell.
    if not (articles or podcasts or topics or is_friday or force):
        log("nothing new + no active topics + not Friday — skipping send")
        if not dry_run:
            STATE_PATH.write_text(json.dumps(state, indent=2))
        return 0

    summaries = []
    for a in articles:
        s = summarize_for_teacher("article", a["title"], a["source"],
                                  a["content"], cfg)
        if s and "NOT RELEVANT" not in s and not s.startswith("[Summarization"):
            summaries.append({**a, "summary": s})
    pod_summaries = []
    for p in podcasts:
        s = summarize_for_teacher("podcast episode", p["title"], p["source"],
                                  p["content"], cfg)
        if s and "NOT RELEVANT" not in s and not s.startswith("[Summarization"):
            pod_summaries.append({**p, "summary": s})

    has_sourced = bool(summaries or pod_summaries or topics)

    # Technique spotlight = the guaranteed local-model fallback so a send day
    # always carries one genuinely useful item instead of an empty shell.
    spotlight = (None, None)
    if has_sourced or is_friday or force:
        spotlight = technique_spotlight(cfg, state)

    # HARD GUARD (Buddy 2026-07-23): never email an empty digest. If nothing
    # survived the relevance filter AND the spotlight didn't render, skip the
    # send instead of mailing Alyssa a title-and-footer shell. (This is the bug
    # that sent an empty 2026-07-23 digest: 1 article fetched, filtered out as
    # NOT RELEVANT, spotlight not rendered → empty email.)
    if not has_sourced and not (spotlight and spotlight[0]):
        log("empty digest (no relevant content, no spotlight) — skipping send")
        if not dry_run:
            STATE_PATH.write_text(json.dumps(state, indent=2))
            telegram("\U0001F4ED *Pedagogy*: dry day and no spotlight rendered "
                     "(Ollama?) — skipped the send so no empty email goes to "
                     "Alyssa. Sources were dry; the model-fallback task covers "
                     "generating content on days like this.", creds)
        return 0

    digest = build_digest(date_str, summaries, pod_summaries,
                          spotlight if spotlight[0] else None, topics, cfg,
                          is_friday)

    outdir = Path(cfg["digest"]["output_dir"])
    outdir.mkdir(parents=True, exist_ok=True)
    prefix = "DRYRUN-daily" if dry_run else "daily"
    outfile = outdir / f"{prefix}-{date_str}.md"
    outfile.write_text(digest)
    log(f"digest written: {outfile}")

    if dry_run:
        log("DRY RUN — no email, no telegram, no state writes")
        return 0

    send_email(f"Literacy Research Digest — {date_str}", digest, cfg, creds)
    STATE_PATH.write_text(json.dumps(state, indent=2))
    telegram(f"📚 *Pedagogy digest sent* ({date_str}): "
             f"{len(summaries)} article(s), {len(pod_summaries)} podcast(s), "
             f"{len(topics)} topic(s)"
             + (f", spotlight: {spotlight[0]}" if spotlight[0] else ""), creds)
    return 0


# ── Selftest (offline) ────────────────────────────────────────────────────────

def selftest():
    fails = 0

    def check(name, cond):
        nonlocal fails
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        fails += 0 if cond else 1

    check("strip_html", strip_html("<p>Hello <b>world</b></p>") == "Hello world")
    check("teacher prompt renders", "4th-grade" in TEACHER_PROMPT and
          "{content}" in TEACHER_PROMPT)
    check("spotlight prompt honest", "do not invent" in SPOTLIGHT_PROMPT)
    check("topic prompt honest", "not invent" in TOPIC_PROMPT)

    # digest assembly with all sections
    cfg = {"digest": {"output_dir": tempfile.mkdtemp()}}
    d = build_digest("2026-07-16",
                     [{"title": "T", "link": "http://x", "source": "S",
                       "summary": "sum"}],
                     [{"title": "Ep", "source": "Pod", "summary": "psum"}],
                     ("Choral reading", "**What it is** ..."),
                     [{"topic": "fluency", "requested_by": "alyssa",
                       "brief": "brief"}],
                     cfg, is_friday=False)
    check("digest: topics first", d.index("requested topics") < d.index("spotlight"))
    check("digest: all sections", all(k in d for k in
          ["Technique spotlight", "Podcast recaps", "Worth your time",
           "REQUEST:"]))
    d2 = build_digest("2026-07-17", [], [], None, [], cfg, is_friday=True)
    check("friday roundup renders", "This week" in d2)

    # empty-send guard (mirrors main): skip iff NO sourced content AND NO spotlight.
    def _skip_empty(has_sourced, spot_ok):
        return (not has_sourced) and (not spot_ok)
    check("guard: dry + no spotlight -> SKIP", _skip_empty(False, False) is True)
    check("guard: dry + spotlight -> send", _skip_empty(False, True) is False)
    check("guard: has content -> send", _skip_empty(True, False) is False)
    # a title/footer-only digest must be detectable as empty
    empty = build_digest("2026-07-23", [], [], None, [], cfg, is_friday=False)
    check("empty digest has no content sections", not any(
        k in empty for k in ["Technique spotlight", "Worth your time",
                             "Podcast recaps", "requested topics"]))

    # spotlight rotation
    st = {}
    cfg2 = {"technique_seeds": ["a", "b"], "digest": {}}
    # (no ollama in selftest — just test rotation bookkeeping)
    used = st.get("spotlights_used", [])
    remaining = [s for s in cfg2["technique_seeds"] if s not in used]
    check("rotation picks unused", remaining[0] == "a")
    remaining2 = [s for s in cfg2["technique_seeds"] if s not in ["a", "b"]]
    check("rotation exhausts then resets", remaining2 == [])

    # ── source discovery (offline: parsing / youtube-map / dedupe, no network) ──
    check("json array extracted from prose",
          _extract_json_array('here you go: [{"a":1}] thanks') == [{"a": 1}])
    check("json array: none -> []", _extract_json_array("no json here") == [])
    check("youtube UC id -> feed",
          youtube_to_rss("UC1234567890abcdefghijkl")
          == "https://www.youtube.com/feeds/videos.xml?channel_id=UC1234567890abcdefghijkl")
    check("youtube /channel/ URL -> feed",
          "channel_id=UCabcdefghij0123456789" in
          youtube_to_rss("https://youtube.com/channel/UCabcdefghij0123456789"))
    check("youtube existing feed passthrough",
          youtube_to_rss("https://www.youtube.com/feeds/videos.xml?channel_id=UCx")
          == "https://www.youtube.com/feeds/videos.xml?channel_id=UCx")
    check("youtube @handle -> '' (needs lookup)", youtube_to_rss("https://youtube.com/@someteacher") == "")
    # vet: dedupe against existing + youtube handle rejection (validate off = offline)
    cfg3 = {"rss": [{"name": "Shanahan on Literacy", "rss": "https://x/feed"}],
            "podcasts": [{"name": "Sold a Story", "feed": "https://y/feed"}]}
    cands = [
        {"name": "Shanahan on Literacy", "type": "blog", "url": "https://x/feed"},   # dup name
        {"name": "New UK Blog", "type": "blog", "url": "https://uk/feed"},           # ok
        {"name": "Handle Channel", "type": "youtube", "url": "https://youtube.com/@h"},  # reject
        {"name": "", "type": "blog", "url": "https://z/feed"},                       # no name
    ]
    acc, rej = vet_candidates(cands, cfg3, validate=False)
    check("vet accepts the new unique blog", any(a["name"] == "New UK Blog" for a in acc))
    check("vet rejects duplicate by name", any(r["name"] == "Shanahan on Literacy" for r in rej))
    check("vet rejects youtube @handle", any(r["name"] == "Handle Channel" for r in rej))
    check("vet skips nameless candidate", all(a["name"] for a in acc))
    # add_sources routes types correctly
    cfg4 = {"rss": [], "podcasts": []}
    add_sources(cfg4, [{"name": "B", "type": "blog", "feed": "http://b"},
                       {"name": "P", "type": "podcast", "feed": "http://p"},
                       {"name": "Y", "type": "youtube", "feed": "http://y"}])
    check("add_sources: blog+youtube -> rss[]", len(cfg4["rss"]) == 2)
    check("add_sources: podcast -> podcasts[]", len(cfg4["podcasts"]) == 1)

    print(f"selftest: {'OK' if fails == 0 else f'{fails} FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if "selftest" in args:
        sys.exit(selftest())
    if "--discover" in args:
        # On-demand source discovery. Default is a DRY report (proposes + validates,
        # writes nothing). Add --apply to actually add validated sources + persist.
        _cfg = load_config(); _creds = load_json(CREDS_PATH, {}); _state = load_json(STATE_PATH, {})
        _dry = "--apply" not in args
        print(discover_sources(_cfg, _creds, _state, dry=_dry))
        if not _dry:
            _state["last_discovery"] = datetime.now().strftime("%Y-%m-%d")
            STATE_PATH.write_text(json.dumps(_state, indent=2))
        sys.exit(0)
    sys.exit(main(dry_run="--dry-run" in args, force="--force" in args))
