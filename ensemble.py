"""ensemble.py — "best answer + cross-check" council synthesis for the client jobs.

Built S57 (2026-08-06) for the CUMULUS migration ensemble step. Gives the
high-stakes client paths (bill_snow_weekly judgment; extensible to others) a
single entry point that is GRACEFULLY DEGRADABLE and driven by a per-box flag,
so the same shared code A/Bs ensemble-vs-baseline by flipping one credential.

Pipeline (only in council mode)
-------------------------------
  1. DRAFT (optional)  — a cheap local first pass, for the judge's context: the
                         TP=2 vLLM endpoint when this box has `vllm_url` (S131,
                         so cumulus1 never loads a second ollama model beside
                         the 27B fallback), else the ollama model in `local`.
                         Best-effort either way; falls through on any miss.
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
      meta = {"mode","members","judge","degraded","reason","est_cost_usd",
              "draft_by"}   # "vllm" | "ollama" | "" — which engine drafted (S131)
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

# S131: answer budget for the vLLM draft. The endpoint runs with thinking ON at
# reasoning_effort=medium, and reasoning tokens count against max_tokens (S125
# measured 291-2,688 per block at medium), so this must leave room for the
# answer after the thinking. _judge_prompt() truncates the draft to 6,000 CHARS
# anyway, so a longer draft is never seen by the judge. Bounded above by the
# provider's own 900 s `vllm_timeout`.
DRAFT_MAX_TOKENS = 6000


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
    assume replies up to max_tokens out. A model missing from llm_pricing.json
    (e.g. a name the model-health self-heal swapped in) is estimated at the
    CONSERVATIVE unknown-model rate rather than aborting — a pricing gap must
    never silently disable the council. Raises only if a provider has NO model."""
    in_tok = max(1, in_chars // 4)
    unknown = float((cfg or {}).get("unknown_model_out_per_m", 25.0))
    total = 0.0
    for p in list(providers) + [judge]:
        model = _model_for(p, creds)
        if not model:
            raise ValueError(f"no model for {p}")
        try:
            total += B.cost_usd(model, in_tok, max_tokens, cfg)
        except Exception:
            total += (in_tok + max_tokens) * unknown / 1_000_000
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
                local=None, session_id=None, app_dir=None, mode=None,
                keep_answers=False):
    """Return (meta, text). See module docstring. Degrades to escalate() on any
    council problem; only raises ProviderError if NO provider is keyed."""
    pol = creds.get("dev_escalation", {}) or {}
    mode = (mode or pol.get("mode", "single")).lower()
    ens = creds.get("ensemble", {}) or {}
    meta = {"mode": mode, "members": [], "judge": None, "degraded": False,
            "reason": "", "est_cost_usd": None, "draft_by": ""}

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

    # 1) optional local draft (best-effort). S131: the TP=2 endpoint FIRST when
    # this box has one (cumulus1 since S127), then the ollama draft exactly as
    # before. Why: Bill's Monday run was the one job that loaded a SECOND ollama
    # model (qwen3-coder:30b, 18 GB) beside the 27B fallback, and that 18 GB is
    # what pinned the endpoint at 0.35 utilisation and blocked a larger model --
    # LOCAL-MODEL-PLAN.md section 2. The judge writes the answer; the draft is
    # context, so nothing Bill sees changes. `draft_by` is recorded because the
    # gate is "the draft came from vLLM and ollama stayed idle", and a counter
    # that is not printed is not measured (S103). An EMPTY vLLM reply is a miss,
    # not a draft -- call() returns "" after its retry rather than raising.
    draft = ""
    if creds.get("vllm_url"):
        try:
            draft = (L.call("vllm", system, user, creds,
                            max_tokens=DRAFT_MAX_TOKENS) or "").strip()
        except Exception:
            draft = ""
        if draft:
            meta["draft_by"] = "vllm"
    if not draft and local:
        draft = _local_draft(system, user, local)
        if draft:
            meta["draft_by"] = "ollama"

    # 2) council: every keyed provider answers independently
    try:
        raw = L.escalate(system, user, creds, max_tokens=max_tokens, mode="council")
    except Exception as e:
        return _baseline(f"council call failed ({e})")
    members = [(p, t) for p, t in raw
               if t and not t.startswith("ERROR:") and t.strip()]
    meta["members"] = [p for p, _ in members]
    if keep_answers:
        # OPT-IN, and that direction matters: several callers log `meta`, and
        # silently fattening it with full model answers would bloat their logs.
        # Asked for by callers that must AUDIT the judge -- alopecia_brief
        # checks that a real disagreement was surfaced rather than smoothed.
        meta["answers"] = list(members)
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


