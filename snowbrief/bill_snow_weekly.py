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
  2. Web-search the CURRENT ENSO / CPC state and published seasonal snowfall
     forecasts for the corridor; fetch a few readable sources.
  3. Ask Claude to decide MATERIAL CHANGE vs not and — only if material — draft a
     dated outlook refresh + the Bill email, in the established honest/probabilistic
     voice, signed as CIRRUS. Structured JSON reply so parsing is deterministic.
  4. FAIL-SAFE: if search is empty, the model errors, JSON won't parse, or the
     decision isn't unambiguously material -> treat as NO material change and send
     NOTHING. A client email only ever goes out on a clear, grounded material change.
  5. Live mode sends to Bill (cc Buddy) via send_bid_email; dry-run prints only.
  6. After a successful send the refresh becomes ref/baseline-outlook.md, so next
     week measures change against what Bill last saw (S553).

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


# S553: the ENSO/CPC queries used to fill all six fetch slots by themselves (the
# third never ran on 11-02), so nothing could ever move the snowfall number. Each
# query now takes one slot per pass, in this order; the last is the fallback.
QUERIES = [
    "NOAA CPC ENSO advisory ONI Nino 3.4 latest",
    "CPC winter outlook 2026-2027 Mid-Atlantic Northeast temperature precipitation",
    "NOAA Climate Prediction Center seasonal outlook discussion winter 2026-27 snowfall",
    "NWS Mount Holly winter outlook 2026-2027 snowfall Philadelphia South Jersey Delaware",
    "NWS Sterling Baltimore Washington winter outlook 2026-2027 snowfall",
    "2026-27 winter seasonal snowfall forecast inches Philadelphia Baltimore Delaware",
    "La Nina El Nino winter 2026 2027 forecast Northeast snow",
]


def select_sources(queries, search, fetch, limit=6):
    """[(url, content)], at most `limit`, taking turns across the queries so no
    one query can use up the slots. A source needs more than 300 readable chars."""
    pending = []
    for q in queries:
        try:
            pending.append(iter(search(q, max_results=6) or []))
        except Exception as e:
            print(f"search_web error for '{q}':", e)
    seen, fetched = set(), []
    while pending and len(fetched) < limit:
        for urls in list(pending):
            if len(fetched) >= limit:
                break
            for u in urls:
                if u in seen:
                    continue
                seen.add(u)
                content = fetch(u)
                if content and len(content) > 300:
                    fetched.append((u, content[:3500]))
                    break
            else:
                pending.remove(urls)
    return fetched


def gather_web():
    """Return (sources_block, url_list). Empty on any failure (=> fail-safe)."""
    try:
        from cirrus_daily import search_web, fetch_article_content, is_article_url
    except Exception as e:
        print("web tools import failed:", e)
        return "", []

    def fetch(u):
        try:
            return fetch_article_content(u)[0] if is_article_url(u) else ""
        except Exception:
            return ""

    fetched = select_sources(QUERIES, search_web, fetch)
    block = "\n\n".join(f"--- SOURCE {i}: {u} ---\n{c}"
                        for i, (u, c) in enumerate(fetched, 1))
    return block, [u for u, _ in fetched]


