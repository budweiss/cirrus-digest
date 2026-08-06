"""ensemble.py — "best answer + cross-check" council synthesis for the client jobs.

Built S57 (2026-08-06) for the CUMULUS migration ensemble step. Gives the
high-stakes client paths (bill_snow_weekly judgment; extensible to others) a
single entry point that is GRACEFULLY DEGRADABLE and driven by a per-box flag,
so the same shared code A/Bs ensemble-vs-baseline by flipping one credential.

Pipeline (only in council mode)
-------------------------------
  1. DRAFT (optional)  — a cheap local qwen first pass, for the judge's context.
  2. CROSS-CHECK       — llm_providers.escalate(mode="council"): every keyed
                         provider answers the SAME prompt independently. This is
                         where one model's hallucinated ENSO figure gets caught.
  3. SYNTHESIZE        — a strong judge (Claude) reconciles the panel into the
                         single best answer, PREFERRING claims multiple members
                         corroborate and FLAGGING disagreements — never averaging.
                         The judge is told to preserve the responders' output
                         format verbatim, so a JSON-demanding caller still gets JSON.

Design principles (match llm_providers.py / llm_budget.py)
---------------------------------------------------------
* GRACEFULLY DEGRADABLE. Any non-council mode, a disabled kill switch, a lone
  usable member, or an over-budget estimate all fall back to the EXISTING
  llm_providers.escalate() single/failover path — same answer the client got
  before. The mode flip is inert until a caller consumes this, and turning it
  back off never changes behavior.
* FAIL-SAFE FOR THE EXTRA SPEND. The council + judge are the *extra* cost over
  baseline. If the per-box budget cap would be exceeded (or a member model is
  unpriced), we DON'T do the expensive thing — we degrade to the cheap baseline,
  which still returns a real answer. Fail-closed on the extra, never on the send.
* STDLIB ONLY (urllib/json). No new deps. Portable CIRRUS (Metal) / CUMULUS (CUDA).
* NEVER RAISES to the caller for a provider/transport problem — it degrades. It
  only raises if there is no keyed provider at all (same as escalate()).

Public API
    best_answer(system, user, creds, *, max_tokens=8000, task="",
                local=None, session_id=None, app_dir=None, mode=None)
        -> (meta: dict, text: str)
      meta = {"mode","members","judge","degraded","reason","est_cost_usd"}
"""

import json
import os
import time
import urllib.request
from pathlib import Path

import llm_providers as L

try:
    import llm_budget as B
except Exception:            # budget guard is optional — absent => unmetered (proceed)
    B = None

_MODEL_FIELD = {
    "anthropic": ("claude_model",),
    "gemini":    ("gemini_model",),
    "grok":      ("grok_model",),
    "openai":    ("openai_model",),
    "deepseek":  ("deepseek_model",),
}
_JUDGE_ORDER = ["anthropic", "openai", "gemini", "grok", "deepseek"]


