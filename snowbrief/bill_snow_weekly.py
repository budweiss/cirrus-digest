#!/usr/bin/env python3
"""
bill_snow_weekly.py  (S49, 2026-08-01)
===============================================================================
CIRRUS-side weekly winter snow-outlook check for Bill (William Hutchins, Knight
Property Services). Replaces the MacBook-tied Cowork task
'bill-snow-weekly-outlook' so Bill's updates no longer depend on the laptop.

It runs entirely on CIRRUS and reuses CIRRUS's own capabilities:
  * web research  — cirrus_daily.search_web + fetch_article_content
  * reasoning      — llm_providers (Claude primary; Gemini/OpenAI failover)
  * delivery       — send_bid_email.py (Gmail SMTP, cc Buddy, From: CIRRUS)

Flow (mirrors the retired task, made fail-safe):
  1. Load the current outlook baseline + climatology + drivers + rates (ref/).
  2. Web-search the CURRENT ENSO / CPC state; fetch a few readable sources.
  3. Ask Claude to decide MATERIAL CHANGE vs not and — only if material — draft a
     dated outlook refresh + the Bill email, in the established honest/probabilistic
     voice, signed as CIRRUS. Structured JSON reply so parsing is deterministic.
  4. FAIL-SAFE: if search is empty, the model errors, JSON won't parse, or the
     decision isn't unambiguously material -> treat as NO material change and send
     NOTHING. A client email only ever goes out on a clear, grounded material change.
  5. Live mode sends to Bill (cc Buddy) via send_bid_email; dry-run prints only.

Guardrails (unchanged from the task): never fabricate numbers; keep the
directional-estimate + placeholder-rate caveats; blocking (not ENSO) drives our
snow; scenario odds, not a single number; consequential/Tier-2 matters (firm bids,
contracts, money) are NEVER auto-committed — those route to Buddy.

Usage:
  python3 bill_snow_weekly.py --dry-run   # research + decide + compose, PRINT, no send
  python3 bill_snow_weekly.py             # live: email Bill ONLY on a material change
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE       = Path(__file__).resolve().parent      # ~/projects/cirrus-digest/snowbrief
DIGEST_DIR = HERE.parent                           # ~/projects/cirrus-digest
REF        = HERE / "ref"
OUT        = HERE / "out"
CREDS_PATH = DIGEST_DIR / "config/credentials.json"

sys.path.insert(0, str(DIGEST_DIR))               # for cirrus_daily + llm_providers

import node_info                                   # S56: sign as the running node
import ensemble                                     # S57: council cross-check + Claude synthesis
NODE = node_info.node_name()                       # CIRRUS (dev) / CUMULUS (beta)


def _local_hint():
    """Best-effort {host,model,num_ctx,timeout} for the ensemble's local-draft
    pass on the RUNNING node (from node_profiles.json + sources.json). Returns
    None if unavailable, in which case the draft is simply skipped. Never raises."""
    try:
        env = os.environ.get("TARGET_ENV", "dev")
        prof = json.loads((DIGEST_DIR / "config/node_profiles.json").read_text()).get(env, {})
        model = prof.get("digest_model")
        if not model:
            return None
        host = "http://localhost:11434"
        try:
            src = json.loads((DIGEST_DIR / "config/sources.json").read_text())
            host = src.get("digest", {}).get("ollama_host", host)
        except Exception:
            pass
        return {"host": host, "model": model,
                "num_ctx": prof.get("num_ctx", 8192), "timeout": 120}
    except Exception:
        return None

TO      = "whutchins@knightpropertysvs.com"
CC      = "Buddy.Weiss@outlook.com"
TODAY   = datetime.now().strftime("%Y-%m-%d")


def _read(name):
    p = REF / name
    try:
        return p.read_text()
    except Exception:
        return ""


def gather_web():
    """Return (sources_block, url_list). Empty on any failure (=> fail-safe)."""
    try:
        from cirrus_daily import search_web, fetch_article_content, is_article_url
    except Exception as e:
        print("web tools import failed:", e)
        return "", []
    queries = [
        "NOAA CPC ENSO advisory ONI Nino 3.4 latest",
        "CPC winter outlook 2026-2027 Mid-Atlantic Northeast temperature precipitation",
        "La Nina El Nino winter 2026 2027 forecast Northeast snow",
    ]
    seen, fetched = set(), []
    for q in queries:
        try:
            urls = search_web(q, max_results=6) or []
        except Exception as e:
            print(f"search_web error for '{q}':", e)
            urls = []
        for u in urls:
            if u in seen:
                continue
            seen.add(u)
            try:
                if not is_article_url(u):
                    continue
                content, _paywalled = fetch_article_content(u)
            except Exception:
                continue
            if content and len(content) > 300:
                fetched.append((u, content[:3500]))
            if len(fetched) >= 6:
                break
        if len(fetched) >= 6:
            break
    block = "\n\n".join(f"--- SOURCE {i}: {u} ---\n{c}"
                        for i, (u, c) in enumerate(fetched, 1))
    return block, [u for u, _ in fetched]


SYSTEM = (
    f"You are {NODE}, preparing a weekly winter snow-outlook check for Buddy's client "
    "Bill (Knight Property Services), corridor Baltimore-Philadelphia-South NJ-Delaware. "
    "You are careful and honest. You NEVER fabricate numbers. You anchor to the provided "
    "climatology and drivers analysis (blocking, not ENSO alone, drives our snow), give "
    "scenario odds rather than a single number, and keep the directional-estimate + "
    f"placeholder-rate caveats. Emails are signed and sent as {NODE} on behalf of Knight "
    "Property Services, and frame the numbers as our best estimate for Bill (the expert) "
    "to review and correct."
)


def build_prompt(web_block, urls):
    baseline = _read("baseline-outlook.md")
    climo    = _read("phl-climatology.md")
    drivers  = _read("Snow-Drivers-Analysis.md")
    corridor = _read("corridor-snowfall.md")
    rates    = _read("DEFAULT-SNOW-RATES.md")
    voice    = _read("voice-sample.md")
    return f"""Decide whether this week's winter outlook has MATERIALLY changed versus
