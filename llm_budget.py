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


# ── resolution: ONE place turns creds into (cfg, box, ledger_path) ─────────────
# S132. This was ensemble._load_budget(); it moved here so llm_providers.call()
# can record every cloud call to the SAME ledger ensemble already writes, without
# a second copy of the path/caps logic that could drift (S102: a copy of the
# resolution is exactly what made a test fake). ensemble delegates to this.
def resolve(creds, app_dir=None):
    """Return (cfg, box, ledger_path), or (None, None, None) when the pricing file
    cannot be loaded. `creds["llm_budget"]` may carry pricing_path, ledger_path,
    box and per_*_usd caps; relative paths resolve against app_dir (default: this
    file's directory, i.e. the repo checkout on either box)."""
    bud = (creds or {}).get("llm_budget") or {}
    app_dir = app_dir or os.path.dirname(os.path.abspath(__file__))
    pricing_path = bud.get("pricing_path", "config/llm_pricing.json")
    if not os.path.isabs(pricing_path):
        pricing_path = os.path.join(app_dir, pricing_path)
    cfg = load_config(pricing_path)
    if not cfg:
        return None, None, None
    caps = cfg.get("caps_usd", {})
    for k, src in (("per_session", "per_session_usd"), ("per_day", "per_day_usd"),
                   ("per_call", "per_call_usd")):
        if bud.get(src) is not None:
            caps[k] = float(bud[src])
    cfg["caps_usd"] = caps
    box = bud.get("box", "unknown")
    ledger = bud.get("ledger_path", "out/llm-spend-ledger.jsonl")
    if not os.path.isabs(ledger):
        ledger = os.path.join(app_dir, ledger)
    return cfg, box, ledger


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
           ledger_path=None, batch=False, cache_read_frac=0.0, task="", tier="",
           strict=True):
    """Append one call to the ledger and return the row (with computed cost).
    Call AFTER a successful API call using its ACTUAL usage.input_tokens/output_tokens.

    strict=True (the default, unchanged): an unpriced model raises, so a budget
    GATE fails closed. strict=False (S132, for OBSERVATION): an unpriced model
    is still written -- costed at `unknown_model_out_per_m` over all tokens and
    flagged "unpriced": true -- because a row that is silently dropped is the
    S103 failure (a counter nobody prints is not a measurement). The report
    sees the row; the flag says the number is a ceiling, not a price."""
    unpriced = False
    try:
        cost = cost_usd(model, in_tok, out_tok, cfg, batch=batch, cache_read_frac=cache_read_frac)
    except ValueError:
        if strict:
            raise
        rate = float((cfg or {}).get("unknown_model_out_per_m", 25.0))
        cost = (max(0, int(in_tok)) + max(0, int(out_tok))) * rate / 1_000_000
        unpriced = True
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "box": box, "session_id": session_id, "provider": provider, "model": model,
        "in_tok": int(in_tok), "out_tok": int(out_tok), "batch": bool(batch),
        "cache_read_frac": round(float(cache_read_frac), 3),
        "cost": round(cost, 6), "task": task, "tier": tier,
    }
    if unpriced:
        row["unpriced"] = True
    if ledger_path:
        os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
        with open(ledger_path, "a") as f:
            f.write(json.dumps(row) + "\n")
    return row


def record_call(creds, provider, model, in_chars, out_chars, *, task="",
                session_id=None, tier="", app_dir=None):
    """S132: best-effort ledger row for ONE completed model call, from what a
    generic caller has -- the prompt and reply LENGTHS, not provider usage
    fields. Tokens are estimated at 4 chars each (the same estimate ensemble has
    used since S57). Never raises: a broken ledger must not break a client job.
    Returns the row, or None when nothing was written (no pricing file, or an
    unwritable path). Local models are priced at $0 in config/llm_pricing.json so
    their VOLUME shows in the report at a true cost."""
    try:
        cfg, box, ledger = resolve(creds, app_dir)
        if cfg is None:
            return None
        return record(session_id or task or "untagged", provider, model or "?",
                      max(0, int(in_chars)) // 4, max(0, int(out_chars)) // 4, cfg,
                      box=box, ledger_path=ledger, task=task or "", tier=tier,
                      strict=False)
    except Exception:
        return None


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


