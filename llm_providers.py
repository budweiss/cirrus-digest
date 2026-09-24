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
  S159 adds kimi_api_key/kimi_model (Moonshot, model id `kimi-k3`). S177
  PROMOTES it into DEFAULT_ORDER (was gated out like ollama/vllm, callable
  but never routed to) after a real local_bench.py comparison on CUMULUS
  showed it agreeing with Anthropic on 8/9 real judgments across two call
  shapes -- see docs/COWORK-WORKLIST.md and the S177 recap. It is still
  dormant-until-keyed like every cloud provider: CIRRUS has no
  kimi_api_key today, so this is currently a CUMULUS-only routing change
  in practice, code shared by both boxes.
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

import base64
import json
import os
import sys
import threading
import time
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ORDER = ["anthropic", "gemini", "grok", "openai", "deepseek", "kimi"]
_TIMEOUT = 120

# S132: every call() records itself to the spend ledger (llm_budget.record_call).
# Buddy, 2026-09-08: "make every cloud call visible." Before this, only
# ensemble's council/judge and Skywarden wrote rows, so llm-spend-report could
# not see halftime, pedagogy, promise_detect or task_solver at all.
#
# The TASK tag defaults to the running script's name -- `halftime_catalogue`,
# `pedagogy_daily`, `intake` (which hosts promise_detect) -- because that is the
# bucket the report maps to a project, and it costs the callers nothing. A
# caller that knows better passes task=; a process can set_default_task().
# "adhoc" is the tag for `python -c` and REPL sessions.
def _derive_default_task():
    try:
        stem = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else ""
    except Exception:
        stem = ""
    return stem if stem and not stem.startswith("-") else "adhoc"


DEFAULT_TASK = _derive_default_task()


def set_default_task(task):
    """Name the ledger bucket for every subsequent call() in this process that
    does not pass its own task=. Empty/None leaves the current default."""
    global DEFAULT_TASK
    if task:
        DEFAULT_TASK = str(task)


# S133: the recording KILL SWITCH for test suites. The hook's DEFAULT destination
# is the real out/ ledger, so any selftest that calls call() with bare creds and a
# stubbed non-empty reply writes a LIVE row. That happened on both boxes the first
# time this shipped -- 10 junk rows each, tagged "llm_providers", box "unknown"
# (T77 in docs/TOOLING-TRAPS.md). selftest() runs under recording(False); only
# its own ledger block, which injects a tempfile ledger, re-enables it. Production
# never touches this; the default is True and the __main__ check pins it there.
_RECORDING = True


class recording:
    """Context manager: `with recording(False): ...` suppresses ledger writes for
    the block and restores the previous state on exit (exceptions included)."""

    def __init__(self, enabled):
        self._want = bool(enabled)

    def __enter__(self):
        global _RECORDING
        self._prev, _RECORDING = _RECORDING, self._want
        return self

    def __exit__(self, *exc):
        global _RECORDING
        _RECORDING = self._prev
        return False


def _record(provider, system, user, reply, creds, task):
    """Best-effort ledger row. Imported lazily and wrapped: the ledger must never
    be able to break a client job, and llm_budget is optional to this module."""
    if not _RECORDING:
        return
    try:
        import llm_budget as _B
        return _B.record_call(creds, provider, last_model() or "?",
                       len(system or "") + len(user or ""), len(reply or ""),
                       task=(task or DEFAULT_TASK), session_id=getattr(_LAST, "session_id", None),
                       app_dir=str(Path(__file__).resolve().parent),
                       in_tok=getattr(_LAST, "usage", {}).get("input"),
                       out_tok=getattr(_LAST, "usage", {}).get("output"))
    except Exception:
        pass

# S103: the MODEL the last call actually put on the wire.
#
# Callers could name the provider ("ollama") but never the model, so a
# catalogue entry could not say which local model produced it -- and the
# qwen2.5:72b -> qwen3.8:27b switch left every pre-switch and post-switch entry
# labelled identically. This is recorded where the request is BUILT, not
# re-derived from creds by a second copy of the resolution logic: a copy is
# exactly what made sm_prov a fake test in S102.
#
# NEVER holds a key or a URL -- _gemini builds its key into the URL, so only
# the bare model name is recorded here.
# S119: PER-THREAD, and it has to be. This used to be a module global with a
# docstring that said "it is not per-thread" -- fine while every caller was
# serial. halftime_catalogue reads last_model() through _tag() to stamp
# extracted_by on every catalogue entry, so once that job sweeps concurrently
# two in-flight calls would overwrite each other's model and the CLIENT ARTEFACT
# would be labelled with whichever finished last. threading.local() gives each
# thread its own slot; a single-threaded caller sees exactly what it saw before.
_LAST = threading.local()


# S141 — truncation ledger. Same idea as _record_usage below: a thing we can
# only act on if it is written down at the moment it is true.
_TRUNC_LEDGER = Path(__file__).resolve().parent / "logs" / "llm_truncations.jsonl"


def _note_finish(reason, model, empty=None):
    """Record a cut-off reply. NEVER raises -- instrumentation on a hot path.

    S257: `empty` marks a reply cut off before ANY text -- the whole budget
    went to thinking. S256 found ~240 of those from anthropic at effort=max,
    invisible because councils fell back to a member answer; model_health's
    paid_empty_verdict alerts on them. Only adapters that know pass it."""
    _LAST.finish_reason = reason
    if reason != "length":
        return
    try:
        _TRUNC_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with open(_TRUNC_LEDGER, "a") as f:
            row = {
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "provider": getattr(_LAST, "provider", "") or "",
                "model": model,
                "task": getattr(_LAST, "task", "") or "",
            }
            if empty is not None:
                row["empty"] = bool(empty)
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass


def last_finish_reason():
    """finish_reason of the most recent call ON THIS THREAD, or None."""
    return getattr(_LAST, "finish_reason", None)


def last_model():
    """Model used by the most recent call() ON THIS THREAD, or None.

    call() clears this BEFORE dispatching, so a provider that raises leaves
    None rather than the previous call's model -- a stale value read as this
    call's answer is the whole failure mode this exists to avoid. Read it
    immediately after the call that produced it.

    Per-thread since S119: concurrent callers must not see each other's model.
    """
    return getattr(_LAST, "model", None)

_KEY_FIELD = {
    # S73: "ollama" is DELIBERATELY absent from DEFAULT_ORDER, and its key field
    # (ollama_url) is not in credentials.json today. available() filters on that
    # field, so the local provider cannot be selected by accident — a caller has
    # to pass order=["ollama"] explicitly. Two independent gates, because this
    # file is on the path of every heavy job on the box and a routing change
    # nobody asked for is the worst kind.
    "ollama":    "ollama_url",
    # S125: the TP=2 vLLM endpoint on cumulus1 (CUMULUS2-TP2-PLAN.md). Same
    # two gates as ollama: absent from DEFAULT_ORDER, and its key field is
    # unset unless someone sets it -- so only an explicit call("vllm", ...)
    # ever reaches it, and blanking vllm_url is the whole rollback.
    "vllm":      "vllm_url",
    "anthropic": "anthropic_api_key",
    "gemini":    "gemini_api_key",
    "grok":      "grok_api_key",
    "openai":    "openai_api_key",
    "deepseek":  "deepseek_api_key",
    # S159: Moonshot's Kimi. Same two gates as ollama/vllm -- absent from
    # DEFAULT_ORDER, so keying it does NOT add a voice to the council or a
    # hop to the failover chain. Only an explicit call("kimi", ...).
    "kimi":      "kimi_api_key",
}


class ProviderError(RuntimeError):
    """Any provider call/config failure (missing key, HTTP error, bad response)."""


class AccountingError(ProviderError):
    """Accounting is uncertain: do not spend again through recovery."""


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


