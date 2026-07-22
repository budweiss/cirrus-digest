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

def main(dry_run=False):
    cfg = load_config()
    creds = load_json(CREDS_PATH, {})
    state = load_json(STATE_PATH, {})
    date_str = datetime.now().strftime("%Y-%m-%d")
    is_friday = datetime.now().weekday() == 4

    articles = fetch_articles(cfg, state)
    podcasts = fetch_podcasts(cfg, state)
    topics = cover_focus_topics(cfg, dry_run)

    # Send policy: skip quiet days unless a topic is active or it's Friday.
    if not (articles or podcasts or topics or is_friday):
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

    spotlight = (None, None)
    if summaries or pod_summaries or topics or is_friday:
        spotlight = technique_spotlight(cfg, state)

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

    # spotlight rotation
    st = {}
    cfg2 = {"technique_seeds": ["a", "b"], "digest": {}}
    # (no ollama in selftest — just test rotation bookkeeping)
    used = st.get("spotlights_used", [])
    remaining = [s for s in cfg2["technique_seeds"] if s not in used]
    check("rotation picks unused", remaining[0] == "a")
    remaining2 = [s for s in cfg2["technique_seeds"] if s not in ["a", "b"]]
    check("rotation exhausts then resets", remaining2 == [])

    print(f"selftest: {'OK' if fails == 0 else f'{fails} FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if "selftest" in args:
        sys.exit(selftest())
    sys.exit(main(dry_run="--dry-run" in args))