SYSTEM = (
    (
        "You are preparing a weekly winter snow-outlook check for Buddy's client Bill (Knight Property "
        'Services), corridor Baltimore-Philadelphia-South NJ-Delaware. You are careful and honest. You NEVER '
        'fabricate numbers. You anchor to the provided climatology and drivers analysis (blocking, not ENSO '
        'alone, drives our snow), give scenario odds rather than a single number, and keep the '
        'directional-estimate + placeholder-rate caveats. Emails are signed and sent as the sending node '
        "named in the task's voice instructions, on behalf of Knight Property Services, and frame the "
        'numbers as our best estimate for Bill (the expert) to review and correct.\nOutput contract:\n1. '
        'Return exactly the requested JSON object, no code fences or surrounding text. Keep reason to one or '
        'two sentences. Only refresh_md uses Markdown; email_body is plain paragraphs.\n2. Use only supplied '
        'task/source facts. Do not add meteorological associations, storm history, personal history, '
        'attachments, or promises of future work. Absence from the supplied text is not proof that an agency '
        'has published nothing. Do not invent a phase, degree, probability or rate.\n3. If material, preserve '
        'supplied scenario probabilities unless new supplied evidence supports a change. Weather '
        'probabilities are distinct from monetary working rates. Both refresh_md and email_body must call '
        "the outlook a directional estimate and the monetary rates placeholders pending Bill's expert "
        'confirmation.\n4. Attribute the blocking-versus-ENSO point to the supplied drivers analysis. Do not '
        'turn it into an unsupported weather prediction.\n5. Keep the email to a greeting, two short factual '
        "paragraphs, the change section from rule 7, one request for Bill's expert review/correction, and "
        "the task's exact node signature. "
        'End after that signature and any supplied source citation. Do not add commitments about what we '
        'will do next, including what happens when Bill replies.\n6. For council synthesis, the original source evidence outranks member claims. Name '
        'actual material disagreements concisely in reason, without inventing member counts or embellishing '
        'the rejected claim. A criticism must be grounded in what the member actually wrote. Keep the same '
        'required schema and format.\n7. If material, both refresh_md and email_body include a short section '
        "headed exactly 'What changed since last week / What did not'. In each text, it has a line starting "
        "'Snowfall estimate:' that gives the estimate and says plainly whether it changed from the baseline, "
        "with the reason; then what changed and what did not; then a last line starting 'Sources:' giving each "
        "web source used and 'published <date>' as that source shows it, or 'publish date not shown' when the "
        "source shows none; never guess a date. Report a published forecast's own figures beside our "
        "estimate; never blend sources into a new range of your own. Anything already in the "
        "baseline goes under what did not change: never present last week's facts as new.\n\nFinal evidence "
        'audit: check each factual clause and commitment against '
        'the original supplied task. Delete anything supported only by a member draft or by your background '
        'knowledge. In a disagreement, describe the unsupported proposition itself, without adding an '
        'allegation about numbers, rates, motives, or claims the member did not make. A false prediction and '
        "a false monetary figure are different errors; never substitute one for the other. Check the task's "
        'distinct weather and monetary caveats in both requested output texts. Then serialize the raw JSON '
        'object and end the email at its required signature, with no promise of future work.'
    )
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
CPC seasonal outlook, a newly published corridor seasonal snowfall forecast that
meaningfully differs from the baseline estimate, a meaningfully changed snowfall
probability, or an imminent significant corridor storm. Essentially-the-same numbers
= NOT material. A source published before the baseline's date is not new evidence, unless
it is a published corridor seasonal snowfall forecast whose figures the baseline does not
already report; then give its date and do not call it new this week.

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
     odds not a single number, under {REFRESH_MAX_CHARS - 2000} characters, including the
     section '{CHANGE_HEADING}'; else empty string>",
  "email_subject": "<if material: 'Pennrose Snow Package — Winter 2026-27 Outlook Update ({TODAY})'; else empty>",
  "email_body": "<if material: the full Bill email body — warm, plain, honest caveats,
     framed as a request for his expert review of the rates/assumptions, including the
     section '{CHANGE_HEADING}', signed '{NODE}'; else empty string>"
}}
If the web findings are missing or too thin to judge a real change, set material_change=false
and say so in reason. Do NOT invent ENSO states or numbers you cannot support from the
baseline or the web findings."""



CHANGE_HEADING = "What changed since last week / What did not"
# The refresh becomes next week's baseline, and the route refuses an oversized
# prompt: an unbounded refresh could make a later Monday unrunnable (S553).
REFRESH_MAX_CHARS = 10000


def has_change_section(text):
    """The section Bill asked for: the snowfall estimate and each source's date."""
    head, found, tail = text.lower().partition(CHANGE_HEADING.lower())
    return bool(found) and "estimate" in tail and "publish" in tail


