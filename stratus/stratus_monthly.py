#!/usr/bin/env python3
"""
stratus_monthly.py  (S49, 2026-08-01)
===============================================================================
CIRRUS-side monthly STRATUS research refresh. Replaces the MacBook-tied Cowork
task 'stratus-monthly-review'. Runs on CIRRUS and reuses the scheduled-agent core:

  * web research — cirrus_daily.search_web + fetch_article_content
  * synthesis    — llm_providers (Claude primary; Gemini/OpenAI failover)
  * delivery     — emails Buddy the new entry (send_digest SMTP), and keeps the
                   research log ON CIRRUS (docs/STRATUS-Research-Log.md), pushed
                   to cirrus-repo so it survives + is versioned. MacBook not involved.

Flow:
  1. Load the current log (recommendation snapshot + watch list) + sizing doc.
  2. Web-search the watch-list topics (AI hardware + local-LLM techniques).
  3. Claude writes a concise dated entry (3-8 bullets w/ source links + a clear
     "Recommendation: unchanged" or "Recommendation change suggested: …").
  4. Insert it above the append marker in the log, commit+push (best-effort), and
     email Buddy the entry.

Usage:
  python3 stratus_monthly.py --dry-run   # research + write entry to stdout, no file/commit/email
  python3 stratus_monthly.py             # append to log, push, email Buddy
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE       = Path(__file__).resolve().parent      # ~/projects/cirrus-digest/stratus
DIGEST_DIR = HERE.parent                           # ~/projects/cirrus-digest
LOG        = DIGEST_DIR / "docs/STRATUS-Research-Log.md"
SIZING     = DIGEST_DIR / "docs/STRATUS-Production-Sizing-and-Architecture.md"
CREDS_PATH = DIGEST_DIR / "config/credentials.json"
MARKER     = "<!-- New monthly entries appended above this line by stratus-monthly-review -->"
LEARNED    = DIGEST_DIR / "learn-watch/claims.jsonl"   # S310: learn_watch's verified article quotes
LEARN_AREAS = ("STRATUS (production sizing)", "Hardware")
LEARN_DAYS, LEARN_MAX = 35, 30
LEARN_CHARS = 14000     # hard budget for the lessons block, whatever LEARN_MAX says
# S313: the route admits prompts up to its reviewed max_user_bytes. The worst
# case -- 8 web sources x 3,000 chars, the full lessons budget, 6,000 chars of
# log context, 3,000 of sizing -- must stay under this envelope; the selftest
# builds that worst case and fails if it does not, so no cap can be raised
# without re-qualifying the route.
PROMPT_ENVELOPE_BYTES = 60000

sys.path.insert(0, str(DIGEST_DIR))               # cirrus_daily + llm_providers + send_digest

TODAY = datetime.now().strftime("%Y-%m-%d")

QUERIES = [
    "NVIDIA DGX Spark successor DGX Station RTX Pro Blackwell 2026 unified memory bandwidth price",
    "H200 B200 next-gen datacenter GPU memory bandwidth 2026",
    "Apple Mac Studio M5 unified memory AMD Strix Halo local LLM inference 2026",
    "local LLM small models 32B closing gap quantization fine-tuning distillation serving throughput 2026",
]

SYSTEM = (
    "You are CIRRUS, doing a monthly research refresh to keep Buddy's STRATUS "
    "production-hardware and local-LLM plan current. Be concise, specific, and honest; "
    "name models/cards, memory capacity, memory bandwidth, and $/GB (not just TOPS). "
    "Always say prices are to be verified before purchase. Do not pad or invent."
)


def gather_web():
    try:
        from cirrus_daily import search_web, fetch_article_content, is_article_url
    except Exception as e:
        print("web tools import failed:", e)
        return "", []
    seen, fetched = set(), []
    for q in QUERIES:
        try:
            urls = search_web(q, max_results=6) or []
        except Exception as e:
            print(f"search_web error '{q}':", e)
            urls = []
        for u in urls:
            if u in seen:
                continue
            seen.add(u)
            try:
                if not is_article_url(u):
                    continue
                content, _ = fetch_article_content(u)
            except Exception:
                continue
            if content and len(content) > 300:
                fetched.append((u, content[:3000]))
            if len(fetched) >= 8:
                break
        if len(fetched) >= 8:
            break
    block = "\n\n".join(f"--- SOURCE {i}: {u} ---\n{c}" for i, (u, c) in enumerate(fetched, 1))
    return block, [u for u, _ in fetched]


def learned_block(path=LEARNED, today=None, days=LEARN_DAYS, cap=LEARN_MAX):
    """S310, Buddy: "feed stratus". The month's learn_watch lessons tagged
    STRATUS or Hardware -- quotes already verified word for word against the
    article -- newest first. Returns (block, urls); ("", []) when there are none."""
    today = today or datetime.now()
    rows = []
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return "", []
    for line in lines:
        try:
            r = json.loads(line)
            if not set(r.get("areas", [])) & set(LEARN_AREAS):
                continue
            if (today - datetime.strptime(r["date"], "%Y-%m-%d")).days > days:
                continue
            rows.append(r)
        except (ValueError, KeyError):
            continue
    rows = sorted(rows, key=lambda r: r["date"], reverse=True)[:cap]
    lines, used = [], 0
    for r in rows:
        line = '- "%s" -- %s (%s, %s) %s' % (r["quote"][:400], r["title"][:90],
                                              r["source"][:40], r["date"], r["url"][:200])
        if used + len(line) + 1 > LEARN_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
    kept = rows[:len(lines)]
    return "\n".join(lines), sorted({r["url"][:200] for r in kept})


def build_prompt(web_block, urls, learned=""):
    log_txt = LOG.read_text() if LOG.exists() else ""
    # keep the snapshot + watch list (everything before the log entries) as context
    context = log_txt.split("## Log entries")[0][:6000]
    sizing = (SIZING.read_text()[:3000] if SIZING.exists() else "")
    return f"""Write THIS MONTH'S research-log entry for STRATUS.

