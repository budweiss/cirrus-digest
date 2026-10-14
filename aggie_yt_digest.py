#!/usr/bin/env python3
"""aggie_yt_digest.py — the OFFER client's daily YouTube brief (prepared S412).

S415 audit (2026-10-14): deployed, but the daily schedule is not installed and
no introductory email has been sent. Delivery regressions are covered in
test_aggie_yt_digest.py; activation still needs a real findings/content check.

Buddy's authorization, 2026-10-12: "build the daily lane and begin sending her
updates" and, when asked where it goes: "her, not here" — the emails go to
Aggie only, never cc'd to Buddy.

What this does (and does not do)
--------------------------------
It does NOT transcribe, extract or summarize anything. The nightly yt-watch
run (com.cirrus.ytwatch 05:30, media worker on cumulus1, inference on cumulus2)
already produces yt-watch/findings/yt-watch-YYYY-MM-DD.md for every watched
channel. Aggie's three channels (lane 'aggie' in yt-watch/channels.json) ride
that same pipeline — same seen-cursor, same caption handling, same
quote-grounded extraction. This script is only the LAST MILE:

  1. fetch each aggie-lane channel's RSS and find the newest video;
  2. if that video was already sent, skip the channel;
  3. otherwise look for that video's section in the findings files (newest
     file backwards). No findings yet is NOT a failure — extraction raced or
     transiently failed, and the video simply waits for its summary; the next
     run sends it. Findings, not the clock, are the source of truth, per
     S102: a video that could not be read is reported, never skipped in
     silence.
  4. if any channel produced a section, compose one plain email and send it
     from the mailbox Aggie wrote to (cirrustask@gmail.com), addressed to her,
     no cc; the published claims are ALWAYS the presenters' own verbatim
     words ("Presenter reports: ..." — the S307 quote-grounding rewrite in
     media_pipeline.analyze), which is exactly the right shape for a client
     brief: what they said, never our endorsement, and a fixed disclaimer.
  5. if NO channel produced anything new, send nothing (an empty "nothing
     today" email would train the client to ignore the digest — same
     reasoning as pedagogy_daily's watch_promises=False note).
  6. record the outcome in job_status as 'aggieyt' so the wrap can see
     silence as well as sends.

The intro email
---------------
The FIRST live run (state file absent or no 'intro_sent') sends a single
intro note instead of a summary and records intro_sent. That note is also
where the one caveat Aggie needs lives: Take FiVe - His Glory stopped posting
to YouTube in July 2025 and streams daily on Rumble instead — we cover the
YouTube channel and offered to follow Rumble if she wants.

State is written only AFTER a confirmed send, atomically (tmp+replace), and
the run takes an exclusive flock so a manual run and the 07:10 fire can never
double-send.
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


def newest_video(channel_id):
    """Newest entry of the channel's RSS feed, or None. Read-only."""
    url = "https://www.youtube.com/feeds/videos.xml?channel_id=" + channel_id
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        xml_text = r.read().decode("utf-8", "replace")
    import xml.etree.ElementTree as ET
    NS = {"a": "http://www.w3.org/2005/Atom",
          "yt": "http://www.youtube.com/xml/schemas/2015"}
    root = ET.fromstring(xml_text)
    e = root.find("a:entry", NS)
    if e is None:
        return None
    vid = e.findtext("yt:videoId", default="", namespaces=NS)
    if not vid:
        return None
    return {
        "video_id": vid,
        "title": (e.findtext("a:title", default="", namespaces=NS) or "").strip(),
        "published": (e.findtext("a:published", default="", namespaces=NS) or "")[:10],
        "url": "https://www.youtube.com/watch?v=%s" % vid,
    }


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
             "about stocks, the market, and government matters. Everything is their words, "
             "summarized; it's information, not financial advice.", ""]
    for sec in sections:
        lines.append("**%s — %s**" % (sec["channel"], sec["title"]))
        lines.append("Published %s · %s" % (sec["published"], sec["url"]))
        lines.append("")
        if sec.get("no_transcript"):
            lines.append("_This video had no captions available, so there's nothing to "
                         "summarize today._")
        elif sec.get("extract_error"):
            lines.append("_Something went wrong on our side reading this video — it will "
                         "be summarized on a later daily brief._")
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


def pick_sections(channels, by_url, state):
    """Which of Aggie's newest videos have findings and haven't been sent."""
    sections, pending = [], []
    sent = state.get("sent", {}) or {}
    for ch in channels:
        name = ch.get("name", "?")
        new = newest_video(ch["channel_id"])
        if not new:
            log("  %s: feed empty/unreadable — skipping" % name)
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
        return work(dry_run)


def work(dry_run):
    import job_status
    channels = aggie_channels()
    if not channels:
        log("REFUSED: no aggie-lane channels in yt-watch/channels.json")
        job_status.record("aggieyt", False, "no aggie-lane channels")
        return 2
    state = load_json(STATE_PATH, {})
    by_url = parse_findings(findings_files())
    sections, pending = pick_sections(channels, by_url, state)
    for p in pending:
        log("  waiting for findings: %s" % p)
    now = datetime.now(NY)
    day_label = now.strftime("%A, %B %d")

    # The one-time intro: sent only before any summary has ever gone out, so a
    # first run with no findings yet still answers the client today.
    if not state.get(INTRO_SENT_KEY):
        intro_path = PROJECT_DIR / "yt-watch/aggie_intro_note.md"
        body = compose_intro(intro_path.read_text(errors="replace"), day_label)
        if not dry_run:
            import mailer
            creds = load_json(CREDS_PATH, {})
            ok = mailer.send(creds["outlook_email"], creds["outlook_password"],
                             AGGIE_TO, "Your daily YouTube brief is starting",
                             body, creds=creds, log=log, watch_promises=False)
            if not ok:
                job_status.record("aggieyt", False, "intro send failed")
                return 3
            state[INTRO_SENT_KEY] = now.strftime("%Y-%m-%d")
            atomic_json(STATE_PATH, state)
            job_status.record("aggieyt", True, "intro sent (%d)" % len(sections))
            log("intro email sent to %s" % AGGIE_TO)
            return 0
        log("DRY RUN: intro would be sent to %s" % AGGIE_TO)
        print("Subject: Your daily YouTube brief is starting\n\n" + body)
        return 0

    if not sections:
        log("nothing new for any aggie channel — no email")
        job_status.record("aggieyt", True,
                          "no new videos (%d still pending findings)" % len(pending))
        return 0
    body = compose_email(sections, day_label)
    subject = "Your daily YouTube brief — " + day_label
    if dry_run:
        print("DRY RUN — nothing sent. Would email to %s:\n" % AGGIE_TO)
        print("Subject: " + subject)
        print(body)
        return 0
    import mailer
    creds = load_json(CREDS_PATH, {})
    ok = mailer.send(creds["outlook_email"], creds["outlook_password"], AGGIE_TO,
                     subject, body, creds=creds, log=log, watch_promises=False)
    if not ok:
        job_status.record("aggieyt", False, "digest send failed")
        return 3
    sent = state.setdefault("sent", {})
    for sec in sections:
        vid = sec["url"].split("v=")[-1]
        sent[sec["channel"]] = {"video_id": vid, "date": now.strftime("%Y-%m-%d")}
        log("sent: %s — %s" % (sec["channel"], vid))
    atomic_json(STATE_PATH, state)
    job_status.record("aggieyt", True, "sent %d section(s)" % len(sections))
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
        ck("client disclaimer present", "not financial advice" in body2)

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

    print("  selftest %s" % ("PASSED" if not failures else "FAILED"))
    return 1 if failures else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    raise SystemExit(main())
