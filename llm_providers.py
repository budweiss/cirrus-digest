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

DEFAULT_ORDER = ["anthropic", "gemini", "grok", "openai", "deepseek"]
_TIMEOUT = 120

_KEY_FIELD = {
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
def _anthropic(creds, system, user, max_tokens):
    key = creds.get("anthropic_api_key")
    if not key:
        raise ProviderError("no anthropic_api_key")
    model = creds.get("claude_dev_model") or creds.get("claude_model") or "claude-sonnet-5"
    resp = _http_post(
        "https://api.anthropic.com/v1/messages",
        {"x-api-key": key, "anthropic-version": "2023-06-01",
         "content-type": "application/json"},
        {"model": model, "max_tokens": max_tokens, "system": system,
         "messages": [{"role": "user", "content": user}]},
    )
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
    return "".join(p.get("text", "")
                   for p in resp["candidates"][0]["content"]["parts"])


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


_PROVIDERS = {
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


def call(provider, system, user, creds, max_tokens=16384):
    """Call ONE provider by name. Returns reply text. Raises ProviderError."""
    if provider not in _PROVIDERS:
        raise ProviderError(f"unknown provider: {provider}")
    return _PROVIDERS[provider](creds, system, user, max_tokens)


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
