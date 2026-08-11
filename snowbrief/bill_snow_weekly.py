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
from datetime import datetime, date
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

# ── Seasonal cadence (Buddy's rule, S59) ──────────────────────────────────────
# Keep running WEEKLY and compare to the LAST run. Before the season opens,
# email Bill ONLY on a BIG change; otherwise stay silent and let the regular
# report wait until the season starts (Bill asked to be updated in late Oct).
STATE        = OUT / "last_run_state.json"   # week-over-week memory
SEASON_OPEN  = (10, 20)   # (month, day) — season opens ~late October
SEASON_CLOSE = (3, 15)    # ...runs through mid-March

def _season_phase(today=None):
    """'in' during Oct 20 → Mar 15, else 'off'."""
    today = today or date.today()
    md = (today.month, today.day)
    return "in" if (md >= SEASON_OPEN or md <= SEASON_CLOSE) else "off"

def _season_year(today=None):
    """Identifier for the winter season an update belongs to (the Oct year)."""
    today = today or date.today()
    return today.year if today.month >= 7 else today.year - 1

def _load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}

def _save_state(snapshot, change_level, sent, opener_done_year=None):
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        prev = _load_state()
        STATE.write_text(json.dumps({
            "last_run": TODAY,
            "change_level": change_level,
            "sent": bool(sent),
            "snapshot": snapshot or {},
            "season_opener_year": opener_done_year
                if opener_done_year is not None
                else prev.get("season_opener_year"),
        }, indent=2))
    except Exception as e:
        print("state save failed:", e)


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


def build_prompt(web_block, urls, prev_snapshot=None, phase="off", opener=False):
    baseline = _read("baseline-outlook.md")
    climo    = _read("phl-climatology.md")
    drivers  = _read("Snow-Drivers-Analysis.md")
    corridor = _read("corridor-snowfall.md")
    rates    = _read("DEFAULT-SNOW-RATES.md")
    voice    = _read("voice-sample.md")
    prev_block = (json.dumps(prev_snapshot, indent=2)
                  if prev_snapshot else "(no prior run on record — this is the first compared run)")
    if opener:
        cadence = ("SEASON OPENER: the winter season has begun and Bill asked to be updated in late "
                   "October. Produce a full current-outlook report email for Bill REGARDLESS of whether "
                   "anything changed — this is his scheduled season-opening update. Set change_level to "
                   "your honest read ('none'/'notable'/'big') but ALWAYS fill email_subject/email_body.")
    elif phase == "in":
        cadence = ("IN-SEASON: draft the email if there is a 'notable' or 'big' change vs the last run; "
                   "otherwise leave email fields empty.")
    else:
        cadence = ("OFF-SEASON (before late October): Bill only wants an early heads-up for something BIG. "
                   "Draft the email ONLY if change_level is 'big'; for 'notable' or 'none', leave the email "
                   "fields empty (we stay silent and wait for the season). Always still report change_level.")
    return f"""Decide how much this week's winter outlook has changed versus (a) the current
baseline AND (b) our LAST RUN's snapshot, classify the size of the change, and — per the
cadence rule below — draft the update for Bill only when it should actually go out.

CLASSIFY change_level as one of:
  • "big"     — an ENSO category shift (label change / clear strengthening or weakening), a
                newly issued CPC seasonal outlook, a meaningfully changed snowfall probability,
                or an imminent significant corridor storm. The kind of thing worth interrupting
                Bill's summer for.
  • "notable" — a real but modest update (e.g. an advisory reworded, probabilities nudged) that
                is worth sending during the season but NOT worth an off-season interruption.
  • "none"    — essentially the same picture as the last run / baseline.

CADENCE RULE FOR THIS RUN: {cadence}

=== LAST RUN SNAPSHOT (compare against this for week-over-week change) ===
{prev_block}

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
  "change_level": "big" | "notable" | "none",
  "material_change": true|false,   // true iff change_level is "big" or "notable"
  "reason": "<one or two sentences on what changed vs the last run, or why nothing did>",
  "state_snapshot": {{"enso": "<current ENSO state/label>", "cpc_outlook": "<latest CPC seasonal read or 'none'>",
     "snow_prob": "<current corridor snowfall probability read>", "notes": "<key indicators to compare next week>"}},
  "refresh_md": "<if drafting: a dated outlook-refresh markdown in the honest,
     probabilistic baseline style, anchored to the climatology/drivers, scenario
     odds not a single number; else empty string>",
  "email_subject": "<if drafting: 'Pennrose Snow Package — Winter 2026-27 Outlook Update ({TODAY})'; else empty>",
  "email_body": "<if drafting per the cadence rule: the full Bill email body — warm, plain, honest caveats,
     framed as a request for his expert review of the rates/assumptions, signed '{NODE}';
     else empty string>"
}}
ALWAYS fill state_snapshot (it is stored for next week's comparison even when nothing is sent).
If the web findings are missing or too thin to judge a real change, set change_level="none",
material_change=false, and say so in reason. Do NOT invent ENSO states or numbers you cannot
support from the baseline or the web findings."""


def decide(phase="off", opener=False):
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
    prompt = build_prompt(web_block, urls, _load_state().get("snapshot"), phase, opener)
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
    force_opener = "--opener" in sys.argv       # manual season-opener test
    OUT.mkdir(parents=True, exist_ok=True)

    phase = _season_phase()
    syear = _season_year()
    prev  = _load_state()
    opener = force_opener or (phase == "in" and prev.get("season_opener_year") != syear)

    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] bill_snow_weekly "
          f"({'dry-run' if dry else 'live'}) phase={phase} season={syear} opener={opener}")

    data, urls = decide(phase, opener)
    level = (data.get("change_level")
             or ("big" if data.get("material_change") else "none")).lower()
    snapshot  = data.get("state_snapshot") or {}
    has_email = bool((data.get("email_body") or "").strip())

    # Buddy's cadence rule (S59): OFF-season => send only on 'big'; IN-season =>
    # 'notable' or 'big'; the season opener always sends Bill his late-Oct update.
    if opener:
        should = has_email
    elif phase == "in":
        should = has_email and level in ("notable", "big")
    else:  # off-season — hold everything but a BIG change
        should = has_email and level == "big"

    print(f"change_level={level} | phase={phase} | opener={opener} | sendable={should}")
    print("reason:", data.get("reason", ""))

    if dry:
        print("=" * 70)
        if should:
            print("SUBJECT:", data.get("email_subject"))
            print("-" * 70); print(data.get("email_body")); print("-" * 70)
            print("REFRESH_MD (first 1200 chars):")
            print((data.get("refresh_md") or "")[:1200])
        else:
            print(f"{phase}-season + change_level='{level}' -> live mode would send NOTHING this run.")
        print("=" * 70)
        print("sources:", *urls, sep="\n  ")
        print("DRY RUN — nothing sent. (state not written in dry-run)")
        return

    if not should:
        reason = data.get("reason", "")
        print(f"holding ({phase}-season, change_level={level}) — {reason}. Nothing sent.")
        _save_state(snapshot, level, sent=False)
        _rec(dry, not _run_failed(data), f"{phase}/{level}: held"[:120])
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
    sent_ok = r.returncode == 0
    _save_state(snapshot, level, sent=sent_ok,
                opener_done_year=syear if (opener and sent_ok) else None)
    _rec(dry, sent_ok, f"{phase}/{level}: sent" if sent_ok else "send failed")


if __name__ == "__main__":
    main()
