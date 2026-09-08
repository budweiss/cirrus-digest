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
import sys
import threading
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ORDER = ["anthropic", "gemini", "grok", "openai", "deepseek"]
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
        _B.record_call(creds, provider, last_model() or "?",
                       len(system or "") + len(user or ""), len(reply or ""),
                       task=(task or DEFAULT_TASK),
                       app_dir=str(Path(__file__).resolve().parent))
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


def _openai_compatible(url, key, model, system, user, max_tokens,
                       timeout=_TIMEOUT):
    """OpenAI Chat Completions shape — shared by OpenAI, xAI (Grok), DeepSeek.

    `timeout` exists for the vLLM path only (S125): a thinking-on extraction
    measured 17-442 s at 12 tok/s, so 120 s would escalate most of them to a
    paid call. Every other provider keeps the module default.
    """
    _LAST.model = model
    resp = _http_post(
        url,
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        {"model": model, "max_tokens": max_tokens,
         "messages": [{"role": "system", "content": system},
                      {"role": "user", "content": user}]},
        timeout=timeout,
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

    _LAST.model = model
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
    return _openai_compatible(url.rstrip("/") + "/v1/chat/completions",
                              "local", model, system, user, max_tokens,
                              timeout=timeout)


_PROVIDERS = {
    "ollama":    _ollama,
    "vllm":      _vllm,
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


def call(provider, system, user, creds, max_tokens=16384, retries=1, *,
         task=None, record=True):
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
    _LAST.model = None            # never let a stale model answer for this call
    if provider not in _PROVIDERS:
        raise ProviderError(f"unknown provider: {provider}")
    reply = ""
    for _ in range(retries + 1):
        reply = _PROVIDERS[provider](creds, system, user, max_tokens) or ""
        if reply.strip():
            break
    if record and reply.strip():
        _record(provider, system, user, reply, creds, task)
    return reply


def escalate(system, user, creds, max_tokens=16384, mode=None, order=None, *,
             task=None, record=True):
    """Policy-driven call across configured providers.

    Reads defaults from creds['dev_escalation'] = {"mode":..., "order":[...]}.
      single   -> (provider, text)     first available in order
      failover -> (provider, text)     try in order until one succeeds
      council  -> [(provider, text_or_'ERROR: ...'), ...]  every available
    Raises ProviderError if no provider has a key.
    S132: task= and record= are forwarded to every call() (see call()).
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
                out.append((p, call(p, system, user, creds, max_tokens,
                                    task=task, record=record)))
            except ProviderError as e:
                out.append((p, f"ERROR: {e}"))
        return out

    if mode == "failover":
        last = None
        for p in avail:
            try:
                return (p, call(p, system, user, creds, max_tokens,
                                task=task, record=record))
            except ProviderError as e:
                last = e
        raise ProviderError(f"all providers failed; last error: {last}")

    # "single" (default)
    p = avail[0]
    return (p, call(p, system, user, creds, max_tokens, task=task, record=record))


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
    try:
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
            return {"choices": [{"message": {"content": "ok"}}]}
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

        # ── S103: last_model() — the model that actually went on the wire ──
        # Driven through the REAL adapters with only the HTTP layer stubbed, so
        # it tests the resolution rather than a restatement of it. Each
        # assertion is paired with its inverse: a recorder that is never
        # cleared, and one that is never set, both look fine from one direction.
        _PROVIDERS.clear()
        _PROVIDERS.update(_real_providers)
        globals()["_http_post"] = lambda *a, **k: {
            "choices": [{"message": {"content": "hi"}}]}
        call("ollama", "s", "u",
             {"ollama_url": "http://x", "ollama_model": "qwen3.8:27b"})
        check("last_model names the LOCAL model, not just the provider",
              last_model() == "qwen3.8:27b")

        call("openai", "s", "u",
             {"openai_api_key": "k", "openai_model": "gpt-x"})
        check("...and it moves with the provider, rather than sticking",
              last_model() == "gpt-x")

        globals()["_http_post"] = lambda *a, **k: {
            "candidates": [{"content": {"parts": [{"text": "hi"}]}}]}
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
    finally:
        globals()["_http_post"] = _real_post
        _PROVIDERS.clear()
        _PROVIDERS.update(_real_providers)
        _rec_guard.__exit__(None, None, None)   # recording back to its pre-suite state

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
