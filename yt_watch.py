#!/usr/bin/env python3
"""YT-WATCH — practitioner-video claim extractor.

Watches a small curated set of YouTube channels and asks ONE question of each new
video: does it contain a claim we could TEST on our own boxes? It is not a
summariser. A summary of a video we will never act on is noise, and noise is how
a daily job becomes unread.

Why it exists: on 2026-09-03 the two-Spark interconnect ran at 13 Gb/s against a
200 Gb/s link. Two practitioner forum threads held the answer -- a ConnectX-7 init
state cleared by a power drain, not a reboot -- and the fix took 60 seconds and
moved us to 109 Gb/s. Finding that late was expensive. See yt-watch/YT-WATCH-SPEC.md.

    python3 yt_watch.py                  # collect, extract, write
    python3 yt_watch.py --dry-run        # do everything, write NOTHING
    python3 yt_watch.py --limit 2        # cap videos processed this run
    python3 yt_watch.py report           # what the last run found
    python3 yt_watch.py --selftest       (also: selftest)

No credentials. No OAuth. No API key. The per-channel RSS feed is public.
"""

import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
CHANNELS_PATH = PROJECT_DIR / "yt-watch/channels.json"
OUT_DIR = PROJECT_DIR / "yt-watch/findings"
SEEN_PATH = PROJECT_DIR / "yt-watch/seen.json"
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id=%s"
TIMEOUT = 45
UA = "Mozilla/5.0 (compatible; cowork-yt-watch/1)"

NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}

# What a claim has to be testable AGAINST. Kept here, in code, rather than in the
# prompt string: the profile is a fact about our estate that changes when the
# estate changes, and burying it in prose makes it drift silently.
HW_PROFILE = """Our stack, which a claim must be testable against:
- 2x NVIDIA DGX Spark (GB10 Grace Blackwell), 128 GB unified memory EACH (121 GB visible)
- DGX OS 7.2.3 on Ubuntu 24.04.4; kernels 6.17.0-1029 (cumulus1) and -1032 (cumulus2)
- ConnectX-7 200GbE direct-attach RoCE between them, MEASURED at 109 Gb/s RDMA
  (~89% of the PCIe Gen5 x4 ceiling; 200 Gb/s is NOT reachable over one cable)
- Serving today: ollama. qwen3.8:27b on CIRRUS (a 64 GB M4 Max), qwen2.5:72b on cumulus1
- NO multi-node serving backend yet: vLLM / TensorRT-LLM is not installed
- NCCL collectives NOT yet validated; nccl-tests absent; NGC container is the intended route
- GPUDirect RDMA UNVERIFIED (apt perftest lacks --use_cuda, nvidia_peermem not loaded)"""

SYSTEM_HARDWARE = """You extract ACTIONABLE, TESTABLE claims from a video transcript.

%s

Return ONLY claims that are:
  (a) specific enough to RUN as a command, config change, or benchmark, and
  (b) applicable to the stack above.

"He got good results with vLLM" is NOT a claim. "vLLM multi-node needs
--distributed-executor-backend ray" IS a claim.

Returning ZERO claims is a correct and expected answer for most videos. Do NOT
manufacture relevance. Do not pad. If nothing applies, return an empty list.

Reply as JSON only: {"claims":[{"claim":"...","why_it_applies":"...","how_to_test":"..."}]}""" % HW_PROFILE