def _openai_compatible(url, key, model, system, user, max_tokens,
                       timeout=_TIMEOUT, extra=None):
    """OpenAI Chat Completions shape — shared by OpenAI, xAI (Grok), DeepSeek.

    `timeout` exists for the vLLM path only (S125): a thinking-on extraction
    measured 17-442 s at 12 tok/s, so 120 s would escalate most of them to a
    paid call. Every other provider keeps the module default.

    `extra` (S139) is merged into the request body — also vLLM-only today, for
    `chat_template_kwargs`. Every other provider passes nothing and its body is
    byte-for-byte what it was.
    """
    _LAST.model = model
    body = {"model": model, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    if extra:
        body.update(extra)
    resp = _http_post(
        url,
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        body,
        timeout=timeout,
    )
    _LAST.model = resp.get("model")  # response identity, never the requested alias
    # S141. finish_reason was read off the wire and dropped on the floor. It is
    # the single most useful field on this response: "length" means the model
    # was still talking when we cut it off. On a LOCAL model that is not a
    # quality problem, it is a BILL -- the halftime jobs treat an unparseable
    # local reply as "the local model could not do it" and escalate to a paid
    # one, so every truncation we cause buys a cloud call. That happened all
    # day on 2026-09-09 (reasoning tokens ate a 4,000 budget) and nothing
    # anywhere recorded it; it had to be reproduced by hand to be seen at all.
    usage = resp.get("usage") or {}
    _LAST.usage = {"input": usage.get("prompt_tokens"), "output": usage.get("completion_tokens")}
    _note_finish(resp["choices"][0].get("finish_reason"), model)
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


_ANTHROPIC_EFFORT_VALUES = ("low", "medium", "high", "xhigh", "max")


def _anthropic_extra(creds):
    """{} or {"thinking": {...}, "output_config": {"effort": ...}}. S177:
    Buddy's ask was "best results when we need to reach out" -- a durable
    creds field (anthropic_effort), not a per-job env var like vLLM's
    VLLM_REASONING_EFFORT, because this is meant to be the standing config
    for every call that reaches the cloud, not a one-off per-job tweak.

    Only current-generation models (Sonnet 5 / Opus 5 / the Fable family)
    accept output_config.effort -- Haiku 4.5 and older models reject it
    with a 400. This is opt-in via credentials.json and deliberately NOT
    auto-derived from the configured model name (that would be a second
    copy of model-capability knowledge to keep in sync, the exact class of
    bug the S103 last_model() docstring warns about) -- whoever sets
    anthropic_effort is responsible for pairing it with a model that
    supports it, same as the vLLM reasoning-effort field already assumes
    of its own server.
    """
    effort = (creds.get("anthropic_effort") or "").strip().lower()
    if effort not in _ANTHROPIC_EFFORT_VALUES:
        return {}
    return {"thinking": {"type": "adaptive"},
            "output_config": {"effort": effort}}


_EFFORT_MIN_MAX_TOKENS = 4096
# S177 (found live, same day the effort feature shipped): Anthropic's
# adaptive thinking draws from the SAME max_tokens budget as the answer
# text -- same mechanism as the S91 Gemini bug (_gemini's own docstring:
# "thinking tokens are drawn from maxOutputTokens before any text is
# emitted"), just Anthropic's turn. Several real call sites across this
# repo size max_tokens for a short answer with NO thinking headroom at all
# -- business_idea_scan.py's _relevance()=200, critique()=300,
# estimate()=700. The moment creds["anthropic_effort"] is set, ALL of them
# were one adaptive-thinking pass away from spending their whole budget on
# thinking and returning EMPTY TEXT, silently -- caught live via the
# alopecia agent's own first dry run (call_local's escalation logged
# "cloud reply from anthropic failed parse/empty check" twice). Fixed
# centrally, once, here: raise the WIRE max_tokens floor only when effort
# is active, never lower a caller's own larger request. No caller
# anywhere in the repo needs to know this floor exists or size around it.
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

    extra = _anthropic_extra(creds)
    wire_max_tokens = max(max_tokens, _EFFORT_MIN_MAX_TOKENS) if extra else max_tokens

    _LAST.model = model
    body = {"model": model, "max_tokens": wire_max_tokens, "system": sys_field,
           "messages": [{"role": "user", "content": user}]}
    body.update(extra)
    resp = _http_post(
        "https://api.anthropic.com/v1/messages",
        {"x-api-key": key, "anthropic-version": "2023-06-01",
         "content-type": "application/json"},
        body,
    )
    _LAST.model = resp.get("model")
    usage = resp.get("usage") or {}
    _LAST.usage = {"input": (usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
                            + usage.get("cache_read_input_tokens", 0)) if usage else None,
                   "output": usage.get("output_tokens")}
    text = "".join(b.get("text", "") for b in resp.get("content", [])
                   if b.get("type") == "text")
    _note_finish("length" if resp.get("stop_reason") == "max_tokens" else resp.get("stop_reason"),
                 model, empty=not text.strip())
    _record_usage("anthropic", model, usage, want_cache)
    return text


def _gemini(creds, system, user, max_tokens):
    key = creds.get("gemini_api_key")
    if not key:
        raise ProviderError("no gemini_api_key")
    model = creds.get("gemini_model")
    if not model:
        raise ProviderError("no gemini_model set in credentials.json")
    _LAST.model = model
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
    usage = resp.get("usageMetadata") or {}
    _LAST.usage = {"input": usage.get("promptTokenCount"),
                   "output": (usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)) if usage else None}
    _LAST.model = resp.get("modelVersion")
    cand = (resp.get("candidates") or [{}])[0]
    finish = cand.get("finishReason")
    _note_finish("length" if finish == "MAX_TOKENS" else finish, _LAST.model)
    parts = ((cand.get("content") or {}).get("parts")) or []
    # S232: MAX_TOKENS with non-empty parts used to return silently -- the
    # thinking preamble ate most of the budget, generation stopped mid-
    # sentence, and the caller (ensemble/pedagogy_daily's topic brief) logged
    # it as a normal success and shipped the fragment to a real client
    # (Alyssa, 2026-09-19). Same root cause as the empty-parts case below,
    # just caught one token later -- a cut-off answer is never a completed
    # one, so both shapes of MAX_TOKENS must fail the same way.
    if not parts or finish == "MAX_TOKENS":
        usage = resp.get("usageMetadata") or {}
        raise ProviderError(
            f"gemini returned {'no' if not parts else 'TRUNCATED'} content: "
            f"finishReason={finish!r}, "
            f"{usage.get('thoughtsTokenCount', 0)} thinking token(s) of a "
            f"{max_tokens}-token budget. If this is MAX_TOKENS the budget is "
            f"below what the model needed to finish — raise max_tokens.")
    return "".join(p.get("text", "") for p in parts)


def generate_image(prompt: str, creds: dict, model: str = None) -> bytes:
    """Generate one image via a Gemini image-output model. Returns raw PNG
    bytes. Raises ProviderError on any failure (no key, no image in the
    response, transport error) -- NOT the same contract as the text call()
    dispatch, because image generation is its own modality with its own
    failure shape (a text-only reply where an image was expected, e.g.).

    S232: built to answer Alyssa's "picture examples" ask for real, after the
    pedagogy digest told her (correctly, for that one text-only call) that it
    couldn't show an image -- gemini-2.5-flash-image is a real, keyed,
    confirmed-working model on the account already paying for gemini_api_key,
    it had just never been called for images anywhere in this codebase.

    Callers that can send a text-only fallback should catch ProviderError
    and do that, rather than let a picture request block content that would
    otherwise have gone out fine.
    """
    key = creds.get("gemini_api_key")
    if not key:
        raise ProviderError("no gemini_api_key")
    model = model or creds.get("gemini_image_model") or "gemini-2.5-flash-image"
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={key}")
    resp = _http_post(url, {"Content-Type": "application/json"},
                       {"contents": [{"parts": [{"text": prompt}]}]})
    cand = (resp.get("candidates") or [{}])[0]
    parts = ((cand.get("content") or {}).get("parts")) or []
    for p in parts:
        data = (p.get("inlineData") or {}).get("data")
        if data:
            return base64.b64decode(data)
    raise ProviderError(
        f"gemini image model returned no image: finishReason="
        f"{cand.get('finishReason')!r}, model={model!r}")


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