# ── self-test (python3 ensemble.py --selftest) — offline, monkeypatched ────────
def selftest():
    """Exercise the decision-making functions in best_answer()/_estimate_cost()/
    _judge_prompt() with explicit inputs and expected outputs, using monkeypatched
    llm_providers so it runs fully offline (no network, no live keys, no ledger
    writes). Returns True on success, False on failure. Never raises — every
    assertion is recorded via check() and the aggregate result is returned."""
    global B, _local_draft
    B = None    # isolate: force unmetered in the offline selftest (no ledger writes)
    ok = True

    def check(name, cond):
        nonlocal ok
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

    # S95: keep_answers is the EVIDENCE behind alopecia_brief's claim that a
    # council disagreement was surfaced rather than smoothed by the judge. If it
    # silently stopped populating, the brief would still render and the audit
    # file would just be missing -- a silent loss of the thing that makes the
    # claim checkable. Both directions are pinned, because the default must stay
    # off: several callers log meta and would have their logs bloated.
    mk, _ = best_answer("sys", "usr", dict(base_creds, dev_escalation={"mode": "council"}),
                        keep_answers=True)
    check("keep_answers returns the raw member answers", len(mk.get("answers") or []) == 3)
    check("...as (provider, text) pairs, not just names",
          all(isinstance(x, tuple) and len(x) == 2 for x in mk["answers"]))
    check("...and they are the COUNCIL's text, not the judge's",
          dict(mk["answers"])["openai"] == '{"answer": 2}')
    md, _ = best_answer("sys", "usr", dict(base_creds, dev_escalation={"mode": "council"}))
    check("default does NOT carry answers (callers log meta)", "answers" not in md)

    m, t = best_answer("sys", "usr", dict(base_creds, dev_escalation={"mode": "single"}))
    check("single mode -> passthrough text", t == "single-answer")
    check("single mode -> not degraded", m["degraded"] is False)

    # council mode with 3 providers -> judge synthesis, not degraded
    m, t = best_answer("sys", "usr", dict(base_creds, dev_escalation={"mode": "council"}))
    check("council -> judge output", t == '{"answer": 1, "note": "reconciled"}')
    check("council -> members recorded", set(m["members"]) == {"anthropic", "gemini", "openai"})
    check("council -> judge is anthropic", m["judge"] == "anthropic")
    check("council -> not degraded", m["degraded"] is False)

    # S131: the vLLM-first draft. Three shapes, mirroring the halftime jobs'
    # Phase B tests: (i) vLLM answers -> it drafts and ollama is NOT touched;
    # (ii) vLLM raises, and (iii) vLLM returns EMPTY -> both fall through to the
    # ollama draft; (iv) no vllm_url -> vLLM is never called, byte-for-byte the
    # old path. Asserted on BEHAVIOUR the judge can see -- the draft text must
    # actually appear in the judge's prompt -- not only on the meta flag (S84:
    # a flag that is set is not proof the thing happened).
    _saved_call, _saved_draft = L.call, _local_draft
    _seen = {"provs": [], "judge_u": "", "vllm_max": None}
    _vllm = {"reply": "VDRAFT", "raise": False}
    def _fake_call(prov, s, u, c, max_tokens=0, retries=1):
        _seen["provs"].append(prov)
        if prov == "vllm":
            _seen["vllm_max"] = max_tokens   # what the draft call actually asked for
            if _vllm["raise"]:
                raise L.ProviderError("endpoint down")
            return _vllm["reply"]
        _seen["judge_u"] = u          # the judge's prompt carries the draft
        return '{"answer": 1, "note": "reconciled"}'
    def _fake_draft(s, u, local, timeout=60):
        _seen["provs"].append("ollama-draft")
        return "ODRAFT"
    L.call, _local_draft = _fake_call, _fake_draft
    _vc = dict(base_creds, dev_escalation={"mode": "council"},
               vllm_url="http://v", vllm_model="m")
    _loc = {"host": "http://o", "model": "qwen3-coder:30b"}
    try:
        # (i) vLLM answers
        _seen.update(provs=[], judge_u="")
        m, _ = best_answer("sys", "usr", _vc, local=_loc)
        check("vllm draft: draft_by == vllm", m["draft_by"] == "vllm")
        check("vllm draft: ollama draft NOT called", "ollama-draft" not in _seen["provs"])
        check("vllm draft: the vLLM text reached the judge",
              "VDRAFT" in _seen["judge_u"] and "ODRAFT" not in _seen["judge_u"])
        check("vllm draft: capped at DRAFT_MAX_TOKENS, not the council's 8000",
              _seen["vllm_max"] == DRAFT_MAX_TOKENS)
        # (ii) vLLM raises -> ollama draft
        _seen.update(provs=[], judge_u="")
        _vllm["raise"] = True
        m, _ = best_answer("sys", "usr", _vc, local=_loc)
        check("vllm down: falls through to ollama draft", m["draft_by"] == "ollama")
        check("vllm down: vLLM was tried first", _seen["provs"][0] == "vllm")
        check("vllm down: the ollama text reached the judge", "ODRAFT" in _seen["judge_u"])
        # (iii) vLLM returns EMPTY -> a miss, not a draft
        _seen.update(provs=[], judge_u=""); _vllm.update(reply="   ", **{"raise": False})
        m, _ = best_answer("sys", "usr", _vc, local=_loc)
        check("vllm empty: treated as a miss, ollama drafts", m["draft_by"] == "ollama")
        check("vllm empty: blank text never reached the judge as the draft",
              "ODRAFT" in _seen["judge_u"])
        # (iv) no vllm_url -> the old path, vLLM never called
        _seen.update(provs=[], judge_u="")
        m, _ = best_answer("sys", "usr", dict(base_creds, dev_escalation={"mode": "council"}),
                           local=_loc)
        check("no vllm_url: vLLM never called", "vllm" not in _seen["provs"])
        check("no vllm_url: ollama draft as before", m["draft_by"] == "ollama")
        _seen.update(provs=[], judge_u="")
        m, _ = best_answer("sys", "usr", dict(base_creds, dev_escalation={"mode": "council"}))
        check("no vllm_url, no local: no draft at all", m["draft_by"] == ""
              and "vllm" not in _seen["provs"] and "ollama-draft" not in _seen["provs"])
    finally:
        L.call, _local_draft = _saved_call, _saved_draft

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

    # budget estimate must TOLERATE an unpriced model (conservative fallback,
    # never abort) — this is the S57 Phase-A finding (gemini-flash-latest gap).
    import llm_budget as _RB
    _cfg = {"models": {"claude-sonnet-5": {"in": 3.0, "out": 15.0}},
            "unknown_model_out_per_m": 25.0,
            "caps_usd": {"per_call": 100, "per_session": 100, "per_day": 200}}
    _creds = {"claude_model": "claude-sonnet-5", "gemini_model": "brand-new-unpriced"}
    _saveB, B = B, _RB
    try:
        est = _estimate_cost(["anthropic", "gemini"], "anthropic", 400, 100, _cfg, _creds)
        check("estimate tolerates unpriced model (no raise, >0)", est > 0)
    finally:
        B = _saveB

    print("PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] in ("selftest", "--selftest"):
        sys.exit(0 if selftest() else 1)