SYSTEM_NEWS = """You extract items that would CHANGE SOMETHING WE DO.

%s

We also run scheduled LLM jobs through llm_providers.py (anthropic, gemini, grok,
openai, deepseek, and ollama for local calls).

Return ONLY items that would change an action: a model worth benchmarking against
our current ones, a provider/API/pricing change affecting llm_providers.py, or a
tool worth trying on this stack.

Commentary, opinion, predictions, and "X announced Y" are NOT items. We already
know things get announced. Returning ZERO items is correct and expected for most
videos -- most AI news changes nothing about what we do. Do NOT manufacture
relevance.

Reply as JSON only: {"claims":[{"claim":"...","why_it_applies":"...","how_to_test":"..."}]}""" % HW_PROFILE


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def _get(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ── feed ─────────────────────────────────────────────────────────────────────

def parse_feed(xml_text):
    """Atom -> [{video_id, title, published, url}]. Pure; the selftest uses it."""
    out = []
    root = ET.fromstring(xml_text)
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", default="", namespaces=NS)
        title = (e.findtext("a:title", default="", namespaces=NS) or "").strip()
        pub = (e.findtext("a:published", default="", namespaces=NS) or "")[:10]
        if vid:
            out.append({"video_id": vid, "title": title, "published": pub,
                        "url": "https://www.youtube.com/watch?v=%s" % vid})
    return out


def fetch_feed(channel_id):
    return parse_feed(_get(FEED_URL % channel_id))


# ── transcript ───────────────────────────────────────────────────────────────

def parse_timedtext(xml_text):
    """YouTube timedtext XML -> plain text. Pure."""
    if not (xml_text or "").strip():
        return ""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return ""
    parts = []
    for el in root.iter():
        if el.tag.endswith("text") and el.text:
            t = re.sub(r"<[^>]+>", " ", el.text)
            t = (t.replace("&amp;", "&").replace("&quot;", '"')
                  .replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">"))
            parts.append(t.strip())
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def fetch_transcript(video_id):
    """Returns (text, reason). text=="" means NO transcript and reason says why.

    A video with no captions is NORMAL. It is recorded explicitly, never skipped
    in silence -- S64: a require_prefix email vanished with zero notice and the
    silence read as 'nothing to report'.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return "", "youtube_transcript_api not installed"
    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=["en", "en-US", "en-GB"])
        text = " ".join(getattr(s, "text", "") or "" for s in fetched)
        text = re.sub(r"\s+", " ", text).strip()
        return (text, "") if text else ("", "empty transcript")
    except Exception as e:
        return "", "%s: %s" % (type(e).__name__, str(e)[:120])


# ── extraction ───────────────────────────────────────────────────────────────

def parse_claims(reply):
    """Model reply -> claim list. Tolerant of code fences and prose padding.

    Returns [] for anything unparseable. That is deliberate: an unreadable reply
    must mean NO CLAIMS, never a crash and never a fabricated one.
    """
    if not (reply or "").strip():
        return []
    m = re.search(r"\{.*\}", reply, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    claims = data.get("claims")
    if not isinstance(claims, list):
        return []
    out = []
    for c in claims:
        if isinstance(c, dict) and (c.get("claim") or "").strip():
            out.append({"claim": str(c.get("claim", "")).strip(),
                        "why_it_applies": str(c.get("why_it_applies", "")).strip(),
                        "how_to_test": str(c.get("how_to_test", "")).strip()})
    return out


def system_for(lane):
    return SYSTEM_NEWS if lane == "news" else SYSTEM_HARDWARE


def extract(video, transcript, lane, caller=None):
    """caller(system, user) -> reply text. Injected so the selftest needs no network."""
    if caller is None:
        import llm_providers
        import cirrus_config
        creds = cirrus_config.load_credentials()
        caller = lambda s, u: llm_providers.escalate(s, u, creds, max_tokens=4000)
    user = "Video: %s\nChannel lane: %s\n\nTranscript:\n%s" % (
        video.get("title", ""), lane, transcript[:60000])
    return parse_claims(caller(system_for(lane), user))


# ── ledger + output ──────────────────────────────────────────────────────────

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def load_channels(path=None):
    d = load_json(path or CHANNELS_PATH, {})
    return [c for c in d.get("channels", []) if c.get("channel_id")]


def render(results, day):
    lines = ["# YT-WATCH findings — %s" % day, ""]
    total = sum(len(r["claims"]) for r in results)
    lines += ["**%d video(s) processed, %d claim(s).**" % (len(results), total), ""]
    if results and total == 0:
        lines += ["_No actionable claims. This is a normal and successful result —"
                  " see rule 1 in YT-WATCH-SPEC.md._", ""]
    for r in results:
        lines.append("## %s — %s" % (r["channel"], r["title"]))
        lines.append("%s · `%s` · %s" % (r["published"], r["lane"], r["url"]))
        if r.get("no_transcript"):
            lines += ["", "> **no transcript** — %s" % r["no_transcript"], ""]
            continue
        if not r["claims"]:
            lines += ["", "**actionable: none**", ""]
            continue
        lines.append("")
        for c in r["claims"]:
            lines.append("- **%s**" % c["claim"])
            if c.get("why_it_applies"):
                lines.append("  - applies: %s" % c["why_it_applies"])
            if c.get("how_to_test"):
                lines.append("  - test: %s" % c["how_to_test"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run(dry_run=False, limit=None, channels=None, feed_fn=None,
        transcript_fn=None, extract_fn=None, seen_path=None, out_dir=None):
    """Injectable throughout so the selftest touches no network and no live file (T32)."""
    channels = channels if channels is not None else load_channels()
    feed_fn = feed_fn or fetch_feed
    transcript_fn = transcript_fn or fetch_transcript
    extract_fn = extract_fn or extract
    seen_path = Path(seen_path or SEEN_PATH)
    out_dir = Path(out_dir or OUT_DIR)

    seen = set(load_json(seen_path, {}).get("video_ids", []))
    results, errors, processed = [], [], 0

    for ch in channels:
        try:
            vids = feed_fn(ch["channel_id"])
        except Exception as e:
            errors.append("%s: %s" % (ch.get("name", "?"), type(e).__name__))
            continue
        for v in vids:
            if v["video_id"] in seen:
                continue
            if limit is not None and processed >= limit:
                break
            processed += 1
            text, reason = transcript_fn(v["video_id"])
            rec = {"channel": ch.get("name", "?"), "lane": ch.get("lane", "hardware"),
                   "claims": [], "no_transcript": "", **v}
            if not text:
                rec["no_transcript"] = reason or "unknown"
            else:
                try:
                    rec["claims"] = extract_fn(v, text, rec["lane"])
                except Exception as e:
                    errors.append("%s extract: %s" % (v["video_id"], type(e).__name__))
                    rec["no_transcript"] = "extract failed: %s" % type(e).__name__
            results.append(rec)
            seen.add(v["video_id"])

    day = datetime.now().strftime("%Y-%m-%d")
    body = render(results, day)
    if not dry_run and results:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / ("yt-watch-%s.md" % day)).write_text(body)
        seen_path.parent.mkdir(parents=True, exist_ok=True)
        seen_path.write_text(json.dumps({"video_ids": sorted(seen)}, indent=1))
    return {"processed": len(results), "claims": sum(len(r["claims"]) for r in results),
            "no_transcript": sum(1 for r in results if r["no_transcript"]),
            "errors": errors, "body": body}


def report():
    files = sorted(OUT_DIR.glob("yt-watch-*.md")) if OUT_DIR.exists() else []
    if not files:
        print("no findings yet")
        return 0
    print(files[-1].read_text())
    return 0


# ── selftest ─────────────────────────────────────────────────────────────────

FEED_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
 <title>Fixture Channel</title>
 <entry><yt:videoId>vid111</yt:videoId><title>DGX Spark cluster</title>
  <published>2026-08-29T12:00:00+00:00</published></entry>
 <entry><yt:videoId>vid222</yt:videoId><title>Second video</title>
  <published>2026-08-28T12:00:00+00:00</published></entry>
</feed>"""


