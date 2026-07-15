#!/usr/bin/env python3
"""
self_review.py — CIRRUS daily/weekly self-improvement pass.

Runs at the END of the daily (and weekly) digest, after action items are
extracted. It makes CIRRUS a little more current each day WITHOUT letting
junk in:

  • AUTO-ADDS new monitoring sources (feeds / podcasts / pages) that VALIDATE
    — URL resolves, looks like RSS/Atom, and isn't already tracked. Low-risk
    and reversible (the overlay is outside git). Boundary set with Buddy
    2026-07-11: sources auto-apply; everything else is proposed.
  • PROPOSES everything else (CIRRUS_NOTE, CAPABILITY_REQUEST, PULL_MODEL) —
    merged into the /approve queue for Buddy's tap. Never auto-applied.
  • Flags HARDWARE / ENVIRONMENT needs in their own section (note only —
    Buddy provisions these).
  • Sends a Telegram summary to the owner after the run.

Invoked from cirrus_daily.py / cirrus_digest.py post-run block, and runnable
standalone:  python3 self_review.py [daily|weekly]
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import requests

import cirrus_bot as B   # reuse config + helpers (import does NOT start the bot)
import dev_loop          # Autonomous Dev-Loop: spec + risk tier + ledger (Phase 1)

# What looks like a "monitor this source" suggestion.
SOURCE_RX = re.compile(
    r'\b(add|subscribe(?:\s+to)?|follow|monitor|track)\b[^.]*?'
    r'\b(rss|feed|podcast|newsletter|blog|substack|medium|author|channel|'
    r'source)\b', re.IGNORECASE)
# What signals a hardware / separate-environment need (note only).
HARDWARE_RX = re.compile(
    r'\b(gpu|vram|unified\s+memory|\d+\s?gb|hardware|dgx|spark|mac\s+studio|'
    r'\bm5\b|rtx|blackwell|separate\s+environment|test\s+environment|sandbox|'
    r'dedicated\s+(server|machine|box|rig))\b', re.IGNORECASE)
URL_RX = re.compile(r'https?://[^\s`\'")\]]+')

# ── Mission-relevance gate (Session 35, 2026-07-15) ──────────────────────────
# Buddy: "how can we only send requests that make sense to work on?" Every
# candidate is scored against the mission by the local model BEFORE it can
# auto-apply or enter /approve. Below-threshold items are kept in the pending
# file with status="filtered" (so dedupe blocks re-entry via /approve merge)
# and logged to logs/self-review-filtered.md for audit/tuning. FAIL-OPEN: if
# the model is unavailable, items pass through — the gate must never eat a
# real proposal. Override the mission text via config/mission.txt.
RELEVANCE_MIN = 6
MISSION_DEFAULT = """CIRRUS serves Buddy's projects and infrastructure:
- Local AI infrastructure: CIRRUS (Mac Studio, Ollama/qwen), CUMULUS (DGX
  Spark beta, coming), STRATUS (future prod). Model upgrades ONLY if they fit
  local hardware and beat current qwen2.5 for digest/agent work.
- The daily/weekly digest pipeline: better summaries, dedupe, sources about
  AI engineering, agentic automation, local/open-weights models, Anthropic/
  Claude ecosystem, self-hosted tooling.
- Real-estate tooling (Aggie): PA agreements of sale, MLS, ZipForms.
- Property management + snow business (Bill): Kent County DE HOAs, aerial
  property measurement, bidding, lead research.