def ready_for_bill(data):
    """The final answer, after synthesis, has both required lines in both texts.

    Stricter than validate_decision on purpose. That check also runs on each panel
    member, where one missing label would abort the whole run; the judge repairs a
    member's slip, so the exact lines are required only of what reaches Bill.
    """
    for text in (data["refresh_md"], data["email_body"]):
        tail = text.lower().partition(CHANGE_HEADING.lower())[2]
        if "snowfall estimate" not in tail or "sources:" not in tail:
            return False
    return True


def validate_decision(data):
    """Reject malformed model decisions before persistence or client delivery."""
    if not isinstance(data, dict) or type(data.get("material_change")) is not bool:
        return False
    fields = ("reason", "refresh_md", "email_subject", "email_body")
    if any(not isinstance(data.get(k), str) for k in fields):
        return False
    if not data["reason"].strip():
        return False
    if data["material_change"]:
        return (all(data[k].strip() for k in fields)
                and len(data["refresh_md"]) <= REFRESH_MAX_CHARS
                and has_change_section(data["refresh_md"])
                and has_change_section(data["email_body"]))
    return all(not data[k].strip() for k in fields[1:])



def weekly_budget(creds, now=None):
    """S350: Buddy approved $20 per ISO week; retain the $5 per-run ceiling."""
    import math
    scoped = dict(creds)
    budget = dict(creds.get("llm_budget") or {})
    for key, ceiling in (("per_session_usd", 20.0), ("per_call_usd", 5.0)):
        value = float(budget.get(key, ceiling))
        if not math.isfinite(value) or value < 0:
            raise ValueError("invalid snow budget")
        budget[key] = min(value, ceiling)
    scoped["llm_budget"] = budget
    year, week, _ = (now or datetime.now()).isocalendar()
    return scoped, f"billsnow:{year}-W{week:02d}"


def decide():
    with open(CREDS_PATH) as source:
        creds, budget_session = weekly_budget(json.load(source))
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
        # S141: keep the hint, so a hint we could not BUILD is reported as our
        # own missing config rather than as "this caller wanted no draft".
        _hint = _local_hint()
        # S340: 6000 output tokens fits the reviewed two-member panel plus
        # synthesis under the then-$1 aggregate cap. All 14 saved normal,
        # no-evidence, disagreement and long-context provider fixtures passed
        # without truncation at this limit. Admission still refuses oversized
        # inputs/prices; never drop a reviewer or truncate evidence to fit.
        council_creds = dict(creds)
        council_creds.pop("anthropic_effort", None)
        meta, text = ensemble.best_answer(SYSTEM, prompt, council_creds, max_tokens=6000,
                                          task="billsnow", local=_hint, session_id=budget_session,
                                          app_dir=str(DIGEST_DIR), mode=mode_override)
        # S131: `draft=` names the engine that wrote the local draft (vllm | ollama
        # | none) -- the journal is the witness that the endpoint drafted and
        # ollama stayed idle on Monday's run. LOCAL-MODEL-PLAN.md Phase 1 step 3.
        print(f"[llm] mode={meta['mode']} members={meta['members']} judge={meta['judge']} "
              f"draft={meta.get('draft_by') or 'none'} "
              f"degraded={meta['degraded']} est=${meta.get('est_cost_usd')} "
              f"({meta['reason']}); {len(text)} chars")
        # S141: the journal has said `draft=` since S131, and nothing read it.
        # Carry it out so the STATUS NOTE carries it too -- that note is printed
        # verbatim by cumulus_daily_brief (20:00, emailed), so a downgrade or a
        # missing draft reaches Buddy without anyone running a session. Bill's
        # email is unchanged either way; that is exactly why it needed a witness
        # rather than an exception.
        _draft_state["by"] = meta.get("draft_by") or ""
        _draft_state["error"] = meta.get("draft_error") or ""
        if not _hint and not _draft_state["error"]:
            # This job always wants a draft. No hint means node_profiles.json
            # has no digest_model for TARGET_ENV -- ours to fix, and invisible
            # until now because ensemble simply skipped the draft.
            _draft_state["error"] = ("no local hint: node_profiles.json has no "
                                     "digest_model for this box")
        if meta.get("draft_error"):
            print(f"[llm] DRAFT DEGRADED: {meta['draft_error']}")
    except Exception as e:
        return {"material_change": False, "error": True,
                "reason": "LLM call failed: " + (
                    str(e.__cause__) if str(e.__cause__) in {
                        'route budget exhausted', 'aggregate budget denied',
                        'foundation contract changed; review required',
                        'input exceeds reviewed scope'} else str(e))}, urls
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
    if not validate_decision(data):
        return {"material_change": False, "error": True,
                "reason": "invalid model decision fields; not sending."}, urls
    if data["material_change"] and not ready_for_bill(data):
        return {"material_change": False, "error": True,
                "reason": "final answer lacks the snowfall estimate or sources line; not sending."}, urls
    # Clean model decision — NOT an error, even if the reason prose happens to
    # contain a word like "failed"/"parse". error stays falsy on this path.
    data["urls"] = urls
    return data, urls


