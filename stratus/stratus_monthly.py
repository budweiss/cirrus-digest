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


def build_prompt(web_block, urls):
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


def synthesize():
    creds = json.load(open(CREDS_PATH))
    try:
        import llm_providers as L
    except Exception as e:
        return None, [], f"llm_providers import failed: {e}"
    web_block, urls = gather_web()
    try:
        provider, text = L.escalate(SYSTEM, build_prompt(web_block, urls), creds, max_tokens=4000)
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


def main():
    dry = "--dry-run" in sys.argv
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] stratus_monthly ({'dry-run' if dry else 'live'})")
    entry, urls, err = synthesize()
    if err:
        print("ERROR:", err, "— nothing written or sent.")
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


if __name__ == "__main__":
    main()