# ── local draft (optional, best-effort) ────────────────────────────────────────
def _local_draft(system, user, local, timeout=60):
    """A cheap local-model first pass for the judge's context. local = {"host","model"}.
    Best-effort: returns "" on any failure so it never blocks the client path."""
    if not local:
        return ""
    host = (local.get("host") or "http://localhost:11434").rstrip("/")
    model = local.get("model")
    if not model:
        return ""
    timeout = int(local.get("timeout", timeout))
    try:
        body = json.dumps({
            "model": model,
            "prompt": f"{system}\n\n{user}",
            "stream": False,
            "options": {"num_ctx": int(local.get("num_ctx", 8192))},
        }).encode()
        req = urllib.request.Request(f"{host}/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (json.loads(r.read().decode()).get("response") or "").strip()
    except Exception:
        return ""


# ── budget estimate (advisory; degrade if the EXTRA council spend is over cap) ──
def _load_budget(creds, app_dir):
    """Return (cfg, box, ledger_path, models_by_provider) or (None,...) if the
    budget guard isn't configured — in which case council proceeds unmetered
    (same as today's client path, which already spends without a guard)."""
    if B is None:
        return None, None, None, {}
    bud = creds.get("llm_budget") or {}
    app_dir = Path(app_dir or Path(__file__).resolve().parent)
    pricing_path = bud.get("pricing_path", "config/llm_pricing.json")
    if not os.path.isabs(pricing_path):
        pricing_path = str(app_dir / pricing_path)
    cfg = B.load_config(pricing_path)
    if not cfg:
        return None, None, None, {}
    # let per-box caps override the pricing file's defaults
    caps = cfg.get("caps_usd", {})
    for k, src in (("per_session", "per_session_usd"), ("per_day", "per_day_usd"),
                   ("per_call", "per_call_usd")):
        if bud.get(src) is not None:
            caps[k] = float(bud[src])
    cfg["caps_usd"] = caps
    box = bud.get("box", "unknown")
    ledger = bud.get("ledger_path", "out/llm-spend-ledger.jsonl")
    if not os.path.isabs(ledger):
        ledger = str(app_dir / ledger)
    return cfg, box, ledger, {}


def _model_for(provider, creds):
    for f in _MODEL_FIELD.get(provider, ()):
        if creds.get(f):
            return creds[f]
    if provider == "anthropic":
        return creds.get("claude_dev_model") or "claude-sonnet-5"
    return None


def _estimate_cost(providers, judge, in_chars, max_tokens, cfg, creds):
    """Rough USD estimate for the council + judge calls. ~4 chars/token in;
    assume replies up to max_tokens out. Raises if any model is unpriced
    (caller treats that as 'not affordable' -> degrade)."""
    in_tok = max(1, in_chars // 4)
    total = 0.0
    for p in list(providers) + [judge]:
        model = _model_for(p, creds)
        if not model:
            raise ValueError(f"no model for {p}")
        total += B.cost_usd(model, in_tok, max_tokens, cfg)
    return total


# ── synthesis / judge ──────────────────────────────────────────────────────────
_JUDGE_SYSTEM = (
    "You are the SYNTHESIS JUDGE for a council of AI models that each answered the "
    "SAME task independently. Your job is to produce the single best final answer. "
    "Rules: PREFER claims that MULTIPLE members corroborate; be skeptical of any "
    "figure, name, or claim only ONE member makes, especially specific numbers "
    "(treat an un-corroborated number as suspect and drop or down-weight it); do NOT "
    "average or split differences — decide; NEVER invent facts not present in the "
    "members' answers or the task. Match the EXACT output format the members were "
    "asked to use (if they returned JSON, you return the same JSON schema and "
    "nothing else). If the task allows a free-text field for notes, you may note a "
    "material disagreement there, but do not add fields."
)


def _judge_prompt(orig_system, orig_user, members, draft):
    parts = ["=== ORIGINAL TASK (the members all received this) ===",
             "--- system ---", orig_system, "--- user ---", orig_user, ""]
    if draft:
        parts += ["=== LOCAL DRAFT (cheap first pass, UNVETTED — context only) ===",
                  draft[:6000], ""]
    parts.append("=== COUNCIL MEMBER ANSWERS (reconcile these) ===")
    for i, (prov, text) in enumerate(members, 1):
        parts += [f"--- MEMBER {i}: {prov} ---", text, ""]
    parts.append("Now return ONLY the single best final answer, in the members' "
                 "exact required format (JSON in = JSON out, same schema, no prose "
                 "around it).")
    return "\n".join(parts)


# ── public entry ────────────────────────────────────────────────────────────────
def best_answer(system, user, creds, *, max_tokens=8000, task="",
                local=None, session_id=None, app_dir=None, mode=None):
    """Return (meta, text). See module docstring. Degrades to escalate() on any
    council problem; only raises ProviderError if NO provider is keyed."""
    pol = creds.get("dev_escalation", {}) or {}
    mode = (mode or pol.get("mode", "single")).lower()
    ens = creds.get("ensemble", {}) or {}
    meta = {"mode": mode, "members": [], "judge": None, "degraded": False,
            "reason": "", "est_cost_usd": None}

    def _baseline(reason):
        meta["degraded"] = (mode == "council")
        meta["reason"] = reason
        prov, text = L.escalate(system, user, creds, max_tokens=max_tokens,
                                mode=("failover" if mode == "council" else mode))
        meta["members"] = [prov]
        meta["judge"] = prov
        return meta, text

    # kill switch / non-council -> baseline (identical to prior behavior)
    if mode != "council":
        return _baseline("non-council mode")
    if not ens.get("enabled", True):
        return _baseline("ensemble kill switch off (ensemble.enabled=false)")

    avail = L.available(creds)
    if not avail:
        # mirror escalate(): no keys at all is a real error, not a degrade
        raise L.ProviderError("no providers have keys configured")
    if len(avail) < 2:
        return _baseline("only one keyed provider — nothing to cross-check")

    # budget gate on the EXTRA council+judge spend
    cfg, box, ledger, _ = _load_budget(creds, app_dir)
    judge = next((p for p in _JUDGE_ORDER if p in avail), avail[0])
    if cfg is not None:
        try:
            est = _estimate_cost(avail, judge, len(system) + len(user), max_tokens, cfg, creds)
            meta["est_cost_usd"] = round(est, 4)
            ok, why = B.allow(session_id or f"{task}-{int(time.time())}", est, cfg,
                              box=box, ledger_path=ledger)
            if not ok:
                return _baseline(f"budget: {why} — using baseline")
        except Exception as e:
            return _baseline(f"budget uncomputable ({e}) — using baseline")

    # 1) optional local draft (best-effort)
    draft = _local_draft(system, user, local) if local else ""

    # 2) council: every keyed provider answers independently
    try:
        raw = L.escalate(system, user, creds, max_tokens=max_tokens, mode="council")
    except Exception as e:
        return _baseline(f"council call failed ({e})")
    members = [(p, t) for p, t in raw
               if t and not t.startswith("ERROR:") and t.strip()]
    meta["members"] = [p for p, _ in members]
    if len(members) < 2:
        # not enough to cross-check — fall back but reuse a good member if we have one
        if members:
            meta["degraded"] = True
            meta["reason"] = "council returned <2 usable replies"
            meta["judge"] = members[0][0]
            return meta, members[0][1]
        return _baseline("council returned no usable replies")

    # record council member spend (best-effort, estimate-based)
    if cfg is not None and ledger:
        for p, t in members:
            try:
                B.record(session_id or task, p, _model_for(p, creds),
                         (len(system) + len(user)) // 4, len(t) // 4, cfg,
                         box=box, ledger_path=ledger, task=f"{task}:council")
            except Exception:
                pass

    # 3) synthesize with the judge (Claude preferred)
    try:
        vetted = L.call(judge, _JUDGE_SYSTEM,
                        _judge_prompt(system, user, members, draft),
                        creds, max_tokens=max_tokens)
        meta["judge"] = judge
    except Exception as e:
        # judge failed — return the answer from the most-preferred available member
        pref = {p: i for i, p in enumerate(_JUDGE_ORDER)}
        best = min(members, key=lambda m: pref.get(m[0], 99))
        meta["degraded"] = True
        meta["reason"] = f"judge {judge} failed ({e}); used {best[0]} member"
        meta["judge"] = best[0]
        return meta, best[1]
    if not (vetted and vetted.strip()):
        pref = {p: i for i, p in enumerate(_JUDGE_ORDER)}
        best = min(members, key=lambda m: pref.get(m[0], 99))
        meta["degraded"] = True
        meta["reason"] = "judge returned empty; used member answer"
        meta["judge"] = best[0]
        return meta, best[1]
    if cfg is not None and ledger:
        try:
            B.record(session_id or task, judge, _model_for(judge, creds),
                     (len(system) + len(user)) // 4, len(vetted) // 4, cfg,
                     box=box, ledger_path=ledger, task=f"{task}:judge")
        except Exception:
            pass
    meta["reason"] = f"council of {len(members)} → {judge} synthesis"
    return meta, vetted


# ── self-test (python3 ensemble.py selftest) — offline, monkeypatched ───────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        B = None    # isolate: force unmetered in the offline selftest (no ledger writes)
        ok = True

        def check(name, cond):
            global ok
            print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
            ok = ok and cond

        base_creds = {"anthropic_api_key": "x", "gemini_api_key": "y",
                      "openai_api_key": "z", "claude_model": "claude-sonnet-5",
                      "gemini_model": "gemini-2.0-flash", "openai_model": "gpt-4o-mini"}

        # non-council mode -> pure escalate passthrough, not degraded
        _calls = {"escalate": []}
        def fake_escalate(s, u, c, max_tokens=0, mode=None, order=None):
            _calls["escalate"].append(mode)
            if mode == "council":
                return [("anthropic", '{"answer": 1}'), ("gemini", '{"answer": 1}'),
                        ("openai", '{"answer": 2}')]
            return ("anthropic", "single-answer")
        L.escalate = fake_escalate
        L.available = lambda c: [p for p in ["anthropic", "gemini", "grok", "openai", "deepseek"]
                                 if c.get(p + "_api_key")]
        L.call = lambda prov, s, u, c, max_tokens=0: '{"answer": 1, "note": "reconciled"}'

        m, t = best_answer("sys", "usr", dict(base_creds, dev_escalation={"mode": "single"}))
        check("single mode -> passthrough text", t == "single-answer")
        check("single mode -> not degraded", m["degraded"] is False)

        # council mode with 3 providers -> judge synthesis, not degraded
        m, t = best_answer("sys", "usr", dict(base_creds, dev_escalation={"mode": "council"}))
        check("council -> judge output", t == '{"answer": 1, "note": "reconciled"}')
        check("council -> members recorded", set(m["members"]) == {"anthropic", "gemini", "openai"})
        check("council -> judge is anthropic", m["judge"] == "anthropic")
        check("council -> not degraded", m["degraded"] is False)

        # kill switch forces baseline
        m, t = best_answer("sys", "usr", dict(base_creds, dev_escalation={"mode": "council"},
                                              ensemble={"enabled": False}))
        check("kill switch -> baseline text", t == "single-answer")
        check("kill switch -> degraded flag set", m["degraded"] is True)

        # only one keyed provider -> baseline (nothing to cross-check)
        one = {"anthropic_api_key": "x", "claude_model": "claude-sonnet-5",
               "dev_escalation": {"mode": "council"}}
        m, t = best_answer("sys", "usr", one)
        check("one provider -> baseline", t == "single-answer" and m["degraded"] is True)

        # council returns <2 usable -> reuse the single good member
        L.escalate = lambda s, u, c, max_tokens=0, mode=None, order=None: (
            [("anthropic", "good"), ("gemini", "ERROR: boom"), ("openai", "  ")]
            if mode == "council" else ("anthropic", "single-answer"))
        m, t = best_answer("sys", "usr", dict(base_creds, dev_escalation={"mode": "council"}))
        check("council <2 usable -> reuse member", t == "good" and m["degraded"] is True)

        # judge prompt carries members + JSON-format instruction
        jp = _judge_prompt("S", "U", [("anthropic", '{"a":1}'), ("gemini", '{"a":2}')], "draftfoo")
        check("judge prompt includes members", "MEMBER 1: anthropic" in jp and "MEMBER 2: gemini" in jp)
        check("judge prompt includes local draft", "draftfoo" in jp)
        check("judge prompt demands same format", "JSON in = JSON out" in jp)

        print("PASS" if ok else "FAIL")
        sys.exit(0 if ok else 1)