# S141 — what the local draft did on this run, for the status note. A plain
# module dict, not a return value: decide() already returns (data, urls) and
# threading a third element through every call site would be a wider change
# than the problem warrants (rule 3). This job is single-threaded and runs once.
_draft_state = {"by": "", "error": ""}


def _draft_note():
    """The draft clause for the status note, or "" when there is nothing to say.

    Shapes: `draft=vllm` (all well, still recorded so a change is visible week
    to week), `draft=ollama DEGRADED: ...`, `draft=NONE: ...`.
    """
    by, err = _draft_state.get("by", ""), _draft_state.get("error", "")
    if not by and not err:
        return ""
    if not by:
        return f"draft=NONE ({err[:70]})" if err else "draft=NONE"
    if err:
        return f"draft={by} DEGRADED ({err[:70]})"
    return f"draft={by}"


def _with_draft(note):
    d = _draft_note()
    return f"{note}; {d}" if d else note


def build_note(info):
    """The ledger note for one run. Self-contained (S150).

    Wraps the three literal shapes the call sites use, so they stop being
    literals scattered across main() and become something a lint can enumerate.
    The draft clause is appended exactly as _with_draft does it.
    """
    if info.get("suppressed"):
        return "already sent today — duplicate send suppressed"
    if info.get("sent") is False:
        base = "send failed"
    elif info.get("sent"):
        base = "sent material update"
        if info.get("baseline_failed"):
            base += "; BASELINE NOT UPDATED"
    else:
        # S245: was `str(reason or "no material change")[:120]` -- the trigger
        # phrase completeness.py's Rule looks for (zero_phrases=("no material
        # change", ...)) only survived truncation if the model's OWN wording
        # happened to say it in the first 120 chars. With the anthropic_effort
        # fix restoring real judge reasoning, the judge now writes long,
        # substantive "why this isn't material" explanations (~470 chars on
        # 2026-09-21) that lead with evidence, not the verdict -- truncation cut
        # the phrase off entirely, and T93 (runner/rule_note_lint.py) caught the
        # Rule reading that live note as UNREADABLE, the exact S81 failure mode
        # ("a check that fires on correct behaviour is one you teach yourself to
        # ignore") on a job that was actually working. Putting the fixed phrase
        # FIRST guarantees it survives truncation regardless of how the model
        # phrases or orders its reasoning.
        reason = str(info.get("reason") or "").strip()
        base = (f"no material change: {reason}" if reason else "no material change")[:120]
    d = info.get("draft") or ""
    return f"{base}; {d}" if d else base