def selftest():
    """Offline: no network, no live channels file, no live ledger, no live out dir (T32)."""
    import tempfile
    ok = fail = 0

    def ck(name, cond):
        nonlocal ok, fail
        ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
        print("  [%s] %s" % ("OK " if cond else "FAIL", name))

    # ── feed parsing
    vids = parse_feed(FEED_FIXTURE)
    ck("feed: parses both entries", len(vids) == 2)
    ck("feed: video id extracted", vids[0]["video_id"] == "vid111")
    ck("feed: title extracted", vids[0]["title"] == "DGX Spark cluster")
    ck("feed: published truncated to a date", vids[0]["published"] == "2026-08-29")
    ck("feed: url built from the id", vids[0]["url"].endswith("watch?v=vid111"))

    # ── transcript parsing
    tt = '<transcript><text start="0">hello &amp;amp; welcome</text><text start="1">to the lab</text></transcript>'
    ck("timedtext: joins cues and unescapes", parse_timedtext(tt) == "hello & welcome to the lab")
    ck("timedtext: empty input is empty, not a crash", parse_timedtext("") == "")
    ck("timedtext: malformed XML is empty, not a crash", parse_timedtext("<not xml") == "")

    # ── claim parsing. The rule the whole project rests on is that ZERO is a
    #    legal answer, so these assert the empty path as hard as the full one.
    good = '```json\n{"claims":[{"claim":"use --distributed-executor-backend ray","why_it_applies":"we have no multi-node backend","how_to_test":"run it"}]}\n```'
    ck("claims: parses through a code fence", len(parse_claims(good)) == 1)
    ck("claims: keeps the three fields",
       parse_claims(good)[0]["how_to_test"] == "run it")
    ck("claims: EMPTY list stays empty", parse_claims('{"claims":[]}') == [])
    ck("claims: unparseable reply -> [] not a crash", parse_claims("I could not find anything useful.") == [])
    ck("claims: empty reply -> []", parse_claims("") == [])
    ck("claims: wrong shape -> []", parse_claims('{"claims":"lots"}') == [])
    ck("claims: a claim with no text is dropped", parse_claims('{"claims":[{"claim":"  "}]}') == [])

    # ── lane routing: the two lanes must NOT share a prompt
    ck("lane: news gets the news system prompt", system_for("news") is SYSTEM_NEWS)
    ck("lane: hardware gets the hardware system prompt", system_for("hardware") is SYSTEM_HARDWARE)
    ck("lane: an unknown lane falls back to hardware", system_for("wat") is SYSTEM_HARDWARE)
    ck("lane: both prompts carry the hardware profile",
       "109 Gb/s" in SYSTEM_HARDWARE and "109 Gb/s" in SYSTEM_NEWS)
    ck("lane: both prompts say zero is correct",
       "ZERO" in SYSTEM_HARDWARE and "ZERO" in SYSTEM_NEWS)

    # ── render
    empty_body = render([{"channel": "C", "title": "T", "published": "2026-01-01",
                          "lane": "news", "url": "u", "claims": [], "no_transcript": ""}], "2026-01-01")
    ck("render: no claims renders 'actionable: none'", "**actionable: none**" in empty_body)
    ck("render: an all-empty run says empty is SUCCESS", "normal and successful" in empty_body)
    nt_body = render([{"channel": "C", "title": "T", "published": "2026-01-01",
                       "lane": "news", "url": "u", "claims": [], "no_transcript": "no captions"}], "2026-01-01")
    ck("render: a missing transcript is REPORTED, not silently dropped",
       "no transcript" in nt_body and "no captions" in nt_body)

    # ── run(), fully injected: no network, no live paths
    chans = [{"name": "Fix", "channel_id": "CID", "lane": "hardware"}]
    feed = lambda cid: parse_feed(FEED_FIXTURE)
    with tempfile.TemporaryDirectory() as td:
        sp, od = Path(td) / "seen.json", Path(td) / "out"
        r1 = run(channels=chans, feed_fn=feed,
                 transcript_fn=lambda v: ("a transcript", ""),
                 extract_fn=lambda v, t, l: [{"claim": "c", "why_it_applies": "", "how_to_test": ""}],
                 seen_path=sp, out_dir=od)
        ck("run: processes both new videos", r1["processed"] == 2)
        ck("run: counts claims", r1["claims"] == 2)
        ck("run: wrote a findings file", any(od.glob("yt-watch-*.md")))
        ck("run: wrote the seen ledger", sp.exists())

        # dedupe -- the second run must do NOTHING
        r2 = run(channels=chans, feed_fn=feed,
                 transcript_fn=lambda v: ("a transcript", ""),
                 extract_fn=lambda v, t, l: [{"claim": "c", "why_it_applies": "", "how_to_test": ""}],
                 seen_path=sp, out_dir=od)
        ck("run: SECOND run re-processes nothing (dedupe holds)", r2["processed"] == 0)

    # a video with no transcript is recorded, and never reaches the model
    with tempfile.TemporaryDirectory() as td:
        called = []
        r3 = run(channels=chans, feed_fn=feed,
                 transcript_fn=lambda v: ("", "no captions"),
                 extract_fn=lambda v, t, l: called.append(v) or [],
                 seen_path=Path(td) / "s.json", out_dir=Path(td) / "o")
        ck("run: no-transcript videos are COUNTED", r3["no_transcript"] == 2)
        ck("run: no-transcript never calls the model", called == [])

    # an extractor that raises must not lose the whole run
    with tempfile.TemporaryDirectory() as td:
        def boom(v, t, l):
            raise RuntimeError("model down")
        r4 = run(channels=chans, feed_fn=feed, transcript_fn=lambda v: ("t", ""),
                 extract_fn=boom, seen_path=Path(td) / "s.json", out_dir=Path(td) / "o")
        ck("run: an extractor exception is recorded, not fatal",
           r4["processed"] == 2 and len(r4["errors"]) == 2)

    # a dead feed must not kill the other channels
    with tempfile.TemporaryDirectory() as td:
        def half(cid):
            if cid == "BAD":
                raise OSError("dns")
            return parse_feed(FEED_FIXTURE)
        r5 = run(channels=[{"name": "Bad", "channel_id": "BAD", "lane": "news"}] + chans,
                 feed_fn=half, transcript_fn=lambda v: ("t", ""),
                 extract_fn=lambda v, t, l: [], seen_path=Path(td) / "s.json",
                 out_dir=Path(td) / "o")
        ck("run: one dead feed does not stop the others", r5["processed"] == 2)
        ck("run: the dead feed is reported as an error", len(r5["errors"]) == 1)

    # --dry-run writes NOTHING
    with tempfile.TemporaryDirectory() as td:
        sp, od = Path(td) / "s.json", Path(td) / "o"
        run(channels=chans, feed_fn=feed, transcript_fn=lambda v: ("t", ""),
            extract_fn=lambda v, t, l: [], seen_path=sp, out_dir=od, dry_run=True)
        ck("run: --dry-run writes no findings file", not od.exists())
        ck("run: --dry-run does not advance the ledger", not sp.exists())

    # the shipped channel list must be real and verified
    # These three were written as bare all(...) checks and the first run proved
    # why that is not good enough: with the file missing, load_channels() returned
    # [], all([]) is True, and BOTH id and lane checks reported OK against zero
    # entries. Only the length check failed. A check that passes because it
    # inspected nothing is the failure mode this whole repo is built against, so
    # each one now requires a non-empty list as part of the assertion.
    live = load_channels()
    ck("channels: the shipped list loads and is non-empty", len(live) >= 1)
    ck("channels: every entry has a UC-shaped id (and there IS at least one)",
       bool(live) and all(re.fullmatch(r"UC[A-Za-z0-9_-]{22}", c["channel_id"]) for c in live))
    ck("channels: every entry declares a known lane (and there IS at least one)",
       bool(live) and all(c.get("lane") in ("hardware", "news") for c in live))
    ck("channels: all four Buddy named are present", len(live) == 4)

    print("\n%d passed, %d failed" % (ok, fail))
    return fail == 0


def main():
    args = sys.argv[1:]
    if "--selftest" in args or "selftest" in args:
        return 0 if selftest() else 1
    if "report" in args:
        return report()
    limit = None
    if "--limit" in args:
        try:
            limit = int(args[args.index("--limit") + 1])
        except Exception:
            pass
    stats = run(dry_run="--dry-run" in args, limit=limit)
    log("processed %d, claims %d, no-transcript %d%s"
        % (stats["processed"], stats["claims"], stats["no_transcript"],
           (", %d error(s)" % len(stats["errors"])) if stats["errors"] else ""))
    if "--dry-run" not in args:
        try:
            import job_status
            # A run that processed nothing because there were no NEW videos is
            # healthy. A run where every feed errored is not.
            healthy = len(stats["errors"]) == 0 or stats["processed"] > 0
            job_status.record("ytwatch", healthy,
                              "%d video(s), %d claim(s)" % (stats["processed"], stats["claims"]))
        except Exception as e:
            print("job_status.record failed: %s" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