# ── self-test (python3 llm_budget.py --selftest) — offline, tempfile only ──────
def selftest():
    """S132. Every path uses a tempdir: this module's whole job is appending to
    a ledger, so a test that touched the real out/ ledger would itself be the
    thing being measured (T32: tests never touch live files)."""
    import tempfile, shutil
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        ok = ok and cond

    td = tempfile.mkdtemp(prefix="llm-budget-selftest-")
    try:
        pricing = os.path.join(td, "pricing.json")
        ledger = os.path.join(td, "sub", "ledger.jsonl")
        with open(pricing, "w") as f:
            json.dump({"models": {"paid": {"in": 1.0, "out": 2.0},
                                  "free-local": {"in": 0.0, "out": 0.0}},
                       "unknown_model_out_per_m": 10.0}, f)
        creds = {"llm_budget": {"pricing_path": pricing, "ledger_path": ledger,
                                "box": "tbox", "per_day_usd": 7.5}}

        cfg, box, lp = resolve(creds)
        check("resolve: honours pricing_path / ledger_path / box from creds",
              cfg is not None and box == "tbox" and lp == ledger)
        check("resolve: per-box cap overrides the pricing file's default",
              cfg["caps_usd"]["per_day"] == 7.5)
        check("resolve: relative paths land under app_dir, not the CWD",
              resolve({}, app_dir=td)[0] is None   # no config/llm_pricing.json in td
              and resolve({"llm_budget": {"pricing_path": "pricing.json"}}, app_dir=td)[0] is not None)

        raised = False
        try:
            record("s", "p", "nobody-priced-this", 100, 100, cfg, ledger_path=ledger)
        except ValueError:
            raised = True
        check("record strict (default): an unpriced model RAISES -- a gate fails closed", raised)
        check("...and wrote nothing", not os.path.exists(ledger))

        row = record("s", "p", "nobody-priced-this", 1000, 1000, cfg, box=box,
                     ledger_path=ledger, task="t", strict=False)
        check("record strict=False: an unpriced model is WRITTEN and flagged",
              row.get("unpriced") is True and os.path.exists(ledger))
        check("...at the conservative unknown rate over all tokens (2000 tok x $10/M)",
              abs(row["cost"] - 0.02) < 1e-9)
        row = record("s", "ollama", "free-local", 5000, 5000, cfg, ledger_path=ledger, strict=False)
        check("record: a $0-priced local model costs 0.0 and is NOT flagged unpriced",
              row["cost"] == 0.0 and "unpriced" not in row)

        rows = [json.loads(l) for l in open(ledger)]
        check("ledger rows carry the schema the spend report reads (ts, task, cost)",
              all(k in rows[0] for k in ("ts", "task", "cost", "provider", "model", "box")))

        rc = record_call(creds, "anthropic", "paid", 4000, 400, task="job-x")
        check("record_call: estimates tokens at 4 chars each and tags the row",
              rc is not None and rc["in_tok"] == 1000 and rc["out_tok"] == 100
              and rc["task"] == "job-x" and rc["session_id"] == "job-x")
        bad = {"llm_budget": dict(creds["llm_budget"],
                                  ledger_path=os.path.join(pricing, "under-a-file", "x.jsonl"))}
        check("record_call: an unwritable ledger returns None and does NOT raise",
              record_call(bad, "anthropic", "paid", 10, 10, task="t") is None)
        check("record_call: no pricing file -> None, no raise",
              record_call({"llm_budget": {"pricing_path": os.path.join(td, "missing.json")}},
                          "anthropic", "paid", 10, 10, task="t") is None)
    finally:
        shutil.rmtree(td, ignore_errors=True)

    print("PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] in ("selftest", "--selftest"):
        sys.exit(0 if selftest() else 1)
