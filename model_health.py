#!/usr/bin/env python3
"""
model_health.py — daily API-model health check + self-heal (S56).
===============================================================================
Runs ON a box (CIRRUS/CUMULUS). For every KEYED API provider it live-tests the
currently-configured model with a tiny call. If a model has been retired /
deprecated ("no longer available", 404, model_not_found, ...), it AUTOMATICALLY
picks a current same-tier replacement from the provider's live model list,
verifies the replacement with another tiny call, writes it into
credentials.json, and notifies via Telegram. No human intervention.

Safety:
  * Only swaps on a MODEL-AVAILABILITY error. Auth/network errors -> alert, NO
    change (so a transient outage never rewrites your config).
  * Always live-tests a candidate BEFORE committing it.
  * Stays in the same cheap/fast tier (haiku / mini / flash) via per-provider
    preference filters. Every change is Telegram-notified + logged.
  * --dry-run: report only, change nothing.

Providers covered: anthropic (claude_model), gemini (gemini_model),
openai (openai_model). Grok/DeepSeek included if keyed.

Usage:
  python3 model_health.py            # check + self-heal + notify
  python3 model_health.py --dry-run  # report only
"""
import json
import os
import re
import sys
import tempfile
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import llm_providers as L   # noqa: E402

CREDS_PATH = HERE / "config" / "credentials.json"
DRY = "--dry-run" in sys.argv

# Budget for the liveness probe. NOT 5 (S75): reasoning-first models spend the
# budget on reasoning before emitting any text, so DeepSeek V4 returns an EMPTY
# string at max_tokens=5 and answers "OK" from 20 up — measured on CIRRUS,
# 2026-08-24. A probe tighter than the smallest model's reasoning preamble
# reports a healthy provider as broken every single day.
PROBE_TOKENS = 64

# provider -> the credentials.json field holding its model
MODEL_FIELD = {
    "anthropic": "claude_model",
    "gemini":    "gemini_model",
    "openai":    "openai_model",
    "grok":      "grok_model",
    "deepseek":  "deepseek_model",
}

MODEL_ERR = re.compile(
    r"(no longer available|not found|does not exist|deprecated|model_not_found|"
    r"not supported|invalid model|unknown model|404|decommission)", re.I)

# Billing / out-of-credits / quota-exhausted failures. These are NOT self-healable
# (you can't swap your way out of an unfunded account) — they need Buddy to add
# funds / confirm auto-refill, so they get their own distinct Telegram alert.
BILLING_ERR = re.compile(
    r"(insufficient[_ ]?(quota|balance|funds|credit)|no credits|credit balance|"
    r"out of (credits|balance)|billing|payment required|add (funds|credits)|"
    r"purchase|top ?up|402|quota exceeded|exceeded your current quota)", re.I)


def load():
    return json.loads(CREDS_PATH.read_text())


def save_field(field, value):
    d = load()
    d[field] = value
    fd, tmp = tempfile.mkstemp(dir=str(CREDS_PATH.parent))
    with os.fdopen(fd, "w") as o:
        json.dump(d, o, indent=2)
        o.write("\n")
    os.replace(tmp, str(CREDS_PATH))
    os.chmod(str(CREDS_PATH), 0o600)


def test_model(provider, creds, model):
    """Live 5-token call forcing `model`. Returns (ok, err_str)."""
    c = dict(creds)
    c[MODEL_FIELD[provider]] = model
    if provider == "anthropic":
        c["claude_dev_model"] = ""      # ensure claude_model is the one used
    try:
        r = L.call(provider, "health check", "Reply with the single word OK.",
                   c, max_tokens=PROBE_TOKENS, retries=1)
        txt = (r or "").strip()
        if txt:
            return (True, "")
        # The call SUCCEEDED but returned no text. This used to return
        # ("", False) — an empty err matches neither MODEL_ERR nor BILLING_ERR,
        # so it fell through to `errored` and printed a reason-less failure.
        # Say what happened, so the next reader is not left guessing.
        return (False, f"empty response at max_tokens={PROBE_TOKENS} — the "
                       f"call succeeded but the model emitted no text")
    except Exception as e:
        return (False, str(e))


