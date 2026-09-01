"""llm_providers.py — multi-provider LLM abstraction for CIRRUS/CUMULUS self-improvement.

Scaffolded S44 (2026-07-22). Lets the self-improvement builder (dev_agent) escalate
to ANY linked frontier model — not just Claude — for help solving improvements.

Design principles
-----------------
* DORMANT UNTIL KEYED. A provider is used only if its API key is present in
  credentials.json. No key => the provider is silently skipped. So dropping this
  module in changes nothing until you add keys + activate it in dev_agent.
* ALIGNED WITH THE EXISTING S41 "LLM panel" CREDENTIAL FIELDS. Same key/model names
  already used by cirrus_bot.call_gemini/call_grok/call_claude and the template:
  anthropic_api_key/claude_dev_model(or claude_model), gemini_api_key/gemini_model,
  grok_api_key/grok_model, openai_api_key/openai_model, deepseek_api_key/deepseek_model.
* BACKWARD COMPATIBLE with dev_agent's Claude call (same api.anthropic.com/v1/messages
  request shape). STDLIB ONLY (urllib) — no new dependencies.

Escalation modes (credentials.json -> dev_escalation.mode)
    "single"   — call the first available provider in `order` (default).
    "failover" — try providers in `order` until one succeeds.
    "council"  — query EVERY available provider; return all replies to compare/vote.
Example:  "dev_escalation": {"mode": "single",
                             "order": ["anthropic","gemini","grok","openai","deepseek"]}

Public API
    available(creds)                         -> [provider,...] that have keys
    call(provider, system, user, creds, ...) -> str            (one provider)
    escalate(system, user, creds, ...)       -> (provider, str) | [(provider, str),...]
"""

import json
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ORDER = ["anthropic", "gemini", "grok", "openai", "deepseek"]
_TIMEOUT = 120

_KEY_FIELD = {
    # S73: "ollama" is DELIBERATELY absent from DEFAULT_ORDER, and its key field
    # (ollama_url) is not in credentials.json today. available() filters on that
    # field, so the local provider cannot be selected by accident — a caller has
    # to pass order=["ollama"] explicitly. Two independent gates, because this
    # file is on the path of every heavy job on the box and a routing change
    # nobody asked for is the worst kind.
    "ollama":    "ollama_url",
    "anthropic": "anthropic_api_key",
    "gemini":    "gemini_api_key",
    "grok":      "grok_api_key",
    "openai":    "openai_api_key",
    "deepseek":  "deepseek_api_key",
}


class ProviderError(RuntimeError):
    """Any provider call/config failure (missing key, HTTP error, bad response)."""


# ── transport ─────────────────────────────────────────────────────────────────
def _http_post(url, headers, body, timeout=_TIMEOUT):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read()[:300] if hasattr(e, "read") else b""
        raise ProviderError(f"HTTP {e.code}: {detail!r}")
    except Exception as e:  # noqa: BLE001 — normalize all transport errors
        raise ProviderError(str(e))


def _openai_compatible(url, key, model, system, user, max_tokens):
    """OpenAI Chat Completions shape — shared by OpenAI, xAI (Grok), DeepSeek."""
    resp = _http_post(
        url,
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        {"model": model, "max_tokens": max_tokens,
         "messages": [{"role": "system", "content": system},
                      {"role": "user", "content": user}]},
    )
    return resp["choices"][0]["message"]["content"]


# ── per-provider adapters (build request + parse reply) ─────────────────────────
# ── prompt caching (S75) ─────────────────────────────────────────────────────
# docs/PAID-ACCESS-REGISTRY.md has flagged "Prompt caching OFF = a cost-savings
# lever if needed later" since 2026-08-10, and nothing in this repo has ever set
# cache_control. Anthropic is our largest LLM line, and every call re-sends the
# whole system prompt at full input price.
#
# Anthropic will not cache a prefix below ~1024 tokens; it silently ignores
# cache_control rather than erroring. Gating on length keeps short calls on the
# exact request shape they already use, so the change is confined to the calls
# that can actually benefit.
_CACHE_MIN_CHARS = 4000        # ~1k tokens, Anthropic's minimum cacheable prefix
_CACHE_LEDGER = Path.home() / "projects/cirrus-digest/logs/llm_cache_usage.jsonl"