def note_samples():
    """Every note shape this job writes. Built by CALLING build_note (S150).

    Bill's weekly snow brief. The quiet shape ("no material change") is the
    normal winter-shoulder outcome and must read ZERO; the send-failed and
    degraded-draft shapes never appear in the ledger on a good week.
    """
    return [
        ("a material update went out", build_note({"sent": True}), "productive"),
        ("no material change this week",
         build_note({"reason": "no material change"}), "zero"),
        ("quiet, with a degraded local draft",
         build_note({"reason": "no material change",
                     "draft": "draft=ollama DEGRADED (vllm down)"}), "zero"),
        # S245 regression case: a REAL judge reason from 2026-09-21, long
        # enough that the trigger phrase would be truncated away if it were not
        # forced to the front (see build_note's else-branch comment). Without
        # the fix this reads UNREADABLE to completeness.py's Rule, not "zero".
        ("a long, substantive judge explanation for why this ISN'T material",
         build_note({"reason": "NOAA's latest ENSO data (weekly Nino-3.4 now "
                     "+2.7C, >90% odds of a very strong event, a new 75% "
                     "chance of a historic/record event) confirms further "
                     "intensification, but the 7/27 baseline had already "
                     "anchored to a very strong El Nino with wide variance."}),
         "zero"),
        # Productive, not zero: the week's brief DID go out, this run just
        # refused to send it twice. Same call already asserted for billnewdev.
        ("sent, but the baseline could not be updated",
         build_note({"sent": True, "baseline_failed": True}), "productive"),
        ("a duplicate run was suppressed",
         build_note({"suppressed": True}), "productive"),
        ("the send failed", build_note({"sent": False}), "blind"),
    ]


def promote_baseline(refresh_md, today=None):
    """Make this week's sent refresh the baseline. True on success, never raises:
    the mail has already gone, so a failure here is reported, not fatal.

    The file stays tracked but changes on the running host from here on, so do
    not commit it: `git pull --ff-only` there refuses a commit touching it (T181).
    """
    text = re.sub(r"[ \t]*\[\d+(?:\s*[,\u2013-]\s*\d+)*\]", "", refresh_md).strip()
    note = (f"*Baseline note ({today or TODAY}): the refresh sent to Bill that day, so the next "
            "run compares against what he last saw. Source markers removed.*")
    first, _, rest = text.partition("\n")
    text = (f"{first}\n\n{note}\n{rest}" if first.startswith("#") else f"{note}\n\n{text}") + "\n"
    path = REF / "baseline-outlook.md"
    try:
        with tempfile.NamedTemporaryFile("w", dir=REF, prefix=".baseline-", delete=False) as tf:
            tf.write(text)
        os.replace(tf.name, path)
        return True
    except Exception as e:
        print(f"WARNING: baseline not updated ({type(e).__name__}): next week compares against the old one.")
        return False


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
    force = "--force-send" in sys.argv
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] bill_snow_weekly ({'dry-run' if dry else 'live'})")

    # S81: refuse a SECOND send on the same day. This unit is in Skywarden's
    # restart allowlist, and a restart re-runs main() from the top -- so a
    # failure that happened AFTER a successful send would mail Bill twice.
    # Checked BEFORE decide(), which costs web research and an LLM call: if the
    # week's mail is already out there is nothing left to decide.
    # Fails OPEN by design -- see send_guard's module docstring.
    if not dry and not force:
        import send_guard
        stamp = send_guard.already_sent_today("billsnow")
        if stamp:
            print(send_guard.blocked_message("billsnow", stamp))
            # Recorded as a healthy run, because it IS one: the week's send
            # happened. Staying silent here would read as a job that never ran.
            _rec(dry, True, build_note({"suppressed": True}))
            return

    data, urls = decide()
    material = (not _run_failed(data) and validate_decision(data)
                and data["material_change"] is True)
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
        _rec(dry, not _run_failed(data),
             build_note({"reason": reason[:120] or "no material change",
                         "draft": _draft_note()}))
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
    baseline_ok = True
    stamped = False
    if r.returncode == 0:
        # Stamp FIRST, then record: the stamp is what stops a duplicate, and
        # the window between the mail leaving and the stamp landing is the only
        # window in which a restart could still double-send. Keep it short.
        import send_guard
        stamped = send_guard.mark_sent("billsnow", data.get("email_subject") or "")
        if not stamped:
            print("WARNING: send stamp not written — a restart could re-send.")
        baseline_ok = promote_baseline(data.get("refresh_md") or "")
    # A stale baseline fails the row so the report card shows it. Without a stamp
    # the row stays ok: a failed row and no stamp would make the catch-up re-send.
    _rec(dry, r.returncode == 0 and (baseline_ok or not stamped),
         build_note({"sent": r.returncode == 0, "baseline_failed": not baseline_ok,
                     "draft": _draft_note()}))


