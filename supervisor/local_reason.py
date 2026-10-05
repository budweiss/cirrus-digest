"""S375 Shadow reasoning — a deterministic SECOND OPINION pass, local models only.

Buddy (2026-10-05, S375): give Skywarden and Hermes local-first reasoning with
cloud fallback; Phase 1 is SHADOW MODE — a free local second opinion recorded
beside every production (cloud) reasoning pass so agreement can be measured
before any default flips. This module deliberately:

* runs BEFORE any default changes and never replaces the production pass,
* NEVER executes anything (no tools, no sends — text verdict only),
* never sends, never retries the production pass, never raises into the
  run loop (fail-open to "shadow did not run", always logged),
* uses ONLY local endpoints: C2's dedicated Qwen 27B FP8 (64K ctx, mostly
  idle — proven reachable from the supervisor account), then C1's loopback
  GPT-OSS 120B (32K). NO cloud fallback here by design: the cloud side of the
  comparison is the production pass itself.

Gate: STATE_DIR/shadow-reason.enabled must EXIST (mirrors the fleet's
hermes.enabled marker pattern). Its absence means the shadow is OFF.
"""
import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

STATE_DIR = Path("/opt/cumulus-supervisor/state")
SHADOW_MARK = STATE_DIR / "shadow-reason.enabled"
SHADOW_LOG = STATE_DIR / "shadow-reason.jsonl"
WALL_BUDGET_S = 120.0

# Endpoint order: C2 Qwen first — 64K context for journal-bearing evidence on a
# dedicated GPU that incident reasoning barely shares. C1 GPT-OSS loopback second.
ENDPOINTS = (
    ("c2-qwen-64k", "http://192.168.100.11:8000/v1/chat/completions", "qwen3.8-27b-fp8"),
    ("c1-gptoss-32k", "http://127.0.0.1:8000/v1/chat/completions", "gpt-oss:120b"),
)

_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

RULES = (
    "You are a SHADOW second opinion for the CUMULUS supervisor (Skywarden). "
    "Given the trigger and evidence, reply with: (1) verdict: real-or-false-alarm, "
    "(2) one concrete next action, (3) confidence low/medium/high. Under 150 words. "
    "Never claim to have executed anything."
)


def _post(url, model, user_prompt):
    body = json.dumps({"model": model, "max_tokens": 512, "temperature": 0.2,
                       "messages": [{"role": "system", "content": RULES},
                                    {"role": "user", "content": user_prompt}]}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    t0 = time.monotonic()
    with _HTTP.open(req, timeout=WALL_BUDGET_S + 10) as resp:
        d = json.load(resp)
    wall = round(time.monotonic() - t0, 3)
    usage = d.get("usage") or {}
    text = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return {"ok": bool(text.strip()), "wall_seconds": wall, "model": model,
            "in_tok": usage.get("prompt_tokens"), "out_tok": usage.get("completion_tokens"),
            "answer": text.strip()}


def shadow_reason(trigger: str, detail: str, evidence: str, *, key: str = "") -> dict:
    """One shadow comparison pass. Returns the row it logged; never raises."""
    row = {"mode": "shadow", "trigger": str(trigger)[:120],
           "detail": str(detail)[:200], "key": str(key)[:64],
           "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "ok": False}
    if len(str(detail)) > 4000:   # bounded evidence budget, hard floor
        detail = str(detail)[:4000]
    for name, url, model in ENDPOINTS:
        try:
            out = _post(url, model, f"TRIGGER: {trigger}\nDETAIL: {detail}\n"
                                    f"EVIDENCE: {evidence[:4000]}")
            row.update(out, endpoint=name)
            break
        except Exception as e:
            # Fail-open ALWAYS — unexpected types included. The shadow must
            # never raise into the heartbeat loop (same rule the package
            # applies to every other monitor); the row records what happened.
            row.update({"endpoint": name, "error": type(e).__name__})
            continue
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(SHADOW_LOG, "a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass
    return row


def maybe_shadow(trigger: str, detail: str, evidence: str, *, key: str = "") -> dict:
    """Marker-gated entry point for the run loop. Never raises; off -> {'ran': False}."""
    if not SHADOW_MARK.exists():
        return {"ran": False}
    try:
        return {"ran": True, **shadow_reason(trigger, detail, evidence, key=key)}
    except Exception as e:
        return {"ran": True, "ok": False, "error": type(e).__name__}
