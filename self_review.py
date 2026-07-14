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

    added, proposed, hardware = [], [], []
    for it in items:
        blob = f"{it.get('detail','')} {it.get('source_line','')}"
        # Auto-add a validated NEW source (feeds only; needs a real URL).
        if it["type"] == "ADD_SOURCE" or SOURCE_RX.search(blob):
            m = URL_RX.search(blob)
            if m and m.group(0) not in known and _valid_feed(m.group(0)):
                if _add_source(m.group(0), it.get("detail", "")):
                    added.append((it.get("detail", ""), m.group(0)))
                    known.add(m.group(0))
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
    _notify(kind, added, proposed, hardware)
    B.log(f"self_review ({kind}): +{len(added)} sources, "
          f"{len(proposed)} proposed, {len(hardware)} hardware/env")


def _notify(kind, added, proposed, hardware):
    d = datetime.now().strftime("%Y-%m-%d")
    lines = [f"🤖 *CIRRUS self-review* ({kind}) — {d}", ""]
    if added:
        lines.append(f"✅ *Auto-added {len(added)} source(s)*:")
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
    if not (added or proposed or hardware):
        lines.append("Nothing new to review today.")
    try:
        B.send_message(B.ALLOWED_ID, "\n".join(lines))
    except Exception as e:
        B.log(f"self_review notify failed: {e}")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "daily")