NOT relevant: consumer/edtech products, crypto/trading, generic AI-ethics
commentary, celebrity/business gossip, anything with no concrete benefit to
the systems or businesses above."""


def _mission() -> str:
    p = B.PROJECT_DIR / "config/mission.txt"
    try:
        if p.exists():
            return p.read_text().strip()
    except Exception:
        pass
    return MISSION_DEFAULT


def _relevance(item: dict):
    """Score a candidate 0-10 against the mission via local Ollama.
    Returns (score:int, why:str). Fail-open: (10, reason) on any error."""
    prompt = (f"{_mission()}\n\n"
              f"Candidate self-improvement proposal:\n"
              f"TYPE: {item.get('type','')}\n"
              f"PROPOSAL: {item.get('detail','')}\n"
              f"WHY (from digest): {item.get('why','')}\n\n"
              f"How relevant and worthwhile is this proposal to the mission "
              f"above? Consider: does it concretely help one of the listed "
              f"systems/businesses? Is it actionable on this infrastructure?\n"
              f"Reply with EXACTLY one line: SCORE: <0-10> | WHY: <one short "
              f"sentence>")
    try:
        r = requests.post(f"{B.OLLAMA_HOST}/api/generate",
                          json={"model": B.MODEL, "prompt": prompt,
                                "stream": False,
                                "options": {"temperature": 0, "num_ctx": 2048}},
                          timeout=90)
        r.raise_for_status()
        text = r.json().get("response", "")
        m = re.search(r'SCORE:\s*(\d+)', text)
        if not m:
            return 10, "gate parse failure — fail-open"
        why_m = re.search(r'WHY:\s*(.+)', text)
        return min(int(m.group(1)), 10), (why_m.group(1).strip()[:120]
                                          if why_m else "")
    except Exception as e:
        return 10, f"gate unavailable ({e}) — fail-open"


def _log_filtered(item: dict, score: int, why: str):
    p = B.PROJECT_DIR / "logs/self-review-filtered.md"
    try:
        if not p.exists():
            p.write_text("# Self-review relevance-gate rejections\n\n"
                         "Audit trail — tune RELEVANCE_MIN / config/mission.txt "
                         "if good items land here.\n\n")
        with open(p, "a") as f:
            f.write(f"- {datetime.now().strftime('%Y-%m-%d %H:%M')} "
                    f"[{score}/10] {item.get('type','')}: "
                    f"{item.get('detail','')[:100]} — {why}\n")
    except Exception as e:
        B.log(f"self_review: filtered-log write failed: {e}")


def _valid_feed(url: str) -> bool:
    try:
        r = requests.get(url, timeout=20,
                         headers={"User-Agent": "CIRRUS-digest/1.0"})
        head = r.text[:2000].lower()
        return r.status_code == 200 and (
            "<rss" in head or "<feed" in head or "<?xml" in head)
    except Exception:
        return False


def _existing_feed_urls() -> set:
    urls = set()
    for p in (B.PROJECT_DIR / "config/sources.json",
              B.PROJECT_DIR / "config/sources.local.json"):
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        src = d.get("web_sources", []) if isinstance(d, dict) else d
        for s in src:
            if isinstance(s, dict) and s.get("rss"):
                urls.add(s["rss"])
    return urls


def _add_source(url: str, name: str) -> bool:
    overlay_path = B.PROJECT_DIR / "config/sources.local.json"
    try:
        overlay = json.loads(overlay_path.read_text()) if overlay_path.exists() else []
    except Exception:
        overlay = []
    if any(s.get("rss") == url for s in overlay):
        return False
    overlay.append({
        "name": name[:60], "rss": url, "type": "blog",
        "added_by": "self-review",
        "added": datetime.now().strftime("%Y-%m-%d")})
    overlay_path.write_text(json.dumps(overlay, indent=2) + "\n")
    B.log(f"self_review: auto-added source {url}")
    return True


def run(kind: str = "daily"):
    prefix = "daily-actions" if kind == "daily" else "weekly-actions"
    date = datetime.now().strftime("%Y-%m-%d")
    latest = B.find_latest_action(prefix)
    if not latest:
        B.log(f"self_review: no {prefix} file found")
        return
    # Snapshot mutable config BEFORE any auto-change, so today's state is
    # always restorable (14-day retention). Never blocks the review.
    try:
        from config_snapshot import take_snapshot
        take_snapshot(tag=kind)
    except Exception as e:
        B.log(f"self_review: snapshot failed (continuing): {e}")

    items = B.extract_recommendations(latest)   # already filtered by the bot
    known = _existing_feed_urls()
    pending = B.load_pending()
    existing_keys = {f"{p['type']}:{p['detail']}" for p in pending}
    existing_details = [p.get("detail", "") for p in pending]

    added, proposed, hardware, filtered = [], [], [], []
    for it in items:
        blob = f"{it.get('detail','')} {it.get('source_line','')}"
        # Mission-relevance gate — BEFORE auto-add and BEFORE proposing, so an
        # off-mission feed can neither self-apply nor reach /approve. Dedupe
        # first (cheap) so we don't spend model time re-scoring known items.
        key0 = f"{it['type']}:{it['detail']}"
        if key0 in existing_keys or B.is_duplicate_detail(it["detail"], existing_details):
            continue
        score, gate_why = _relevance(it)
        if score < RELEVANCE_MIN:
            it["status"] = "filtered"
            it["filter_reason"] = f"relevance {score}/10: {gate_why}"
            pending.append(it)              # tracked → dedupe blocks re-entry
            existing_keys.add(key0)
            existing_details.append(it["detail"])
            _log_filtered(it, score, gate_why)
            filtered.append(it)
            continue
        # Auto-add a validated NEW source (feeds only; needs a real URL).
        # Tier-0 auto-apply (Phase 2): dev_loop.may_auto_apply gates this so ONLY
        # Tier-0-classified proposals can ever self-apply (defense-in-depth), and
        # every auto-apply is written to the self-changes ledger.
        if it["type"] == "ADD_SOURCE" or SOURCE_RX.search(blob):
            m = URL_RX.search(blob)
            if (m and m.group(0) not in known
                    and dev_loop.may_auto_apply(it["type"], it.get("detail", ""),
                                                it.get("source_line", ""))
                    and _valid_feed(m.group(0))):
                if _add_source(m.group(0), it.get("detail", "")):
                    added.append((it.get("detail", ""), m.group(0)))
                    known.add(m.group(0))
                    try:
                        dev_loop.ledger_append(
                            {"event": "auto-applied", "id": f"src-{date}-{len(added)}",
                             "tier_name": dev_loop.TIER_NAME[dev_loop.TIER_AUTO],
                             "detail": f"source: {it.get('detail','')[:60]} ({m.group(0)})",
                             "result": "added to sources.local.json",
                             "target_env": dev_loop.TARGET_ENV},
                            B.PROJECT_DIR)
                    except Exception as e:
                        B.log(f"self_review: ledger(auto-applied) failed: {e}")
                    continue   # applied — don't also queue it
        # Flag hardware/env needs (note only; still propose the item).
        if it["type"] == "CAPABILITY_REQUEST" and HARDWARE_RX.search(blob):
            hardware.append(it)
        # Propose everything else — merge into /approve queue (deduped).
        key = f"{it['type']}:{it['detail']}"
        if key in existing_keys or B.is_duplicate_detail(it["detail"], existing_details):
            continue
        # Dev-Loop Phase 1: attach a machine-actionable spec + risk tier, and
        # record the proposal in the append-only self-changes ledger. Additive
        # metadata — existing /approve consumers ignore the extra key.
        it["dev_spec"] = dev_loop.make_spec(it, len(proposed) + 1)
        try:
            dev_loop.ledger_append(
                {"event": "proposal", "id": it["dev_spec"]["id"],
                 "tier_name": it["dev_spec"]["tier_name"],
                 "detail": it.get("detail", ""), "result": "queued for /approve"},
                B.PROJECT_DIR)
        except Exception as e:
            B.log(f"self_review: ledger append failed (continuing): {e}")
        pending.append(it)
        existing_keys.add(key)
        existing_details.append(it["detail"])
        proposed.append(it)

    B.save_pending(pending)
    # Phase 2: write the daily "what CIRRUS changed about itself" report from the
    # ledger (auto-applied + proposed), derived + safe to regenerate.
    try:
        rpt, summary = dev_loop.write_self_changes_report(B.PROJECT_DIR, date)
        B.log(f"self_review: self-changes report {rpt.name} {summary}")
    except Exception as e:
        B.log(f"self_review: self-changes report failed (continuing): {e}")
    _notify(kind, added, proposed, hardware, filtered)
    B.log(f"self_review ({kind}): +{len(added)} sources, "
          f"{len(proposed)} proposed, {len(hardware)} hardware/env, "
          f"{len(filtered)} filtered off-mission "
          f"[target={dev_loop.TARGET_ENV}]")


def _notify(kind, added, proposed, hardware, filtered=None):
    d = datetime.now().strftime("%Y-%m-%d")
    lines = [f"🤖 *CIRRUS self-review* ({kind}) — {d}", ""]
    if added:
        lines.append(f"✅ *Auto-added {len(added)} source(s)* → {dev_loop.TARGET_ENV}:")
        for name, url in added[:8]:
            lines.append(f"• {name[:50]} — {url}")
        lines.append("")
    if hardware:
        lines.append(f"🖥 *Hardware/env needs ({len(hardware)})* — you provision:")
        for it in hardware[:6]:
            lines.append(f"• {it['detail'][:80]}")
        lines.append("")
    if proposed:
        pulls = [i for i in proposed if i["type"] == "PULL_MODEL"]
        caps = [i for i in proposed if i["type"] == "CAPABILITY_REQUEST"]
        notes = [i for i in proposed if i["type"] not in ("PULL_MODEL", "CAPABILITY_REQUEST")]
        lines.append(f"📋 *{len(proposed)} proposed for /approve*:")
        if pulls:
            lines.append(f"  models: " + ", ".join(i['detail'][:40] for i in pulls[:5]))
        if caps:
            lines.append(f"  capabilities: {len(caps)}")
        if notes:
            lines.append(f"  notes/sources: {len(notes)}")
        # Dev-Loop risk breakdown (Phase 1): how many are safe-to-automate.
        tiers = {}
        for i in proposed:
            t = (i.get("dev_spec") or {}).get("tier_name", "unclassified")
            tiers[t] = tiers.get(t, 0) + 1
        if tiers:
            lines.append("  risk: " + ", ".join(
                f"{n}× {t.split('(')[0].strip()}" for t, n in sorted(tiers.items())))
        lines.append("Tap /approve to review.")
    if filtered:
        lines.append(f"🧹 _{len(filtered)} off-mission item(s) filtered by the "
                     f"relevance gate (logs/self-review-filtered.md)_")
        lines.append("")
    if not (added or proposed or hardware or filtered):
        lines.append("Nothing new to review today.")
    try:
        B.send_message(B.ALLOWED_ID, "\n".join(lines))
    except Exception as e:
        B.log(f"self_review notify failed: {e}")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "daily")