the current baseline, and if so, draft the update for Bill.

MATERIAL = an ENSO category shift (e.g. strengthening/weakening/label change), a new
CPC seasonal outlook, a meaningfully changed snowfall probability, or an imminent
significant corridor storm. Essentially-the-same numbers = NOT material.

=== CURRENT BASELINE OUTLOOK (what Bill last saw) ===
{baseline}

=== PHL CLIMATOLOGY (anchor) ===
{climo}

=== SNOW DRIVERS ANALYSIS (blocking, not ENSO alone) ===
{drivers}

=== CORRIDOR SNOWFALL HISTORY ===
{corridor}

=== DEFAULT WORKING RATES (placeholders until Bill confirms) ===
{rates}

=== VOICE SAMPLE (match this warm, plain, honest tone; sign as {NODE}) ===
{voice}

=== CURRENT WEB FINDINGS (fetched just now; cite as [1],[2]… mapping to the URL list) ===
{web_block if web_block else "(NO web sources were retrieved this run.)"}

URL LIST: {json.dumps(urls)}

Return ONLY a JSON object, no prose around it, with EXACTLY these keys:
{{
  "material_change": true|false,
  "reason": "<one or two sentences on what changed or why nothing did>",
  "refresh_md": "<if material: a dated outlook-refresh markdown in the honest,
     probabilistic baseline style, anchored to the climatology/drivers, scenario
     odds not a single number; else empty string>",
  "email_subject": "<if material: 'Pennrose Snow Package — Winter 2026-27 Outlook Update ({TODAY})'; else empty>",
  "email_body": "<if material: the full Bill email body — warm, plain, honest caveats,
     framed as a request for his expert review of the rates/assumptions, signed '{NODE}';
     else empty string>"
}}
If the web findings are missing or too thin to judge a real change, set material_change=false
and say so in reason. Do NOT invent ENSO states or numbers you cannot support from the
baseline or the web findings."""


def decide():
    creds = json.load(open(CREDS_PATH))
    try:
        import llm_providers as L
    except Exception as e:
        return {"material_change": False, "error": True,
                "reason": f"llm_providers import failed: {e}"}, []
    web_block, urls = gather_web()
    if not web_block:
        # Fail-safe: no fresh evidence -> do not send. This IS a real failure of
        # the run (we couldn't gather evidence), so flag it for the status ledger.
        return {"material_change": False, "error": True,
                "reason": "no web sources retrieved this run; not sending on stale data.",
                "urls": urls}, urls
    prompt = build_prompt(web_block, urls)
    try:
        # S57: council cross-check + Claude synthesis when dev_escalation.mode=council;
        # gracefully degrades to the prior single/failover escalate() otherwise. The
        # judge preserves the JSON schema below, so parsing is unchanged either way.
        # --council forces ensemble mode for A/B dry-runs without editing stored
        # creds (Phase A). Scheduled/live runs use the box's dev_escalation.mode.
        mode_override = "council" if "--council" in sys.argv else None
        meta, text = ensemble.best_answer(SYSTEM, prompt, creds, max_tokens=8000,
                                          task="billsnow", local=_local_hint(),
                                          app_dir=str(DIGEST_DIR), mode=mode_override)
        print(f"[llm] mode={meta['mode']} members={meta['members']} judge={meta['judge']} "
              f"degraded={meta['degraded']} est=${meta.get('est_cost_usd')} "
              f"({meta['reason']}); {len(text)} chars")
    except Exception as e:
        return {"material_change": False, "error": True,
                "reason": f"LLM call failed: {e}"}, urls
    # Extract the JSON object (be tolerant of surrounding text / code fences)
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        return {"material_change": False, "error": True,
                "reason": "model reply was not parseable JSON; not sending."}, urls
    try:
        data = json.loads(m.group(0))
    except Exception as e:
        return {"material_change": False, "error": True,
                "reason": f"JSON parse failed ({e}); not sending."}, urls
    # Clean model decision — NOT an error, even if the reason prose happens to
    # contain a word like "failed"/"parse". error stays falsy on this path.
    data["urls"] = urls
    return data, urls


def _rec(dry, ok, note=""):
    if dry:
        return
    try:
        import job_status
        job_status.record("billsnow", ok, note)
    except Exception:
        pass


def _run_failed(data):
    """True only when the run itself errored (import/LLM/JSON/no-web-sources).

    We rely on the explicit data["error"] flag set by decide(), NOT on keyword-
    matching the model's free-text reason. A clean 'no material change' verdict
    is a SUCCESS even when its prose contains words like 'failed' or 'parse'
    (that false-positive is exactly what tripped the S49 status ledger)."""
    return bool(data.get("error"))


def main():
    dry = "--dry-run" in sys.argv
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] bill_snow_weekly ({'dry-run' if dry else 'live'})")

    data, urls = decide()
    material = bool(data.get("material_change")) and bool((data.get("email_body") or "").strip())
    print("material_change:", data.get("material_change"), "| sendable:", material)
    print("reason:", data.get("reason", ""))

    if dry:
        print("=" * 70)
        if material:
            print("SUBJECT:", data.get("email_subject"))
            print("-" * 70)
            print(data.get("email_body"))
            print("-" * 70)
            print("REFRESH_MD (first 1200 chars):")
            print((data.get("refresh_md") or "")[:1200])
        else:
            print("No material change -> live mode would send NOTHING.")
        print("=" * 70)
        print("sources:", *urls, sep="\n  ")
        print("DRY RUN — nothing sent.")
        return

    if not material:
        reason = data.get("reason", "")
        print(f"no material change this week — {reason}. Nothing sent.")
        _rec(dry, not _run_failed(data), reason[:120] or "no material change")
        return

    # Persist the refresh, then send to Bill (cc Buddy) via the shared SMTP sender.
    refresh_path = OUT / f"SNOW-2026-27-Outlook-Refresh-{TODAY}.md"
    refresh_path.write_text(data.get("refresh_md") or "")
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        tf.write(data.get("email_body") or "")
        bodyfile = tf.name
    env = dict(os.environ, CC_EMAIL=CC)
    r = subprocess.run([sys.executable, str(DIGEST_DIR / "send_bid_email.py"),
                        TO, data.get("email_subject") or f"Snow outlook update ({TODAY})",
                        bodyfile],
                       cwd=str(DIGEST_DIR), capture_output=True, text=True, env=env)
    print((r.stdout or "") + (r.stderr or ""))
    print("send exit:", r.returncode, "| refresh:", refresh_path.name)
    _rec(dry, r.returncode == 0, "sent material update" if r.returncode == 0 else "send failed")


if __name__ == "__main__":
    main()
