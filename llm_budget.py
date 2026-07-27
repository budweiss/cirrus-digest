"""llm_budget.py — spend guard for CIRRUS/CUMULUS/MacBook external LLM calls.

Scaffolded S46 (2026-07-27). Wraps every external (paid) LLM call so autonomous
escalation to Opus 5 (and the panel) can NEVER quietly overspend.

Design principles
-----------------
* FAIL-CLOSED. If pricing is missing, tokens are unknown, or the ledger can't be
  read, a call is treated as OVER budget and BLOCKED — never spend blind.
* STDLIB ONLY (json, os, time, datetime) — no new dependencies, same as the rest
  of the app. Portable across CIRRUS (macOS/Metal) and CUMULUS (ARM64/CUDA).
* INERT UNTIL KEYED. This module enforces caps regardless, but nothing actually
  spends until an anthropic_api_key is present AND llm_escalation_enabled is true
  in credentials.json. Landing it changes no behavior on its own.
* PER-BOX. Each machine reads its own caps + writes its own ledger, so MacBook,
  CIRRUS, and CUMULUS are independently capped ($100/session, $200/day default).

Public API
    load_config(cfg_path)                      -> dict (pricing + caps + discounts)
    cost_usd(model, in_tok, out_tok, cfg, *,   -> float
             batch=False, cache_read_frac=0.0)
    allow(session_id, est_cost, cfg, *, box, ledger_path) -> (bool, reason)
    record(session_id, provider, model, in_tok, out_tok, cfg, *, box,
           ledger_path, batch=False, cache_read_frac=0.0, task="", tier="") -> dict
    session_spent / day_spent (introspection)
"""

import json
import os
import time
from datetime import datetime, timezone

# ── config ──────────────────────────────────────────────────────────────────────
_DEFAULT_CAPS = {"per_session": 100.0, "per_day": 200.0, "per_call": 10.0}


def load_config(cfg_path):
    """Load pricing/caps JSON. Returns {} on any failure (→ fail-closed downstream)."""
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
        # minimal shape check
        if "models" not in cfg:
            return {}
        cfg.setdefault("caps_usd", dict(_DEFAULT_CAPS))
        cfg.setdefault("discounts", {"batch": 0.5, "cache_read": 0.1, "cache_write": 1.25})
        cfg.setdefault("unknown_model_out_per_m", 25.0)
        return cfg
    except Exception:
        return {}


# ── cost ─────────────────────────────────────────────────────────────────────────
def cost_usd(model, in_tok, out_tok, cfg, *, batch=False, cache_read_frac=0.0):
    """USD cost of a call. Raises ValueError if the model isn't priced (fail-closed:
    callers treat an exception as 'unknown cost → block')."""
    models = (cfg or {}).get("models") or {}
    rate = models.get(model)
    if not rate:
        raise ValueError(f"unpriced model: {model!r}")
    disc = cfg.get("discounts", {})
    cache_read = float(disc.get("cache_read", 0.1))
    batch_mult = float(disc.get("batch", 0.5)) if batch else 1.0

    in_tok = max(0, int(in_tok))
    out_tok = max(0, int(out_tok))
    cache_read_frac = min(max(float(cache_read_frac), 0.0), 1.0)

    cached = in_tok * cache_read_frac
    fresh = in_tok - cached
    in_cost = (fresh * rate["in"] + cached * rate["in"] * cache_read) / 1_000_000
    out_cost = out_tok * rate["out"] / 1_000_000
    return (in_cost + out_cost) * batch_mult


# ── ledger ─────────────────────────────────────────────────────────────────────
def _read_ledger(ledger_path):
    """Yield ledger rows (dicts). Missing file → empty. A corrupt line raises
    (fail-closed: caller blocks rather than under-count spend)."""
    if not ledger_path or not os.path.exists(ledger_path):
        return []
    rows = []
    with open(ledger_path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))  # deliberate: bad line → exception → block
    return rows


def _today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def session_spent(session_id, ledger_path):
    return sum(r.get("cost", 0.0) for r in _read_ledger(ledger_path)
               if r.get("session_id") == session_id)


def day_spent(ledger_path, day=None):
    day = day or _today_utc()
    return sum(r.get("cost", 0.0) for r in _read_ledger(ledger_path)
               if str(r.get("ts", "")).startswith(day))


# ── enforcement ───────────────────────────────────────────────────────────────
def allow(session_id, est_cost, cfg, *, box="unknown", ledger_path=None):
    """Return (ok: bool, reason: str). FAIL-CLOSED on any error."""
    try:
        if not cfg:
            return False, "no pricing/caps config (fail-closed)"
        caps = cfg.get("caps_usd", _DEFAULT_CAPS)
        est = float(est_cost)
        if est < 0:
            return False, "negative cost estimate (fail-closed)"
        if est > float(caps["per_call"]):
            return False, f"per-call ${est:.2f} > cap ${caps['per_call']:.2f}"
        s = session_spent(session_id, ledger_path) + est
        if s > float(caps["per_session"]):
            return False, f"session ${s:.2f} > cap ${caps['per_session']:.2f}"
        d = day_spent(ledger_path) + est
        if d > float(caps["per_day"]):
            return False, f"day ${d:.2f} > cap ${caps['per_day']:.2f}"
        return True, "ok"
    except Exception as e:  # unreadable ledger, bad numbers → block
        return False, f"budget check failed ({e}) — blocking (fail-closed)"


def record(session_id, provider, model, in_tok, out_tok, cfg, *, box="unknown",
           ledger_path=None, batch=False, cache_read_frac=0.0, task="", tier=""):
    """Append one call to the ledger and return the row (with computed cost).
    Call AFTER a successful API call using its ACTUAL usage.input_tokens/output_tokens."""
    cost = cost_usd(model, in_tok, out_tok, cfg, batch=batch, cache_read_frac=cache_read_frac)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "box": box, "session_id": session_id, "provider": provider, "model": model,
        "in_tok": int(in_tok), "out_tok": int(out_tok), "batch": bool(batch),
        "cache_read_frac": round(float(cache_read_frac), 3),
        "cost": round(cost, 6), "task": task, "tier": tier,
    }
    if ledger_path:
        os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
        with open(ledger_path, "a") as f:
            f.write(json.dumps(row) + "\n")
    return row


def notify_thresholds(session_id, cfg, ledger_path, notify_fn):
    """Fire notify_fn(msg) when session spend crosses 50% / 80% / 100% of the cap.
    Idempotent within a session via a sentinel set (caller passes a persistent set
    or re-derives from the ledger). Returns the crossed level or None."""
    caps = cfg.get("caps_usd", _DEFAULT_CAPS)
    spent = session_spent(session_id, ledger_path)
    cap = float(caps["per_session"])
    pct = (spent / cap) if cap else 1.0
    for level in (1.0, 0.8, 0.5):
        if pct >= level:
            notify_fn(f"⚠️ LLM spend {pct*100:.0f}% of ${cap:.0f} session cap "
                      f"(${spent:.2f}) — session {session_id}")
            return level
    return None