=== CURRENT LOG (recommendation snapshot + watch list) ===
{context}

=== CURRENT SIZING/ARCHITECTURE (excerpt) ===
{sizing}

=== WEB FINDINGS (fetched just now; cite as markdown links to these URLs) ===
{web_block if web_block else "(NO web sources retrieved this run — say so and keep the recommendation unchanged.)"}

=== FROM ARTICLES WE READ THIS MONTH (learn_watch; quotes verified word for word, claims are the authors', not tested by us) ===
{learned if learned else "(none tagged STRATUS or Hardware this month)"}

URL LIST: {json.dumps(urls)}

Write ONLY the markdown entry (no preamble), starting with a header line exactly:
### {TODAY}
Then 3-8 concise bullets of concrete findings, each with a markdown source link where
possible (AI hardware: DGX Spark successors, RTX Pro/Blackwell, H200/B200, Mac Studio
M-series, AMD Strix Halo — track memory capacity, bandwidth, $/GB; clustering/interconnect;
local-LLM techniques: stronger small models, quantization, fine-tuning beyond QLoRA,
distillation, serving throughput; and whether the local-vs-frontier gap is shrinking).
End with a final line that is EITHER "Recommendation: unchanged." OR
"Recommendation change suggested: <what and why>." Prices must say "verify before purchase."
Do not invent numbers you cannot support from the findings."""


def fitted_prompt(web_block, urls, learned, limit=PROMPT_ENVELOPE_BYTES):
    """build_prompt, trimmed until it fits the route's reviewed max_user_bytes.
    S313: the worst case of every capped input (non-Latin pages are 2-3 bytes a
    character) measured 82,531 bytes against a 60,000 envelope; admission would
    refuse the whole month's run. Oldest lessons go first, then the last web
    source; what was dropped is said in the prompt, never silently."""
    lessons = learned.split("\n") if learned else []
    sources = web_block.split("\n\n--- SOURCE ") if web_block else []
    dropped_l = dropped_s = 0
    while True:
        web = "\n\n--- SOURCE ".join(sources)
        note = ""
        if dropped_l or dropped_s:
            note = ("\n(Trimmed to fit the reviewed input size: %d older lesson(s) and %d web "
                    "source(s) omitted.)" % (dropped_l, dropped_s))
        body = web + "\n" + "\n".join(lessons)
        # only URLs whose text is still in the prompt may be cited (source fidelity)
        prompt = build_prompt(web, [u for u in urls if u in body], "\n".join(lessons) + note)
        if len(prompt.encode()) <= limit or (not lessons and len(sources) <= 1):
            return prompt
        if lessons:
            lessons.pop(); dropped_l += 1
        else:
            sources.pop(); dropped_s += 1


def synthesize():
    creds = json.load(open(CREDS_PATH))
    try:
        import llm_providers as L
    except Exception as e:
        return None, [], f"llm_providers import failed: {e}"
    web_block, urls = gather_web()
    learned, learned_urls = learned_block()
    urls = urls + [u for u in learned_urls if u not in urls]
    try:
        provider, text = L.escalate(SYSTEM, fitted_prompt(web_block, urls, learned), creds, max_tokens=4000, task='stratus:monthly')
        print(f"[llm] provider={provider}, {len(text)} chars; sources={len(urls)}")
    except Exception as e:
        return None, urls, f"LLM call failed: {e}"
    entry = text.strip()
    if not entry.startswith("### "):
        entry = f"### {TODAY}\n{entry}"
    return entry, urls, None


def git_push_log():
    def run(*a):
        return subprocess.run(["git", "-C", str(DIGEST_DIR), *a],
                              capture_output=True, text=True)
    out = []
    for a in (["add", "docs/STRATUS-Research-Log.md"],
              ["commit", "-m", f"stratus monthly research {TODAY}"],
              ["pull", "--rebase"],
              ["push", "origin", "main"]):
        r = run(*a)
        out.append(f"$ git {' '.join(a)} -> {r.returncode}\n{(r.stdout + r.stderr).strip()[:300]}")
    return "\n".join(out)


def _rec(dry, ok, note=""):
    if dry:
        return
    try:
        import job_status
        job_status.record("stratusreview", ok, note)
    except Exception:
        pass


def selftest():
    """Offline: learned_block only (T32: a temp file, never the live log)."""
    import tempfile
    ok = True

    def ck(name, cond):
        nonlocal ok
        print("  [%s] %s" % ("OK " if cond else "FAIL", name))
        ok = ok and cond
    now = datetime(2026, 10, 1, 5, 15)
    rows = [{"date": "2026-09-26", "title": "Two-node Spark", "url": "u1", "source": "s",
             "quote": "q1", "areas": ["Hardware"]},
            {"date": "2026-09-27", "title": "Rack notes", "url": "u2", "source": "s",
             "quote": "q2", "areas": ["STRATUS (production sizing)"]},
            {"date": "2026-09-27", "title": "Skill files", "url": "u3", "source": "s",
             "quote": "q3", "areas": ["Agent harness & instruction files"]},
            {"date": "2026-07-01", "title": "Old", "url": "u4", "source": "s",
             "quote": "q4", "areas": ["Hardware"]}]
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "claims.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
        block, urls = learned_block(p, today=now)
        ck("learned: STRATUS and Hardware rows only", urls == ["u1", "u2"])
        ck("learned: newest first", block.index("Rack notes") < block.index("Two-node Spark"))
        ck("learned: older than the window is dropped", "Old" not in block)
        ck("learned: a bad line is skipped, not fatal", len(block.splitlines()) == 2)
        ck("learned: a missing file is empty, not an error",
           learned_block(Path(td) / "nope.jsonl", today=now) == ("", []))
    ck("prompt: carries the learned block", "q2" in build_prompt("", [], "- \"q2\""))
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "claims.jsonl"
        big = [{"date": "2026-09-30", "title": "T" * 200, "url": "https://example.com/" + "u" * 300,
                "source": "S" * 80, "quote": "é" * 900, "areas": ["Hardware"]} for _ in range(80)]
        p.write_text("\n".join(json.dumps(r) for r in big))
        block, _ = learned_block(p, today=now)
        ck("learned: block never exceeds LEARN_CHARS", 0 < len(block) <= LEARN_CHARS)
        # the reviewed size envelope: worst case of every capped input together
        global LOG, SIZING
        saved = LOG, SIZING
        (Path(td) / "log.md").write_text("L" * 20000 + "## Log entries")
        (Path(td) / "sizing.md").write_text("Z" * 20000)
        LOG, SIZING = Path(td) / "log.md", Path(td) / "sizing.md"
        try:
            web = "\n\n".join(f"--- SOURCE {i}: https://example.com/{'w' * 180} ---\n" + "é" * 3000
                               for i in range(1, 9))
            urls = ["https://example.com/" + "w" * 180] * 8 + _
            raw = build_prompt(web, urls, block)
            worst = fitted_prompt(web, urls, block)
        finally:
            LOG, SIZING = saved
        ck("envelope: the untrimmed worst case really is over (%d bytes) -- so the trim is exercised"
           % len(raw.encode()), len(raw.encode()) > PROMPT_ENVELOPE_BYTES)
        ck("envelope: the fitted worst case fits the reviewed max_user_bytes (%d <= %d)"
           % (len(worst.encode()), PROMPT_ENVELOPE_BYTES), len(worst.encode()) <= PROMPT_ENVELOPE_BYTES)
        ck("envelope: the trim is announced, not silent", "Trimmed to fit" in worst)
        small = fitted_prompt("--- SOURCE 1: https://a.example ---\ntext", ["https://a.example", "https://gone.example"],
                              "- \"q\" -- t (s, d) https://l.example", limit=10 ** 6)
        ck("envelope: a URL whose text is not in the prompt is not offered for citation",
           "https://gone.example" not in small and "https://l.example" in small)
        ck("envelope: web sources survive before lessons are all gone, header intact",
           "### " in worst and "SOURCE 1:" in worst)
    print("selftest:", "PASS" if ok else "FAIL")
    return ok


def main():
    # T107: an argument main() does not know must never fall through to the LIVE
    # monthly run (research + log commit + email). selftest is handled first.
    if "selftest" in sys.argv[1:] or "--selftest" in sys.argv[1:]:
        sys.exit(0 if selftest() else 1)
    dry = "--dry-run" in sys.argv
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] stratus_monthly ({'dry-run' if dry else 'live'})")
    entry, urls, err = synthesize()
    if err:
        print("ERROR:", err, "— nothing written or sent.")
        _rec(dry, False, err[:120])
        return
    print("=" * 70)
    print(entry)
    print("=" * 70)

    if dry:
        print("sources:", *urls, sep="\n  ")
        print("DRY RUN — nothing written, committed, or emailed.")
        return

    # Insert above the marker in the log (newest first, under "Log entries").
    if LOG.exists() and MARKER in LOG.read_text():
        txt = LOG.read_text()
        txt = txt.replace(MARKER, entry.rstrip() + "\n\n" + MARKER, 1)
        LOG.write_text(txt)
        print("appended entry to", LOG)
        print(git_push_log())
    else:
        print("WARN: log or marker missing; skipping file update (still emailing).")

    # Email Buddy the entry.
    try:
        from send_digest import send_email
        send_email(f"🛰 STRATUS monthly research — {TODAY}",
                   entry + "\n\n---\n*Sources:*\n" + "\n".join(f"- {u}" for u in urls)
                   + "\n\n*Composed by CIRRUS (stratus_monthly.py) — no MacBook required.*")
        print("emailed Buddy.")
    except Exception as e:
        print("email error:", e)
    _rec(dry, True, "logged + emailed")


if __name__ == "__main__":
    main()