def _prompt_with_stub_refs():
    global _read
    real = _read
    _read = lambda name: ""
    try:
        return build_prompt("", [])
    finally:
        _read = real


def selftest() -> int:
    """Offline. Touches no network, no creds, no file -- and no live state:
    it only exercises the pure note builders (T32).

    This file had NO selftest until S141, which is why `dev_findings`'
    blind_gate rule exists: dev_agent's gate 2 runs "the changed module's own
    selftest" and was reporting `selftest` for this module while inspecting
    nothing at all.
    """
    fails = 0

    def ck(name, cond):
        nonlocal fails
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        fails += 0 if cond else 1

    # S553: the snowfall queries must get slots even when ENSO alone could fill them.
    def fake_search(q, max_results=6):
        return [f"https://{QUERIES.index(q)}.example/{i}" for i in range(max_results)]
    picked = [u for u, _ in select_sources(QUERIES, fake_search, lambda u: "x" * 400)]
    ck("six sources at most, one per query first", len(picked) == 6
       and len({u.split(".")[0] for u in picked}) == 6)
    ck("published snowfall forecast queries get a slot",
       any("https://5." in u for u in picked))
    sparse = select_sources(["a", "b"], lambda q, max_results=6: ["u1", "u2", "u3"] if q == "a" else [],
                            lambda u: "x" * 400)
    ck("an empty query's turns go to the others", [u for u, _ in sparse] == ["u1", "u2", "u3"])
    ck("a short page is not a source",
       select_sources(["a"], lambda q, max_results=6: ["u"], lambda u: "x" * 300) == [])
    ck("a failed search does not end the run",
       len(select_sources(["bad", "a"], lambda q, max_results=6: 1 / 0 if q == "bad" else ["u"],
                          lambda u: "x" * 400)) == 1)

    # S553: a material update must carry the section, in both texts.
    section = f"{CHANGE_HEADING}\nSnowfall estimate unchanged. Source, published 11-01."
    good = dict(material_change=True, reason="r", refresh_md=section, email_subject="s", email_body=section)
    ck("the change section passes in both texts", validate_decision(good))
    for key in ("refresh_md", "email_body"):
        ck(f"{key} without the section is refused", not validate_decision(dict(good, **{key: "body"})))
    ck("a section with no snowfall estimate is refused",
       not validate_decision(dict(good, email_body=f"{CHANGE_HEADING}\nSource, published 11-01.")))
    ck("a section with no publish dates is refused",
       not validate_decision(dict(good, email_body=f"{CHANGE_HEADING}\nSnowfall estimate unchanged.")))
    ck("the final answer with both lines is ready",
       ready_for_bill(dict(good, refresh_md=section + "\nSources: x, published 11-01.",
                           email_body=section + "\nSources: x, published 11-01.")))
    ck("a final answer with no Sources line is not ready", not ready_for_bill(good))
    ck("a final answer with no estimate line is not ready",
       not ready_for_bill(dict(good, email_body=f"{CHANGE_HEADING}\nThe estimate held. Sources: x, published 11-01.",
                               refresh_md=section + "\nSources: x, published 11-01.")))
    ck("an oversized refresh is refused",
       not validate_decision(dict(good, refresh_md=section + "x" * REFRESH_MAX_CHARS)))
    ck("a quiet week needs no section",
       validate_decision(dict(material_change=False, reason="r", refresh_md="", email_subject="", email_body="")))
    ck("the prompt and the model contract name the same heading",
       CHANGE_HEADING in SYSTEM and CHANGE_HEADING in _prompt_with_stub_refs())

    # S553: the baseline becomes what Bill last saw. REF points at a temp dir (T32).
    global REF
    real_ref = REF
    with tempfile.TemporaryDirectory() as tmp:
        REF = Path(tmp)
        try:
            ok = promote_baseline("# Refresh\nIt rose [1], then fell [2][3] and [4, 5].\n", "2026-11-09")
            text = (REF / "baseline-outlook.md").read_text()
            ck("promotion reports success", ok)
            ck("citation markers are stripped", "It rose, then fell and." in text and "[" not in text)
            ck("a one-line provenance note follows the title",
               text.split("\n")[2].startswith("*Baseline note (2026-11-09)"))
            ck("no temp file is left behind", [p.name for p in REF.iterdir()] == ["baseline-outlook.md"])
            REF = Path(tmp) / "missing"
            ck("a failed promotion returns False, not an exception", promote_baseline("# R\nx") is False)
        finally:
            REF = real_ref
    ck("a failed promotion is in the note",
       "BASELINE NOT UPDATED" in build_note({"sent": True, "baseline_failed": True}))

    saved = dict(_draft_state)
    try:
        # A run whose draft came off the endpoint: recorded, no alarm. It is
        # still SAID, so a change from vllm to ollama is visible week to week
        # in the daily brief rather than only in the journal.
        _draft_state.update(by="vllm", error="")
        ck("a clean vllm draft is named in the note", _draft_note() == "draft=vllm")
        ck("...and is appended to the run's own reason",
           _with_draft("no material change") == "no material change; draft=vllm")

        # The silent downgrade: the endpoint was configured and did not answer.
        _draft_state.update(by="ollama", error="downgraded to ollama after vllm: ProviderError: endpoint down")
        ck("a downgrade to ollama is marked DEGRADED, not passed off as normal",
           _draft_note().startswith("draft=ollama DEGRADED"))
        ck("...and names the cause", "ProviderError" in _draft_note())

        # Both engines gone: the judge got no draft at all.
        _draft_state.update(by="", error="vllm: URLError: refused; ollama qwen3: URLError: refused")
        ck("no draft at all reads NONE, never blank", _draft_note().startswith("draft=NONE"))
        ck("...and carries the reason into the note",
           "URLError" in _with_draft("sent material update"))

        # A caller that never wanted a draft must not manufacture an alarm (T9).
        # This job ALWAYS asks for a draft, so "no hint" is our own missing
        # config, not "the caller wanted none". The message must say which.
        _draft_state.update(by="", error="no local hint: node_profiles.json has no digest_model for this box")
        ck("a hint we could not BUILD is blamed on our config, not the caller",
           "node_profiles.json" in _draft_note()
           and "requested by this caller" not in _draft_note())

        _draft_state.update(by="", error="")
        ck("a run with no draft state says nothing at all", _draft_note() == "")
        ck("...and leaves the note byte-identical",
           _with_draft("sent material update") == "sent material update")

        # The note is a status field read by the daily brief; it must stay short.
        _draft_state.update(by="ollama", error="x" * 400)
        ck("a huge error is truncated, so the status row stays a row",
           len(_draft_note()) < 100)

        # S245: completeness.py's Rule for this job matches on the literal
        # phrase "no material change" (its zero_phrases). It read a REAL
        # 2026-09-21 judge note as UNREADABLE because that phrase only
        # survived build_note's 120-char truncation by luck of wording -- a
        # long, substantive "why this isn't material" explanation (which the
        # anthropic_effort fix makes routine now) pushed it past the cutoff.
        # These two assertions are the actual regression test for that: the
        # phrase must be present in the built note NO MATTER how the model
        # orders or lengthens its reasoning.
        long_reason = ("NOAA's latest ENSO data confirms further intensification, "
                       "but the baseline already anchored to a very strong El Nino "
                       "with wide variance and boom-or-bust framing, so scenario "
                       "odds and the central estimate are unchanged and this does "
                       "not meet the bar for a client-facing update this week.")
        assert len(long_reason) > 120, "test fixture must exceed the truncation length"
        ck("a long judge reason still reads as a quiet week to completeness.py",
           "no material change" in build_note({"reason": long_reason}))
        ck("...even though the reason alone would have pushed the phrase past 120 chars",
           "no material change" not in long_reason[:120])
    finally:
        _draft_state.clear()
        _draft_state.update(saved)

    print(f"\n{'ALL PASS' if fails == 0 else f'{fails} FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    if "selftest" in sys.argv or "--selftest" in sys.argv:
        sys.exit(selftest())
    main()