# ── provider model-list fetchers (return ordered candidate model ids) ──────────
def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def candidates_anthropic(creds):
    try:
        d = _get("https://api.anthropic.com/v1/models",
                 {"x-api-key": creds["anthropic_api_key"],
                  "anthropic-version": "2023-06-01"})
        ids = [m["id"] for m in d.get("data", [])]
    except Exception:
        return []
    haiku = sorted([i for i in ids if "haiku" in i], reverse=True)
    sonnet = sorted([i for i in ids if "sonnet" in i], reverse=True)
    return haiku + sonnet


def candidates_openai(creds):
    try:
        d = _get("https://api.openai.com/v1/models",
                 {"Authorization": f"Bearer {creds['openai_api_key']}"})
        ids = [m["id"] for m in d.get("data", [])]
    except Exception:
        return []
    bad = ("embed", "whisper", "tts", "audio", "image", "dall", "realtime",
           "moderation", "vision", "search", "transcribe")
    chat = [i for i in ids if i.startswith(("gpt", "o1", "o3", "o4"))
            and not any(b in i for b in bad)]
    mini = sorted([i for i in chat if "mini" in i or "nano" in i], reverse=True)
    rest = sorted([i for i in chat if i not in mini], reverse=True)
    return mini + rest


def candidates_gemini(creds):
    try:
        d = _get("https://generativelanguage.googleapis.com/v1beta/models?key="
                 + creds["gemini_api_key"])
        ms = [m["name"].split("/")[-1] for m in d.get("models", [])
              if "generateContent" in m.get("supportedGenerationMethods", [])]
    except Exception:
        return []
    bad = ("image", "tts", "audio", "vision", "embedding", "preview", "lyria",
           "nano-banana", "deep-research")
    flash = [m for m in ms if "flash" in m and not any(b in m for b in bad)]
    # self-updating aliases first, then newest specific flash models
    alias = [m for m in flash if m.endswith("latest")]
    specific = sorted([m for m in flash if m not in alias], reverse=True)
    return alias + specific


def candidates_grok(creds):
    try:
        d = _get("https://api.x.ai/v1/models",
                 {"Authorization": f"Bearer {creds['grok_api_key']}"})
        ids = [m["id"] for m in d.get("data", [])]
    except Exception:
        return []
    bad = ("image", "vision", "embed", "audio", "tts")
    chat = [i for i in ids if "grok" in i and not any(b in i for b in bad)]
    mini = sorted([i for i in chat if "mini" in i or "fast" in i], reverse=True)
    rest = sorted([i for i in chat if i not in mini], reverse=True)
    return mini + rest


CANDIDATES = {
    "anthropic": candidates_anthropic,
    "gemini":    candidates_gemini,
    "openai":    candidates_openai,
    "grok":      candidates_grok,
    "deepseek":  lambda c: [],
}


def tg(msg):
    creds = load()
    tok = creds.get("telegram_bot_token", "")
    uid = str(creds.get("telegram_user_id", "")).strip()
    if not tok or not uid or DRY:
        return
    try:
        data = urllib.parse.urlencode(
            {"chat_id": uid, "text": msg, "parse_mode": "Markdown"}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=15)
    except Exception:
        pass


def node_name():
    try:
        env = os.environ.get("TARGET_ENV", "dev")
        prof = json.loads((HERE / "config" / "node_profiles.json").read_text())
        return prof.get(env, {}).get("node", "CIRRUS")
    except Exception:
        return "CIRRUS"