def _kimi(creds, system, user, max_tokens):
    """Moonshot AI's Kimi (K3), via its OpenAI-compatible endpoint. S159.

    Buddy asked for Kimi "as an option alongside Opus 5 and Fable 5.1." TWO
    different things wear that name and only the first one lives in this file:

      * KIMI AS A PROVIDER (this). A backend the boxes' jobs can call the same
        way they call openai/grok/deepseek -- council, cumulus-ask-provider,
        bench. Chat Completions shape, $3/$15 per Mtok, 1M context.
      * KIMI AS THE MODEL OF A CLAUDE CODE SESSION. Not a provider at all:
        that is ANTHROPIC_BASE_URL=https://api.moonshot.ai/anthropic on a
        separate `claude` process, and NOTHING in this file affects it.
        docs/KIMI-K3-ACCESS.md carries that recipe and its stray-
        ANTHROPIC_API_KEY trap.

    ABSENT FROM DEFAULT_ORDER on purpose -- the S73/S92 gate. Every other cloud
    provider here is dormant-until-keyed AND in DEFAULT_ORDER, so the moment a
    key lands it becomes a fifth council voice and a new line on the bill,
    everywhere, with no decision made. Kimi is keyed and then *measured*;
    promoting it is one line in DEFAULT_ORDER and should follow a bench rather
    than precede one. available() will not list it until then -- that is the
    design, not a bug, and it is the same thing creds-llm-check reports for
    ollama.

    Always-on reasoning ("thinking mode"): its thinking tokens are drawn from
    max_tokens before any answer text, which is the S91 gemini trap and the
    S74/S75 deepseek one. Budget accordingly -- a small max_tokens here buys an
    empty `content`, not a short answer. call()'s finish_reason=="length" note
    is what surfaces it.
    """
    key = creds.get("kimi_api_key")
    if not key:
        raise ProviderError("no kimi_api_key")
    model = creds.get("kimi_model")
    if not model:
        raise ProviderError("no kimi_model set in credentials.json "
                            "(the API id is `kimi-k3`)")
    response_format = creds.get('kimi_response_format')
    extra = None
    if response_format is not None:
        if (not isinstance(response_format, dict)
            or response_format.get('type') != 'json_schema'
            or not isinstance(response_format.get('json_schema'), dict)
            or response_format['json_schema'].get('strict') is not True):
            raise ProviderError('invalid Kimi structured output contract')
        extra = {'response_format': response_format}
    effort = creds.get('kimi_reasoning_effort')
    if effort is not None:
        if effort not in ('low', 'high', 'max'):
            raise ProviderError('invalid Kimi reasoning effort')
        extra = dict(extra or {}, reasoning_effort=effort)
    return _openai_compatible("https://api.moonshot.ai/v1/chat/completions",
                              key, model, system, user, max_tokens, extra=extra)


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

      CIRRUS  — ENABLED S92 (Buddy) with qwen3.8:27b, after the bench showed it
                reasons better than both qwen2.5:14b and the 72B. Before that it
                was ABSENT since S73 and every caller escalated to cloud; the
                idle 72B there was deleted in S92 because nothing could reach
                it. promise_detect (which runs on every client send via
                task_solver) is the caller this actually changes.
      CUMULUS — ollama_url is set and ollama_model IS qwen2.5:72b. Measured in
                the live halftime snapshot: 120 entries "ollama (local)" vs 16
                "anthropic (escalated)". That 47 GB model is doing ~88% of the
                extraction on Justin's job. Deleting it would not crash anything
                — it would silently convert every entity to a PAID Claude call
                and peg the escalation-rate metric at 100%, which is worse than
                a crash because nothing would report it.

    So: before removing a local model, check `ollama_model` in that box's
    credentials.json and the `extracted_by`/`by` fields in its output. Do not
    reason from this docstring's first paragraph, which was true in S73 and is
    not now — BOTH boxes now run a local model through this backend.

    The S73 guard still holds and must keep holding: `ollama` is absent from
    DEFAULT_ORDER, so available() does not list it even with ollama_url set
    (verified on both boxes). Enabling the backend does NOT put local into
    automatic routing or the council — only an explicit call("ollama", ...)
    reaches it. If available() ever lists ollama, that is the regression S73
    warned about.

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


VLLM_DEFAULT_TIMEOUT = 900

# S139 (LOCAL-MODEL-PLAN step 5): per-JOB reasoning effort for the endpoint.
# The server's default is `--default-chat-template-kwargs reasoning_effort=medium`
# (serve-tp2.sh); a unit that sets VLLM_REASONING_EFFORT overrides it for its own
# requests only, via the OpenAI-compatible `chat_template_kwargs` field -- no
# server restart, no effect on any other job. Unset = the body is unchanged.
# Anything outside the template's vocabulary is ignored (and the request goes out
# at the server default) rather than 400-ing a client job over a typo. The
# vocabulary is the Qwen3.8 chat_template.jinja's, read 2026-09-08: it raises on
# anything but these three -- "high" is NOT one of them (probed: HTTP error).
VLLM_EFFORT_ENV = "VLLM_REASONING_EFFORT"
VLLM_EFFORT_VALUES = ("low", "medium", "xhigh")


def _vllm_extra():
    """{} or {"chat_template_kwargs": {"reasoning_effort": <env>}}."""
    v = (os.environ.get(VLLM_EFFORT_ENV) or "").strip().lower()
    if v in VLLM_EFFORT_VALUES:
        return {"chat_template_kwargs": {"reasoning_effort": v}}
    return {}