def _record_usage(provider, model, usage, cached):
    """Append one line of token accounting. NEVER raises.

    Exists because turning caching on and ASSUMING it worked is exactly the
    failure this project keeps auditing. Caching only pays when the prefix
    repeats byte-identically; if `cache_read` stays at 0 across a week, the
    prefixes are not repeating and the lever is worthless HERE regardless of
    what it does elsewhere. This ledger is what makes that answerable.
    """
    try:
        rec = {
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "provider": provider,
            "model": model,
            "cache_requested": bool(cached),
            "input": usage.get("input_tokens"),
            "output": usage.get("output_tokens"),
            "cache_write": usage.get("cache_creation_input_tokens"),
            "cache_read": usage.get("cache_read_input_tokens"),
        }
        _CACHE_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with open(_CACHE_LEDGER, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _anthropic(creds, system, user, max_tokens):
    key = creds.get("anthropic_api_key")
    if not key:
        raise ProviderError("no anthropic_api_key")
    model = creds.get("claude_dev_model") or creds.get("claude_model") or "claude-sonnet-5"

    # creds["prompt_cache"] = false is the off switch if this ever misbehaves.
    want_cache = (creds.get("prompt_cache", True)
                  and len(system or "") >= _CACHE_MIN_CHARS)
    if want_cache:
        sys_field = [{"type": "text", "text": system,
                      "cache_control": {"type": "ephemeral"}}]
    else:
        sys_field = system

    resp = _http_post(
        "https://api.anthropic.com/v1/messages",
        {"x-api-key": key, "anthropic-version": "2023-06-01",
         "content-type": "application/json"},
        {"model": model, "max_tokens": max_tokens, "system": sys_field,
         "messages": [{"role": "user", "content": user}]},
    )
    _record_usage("anthropic", model, resp.get("usage") or {}, want_cache)
    return "".join(b.get("text", "") for b in resp.get("content", [])
                   if b.get("type") == "text")


def _gemini(creds, system, user, max_tokens):
    key = creds.get("gemini_api_key")
    if not key:
        raise ProviderError("no gemini_api_key")
    model = creds.get("gemini_model")
    if not model:
        raise ProviderError("no gemini_model set in credentials.json")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={key}")
    resp = _http_post(
        url, {"Content-Type": "application/json"},
        {"system_instruction": {"parts": [{"text": system}]},
         "contents": [{"role": "user", "parts": [{"text": user}]}],
         "generationConfig": {"maxOutputTokens": max_tokens}},
    )
    # S91: this was `resp["candidates"][0]["content"]["parts"]`, unguarded.
    # A candidate that finished on MAX_TOKENS (or a safety block) carries a
    # `content` with NO `parts` at all, so the KeyError surfaced as the bare
    # string 'parts' — an error message that names neither the provider, the
    # cause, nor the fix. cirrus-modelhealth failed on it every morning from
    # 2026-08-31 and said only "errored: gemini=...: 'parts'".
    #
    # Why it started: gemini-flash-latest is a THINKING model, and thinking
    # tokens are drawn from maxOutputTokens before any text is emitted. On the
    # health probe's trivial prompt it spends 51-61 tokens thinking (measured
    # on cumulus1, 2026-09-01, 12 runs), so a 64-token budget left room for the
    # answer only about half the time — 5 of 12 runs came back with no parts.
    # Same class as the S74/S75 DeepSeek finding, one provider over.
    cand = (resp.get("candidates") or [{}])[0]
    parts = ((cand.get("content") or {}).get("parts")) or []
    if not parts:
        usage = resp.get("usageMetadata") or {}
        raise ProviderError(
            f"gemini returned no content: finishReason="
            f"{cand.get('finishReason')!r}, "
            f"{usage.get('thoughtsTokenCount', 0)} thinking token(s) of a "
            f"{max_tokens}-token budget. If this is MAX_TOKENS the budget is "
            f"below the model's thinking preamble — raise max_tokens.")
    return "".join(p.get("text", "") for p in parts)


def _grok(creds, system, user, max_tokens):
    key = creds.get("grok_api_key")
    if not key:
        raise ProviderError("no grok_api_key")
    model = creds.get("grok_model")
    if not model:
        raise ProviderError("no grok_model set in credentials.json")
    return _openai_compatible("https://api.x.ai/v1/chat/completions",
                              key, model, system, user, max_tokens)


def _openai(creds, system, user, max_tokens):
    key = creds.get("openai_api_key")
    if not key:
        raise ProviderError("no openai_api_key")
    model = creds.get("openai_model")
    if not model:
        raise ProviderError("no openai_model set in credentials.json")
    return _openai_compatible("https://api.openai.com/v1/chat/completions",
                              key, model, system, user, max_tokens)


def _deepseek(creds, system, user, max_tokens):
    key = creds.get("deepseek_api_key")
    if not key:
        raise ProviderError("no deepseek_api_key")
    model = creds.get("deepseek_model")
    if not model:
        raise ProviderError("no deepseek_model set in credentials.json")
    return _openai_compatible("https://api.deepseek.com/v1/chat/completions",
                              key, model, system, user, max_tokens)


def _ollama(creds, system, user, max_tokens):
    """The LOCAL model, via Ollama's OpenAI-compatible endpoint.

    S73. Both boxes held qwen2.5:72b and neither ever called it: llm_providers.py
    had no local backend at all, so 1,218 cloud calls went out in 7 days while
    two 47 GB models sat idle. This is the missing backend.

    ★ S92 — THAT IS NO LONGER TRUE, AND THE STALE VERSION NEARLY COST A CLIENT
    JOB. Since S78/S79, halftime_catalogue, halftime_routing and promise_detect
    all call `call("ollama", ...)` EXPLICITLY, which bypasses available() (ollama
    is absent from DEFAULT_ORDER, so available() never lists it — that gate stops
    accidental ROUTING, not a deliberate call). The two boxes now differ:

      CIRRUS  — ollama_url and ollama_model are ABSENT, so this raises
                immediately and every caller escalates to cloud. The local
                provider is genuinely unused here. Its 72B was deleted in S92.
      CUMULUS — ollama_url is set and ollama_model IS qwen2.5:72b. Measured in
                the live halftime snapshot: 120 entries "ollama (local)" vs 16
                "anthropic (escalated)". That 47 GB model is doing ~88% of the
                extraction on Justin's job. Deleting it would not crash anything
                — it would silently convert every entity to a PAID Claude call
                and peg the escalation-rate metric at 100%, which is worse than
                a crash because nothing would report it.

    So: before removing a local model, check `ollama_model` in that box's
    credentials.json and the `extracted_by` counts in its output. Do not reason
    from this docstring's first paragraph, which was true in S73 and is not now.

    It exists to be MEASURED, not to be routed to. Adding it to DEFAULT_ORDER,
    or to the council, is a separate decision that should follow evidence — see
    local_bench.py, which replays real prompts through it and scores the answers
    against what the cloud returned.

    No API key: Ollama is unauthenticated on loopback. `ollama_url` doubles as
    the enable flag, which is why it is the key field.
    """
    url = creds.get("ollama_url")
    if not url:
        raise ProviderError("no ollama_url — local provider not enabled")
    model = creds.get("ollama_model")
    if not model:
        raise ProviderError("no ollama_model set in credentials.json")
    # Same Chat Completions shape as OpenAI/Grok/DeepSeek; the key is ignored.
    return _openai_compatible(url.rstrip("/") + "/v1/chat/completions",
                              "local", model, system, user, max_tokens)


_PROVIDERS = {
    "ollama":    _ollama,
    "anthropic": _anthropic,
    "gemini":    _gemini,
    "grok":      _grok,
    "openai":    _openai,
    "deepseek":  _deepseek,
}


# ── public API ──────────────────────────────────────────────────────────────────
def available(creds):
    """Providers that have an API key configured, in DEFAULT_ORDER order."""
    return [p for p in DEFAULT_ORDER if creds.get(_KEY_FIELD[p])]


def call(provider, system, user, creds, max_tokens=16384, retries=1):
    """Call ONE provider by name. Returns reply text. Raises ProviderError.

    Retries once (retries=1) on an EMPTY/whitespace reply. Guards the S47 #8
    case where Claude returned 0 chars on a live call — a transient empty reply
    shouldn't silently cede the primary provider to failover. Transport/config
    failures still raise immediately (ProviderError, no retry); the caller
    handles those. Returns the (possibly still-empty) reply after the retries.
    """
    if provider not in _PROVIDERS:
        raise ProviderError(f"unknown provider: {provider}")
    reply = ""
    for _ in range(retries + 1):
        reply = _PROVIDERS[provider](creds, system, user, max_tokens) or ""
        if reply.strip():
            return reply
    return reply


def escalate(system, user, creds, max_tokens=16384, mode=None, order=None):
    """Policy-driven call across configured providers.

    Reads defaults from creds['dev_escalation'] = {"mode":..., "order":[...]}.
      single   -> (provider, text)     first available in order
      failover -> (provider, text)     try in order until one succeeds
      council  -> [(provider, text_or_'ERROR: ...'), ...]  every available
    Raises ProviderError if no provider has a key.
    """
    pol = creds.get("dev_escalation", {}) or {}
    mode = mode or pol.get("mode", "single")
    order = order or pol.get("order") or DEFAULT_ORDER
    avail = [p for p in order if creds.get(_KEY_FIELD.get(p, "")) and p in _PROVIDERS]
    if not avail:
        raise ProviderError("no providers have keys configured")

    if mode == "council":
        out = []
        for p in avail:
            try:
                out.append((p, call(p, system, user, creds, max_tokens)))
            except ProviderError as e:
                out.append((p, f"ERROR: {e}"))
        return out

    if mode == "failover":
        last = None
        for p in avail:
            try:
                return (p, call(p, system, user, creds, max_tokens))
            except ProviderError as e:
                last = e
        raise ProviderError(f"all providers failed; last error: {last}")

    # "single" (default)
    p = avail[0]
    return (p, call(p, system, user, creds, max_tokens))


# ── self-test (python3 llm_providers.py selftest) ─────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        _ok = True

        def check(name, cond):
            global _ok
            print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
            _ok = _ok and cond

        # retry-on-empty: provider returns '' then '  ' then a real reply
        _seq = iter(["", "  ", "real answer"])
        _PROVIDERS["anthropic"] = lambda c, s, u, m: next(_seq)
        got = call("anthropic", "sys", "usr", {"anthropic_api_key": "x"}, retries=2)
        check("call: retries past empty/whitespace to a real reply", got == "real answer")

        # retry exhausted -> returns the empty reply (caller fails over), no raise
        _PROVIDERS["anthropic"] = lambda c, s, u, m: ""
        check("call: returns '' after retries exhausted (no raise)",
              call("anthropic", "s", "u", {"anthropic_api_key": "x"}, retries=1) == "")

        # available() reflects only keyed providers, in DEFAULT_ORDER
        check("available: only keyed", available({"openai_api_key": "y"}) == ["openai"])
        try:
            call("nope", "s", "u", {})
            _unknown_raised = False
        except ProviderError:
            _unknown_raised = True
        check("call: unknown provider raises", _unknown_raised)

        print("PASS" if _ok else "FAIL")
        sys.exit(0 if _ok else 1)