def main():
    creds = load()
    providers = L.available(creds)
    healthy, healed, broken, errored, needs_funding = [], [], [], [], []

    for p in providers:
        field = MODEL_FIELD.get(p)
        if not field:
            continue
        model = creds.get(field) or ""
        ok, err = test_model(p, creds, model)
        if ok:
            healthy.append(f"{p}={model}")
            continue
        if not MODEL_ERR.search(err):
            # billing/credits exhaustion is distinct from a transient auth/network
            # blip — flag it so Buddy knows to check funding / auto-refill.
            if BILLING_ERR.search(err):
                needs_funding.append(f"{p}={model}: {err[:140]}")
            else:
                errored.append(f"{p}={model}: {err[:120]}")   # auth/network — no change
            continue
        # model-availability failure -> try to self-heal
        chosen = None
        for cand in CANDIDATES.get(p, lambda c: [])(creds):
            if cand == model:
                continue
            cok, _ = test_model(p, creds, cand)
            if cok:
                chosen = cand
                break
        if chosen:
            if not DRY:
                save_field(field, chosen)
                creds = load()
            healed.append(f"{p}: {model} -> {chosen}")
        else:
            broken.append(f"{p}={model}: no working replacement found ({err[:80]})")

    stamp = f"{node_name()} {datetime.now():%Y-%m-%d %H:%M}"
    print(f"[{stamp}] model-health {'(dry-run)' if DRY else ''}")
    for label, items in (("healthy", healthy), ("healed", healed),
                         ("broken", broken), ("needs_funding", needs_funding),
                         ("errored", errored)):
        for it in items:
            print(f"  {label}: {it}")

    # Notify only when something needs attention or changed.
    if healed or broken or errored or needs_funding:
        lines = [f"🩺 *{node_name()} model-health*"]
        if healed:
            lines += ["*auto-healed:*"] + [f"• {h}" for h in healed]
        if needs_funding:
            lines += ["*💳 NEEDS FUNDING — check auto-refill:*"] + \
                     [f"• {n}" for n in needs_funding] + \
                     ["_These accounts appear out of credits. Auto-refill/auto-recharge "
                      "is a per-provider billing setting I can't toggle remotely — log in "
                      "and I'll walk you through turning it on so this doesn't recur._"]
        if broken:
            lines += ["*BROKEN (needs you):*"] + [f"• {b}" for b in broken]
        if errored:
            lines += ["*errors (no change):*"] + [f"• {e}" for e in errored]
        tg("\n".join(lines))

    # Run-status ledger (best-effort).
    try:
        import job_status
        note = (f"{len(healthy)} ok, {len(healed)} healed, {len(broken)} broken, "
                f"{len(needs_funding)} needs-funding, {len(errored)} err")
        job_status.record("modelhealth",
                          ok=(not broken and not errored and not needs_funding),
                          note=note)
    except Exception:
        pass

    sys.exit(1 if (broken or errored or needs_funding) else 0)


def selftest():
    """Offline: verify error classification routes correctly."""
    cases = [
        # (sample provider error text, expect_model_heal, expect_needs_funding)
        ("HTTP 404: model_not_found: claude-x is not found", True, False),
        ("HTTP 400: model 'gpt-x' has been deprecated", True, False),
        ("HTTP 429: You exceeded your current quota, please check your billing", False, True),
        ("HTTP 402: insufficient_quota", False, True),
        ("error: Your credit balance is too low to access the API", False, True),
        ("xAI 403: no credits — add funds to continue", False, True),
        ("HTTP 401: invalid x-api-key", False, False),          # auth -> plain error
        ("read operation timed out", False, False),             # network -> plain error
        # S75: an empty reply is a plain error, NOT a model swap and NOT a
        # funding problem. Swapping models would be wrong (the model works, the
        # budget was too small) and the old code emitted "" here, which showed
        # up as a failure with no reason at all.
        ("empty response at max_tokens=64 — the call succeeded but the model "
         "emitted no text", False, False),
    ]
    fails = 0
    for txt, exp_model, exp_fund in cases:
        m = bool(MODEL_ERR.search(txt))
        f = bool(BILLING_ERR.search(txt)) and not m
        ok = (m == exp_model) and (f == exp_fund)
        print(f"  [{'OK ' if ok else 'FAIL'}] model={m} funding={f} :: {txt[:50]}")
        fails += 0 if ok else 1
    print("PASS" if not fails else f"{fails} FAILURE(S)")
    return 1 if fails else 0


if __name__ == "__main__":
    if "selftest" in sys.argv:
        sys.exit(selftest())
    main()