def _vllm(creds, system, user, max_tokens):
    """The TP=2 vLLM endpoint (cumulus1 127.0.0.1:8000, cumulus2 as the other
    tensor-parallel rank). S125, CUMULUS2-TP2-PLAN.md.

    Same OpenAI-compatible shape as _ollama, two differences: its own timeout
    (`vllm_timeout`, default VLLM_DEFAULT_TIMEOUT) because the model thinks
    for 2-6k tokens per extraction block, and its own key field so the
    halftime jobs can prefer it and fall back to ollama when it is absent or
    down. `vllm_url` unset = this function is never reached.
    """
    url = creds.get("vllm_url")
    if not url:
        raise ProviderError("no vllm_url — TP=2 endpoint not enabled")
    model = creds.get("vllm_model")
    if not model:
        raise ProviderError("no vllm_model set in credentials.json")
    try:
        timeout = float(creds.get("vllm_timeout") or VLLM_DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = VLLM_DEFAULT_TIMEOUT
    extra = _vllm_extra()
    response_format = creds.get('vllm_response_format')
    if response_format is not None:
        if not isinstance(response_format, dict) or response_format.get('type') != 'json_schema':
            raise ProviderError('invalid vllm response format')
        extra = dict(extra, response_format=response_format)
    return _openai_compatible(url.rstrip("/") + "/v1/chat/completions",
                              "local", model, system, user, max_tokens,
                              timeout=timeout, extra=extra)


_PROVIDERS = {
    "ollama":    _ollama,
    "vllm":      _vllm,
    "anthropic": _anthropic,
    "gemini":    _gemini,
    "grok":      _grok,
    "openai":    _openai,
    "deepseek":  _deepseek,
    "kimi":      _kimi,
}


# ── public API ──────────────────────────────────────────────────────────────────
def available(creds):
    """Providers that have an API key configured, in DEFAULT_ORDER order."""
    return [p for p in DEFAULT_ORDER if creds.get(_KEY_FIELD[p])]


def call(provider, system, user, creds, max_tokens=16384, retries=1, *,
         task=None, record=True, session_id=None, privacy=None, strict_accounting=False):
    """Call ONE provider by name. Returns reply text. Raises ProviderError.

    Retries once (retries=1) on an EMPTY/whitespace reply. Guards the S47 #8
    case where Claude returned 0 chars on a live call — a transient empty reply
    shouldn't silently cede the primary provider to failover. Transport/config
    failures still raise immediately (ProviderError, no retry); the caller
    handles those. Returns the (possibly still-empty) reply after the retries.

    S132: a NON-EMPTY reply is recorded to the spend ledger (llm_budget) under
    `task` (default: this process's script name) -- local providers too, at $0,
    so the report shows local volume beside cloud cost. record=False is for a
    caller that records the call itself with richer tags (ensemble's council
    and judge rows) -- without it those calls would be counted twice.
    """
    _LAST.model = None  # blocked calls must not expose the previous model
    import llm_routing
    try:
        route_policy = llm_routing.authorize(
            provider, task or DEFAULT_TASK, creds, system, user,
            max(max_tokens, _EFFORT_MIN_MAX_TOKENS) if provider == "anthropic" and _anthropic_extra(creds) else max_tokens,
            privacy=privacy, session_id=session_id)
    except llm_routing.RoutingError as exc:
        raise ProviderError(str(exc)) from exc
    if route_policy.get("profile") and provider not in llm_routing.LOCAL:
        retries = 0  # one paid attempt per selected provider
    _LAST.usage = {}
    _LAST.model = None            # never let a stale model answer for this call
    _LAST.finish_reason = None
    _LAST.provider = provider     # S141: so a truncation record can name it
    _LAST.task = task or ""
    _LAST.session_id = session_id
    if provider not in _PROVIDERS:
        raise ProviderError(f"unknown provider: {provider}")
    attempt_id = uuid.uuid4().hex
    started = time.monotonic()
    reply = ""
    for _ in range(retries + 1):
        _LAST.usage = {}
        reply = ""
        try:
            reply = _PROVIDERS[provider](creds, system, user, max_tokens) or ""
        finally:
            # A billed empty/truncated response still consumed tokens. Record
            # each retry separately, but never invent usage for transport errors.
            if record and (reply.strip() or any(v is not None for v in _LAST.usage.values())):
                recorded = _record(provider, system, user, reply, creds, task)
                if strict_accounting and recorded is None:
                    raise AccountingError("selected call accounting failed; review required")
            if route_policy.get("profile") and _RECORDING:
                try:
                    llm_routing.audit(task or DEFAULT_TASK, provider,
                        "reply_received" if reply.strip() else "no_usable_reply",
                        route_policy, attempt_id=attempt_id,
                        elapsed_seconds=round(time.monotonic()-started, 3))
                except OSError as exc:
                    raise (AccountingError if strict_accounting else ProviderError)(
                        "routing audit unavailable after provider call") from exc
        if reply.strip():
            break
    return reply


def escalate(system, user, creds, max_tokens=16384, mode=None, order=None, *,
             task=None, record=True, session_id=None, privacy=None):
    """Policy-driven call across configured providers.

    Reads defaults from creds['dev_escalation'] = {"mode":..., "order":[...]}.
      single   -> (provider, text)     first available in order
      failover -> (provider, text)     try in order until one succeeds
      council  -> [(provider, text_or_'ERROR: ...'), ...]  every available
    Raises ProviderError if no provider has a key.
    S132: task= and record= are forwarded to every call() (see call()).
    """
    import llm_routing
    try:
        selected, route_policy = llm_routing.cloud_order(
            task or DEFAULT_TASK, creds, order=order, privacy=privacy)
    except llm_routing.RoutingError as exc:
        raise ProviderError(str(exc)) from exc
    if route_policy.get("profile"):
        order = selected
    pol = creds.get("dev_escalation", {}) or {}
    mode = mode or pol.get("mode", "single")
    order = order if route_policy.get("profile") else (order or pol.get("order") or DEFAULT_ORDER)
    avail = [p for p in order if creds.get(_KEY_FIELD.get(p, "")) and p in _PROVIDERS]
    if route_policy.get("profile"):
        avail = avail[:route_policy["max_cloud_providers"]]
    if not avail:
        raise ProviderError("no approved providers have keys configured")

    if privacy is not None:
        creds = dict(creds, llm_privacy=route_policy["privacy"])

    if mode == "council":
        out = []
        for p in avail:
            try:
                out.append((p, call(p, system, user, creds, max_tokens,
                                    task=task, record=record, session_id=session_id)))
            except ProviderError as e:
                out.append((p, f"ERROR: {e}"))
        return out

    if mode == "failover":
        last = None
        for p in avail:
            try:
                return (p, call(p, system, user, creds, max_tokens,
                                task=task, record=record, session_id=session_id))
            except ProviderError as e:
                last = e
        raise ProviderError(f"all providers failed; last error: {last}")

    # "single" (default)
    p = avail[0]
    return (p, call(p, system, user, creds, max_tokens, task=task, record=record, session_id=session_id))


def call_local_first(system, user, creds, max_tokens=2048, *, task=None,
                     parse=None, stats=None, local_model=None, retries=0, privacy=None,
                     local_provider=None):
    """Try vLLM, then ollama, then escalate to the cloud council. S177.

    This is the SAME three-tier fallback halftime_catalogue.py's local-
    extraction path has used since S125/S92 (vLLM -> ollama -> escalate,
    a vLLM miss counted under `stats['vllm_fallback']` rather than as an
    escalation, so a dead endpoint shows up honestly instead of quietly
    becoming a paid call) -- extracted here so a NEW call site doesn't
    hand-roll those ~15 lines a third and fourth time. Existing call sites
    are NOT migrated to this; their hand-rolled version is proven live and
    touching working code to DRY it up is not what this was asked for.

    parse: optional callable(raw_text) -> result. If given, a tier's reply
    is accepted only when parse(raw) is not None/falsy -- mirrors halftime's
    own `parse_acts` gate exactly (an unparseable reply falls through to the
    next tier rather than being "accepted" as garbage). If omitted, any
    non-empty (stripped) reply is accepted.

    stats: optional dict, incremented under 'vllm_fallback' on a vLLM miss
    -- same convention halftime uses, so llm-spend-report can tell a dead
    endpoint from a real cloud escalation rather than conflating them.

    local_model: optional override for WHICH ollama model to try, without
    touching the box's single configured `ollama_model` credential or
    adding a task-class registry ahead of any evidence one is needed
    (see bench_local.md / local_bench.py) -- constructs a creds copy with
    ollama_model overridden; the vLLM tier is unaffected (its model is
    fixed by the endpoint, not a per-call choice).

    local_provider: optional strict selection of ollama or vllm. When set,
    no other local/cloud provider is attempted on failure. local_model may
    select an Ollama specialist; it cannot override a fixed vLLM endpoint.

    Returns (result, tier) where tier is "vllm", "ollama", or the cloud
    provider name escalate() actually used. Raises ProviderError only if
    the cloud tier itself fails or fails parse -- same raise contract as
    call()/escalate(); a caller that wants "never raises" (like halftime)
    wraps this in try/except itself rather than this function swallowing
    errors a different way than its siblings do.

    S177: the cloud-escalation tier deliberately does NOT engage
    creds["anthropic_effort"], even when the box has it configured --
    found live, twice, via the alopecia agent's own dry runs: this
    function's whole premise is "local failed, get a workable answer fast,
    local-equivalent effort", not "give me your deepest reasoning". Forcing
    max-effort adaptive thinking onto what's supposed to be the cheap/fast
    fallback tier repeatedly produced empty replies (thinking consuming the
    whole token budget) on ordinary routine-classification prompts that
    never needed deep reasoning in the first place. A caller that
    genuinely wants full effort on its cloud tier should call escalate()
    directly (as call_council does), not through this function.

    S274: the tier now SETS effort to low rather than dropping the key.
    claude-sonnet-5 thinks by default -- a request with no effort runs at
    HIGH -- so dropping it bought the exact deep reasoning this tier exists
    to avoid (measured on halftime routing: 4,000 tokens all thinking, empty
    reply, every night for a week).
    """
    # An explicit specialist target must never silently become another model.
    if local_provider not in (None, "ollama", "vllm"):
        raise ProviderError("unknown local provider")
    if local_provider == "vllm" and local_model is not None:
        raise ProviderError("vllm model is fixed by its endpoint")
    import llm_routing
    try:
        route_policy = llm_routing.policy(task or DEFAULT_TASK, creds, privacy)
    except llm_routing.RoutingError as exc:
        raise ProviderError(str(exc)) from exc
    creds = dict(creds, llm_privacy=route_policy["privacy"])

    def _accept(raw):
        if parse is None:
            return raw if (raw or "").strip() else None
        return parse(raw)

    if local_provider in (None, "vllm") and creds.get("vllm_url"):
        try:
            raw = call("vllm", system, user, creds, max_tokens=max_tokens,
                      retries=retries, task=task)
            result = _accept(raw)
            if result is not None:
                return result, "vllm"
        except ProviderError:
            pass
        if stats is not None:
            stats["vllm_fallback"] = stats.get("vllm_fallback", 0) + 1

    if local_provider in (None, "ollama") and creds.get("ollama_url"):
        _oc = creds if local_model is None else dict(creds, ollama_model=local_model)
        try:
            raw = call("ollama", system, user, _oc, max_tokens=max_tokens,
                      retries=retries, task=task)
            result = _accept(raw)
            if result is not None:
                return result, "ollama"
        except ProviderError:
            pass

    if local_provider is not None:
        raise ProviderError("selected local provider unavailable or output rejected")

    if route_policy.get("profile") and _RECORDING:
        try:
            llm_routing.audit(task or DEFAULT_TASK, "local", "local_unavailable_or_rejected", route_policy)
        except OSError as exc:
            raise ProviderError("routing audit unavailable") from exc
    _cloud_creds = dict(creds, anthropic_effort="low")
    provider, raw = escalate(system, user, _cloud_creds, max_tokens=max_tokens,
                             mode="single", task=task)
    result = _accept(raw)
    if result is None:
        raise ProviderError(
            f"cloud reply from {provider} failed parse/empty check")
    return result, provider


# ── self-test (python3 llm_providers.py selftest | --selftest) ────────────────
def selftest():
    """Exercise the decision functions with explicit inputs. Returns True/False.

    S104: lifted out of `if __name__ == "__main__":` into a plain function so
    dev_agent's gate 2 can import and call it directly. That was the ask in
    dev-loop build `prop-2026-08-27-649612`, which could not be shipped as
    built: it was cut against the 2026-08-31 file and conflicts with the S103
    last_model() work, so a rebase would have clobbered it. Its new assertions
    are carried over here instead -- `escalate()` had NO coverage at all, and
    it is the fallback every lane uses when the local model fails.

    No network and no credentials: the provider adapters and the HTTP layer are
    stubbed for the duration and restored in a `finally`. That restore matters
    more now than it did as a __main__ block -- an importable selftest that
    leaves `_PROVIDERS` monkeypatched would poison the caller's process.
    """
    _ok = True

    def check(name, cond):
        nonlocal _ok
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        _ok = _ok and cond

    _real_providers = dict(_PROVIDERS)
    _real_post = _http_post
    # S133 / T77: the whole suite runs with ledger recording OFF. Every check
    # below that calls call() with bare creds and a stubbed reply would
    # otherwise append to the REAL out/ ledger on the box running it.
    _rec_guard = recording(False)
    _rec_guard.__enter__()
    # S257: the cache-usage and truncation ledgers were NOT covered by the
    # guard above -- every stubbed anthropic call appended an all-null row to
    # the LIVE logs/llm_cache_usage.jsonl on the box running the suite (T32).
    import tempfile as _tf0, shutil as _sh0
    _td0 = _tf0.mkdtemp(prefix="llmp-selftest-")
    _live_ledgers = {k: globals()[k] for k in ("_CACHE_LEDGER", "_TRUNC_LEDGER")}
    for _k in _live_ledgers:
        globals()[_k] = Path(_td0) / (_k.lower() + ".jsonl")
    try:
        check("selftest: cache + truncation ledgers point at a temp dir, not live logs",
              all(str(globals()[k]).startswith(_td0) for k in _live_ledgers))
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
        check("available: empty creds -> no providers", available({}) == [])
        check("available: ordering follows DEFAULT_ORDER, not dict order",
              available({"grok_api_key": "g", "anthropic_api_key": "a"})
              == ["anthropic", "grok"])

        try:
            call("nope", "s", "u", {})
            _unknown_raised = False
        except ProviderError:
            _unknown_raised = True
        check("call: unknown provider raises", _unknown_raised)

        # ── S159: the kimi provider. S177: PROMOTED into DEFAULT_ORDER after a
        #    real local_bench.py comparison (not a guess) showed it agreeing
        #    with Anthropic on 8/9 real judgments -- it is still
        #    dormant-until-keyed like every cloud provider, same as the other
        #    four; the only thing that changed is its ORDER position, not the
        #    key-gate every provider already has.
        check("kimi: registered with a key field",
              "kimi" in _PROVIDERS and _KEY_FIELD.get("kimi") == "kimi_api_key")
        check("kimi: now IN DEFAULT_ORDER (S177 promotion, evidence-based)",
              "kimi" in DEFAULT_ORDER)
        check("kimi: still dormant until keyed, exactly like every other "
              "cloud provider -- promotion changed its ORDER, not the gate",
              "kimi" not in available({}))
        check("kimi: keying it makes it selectable, same as any provider",
              available({"kimi_api_key": "EXAMPLE-fake-kimi-key-not-real"}) == ["kimi"])
        _kimi_keyed = {"kimi_api_key": "EXAMPLE-fake-kimi-key-not-real"}
        for _c, _want, _label in (
                ({}, "no kimi_api_key", "unkeyed"),
                (_kimi_keyed, "no kimi_model", "keyed but no model")):
            try:
                call("kimi", "s", "u", _c)
                _raised = ""
            except ProviderError as _e:
                _raised = str(_e)
            check(f"kimi: {_label} raises before any network call",
                  _want in _raised)

        # ── escalate(): the fallback path every lane uses. Ported from
        #    prop-2026-08-27-649612, which found it wholly untested.
        try:
            escalate("s", "u", {})
            _no_keys_raised = False
        except ProviderError:
            _no_keys_raised = True
        check("escalate: no keys configured raises", _no_keys_raised)

        _PROVIDERS["anthropic"] = lambda c, s, u, m: "claude reply"
        _PROVIDERS["openai"] = lambda c, s, u, m: "openai reply"
        check("escalate: single picks the FIRST available in order",
              escalate("s", "u", {"anthropic_api_key": "a", "openai_api_key": "o"},
                       mode="single") == ("anthropic", "claude reply"))

        _PROVIDERS["anthropic"] = lambda c, s, u, m: (
            _ for _ in ()).throw(ProviderError("down"))
        check("escalate: failover falls through to the next provider",
              escalate("s", "u", {"anthropic_api_key": "a", "openai_api_key": "o"},
                       mode="failover") == ("openai", "openai reply"))
        check("escalate: council asks every provider and captures the failure "
              "inline rather than losing it",
              dict(escalate("s", "u",
                            {"anthropic_api_key": "a", "openai_api_key": "o"},
                            mode="council"))
              == {"anthropic": "ERROR: down", "openai": "openai reply"})

        # ── S125: the vLLM provider. Two gates, then the timeout. ──────────
        _PROVIDERS.clear()
        _PROVIDERS.update(_real_providers)
        check("vllm: NOT in available() even with vllm_url set (S73 gate)",
              "vllm" not in available({"vllm_url": "http://x",
                                       "anthropic_api_key": "a"}))
        try:
            call("vllm", "s", "u", {})
            _vllm_raised = False
        except ProviderError:
            _vllm_raised = True
        check("vllm: no vllm_url raises rather than guessing an endpoint",
              _vllm_raised)
        _seen_timeout = {}

        def _recording_post(url, headers, body, timeout=_TIMEOUT):
            _seen_timeout["t"] = timeout
            _seen_timeout["url"] = url
            return {"model": body["model"], "choices": [{"message": {"content": "ok"}}]}
        globals()["_http_post"] = _recording_post
        call("vllm", "s", "u", {"vllm_url": "http://v/", "vllm_model": "srv"})
        check("vllm: the 900 s default timeout REACHES the transport",
              _seen_timeout.get("t") == VLLM_DEFAULT_TIMEOUT
              and _seen_timeout.get("url") == "http://v/v1/chat/completions")
        check("vllm: last_model names the SERVED model (extracted_by reads it)",
              last_model() == "srv")
        call("vllm", "s", "u", {"vllm_url": "http://v", "vllm_model": "m",
                                "vllm_timeout": "30"})
        check("vllm: vllm_timeout overrides it", _seen_timeout.get("t") == 30.0)
        call("ollama", "s", "u", {"ollama_url": "http://o", "ollama_model": "m"})
        check("...and ollama still gets the module default, untouched",
              _seen_timeout.get("t") == _TIMEOUT)

        # ── S139: per-job reasoning effort reaches the wire, and ONLY when set ──
        _seen_body = {}

        def _body_post(url, headers, body, timeout=_TIMEOUT):
            _seen_body["b"] = body
            return {"model": body["model"], "choices": [{"message": {"content": "ok"}}]}
        globals()["_http_post"] = _body_post
        _vc = {"vllm_url": "http://v", "vllm_model": "m"}
        _saved_effort = os.environ.pop(VLLM_EFFORT_ENV, None)
        try:
            call("vllm", "s", "u", _vc)
            check("effort: env unset -> no chat_template_kwargs in the body",
                  "chat_template_kwargs" not in _seen_body["b"])
            os.environ[VLLM_EFFORT_ENV] = "low"
            call("vllm", "s", "u", _vc)
            check("effort: env=low -> chat_template_kwargs.reasoning_effort=low",
                  _seen_body["b"].get("chat_template_kwargs")
                  == {"reasoning_effort": "low"})
            check("...and the rest of the body is intact (model, max_tokens, 2 messages)",
                  _seen_body["b"]["model"] == "m"
                  and isinstance(_seen_body["b"]["max_tokens"], int)
                  and len(_seen_body["b"]["messages"]) == 2)
            os.environ[VLLM_EFFORT_ENV] = " Medium "
            call("vllm", "s", "u", _vc)
            check("effort: case/whitespace normalised",
                  _seen_body["b"].get("chat_template_kwargs")
                  == {"reasoning_effort": "medium"})
            os.environ[VLLM_EFFORT_ENV] = "turbo"
            call("vllm", "s", "u", _vc)
            check("effort: a value outside the template vocabulary is DROPPED, "
                  "not sent (no 400 on a client job)",
                  "chat_template_kwargs" not in _seen_body["b"])
            os.environ[VLLM_EFFORT_ENV] = "low"
            call("ollama", "s", "u", {"ollama_url": "http://o", "ollama_model": "m"})
            check("effort: ollama body untouched even with the env set",
                  "chat_template_kwargs" not in _seen_body["b"])
        finally:
            if _saved_effort is None:
                os.environ.pop(VLLM_EFFORT_ENV, None)
            else:
                os.environ[VLLM_EFFORT_ENV] = _saved_effort

        # ── S177: anthropic_effort — same idea as vLLM's per-job reasoning
        # effort, but a durable creds field rather than an env var, and only
        # reaches the wire when explicitly set (opt-in, no default).
        call("anthropic", "s", "u", {"anthropic_api_key": "k"})
        check("anthropic effort: no anthropic_effort set -> body unchanged, "
              "no thinking/output_config fields sent",
              "thinking" not in _seen_body["b"] and "output_config" not in _seen_body["b"])
        call("anthropic", "s", "u", {"anthropic_api_key": "k", "anthropic_effort": "max"})
        check("anthropic effort: anthropic_effort=max -> adaptive thinking + "
              "output_config.effort=max reach the wire",
              _seen_body["b"].get("thinking") == {"type": "adaptive"}
              and _seen_body["b"].get("output_config") == {"effort": "max"})
        call("anthropic", "s", "u",
             {"anthropic_api_key": "k", "anthropic_effort": " XHIGH "})
        check("anthropic effort: case/whitespace normalised, same as vLLM's",
              _seen_body["b"].get("output_config") == {"effort": "xhigh"})
        call("anthropic", "s", "u",
             {"anthropic_api_key": "k", "anthropic_effort": "ultra"})
        check("anthropic effort: a value outside the vocabulary is DROPPED, "
              "not sent (no 400 on a client job, same policy as vLLM's gate)",
              "output_config" not in _seen_body["b"])

        # ── S177: the wire max_tokens floor -- found LIVE the same day the
        # effort feature shipped. Adaptive thinking draws from the same
        # max_tokens budget as the answer (same mechanism as the S91 Gemini
        # bug, Anthropic's turn); several real callers size max_tokens for a
        # short answer with zero thinking headroom (business_idea_scan.py:
        # 200/300/700) and would silently get empty replies the moment
        # effort is active. This must NOT touch callers when effort is off.
        call("anthropic", "s", "u",
             {"anthropic_api_key": "k"}, max_tokens=200)
        check("anthropic effort OFF: a small caller max_tokens (200) reaches "
              "the wire UNCHANGED -- the floor only applies when effort is on",
              _seen_body["b"]["max_tokens"] == 200)
        call("anthropic", "s", "u",
             {"anthropic_api_key": "k", "anthropic_effort": "max"}, max_tokens=200)
        check("anthropic effort ON: a small caller max_tokens (200) is "
              "RAISED to the floor, so thinking can't zero out the answer "
              "(this is the exact bug the alopecia agent's first dry run hit)",
              _seen_body["b"]["max_tokens"] == _EFFORT_MIN_MAX_TOKENS)
        call("anthropic", "s", "u",
             {"anthropic_api_key": "k", "anthropic_effort": "max"}, max_tokens=16000)
        check("anthropic effort ON: a caller ALREADY ABOVE the floor (16000) "
              "is left alone -- this raises a floor, it never lowers a "
              "caller's own larger request",
              _seen_body["b"]["max_tokens"] == 16000)

        # ── S257: a cut-off reply is marked empty / not-empty for the alert ──
        # Called on the adapter directly: call() retries an empty reply, which
        # would log two rows and blur which case produced which.
        _trunc_suite = _TRUNC_LEDGER
        globals()["_TRUNC_LEDGER"] = Path(_td0) / "s257-trunc.jsonl"
        for _content in ([], [{"type": "text", "text": "partial"}]):
            globals()["_http_post"] = lambda *a, _c=_content, **k: {
                "content": _c, "stop_reason": "max_tokens"}
            _anthropic({"anthropic_api_key": "k"}, "s", "u", 200)
        _rows = [json.loads(l) for l in _TRUNC_LEDGER.read_text().splitlines()]
        check("a reply cut off with NO text is logged empty=True",
              len(_rows) == 2 and _rows[0].get("empty") is True)
        check("a cut-off reply WITH text is logged empty=False",
              len(_rows) == 2 and _rows[1].get("empty") is False)
        globals()["_TRUNC_LEDGER"] = _trunc_suite

        # ── S103: last_model() — the model that actually went on the wire ──
        # Driven through the REAL adapters with only the HTTP layer stubbed, so
        # it tests the resolution rather than a restatement of it. Each
        # assertion is paired with its inverse: a recorder that is never
        # cleared, and one that is never set, both look fine from one direction.
        _PROVIDERS.clear()
        _PROVIDERS.update(_real_providers)
        globals()["_http_post"] = lambda *a, **k: {
            "model": a[2]["model"], "choices": [{"message": {"content": "hi"}}]}
        call("ollama", "s", "u",
             {"ollama_url": "http://x", "ollama_model": "qwen3.8:27b"})
        check("last_model names the LOCAL model, not just the provider",
              last_model() == "qwen3.8:27b")

        call("openai", "s", "u",
             {"openai_api_key": "k", "openai_model": "gpt-x"})
        check("...and it moves with the provider, rather than sticking",
              last_model() == "gpt-x")

        globals()["_http_post"] = lambda *a, **k: {
            "modelVersion": "gemini-9", "candidates": [{"content": {"parts": [{"text": "hi"}]}}]}
        # The literal below carries EXAMPLE deliberately: it is the placeholder
        # marker runner/pre-commit-secret-scan allows, and that guard blocked
        # this line when it was first written. It is a fixture, not a dodge --
        # testing "the key never leaks" needs a stand-in key.
        _fake_key = "EXAMPLE-fake-gemini-key-not-real"
        call("gemini", "s", "u",
             {"gemini_api_key": _fake_key, "gemini_model": "gemini-9"})
        check("gemini records the bare model — its key is in the URL and "
              "must never reach a field that gets written to a KB",
              last_model() == "gemini-9"
              and _fake_key not in (last_model() or ""))

        # S232 — a THINKING model can hit MAX_TOKENS AFTER emitting some real
        # text, not just before any. That non-empty-but-cut-off case used to
        # return silently as if it were a complete answer (confirmed live:
        # Alyssa's pedagogy topic brief stopped mid-sentence and still logged
        # as a success). Both shapes of MAX_TOKENS must raise the same way.
        globals()["_http_post"] = lambda *a, **k: {
            "modelVersion": "gemini-9", "usageMetadata": {"thoughtsTokenCount": 1200},
            "candidates": [{"finishReason": "MAX_TOKENS",
                             "content": {"parts": [{"text": "Alyssa, here is the sta"}]}}]}
        try:
            call("gemini", "s", "u",
                 {"gemini_api_key": _fake_key, "gemini_model": "gemini-9"})
            _truncated_raised = False
        except ProviderError:
            _truncated_raised = True
        check("gemini: MAX_TOKENS with PARTIAL text still raises -- a "
              "cut-off answer must never look like a completed one",
              _truncated_raised)

        # S232 — generate_image(): its own code path (not the call() dispatch),
        # own tests. Real bytes out of a mocked inlineData response, and a
        # missing key refuses the same way every other provider does.
        _fake_png = b"\x89PNG\r\n\x1a\nFAKE-IMAGE-BYTES"
        globals()["_http_post"] = lambda *a, **k: {
            "candidates": [{"finishReason": "STOP", "content": {"parts": [
                {"text": "Here you go"},
                {"inlineData": {"mimeType": "image/png",
                                 "data": base64.b64encode(_fake_png).decode()}}]}}]}
        _img = generate_image("draw a mind-map", {"gemini_api_key": _fake_key})
        check("generate_image: returns the decoded image bytes from inlineData",
              _img == _fake_png)

        try:
            generate_image("draw a mind-map", {})
            _no_key_raised = False
        except ProviderError:
            _no_key_raised = True
        check("generate_image: no gemini_api_key raises, same as every other provider",
              _no_key_raised)

        globals()["_http_post"] = lambda *a, **k: {
            "candidates": [{"finishReason": "STOP",
                             "content": {"parts": [{"text": "no image today"}]}}]}
        try:
            generate_image("draw a mind-map", {"gemini_api_key": _fake_key})
            _no_image_raised = False
        except ProviderError:
            _no_image_raised = True
        check("generate_image: a text-only reply (no inlineData) raises rather "
              "than silently returning nothing to attach",
              _no_image_raised)

        # The inverse that matters: a FAILED call must not leave the previous
        # call's model standing in for an answer it never gave.
        try:
            call("ollama", "s", "u", {"ollama_url": "http://x"})   # no model
        except ProviderError:
            pass
        check("a call that never reached the wire leaves None, not the "
              "last successful model",
              last_model() is None)

        # ── S119: last_model() is PER-THREAD. halftime_catalogue stamps every
        # entry's extracted_by from it, so if two concurrent extractions shared
        # one slot the client artefact would be labelled with whichever call
        # happened to finish last -- silently, and only in the parallel path.
        import threading as _threading
        _seen = {}
        _ready = _threading.Barrier(2)

        def _worker(name):
            # Set the slot exactly as a provider does, then wait for the other
            # thread to have set ITS value before reading. Both writes land
            # before either read, so a SHARED slot cannot pass by scheduling
            # luck -- one of the two reads would see the other's model.
            _LAST.model = name
            _ready.wait(timeout=5)
            _seen[name] = last_model()
        _t1 = _threading.Thread(target=_worker, args=("model-A",))
        _t2 = _threading.Thread(target=_worker, args=("model-B",))
        _t1.start(); _t2.start(); _t1.join(5); _t2.join(5)
        check("last_model() is isolated per thread — concurrent calls cannot "
              "mislabel each other",
              _seen.get("model-A") == "model-A"
              and _seen.get("model-B") == "model-B")
        check("...and the MAIN thread is untouched by what worker threads set",
              last_model() is None)

        # ── S132: every call() lands in the spend ledger. Tempfile ledger and
        # pricing (T32: never the live out/ ledger) injected through the same
        # creds["llm_budget"] block the boxes use, so the resolution under test
        # is the real one. Each positive check has its inverse (record=False,
        # empty reply, unwritable path) so a hook that fires ALWAYS or NEVER
        # both fail here.
        import tempfile as _tf, os as _os, shutil as _sh
        _td = _tf.mkdtemp(prefix="llmprov-selftest-")
        _pricing = _os.path.join(_td, "pricing.json")
        _ledger = _os.path.join(_td, "sub", "ledger.jsonl")
        with open(_pricing, "w") as _f:
            json.dump({"models": {"stub-model": {"in": 1.0, "out": 2.0},
                                  "local-free": {"in": 0.0, "out": 0.0}}}, _f)
        _bud = {"llm_budget": {"pricing_path": _pricing, "ledger_path": _ledger,
                               "box": "selftest"}}

        def _rows():
            return ([json.loads(l) for l in open(_ledger)]
                    if _os.path.exists(_ledger) else [])

        class _NoRow(dict):
            # Missing keys read as None, so `_last()["task"] == "x"` is simply
            # False when nothing was written. A hook that never fires must FAIL
            # these checks legibly -- not crash the suite with IndexError or
            # KeyError -- because the mutation control needs the full pattern.
            def __missing__(self, key):
                return None

        def _last():
            return (_rows() or [_NoRow()])[-1]

        def _stub(model, text="reply"):
            def _p(c, s, u, m):
                _LAST.model = model       # exactly what a real adapter does
                return text
            return _p

        # T77 first: with recording OFF (as the suite has been running so far), a
        # call that would otherwise record writes NOTHING -- this is the guard
        # that keeps every other check in this file off the live ledger.
        _PROVIDERS["anthropic"] = _stub("stub-model")
        call("anthropic", "sys", "usr", dict(_bud, anthropic_api_key="k"))
        check("ledger: with recording(False) a recordable call writes NOTHING "
              "(T77 -- the suite's live-ledger guard)",
              not _os.path.exists(_ledger) and _RECORDING is False)
        _prev_rec = _RECORDING
        globals()["_RECORDING"] = True      # this block, and only this block, records

        call("anthropic", "sys", "usr", dict(_bud, anthropic_api_key="k"))
        _r = _rows()
        check("ledger: a cloud call writes exactly one row", len(_r) == 1)
        check("ledger: the row carries provider, the WIRE model, the box and a cost",
              bool(_r) and _r[0]["provider"] == "anthropic"
              and _r[0]["model"] == "stub-model" and _r[0]["box"] == "selftest"
              and _r[0]["cost"] > 0)
        check("ledger: the default task is this process's script name",
              bool(_r) and _r[0]["task"] == DEFAULT_TASK and DEFAULT_TASK != "")
        call("anthropic", "s", "u", dict(_bud, anthropic_api_key="k"), task="tagged-x")
        check("ledger: task= overrides the default and doubles as session_id",
              _last()["task"] == "tagged-x" and _last()["session_id"] == "tagged-x")
        _n = len(_rows())
        call("anthropic", "s", "u", dict(_bud, anthropic_api_key="k"), record=False)
        check("ledger: record=False writes nothing (ensemble records its own "
              "council/judge -- otherwise counted twice)", len(_rows()) == _n)
        _PROVIDERS["ollama"] = _stub("local-free")
        call("ollama", "s", "u", dict(_bud, ollama_url="http://o", ollama_model="local-free"))
        check("ledger: a LOCAL call is recorded at $0 -- volume visible, cost true",
              _last()["provider"] == "ollama" and _last()["cost"] == 0.0
              and not _last().get("unpriced"))
        _PROVIDERS["openai"] = _stub("brand-new-unpriced")
        call("openai", "s", "u", dict(_bud, openai_api_key="k"))
        check("ledger: an UNPRICED model is still written, flagged, at the "
              "conservative rate -- never silently dropped (S103)",
              _last()["model"] == "brand-new-unpriced"
              and _last().get("unpriced") is True and _last()["cost"] > 0)
        _PROVIDERS["anthropic"] = _stub("stub-model", "")
        _n = len(_rows())
        call("anthropic", "s", "u", dict(_bud, anthropic_api_key="k"))
        check("ledger: an EMPTY reply is not a billable call -- no row", len(_rows()) == _n)
        _PROVIDERS["anthropic"] = _stub("stub-model")
        _bad = {"llm_budget": dict(_bud["llm_budget"],
                                   ledger_path=_os.path.join(_pricing, "under-a-file", "x.jsonl"))}
        _got = call("anthropic", "s", "u", dict(_bad, anthropic_api_key="k"))
        check("ledger: an unwritable ledger never breaks the call (best-effort)",
              _got == "reply")
        _PROVIDERS["anthropic"] = _stub("stub-model", "A")
        _PROVIDERS["openai"] = _stub("stub-model", "B")
        _n = len(_rows())
        escalate("s", "u", dict(_bud, anthropic_api_key="a", openai_api_key="o"),
                 mode="council", task="esc-tag")
        check("ledger: escalate() forwards task= to EVERY member call",
              len(_rows()) == _n + 2 and all(x["task"] == "esc-tag" for x in _rows()[-2:]))
        _n = len(_rows())
        escalate("s", "u", dict(_bud, anthropic_api_key="a"), mode="single", record=False)
        check("ledger: escalate() forwards record=False", len(_rows()) == _n)
        _LAST.model = None            # leave the thread slot as the caller found it
        globals()["_RECORDING"] = _prev_rec   # back to OFF for the rest of the suite
        _sh.rmtree(_td, ignore_errors=True)

        # ── S177: call_local_first() — the extracted vLLM->ollama->cloud chain ──
        # Reuses the _stub(model, text) helper already defined above (line 998);
        # its first arg is the recorded "model" (irrelevant here), second is
        # the reply text, which is all these checks care about.
        _PROVIDERS.clear()
        _PROVIDERS.update(_real_providers)
        _PROVIDERS["vllm"] = _stub("m", "vllm answer")
        _PROVIDERS["ollama"] = _stub("m", "ollama answer")
        _PROVIDERS["anthropic"] = _stub("m", "cloud answer")
        _cr = {"vllm_url": "http://v", "vllm_model": "m",
              "ollama_url": "http://o", "ollama_model": "m",
              "anthropic_api_key": "a"}
        _res, _tier = call_local_first("s", "u", _cr)
        check("call_local_first: vLLM configured and healthy -> used, no fallback",
              _res == "vllm answer" and _tier == "vllm")

        _PROVIDERS["vllm"] = lambda c, s, u, m: (
            _ for _ in ()).throw(ProviderError("down"))
        _stats = {}
        _res, _tier = call_local_first("s", "u", _cr, stats=_stats)
        check("call_local_first: dead vLLM falls to ollama, counted "
              "vllm_fallback (never silently an escalation)",
              _res == "ollama answer" and _tier == "ollama"
              and _stats.get("vllm_fallback") == 1)

        _PROVIDERS["ollama"] = lambda c, s, u, m: (
            _ for _ in ()).throw(ProviderError("down too"))
        _res, _tier = call_local_first("s", "u", _cr)
        check("call_local_first: both local tiers down -> escalates to cloud",
              _res == "cloud answer" and _tier == "anthropic")

        _res, _tier = call_local_first(
            "s", "u", {"anthropic_api_key": "a"})
        check("call_local_first: no local creds at all -> straight to cloud, "
              "no vLLM/ollama attempted",
              _res == "cloud answer" and _tier == "anthropic")

        _PROVIDERS["vllm"] = _stub("m", "vllm answer")
        _PROVIDERS["ollama"] = _stub("m", "ollama answer")
        _res, _tier = call_local_first(
            "s", "u", _cr,
            parse=lambda r: r if r == "cloud answer" else None)
        check("call_local_first: a parse gate that rejects both local tiers "
              "falls all the way to cloud (mirrors halftime's parse_acts gate)",
              _res == "cloud answer" and _tier == "anthropic")
        _res, _tier = call_local_first(
            "s", "u", _cr,
            parse=lambda r: r if r == "ollama answer" else None)
        check("call_local_first: parse gate rejects vLLM's reply specifically "
              "but accepts ollama's -- falls through one tier, not all",
              _tier == "ollama")

        try:
            call_local_first("s", "u", _cr, parse=lambda r: None)
            _all_rejected_raised = False
        except ProviderError:
            _all_rejected_raised = True
        check("call_local_first: a parse gate that rejects EVERY tier "
              "including cloud raises, rather than returning a value that "
              "failed its own gate", _all_rejected_raised)

        _seen_model = {}

        def _capture_ollama(c, s, u, m):
            _seen_model["m"] = c.get("ollama_model")
            return "ok"
        _PROVIDERS["ollama"] = _capture_ollama
        call_local_first("s", "u",
                         {"ollama_url": "http://o", "ollama_model": "default-model",
                          "anthropic_api_key": "a"},
                         local_model="qwen2.5-coder:14b")
        check("call_local_first: local_model= overrides the box's configured "
              "ollama_model for this call only",
              _seen_model.get("m") == "qwen2.5-coder:14b")

        # S177: the cloud-escalation tier must NOT inherit anthropic_effort --
        # found live via the alopecia agent's own dry runs (see call_local_first's
        # docstring). Real adapters, HTTP stubbed, so this tests the actual
        # _anthropic() code path, not a restatement of the fix.
        _PROVIDERS.clear()
        _PROVIDERS.update(_real_providers)
        _seen_effort_body = {}
        globals()["_http_post"] = lambda url, headers, body, timeout=_TIMEOUT: (
            _seen_effort_body.update(b=body) or
            {"choices": [{"message": {"content": "ok"}}],
             "content": [{"type": "text", "text": "ok"}]})
        call_local_first("s", "u", {"anthropic_api_key": "a",
                                    "anthropic_effort": "max"})
        check("call_local_first: the box's anthropic_effort does NOT reach the "
              "wire through this function's cloud tier -- it sends LOW, never "
              "the configured max and never nothing (S274: no effort = HIGH "
              "on claude-sonnet-5)",
              _seen_effort_body["b"].get("output_config") == {"effort": "low"})
        call_local_first("s", "u", {"anthropic_api_key": "a"})
        check("...and LOW is sent even when the box configures no effort at all",
              _seen_effort_body["b"].get("output_config") == {"effort": "low"})
        call("anthropic", "s", "u", {"anthropic_api_key": "a",
                                     "anthropic_effort": "max"})
        check("...while a DIRECT call()/escalate() (e.g. call_council's own "
              "path) still gets full effort -- this strips it only inside "
              "call_local_first, not from the credential globally",
              _seen_effort_body["b"].get("output_config") == {"effort": "max"})

        _LAST.model = None            # leave the thread slot as the caller found it
        _PROVIDERS.clear()
        _PROVIDERS.update(_real_providers)
    finally:
        globals()["_http_post"] = _real_post
        _PROVIDERS.clear()
        _PROVIDERS.update(_real_providers)
        _rec_guard.__exit__(None, None, None)   # recording back to its pre-suite state
        globals().update(_live_ledgers)         # S257: live ledger paths restored
        _sh0.rmtree(_td0, ignore_errors=True)

    return _ok


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] in ("selftest", "--selftest"):
        # selftest() is now IMPORTABLE, which is the whole point of lifting it
        # out of this block -- so its cleanup is load-bearing in a way it never
        # was as a __main__ script: a caller like dev_agent's gate 2 keeps
        # running afterwards with whatever this left behind. Nothing inside
        # selftest() can check its own `finally`, so the check lives here.
        # Without it, deleting the restore passes the entire suite (measured).
        _snap_providers = dict(_PROVIDERS)
        _snap_post = _http_post
        _passed = selftest()
        _clean = (_PROVIDERS == _snap_providers
                  and all(_PROVIDERS[k] is _snap_providers[k] for k in _snap_providers)
                  and _http_post is _snap_post
                  and last_model() is None
                  and _RECORDING is True)      # S133: recording must be back ON
        print(f"  [{'OK ' if _clean else 'FAIL'}] selftest leaves no stubbed "
              f"provider, HTTP layer, last_model or recording switch behind "
              f"(it is importable)")
        _passed = _passed and _clean
        print("PASS" if _passed else "FAIL")
        sys.exit(0 if _passed else 1)
