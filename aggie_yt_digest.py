#!/usr/bin/env python3
"""Aggie's daily brief: reviewed C1/C2 results, delivery owned by CIRRUS.

Dedicated collection runs separately from the shared video backlog. Only a
source-grounded, locally reviewed summary is eligible. The first confirmed
email includes both the setup update and available summaries. Newest videos
without usable results remain pending, and old episodes are labeled as old.
Buddy authorized daily delivery to Aggie only, with no cc, October 12/14.
The send cursor advances only after confirmed SMTP success. A lock prevents
concurrent runs; source, review and delivery evidence are retained separately.
"""
import fcntl
import json
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_DIR = Path(__file__).resolve().parent        # ~/projects/cirrus-digest on the box
STATE_PATH = PROJECT_DIR / "yt-watch/aggie_digest_state.json"
FINDINGS_DIR = PROJECT_DIR / "yt-watch/findings"
CREDS_PATH = PROJECT_DIR / "config/credentials.json"
AGGIE_TO = "weiss.aggie@gmail.com"

NY = ZoneInfo("America/New_York")
INTRO_SENT_KEY = "intro_sent"


def log(msg):
    print("[%s] %s" % (datetime.now(NY).strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def atomic_json(path, data):
    p = Path(path)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(p)


def aggie_channels():
    """The channels Aggie named, from the LIVE channels.json (box truth)."""
    d = load_json(PROJECT_DIR / "yt-watch/channels.json", {})
    return [c for c in d.get("channels", [])
            if c.get("lane") == "aggie" and c.get("channel_id")]


def feed_videos(channel_id, count=1):
    """The channel's newest `count` RSS entries, newest first. Read-only.

    Three attempts with backoff (S417): the 2026-10-15 00:30 collection had
    ALL aggie channels fail with a single HTTPError each, while the same
    feeds fetched fine at 05:35 — the known intermittent platform-wide feed
    404 window (yt-watch met it for years; see TRANSIENT notes in yt_watch).
    A one-shot fetch turns that nightly window into a whole lost brief.
    """
    import time as _time
    url = "https://www.youtube.com/feeds/videos.xml?channel_id=" + channel_id
    last_exc = None
    for attempt in range(3):
        if attempt:
            _time.sleep(30 * attempt)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                xml_text = r.read().decode("utf-8", "replace")
            break
        except Exception as exc:
            last_exc = exc
            log("  feed fetch attempt %d/3 failed: %s" % (attempt + 1, str(exc)[:80]))
    else:
        raise last_exc
    import xml.etree.ElementTree as ET
    NS = {"a": "http://www.w3.org/2005/Atom",
          "yt": "http://www.youtube.com/xml/schemas/2015"}
    root = ET.fromstring(xml_text)
    out = []
    for e in root.findall("a:entry", NS)[:max(1, int(count))]:
        vid = e.findtext("yt:videoId", default="", namespaces=NS)
        if not vid:
            continue
        out.append({
            "video_id": vid,
            "title": (e.findtext("a:title", default="", namespaces=NS) or "").strip(),
            "published": (e.findtext("a:published", default="", namespaces=NS) or "")[:10],
            "url": "https://www.youtube.com/watch?v=%s" % vid,
        })
    return out


def newest_video(channel_id):
    """Newest entry of the channel's RSS feed, or None. Read-only."""
    videos = feed_videos(channel_id, count=1)
    return videos[0] if videos else None


# ── findings parsing ────────────────────────────────────────────────────────
#
# yt_watch.render() format, per video:
#   ## <channel name> — <video title>
#   <published YYYY-MM-DD> · `<lane>` · <url>
#   - **<claim text>**
#     - applies: ...
#     - test: ...
# or the failure markers:
#   > **no transcript** — <reason>
#   > **extraction FAILED** — <reason>

SEC_RX = re.compile(r"^## (.+?) — (.+)$")
INFO_RX = re.compile(r"^(\d{4}-\d{2}-\d{2}) · `(.+?)` · (\S+)\s*$")
CLAIM_RX = re.compile(r"^- \*\*(.+?)\*\*\s*$")
NO_TR_RX = re.compile(r"^> \*\*no transcript\*\* — (.*)$")
FAIL_RX = re.compile(r"^> \*\*extraction FAILED\*\* —(.*)")


def parse_findings(paths):
    """Findings files -> {video_url: record}. Newest file wins on duplicates."""
    by_url = {}
    for path in paths:
        try:
            lines = Path(path).read_text(errors="replace").splitlines()
        except Exception:
            continue
        cur = None
        for line in lines:
            m = SEC_RX.match(line)
            if m:
                cur = {"channel": m.group(1).strip(), "title": m.group(2).strip(),
                       "published": "", "url": "", "claims": [],
                       "no_transcript": "", "extract_error": ""}
                continue
            if cur is None:
                continue
            m = INFO_RX.match(line)
            if m:
                cur["published"], _lane, cur["url"] = m.group(1), m.group(2), m.group(3)
                by_url.setdefault(cur["url"], cur)
                continue
            m = NO_TR_RX.match(line)
            if m:
                cur["no_transcript"] = m.group(1).strip()
                continue
            m = FAIL_RX.match(line)
            if m:
                cur["extract_error"] = m.group(1).strip()
                continue
            m = CLAIM_RX.match(line)
            if m:
                cur["claims"].append(m.group(1))
    return by_url


def findings_files(limit=14):
    """Newest findings files first (daily cadence gives plenty of slack)."""
    if not FINDINGS_DIR.exists():
        return []
    files = sorted(FINDINGS_DIR.glob("yt-watch-*.md"), reverse=True)
    return files[:limit]


def reviewed_summaries():
    """Only dedicated, reviewed results are eligible for client delivery."""
    by_url = {}
    for path in (PROJECT_DIR / 'yt-watch/aggie-summaries').glob('*.json'):
        data = load_json(path, {})
        if data.get('review', {}).get('approved') is not True or not data.get('channel'):
            continue
        video = data.get('video', {})
        if not video.get('url') or not isinstance(data.get('bullets'), list):
            continue
        by_url[video['url']] = dict(video, channel=data['channel'],
            bullets=data['bullets'], stale=data.get('stale', False), claims=[],
            no_transcript='', extract_error='')
    return by_url


# ── composing ────────────────────────────────────────────────────────────────

def render_quote_prefix(claim):
    """analyze() publishes claims as 'Presenter reports: <verbatim quote>'.

    The email shows the exact words in quotation marks; anything the publisher
    prefix finds odd is still the source's words, never ours. Keeping the
    prefix's meaning but not its machinery reads better for a client.
    """
    text = re.sub(r"^Presenter reports:\s*", "", claim, flags=re.I).strip()
    return text


def compose_email(sections, day_label):
    lines = ["Hi Aggie,", "",
             "Here's today's brief on the channels you follow — what each presenter said "
             "about stocks, the market, and government matters. These are summaries of their "
             "views, not independently verified facts or financial advice.", ""]
    for sec in sections:
        lines.append("**%s — %s**" % (sec["channel"], sec["title"]))
        lines.append("Published %s · %s" % (sec["published"], sec["url"]))
        lines.append("")
        if sec.get('stale'):
            lines.append("The latest post found on this YouTube channel is dated %s. "
                         "I’m continuing to check for new videos; this older episode "
                         "is not included as current market or government news." % sec['published'])
        elif sec.get("no_transcript"):
            lines.append("_This video had no captions available, so there's nothing to "
                         "summarize today._")
        elif sec.get("extract_error"):
            lines.append("_Something went wrong on our side reading this video — it will "
                         "be summarized on a later daily brief._")
        elif 'bullets' in sec and sec['bullets']:
            lines.extend('- ' + b for b in sec['bullets'])
        elif not sec["claims"]:
            lines.append("_No specific stock, market or government comments detected in "
                         "this one._")
        else:
            for c in sec["claims"]:
                lines.append('- "%s"' % render_quote_prefix(c))
        lines.append("")
    lines.append("A summary usually lands in your inbox each morning once the videos are in. "
                 "If a channel posts nothing on a day, you'll simply get nothing for it — "
                 "no filler.")
    lines.append("")
    lines.append("— CIRRUS, on behalf of Buddy")
    return "\n".join(lines).strip() + "\n"


def compose_intro(intro_body, day_label):
    # The template's leading maintenance notes are not part of the email.
    lines = intro_body.splitlines()
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith("#")):
        lines.pop(0)
    intro_body = "\n".join(lines)
    return ("Hi Aggie,\n\n" + intro_body.strip() + "\n\n— CIRRUS, on behalf of Buddy\n")


def catchup_sections(channels, by_url, state, cap=6):
    """Reviewed non-stale summaries Aggie has NOT yet been sent, newest first.

    S417 catch-up (Buddy asked 2026-10-15: "are there any old posting you can
    run with?"). Unlike pick_sections this is NOT gated on 'newest only' and
    does NO feed checks: it works purely from the reviewed cache, so it sends
    nothing that failed collection or review, never re-sends (cursor = state
    'sent' latest ids + 'sent_extra' history), never crosses the 30-day line
    (stale summaries are excluded here — TF's 2025 episodes stay labeled old,
    not republished as catch-up content), and is capped to bound the email.
    """
    sent = state.get("sent", {}) or {}
    sent_extra = state.get("sent_extra", {}) or {}
    names = [c.get("name", "?") for c in channels]
    order = {n: i for i, n in enumerate(names)}
    sections = []
    for url, sec in by_url.items():
        name = sec.get("channel")
        if name not in order or sec.get("stale") or sec.get("extract_error"):
            continue
        vid = url.split("v=")[-1]
        if (sent.get(name) or {}).get("video_id") == vid:
            continue
        if vid in (sent_extra.get(name) or {}):
            continue
        section = dict(sec)
        section.setdefault("url", url)
        sections.append(section)
    # Cap against the NEWEST posts, then present per channel oldest-first
    # inside each channel block so catch-up reads naturally.
    sections.sort(key=lambda s: (order[s["channel"]], s.get("published", "")))
    if len(sections) > cap:
        kept = set(id(s) for s in
                   sorted(sections, key=lambda s: (s.get("published", ""), order[s["channel"]]),
                          reverse=True)[:cap])
        sections = [s for s in sections if id(s) in kept]
    return sections


def pick_sections(channels, by_url, state):
    """Which of Aggie's newest videos have findings and haven't been sent."""
    sections, pending = [], []
    sent = state.get("sent", {}) or {}
    for ch in channels:
        name = ch.get("name", "?")
        try:
            new = newest_video(ch["channel_id"])
        except Exception as exc:
            pending.append(name + ' (feed check failed: ' + type(exc).__name__ + ')')
            continue
        if not new:
            pending.append(name + ' (feed empty)')
            continue
        if (sent.get(name) or {}).get("video_id") == new["video_id"]:
            continue
        sec = by_url.get(new["url"])
        if sec and sec.get("channel") == name and not sec.get("extract_error"):
            sections.append(sec)
        else:
            reason = "extraction needs retry" if sec and sec.get("extract_error") else "no findings yet"
            pending.append("%s (%s pub %s — %s)"
                           % (name, (new["title"] or "")[:50], new["published"], reason))
    return sections, pending


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = "dry"
    backfill = "--backfill" in argv
    for a in argv:
        if a in ("--live", "--mode=live"):
            mode = "live"
    dry_run = mode != "live"
    lock_path = PROJECT_DIR / "yt-watch/.aggie_digest.lock"
    with open(lock_path, "w") as lockf:
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("already running — refusing to double-send")
            return 0
        return work(dry_run, backfill=backfill)


def work(dry_run, backfill=False):
    import job_status
    channels = aggie_channels()
    if not channels:
        log("REFUSED: no aggie-lane channels in yt-watch/channels.json")
        job_status.record("aggieyt", False, "no aggie-lane channels")
        return 2
    state = load_json(STATE_PATH, {})
    by_url = reviewed_summaries()
    if backfill:
        sections = catchup_sections(channels, by_url, state)
        pending = []
    else:
        sections, pending = pick_sections(channels, by_url, state)
    for p in pending:
        log("  waiting for findings: %s" % p)
    now = datetime.now(NY)
    day_label = now.strftime("%A, %B %d")

    if not sections:
        log("no reviewed new summaries — no email")
        if not dry_run:
            job_status.record("aggieyt", not pending,
                              "no new summaries (%d pending)" % len(pending))
        return 1 if pending else 0
    body = compose_email(sections, day_label)
    subject = "Your daily YouTube brief — " + day_label
    introductory = not state.get(INTRO_SENT_KEY)
    if introductory:
        intro_path = PROJECT_DIR / "yt-watch/aggie_intro_note.md"
        intro = compose_intro(intro_path.read_text(errors="replace"), day_label)
        body = intro.rsplit('\n\n— CIRRUS', 1)[0] + '\n\n' + body.removeprefix('Hi Aggie,\n\n')
        subject = "Your daily video summaries are ready"
    elif backfill:
        subject = "Your YouTube catch-up brief — " + day_label
        body = body.removeprefix('Hi Aggie,\n\n')
        body = ("Hi Aggie,\n\n"
                "A short catch-up: here are earlier posts from the past month on "
                "your channels, in the same style as the daily briefs — what the "
                "presenters said about stocks, the market and government matters, "
                "attributed to them as always. Not financial advice, and just "
                "information.\n\n" + body.removeprefix('Hi Aggie,\n\n'))
    if pending:
        body += "\nSome new videos are still being checked and will follow when their summaries are ready.\n"
    if dry_run:
        print("DRY RUN — nothing sent. Would email to %s:\n" % AGGIE_TO)
        print("Subject: " + subject)
        print(body)
        return 0
    import mailer
    creds = load_json(CREDS_PATH, {})
    try:
        ok = mailer.send(creds["outlook_email"], creds["outlook_password"], AGGIE_TO,
                         subject, body, creds=creds, log=log, watch_promises=False)
    except Exception:
        job_status.record('aggieyt', False, 'digest send raised; cursor preserved')
        raise
    if not ok:
        job_status.record("aggieyt", False, "digest send failed")
        return 3
    sent = state.setdefault("sent", {})
    if introductory:
        state[INTRO_SENT_KEY] = now.strftime("%Y-%m-%d")
    for sec in sections:
        vid = sec["url"].split("v=")[-1]
        if backfill:
            extra = state.setdefault("sent_extra", {}).setdefault(sec["channel"], {})
            extra[vid] = now.strftime("%Y-%m-%d")
        else:
            sent[sec["channel"]] = {"video_id": vid, "date": now.strftime("%Y-%m-%d")}
        log("sent%s: %s — %s" % (" (catch-up)" if backfill else "", sec["channel"], vid))
    if backfill:
        # After a catch-up, every cached summary is sent; point the per-channel
        # cursor at the NEWEST cached video so the daily flow never re-sends a
        # newer-than-cursor video the client has already received.
        newest_by_channel = {}
        for url, sec2 in by_url.items():
            if sec2.get("channel") and not sec2.get("stale"):
                vid2 = url.split("v=")[-1]
                newest_by_channel.setdefault(sec2["channel"], []).append(
                    (sec2.get("published", ""), vid2))
        for name2, items in newest_by_channel.items():
            newest_vid = max(items)[1]
            sent[name2] = {"video_id": newest_vid, "date": now.strftime("%Y-%m-%d")}
    atomic_json(STATE_PATH, state)
    job_status.record("aggieyt", not pending, "sent %d section(s); %d pending" % (len(sections), len(pending)))
    return 0


# ── selftest ────────────────────────────────────────────────────────────────
# Same standard as the other modules here: the email is a client-facing
# external send, so the parsing/decision logic is exercised on fixtures before
# it goes anywhere near a mailbox. No network, no files outside a temp dir.

def selftest() -> int:
    import tempfile
    failures = 0
    def ck(name, cond):
        nonlocal failures
        print("  %s %s" % ("PASS" if cond else "FAIL", name))
        if not cond:
            failures += 1

    FINDINGS_FIXTURE = """# YT-WATCH findings — 2026-10-12

**2 video(s) processed, 3 claim(s).**

## Money On The Move — God Said What's Coming To The Markets
2026-10-06 · `aggie` · https://www.youtube.com/watch?v=7jxq5gCd2aY

- **Presenter reports: XRP will multiply when the window opens**
  - applies: Candidate for review against our stack; verify model identity, settings and workload before treating the result as applicable.
  - test: Proposed test, not performed: compare against the verbatim span above.
- **Presenter reports: the market dips first, then runs into next year**
  - applies: filler

## Financial Education — A Stock I Just Bought
2026-10-11 · `aggie` · https://www.youtube.com/watch?v=fed12345678

## Take FiVe - His Glory — Old Episode
2025-07-03 · `aggie` · https://www.youtube.com/watch?v=old12345678

- **Presenter reports: silver is going higher**
  - applies: f

## Someone Else — Unrelated
2026-10-12 · `hardware` · https://www.youtube.com/watch?v=hw123456789

- **Presenter reports: test the DGX Spark pool config**
  - test: none
"""

    with tempfile.TemporaryDirectory() as tmp:
        fd = Path(tmp) / "yt-watch/findings"
        fd.mkdir(parents=True)
        (fd / "yt-watch-2026-10-12.md").write_text(FINDINGS_FIXTURE)

        def set_project(tmp_path):
            aggie_yt.PROJECT_DIR = tmp_path
            aggie_yt.FINDINGS_DIR = fd

        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("aggie_yt_digest", __file__)
        aggie_yt = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(aggie_yt)
        set_project(tmp)
        by_url = aggie_yt.parse_findings(aggie_yt.findings_files())
        ck("parses all three aggie sections",
           set(by_url) >= {"https://www.youtube.com/watch?v=7jxq5gCd2aY",
                           "https://www.youtube.com/watch?v=fed12345678",
                           "https://www.youtube.com/watch?v=old12345678"})
        sec = by_url["https://www.youtube.com/watch?v=7jxq5gCd2aY"]
        ck("section fields survive the parse",
           sec["channel"] == "Money On The Move" and sec["published"] == "2026-10-06"
           and sec["title"].startswith("God Said"))
        ck("claims captured verbatim",
           sec["claims"][0] == "Presenter reports: XRP will multiply when the window opens")
        ck("non-flagged videos parse with empty claims",
           by_url["https://www.youtube.com/watch?v=fed12345678"]["claims"] == [])

        # empty-claims rendering says what happened instead of staying silent
        body = aggie_yt.compose_email([by_url["https://www.youtube.com/watch?v=fed12345678"]], "X")
        ck("zero-claim video is stated, not skipped",
           "No specific stock, market or government comments" in body)
        body2 = aggie_yt.compose_email([sec], "X")
        ck("claim quotes are quoted for the client", '"XRP will multiply' in body2)
        ck("client disclaimer present", "financial advice" in body2)

        # pick_sections: already-sent video is not sent again
        state = {"sent": {"Money On The Move": {"video_id": "7jxq5gCd2aY"}}}
        chans = [{"name": "Money On The Move", "channel_id": "UCX", "lane": "aggie"}]
        orig_new = aggie_yt.newest_video
        try:
            aggie_yt.newest_video = lambda cid: {
                "video_id": "7jxq5gCd2aY", "title": "t", "published": "2026-10-06",
                "url": "https://www.youtube.com/watch?v=7jxq5gCd2aY"}
            secs, pend = aggie_yt.pick_sections(chans, by_url, state)
            ck("already-sent video is skipped", secs == [] and pend == [])
            # unseen newest present in findings -> a section
            state = {}
            secs, pend = aggie_yt.pick_sections(chans, by_url, state)
            ck("new video with findings becomes a section",
               len(secs) == 1 and secs[0]["channel"] == "Money On The Move")
            # newest unseen WITHOUT findings -> pending, not silently sent
            aggie_yt.newest_video = lambda cid: {
                "video_id": "newnew11111", "title": "Brand New", "published": "2026-10-12",
                "url": "https://www.youtube.com/watch?v=newnew11111"}
            secs, pend = aggie_yt.pick_sections(chans, by_url, state)
            ck("video without findings waits (never fabricated)", secs == [] and len(pend) == 1)
        finally:
            aggie_yt.newest_video = orig_new
        # state round-trip must be atomic-safe (identical json)
        sp = Path(tmp) / "state.json"
        aggie_yt.atomic_json(sp, {"sent": {"a": {}}})
        ck("state file round-trips", load_json(sp, {}) == {"sent": {"a": {}}})

        # catch-up selection: never re-sends, never crosses the 30-day line
        chans2 = [{"name": "Money On The Move"}, {"name": "Financial Education"}]
        by2 = {
            "https://www.youtube.com/watch?v=sentold12345": {"channel": "Money On The Move", "published": "2026-10-13", "stale": False},
            "https://www.youtube.com/watch?v=cand1aaaaaaa": {"channel": "Money On The Move", "published": "2026-09-29", "stale": False},
            "https://www.youtube.com/watch?v=cand2aaaaaaa": {"channel": "Financial Education", "published": "2026-10-12", "stale": False},
            "https://www.youtube.com/watch?v=stale1aaaaaaaa": {"channel": "Take FiVe - His Glory", "published": "2025-07-03", "stale": True},
        }
        state2 = {"sent": {"Money On The Move": {"video_id": "sentold12345"}}}
        picked = aggie_yt.catchup_sections(chans2, by2, state2)
        ck("catch-up excludes already-sent and stale, includes unsent",
           [s["url"] for s in picked] == ["https://www.youtube.com/watch?v=cand1aaaaaaa",
                                          "https://www.youtube.com/watch?v=cand2aaaaaaa"])
        state3 = {"sent": {"Money On The Move": {"video_id": "sentold12345"}},
                  "sent_extra": {"Financial Education": {"cand2aaaaaaa": "2026-10-15"}}}
        picked3 = aggie_yt.catchup_sections(chans2, by2, state3)
        ck("catch-up never re-sends recorded history",
           [s["url"] for s in picked3] == ["https://www.youtube.com/watch?v=cand1aaaaaaa"])

    print("  selftest %s" % ("PASSED" if not failures else "FAILED"))
    return 1 if failures else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    raise SystemExit(main())
