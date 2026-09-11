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
from datetime import datetime, timedelta
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
# S91 (2026-09-01): 64 was itself too tight, one provider over. gemini-flash-latest
# is a THINKING model and draws thinking tokens from the same budget before it
# emits any text: measured on cumulus1 over 12 runs it spent 51-61 tokens
# thinking on this very prompt, so 5 of 12 probes came back with NO text and
# cirrus-modelhealth reported a healthy Gemini as broken every morning from
# 2026-08-31. Note the shape: not a clean break but a ~40% flaky one, which is
# why it read as intermittent rather than as a bad constant.
#
# 512 is ~8x the largest thinking preamble measured, and the probe is five calls
# a day in total, so headroom here costs effectively nothing while a too-tight
# budget costs a false alarm every single morning. If this ever needs raising a
# THIRD time, stop ratcheting and drop thinking from the probe instead.
PROBE_TOKENS = 512

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
    bad = ("image", "tts", "audio", "vision", "embedding", "lyria",
           "nano-banana", "deep-research", "computer-use", "robotics",
           "transcribe", "gemma")
    usable = [m for m in ms if not any(b in m for b in bad)]

    # S92 — TWO corrections, both of which this healer would otherwise have
    # made worse rather than better.
    #
    # 1. STAY IN THE CONFIGURED TIER. This used to consider FLASH models only,
    #    so a failing Pro model was "healed" by silently dropping to the budget
    #    tier. Buddy moved both boxes OFF Flash on 2026-09-01 precisely because
    #    its answers were unreliable in the client-facing council; a self-heal
    #    that quietly puts it back is undoing a human decision without saying so.
    #    If nothing in the same tier works, return NOTHING — model_health then
    #    reports "broken (needs you)", which is the honest outcome. An alert
    #    beats a silent downgrade.
    #
    # 2. PREFER A PINNED VERSION OVER A ROLLING ALIAS. This used to return
    #    `alias + specific`, aliases FIRST. That is how a heal lands on
    #    `gemini-flash-latest` — the exact rolling alias that rolled onto a
    #    thinking model and broke cirrus-modelhealth every morning from
    #    2026-08-31 (T55). A pinned name can be retired loudly; an alias
    #    changes underneath you silently. Aliases are kept as a LAST resort,
    #    because some key is better than none.
    cur = (creds.get("gemini_model") or "")
    want_flash = "flash" in cur
    tier = [m for m in usable if ("flash" in m) == want_flash]
    # A preview is acceptable only if that is already what we are running --
    # never promote a stable config onto a preview behind the operator's back.
    if "preview" not in cur:
        tier = [m for m in tier if "preview" not in m]
    alias = [m for m in tier if m.endswith("latest")]
    specific = sorted([m for m in tier if m not in alias], reverse=True)
    return specific + alias


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


# ── Local runtime drift (S91) ─────────────────────────────────────────────────
# Buddy, 2026-09-01: "is there any reason our agents running on Cirrus and Cumulus
# can test for this at least once or twice a week instead of us finding this now?"
#
# Fair hit. This file has watched the five PAID API providers daily since S56 and
# even self-heals a retired model — but nothing watched the LOCAL runtime. CIRRUS
# sat on ollama 0.24.0 while 0.33.2 shipped, and we only found out because a
# model refused to pull and blocked a benchmark. That is the same class of gap
# this file already exists to close, one layer down.
#
# Deliberately REPORT-ONLY. Upgrading a runtime that serves the 02:00 digest is
# not a self-heal: today's upgrade needed a symlink swap, a 650 MB tree, and a
# rollback when the first attempt broke generation. Detection is automatic;
# the upgrade stays a decision.
RUNTIME_STATE = HERE / "logs" / "runtime-drift.json"
OLLAMA_RELEASES = "https://api.github.com/repos/ollama/ollama/releases/latest"


def _installed_ollama():
    """Version of the ollama BINARY, not of whatever server happens to answer.

    `ollama --version` queries the running server, so it reports the SERVER's
    version regardless of which binary you hand it — that is exactly how S91's
    upgrade "verified" a new binary and printed the old version back. Pointing
    OLLAMA_HOST at a dead port makes it report the client's own version.
    """
    import subprocess
    for exe in ("/usr/local/bin/ollama", "/usr/bin/ollama", "ollama"):
        try:
            env = dict(os.environ, OLLAMA_HOST="127.0.0.1:1")
            r = subprocess.run([exe, "--version"], capture_output=True, text=True,
                               timeout=20, env=env)
            m = re.search(r"client version is ([0-9][0-9.]*)", r.stdout + r.stderr)
            if m:
                return m.group(1)
            m = re.search(r"version is ([0-9][0-9.]*)", r.stdout + r.stderr)
            if m:
                return m.group(1)
        except Exception:
            continue
    return ""


def _latest_ollama():
    try:
        req = urllib.request.Request(
            OLLAMA_RELEASES, headers={"User-Agent": "cirrus-modelhealth"})
        with urllib.request.urlopen(req, timeout=25) as r:
            return (json.loads(r.read().decode()).get("tag_name") or "").lstrip("v")
    except Exception:
        return ""


def _ver_tuple(v):
    out = []
    for part in (v or "").split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return tuple(out + [0, 0, 0])[:3]


# S137. A cold load of a 17 GB model on the Mac takes longer than llm_providers'
# 120 s; this probe has its own budget so a slow load is not misread as broken.
LOCAL_LOAD_TIMEOUT = 180


def check_local_model_loads(creds):
    """(line, should_notify). Does the CONFIGURED local model actually LOAD and
    answer? One tiny completion against ollama_url with ollama_model, judged on
    the HTTP STATUS: a 200 means the runtime loaded the weights and generated;
    the text is irrelevant (so a thinking model's reasoning budget cannot make a
    healthy box read as broken). A 500 or a timeout means every local call on
    this box is going to the PAID model.

    Why this exists (S137, 2026-09-08): CIRRUS ran ollama 0.24.0 while
    qwen3.8:27b requires 0.32.12 ("unknown model architecture: qwen35"). The
    05:30 health run said "runtime: 0.24.0 is BEHIND", "models: 5 current" and
    five healthy cloud providers -- and every local call had returned HTTP 500
    since 2026-09-05, three days, unseen. /api/tags says a model EXISTS; the
    version check says the runtime is OLD; only a completion says it LOADS.
    Found because a promise-detection probe escalated to Haiku where ollama was
    expected. Notifies EVERY run it fails, unlike the once-per-release drift
    rule: drift is a decision, a model that will not load is an outage.
    Never raises."""
    url = (creds or {}).get("ollama_url")
    model = (creds or {}).get("ollama_model")
    if not url or not model:
        return ("local model: not configured on this box (no ollama_url / ollama_model)", False)
    body = json.dumps({"model": model, "max_tokens": 32,
                       "messages": [{"role": "user", "content": "Reply with the single word OK."}]}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = datetime.now()
    try:
        with urllib.request.urlopen(req, timeout=LOCAL_LOAD_TIMEOUT) as r:
            r.read()
        secs = (datetime.now() - t0).total_seconds()
        return (f"local model {model} LOADS and answers ({secs:.0f} s)", False)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode(errors="replace")[:160]
        except Exception:
            detail = ""
        return (f"local model {model} FAILS TO LOAD: HTTP {e.code} {detail} "
                f"-- every local call on this box is going to the PAID model", True)
    except Exception as e:  # noqa: BLE001 -- timeout, refused, DNS: all mean "not serving"
        return (f"local model {model} FAILS TO LOAD: {type(e).__name__}: {str(e)[:120]} "
                f"-- every local call on this box is going to the PAID model", True)


# ── Local → paid fallback rate (S141, watcher audit item 1) ───────────────────
# THE CLASS THIS GUARDS: a job that still runs, still reports success, and has
# quietly stopped using the free local model -- every call going to a paid one.
# S137 is the case: CIRRUS's local model returned HTTP 500 for three days and
# every "local" call there was billed, behind three green checks.
#
# check_local_model_loads() (S137) answers "can the model load AT ALL". This
# answers the question it cannot: "is the WORK actually going there?" A model
# that loads but is bypassed -- wrong config, a fallback chain that never
# recovers, a timeout too tight -- looks identical to a healthy box until the
# bill arrives.
#
# Providers that are FREE and ours. Everything else is billed.
LOCAL_PROVIDERS = {"vllm", "ollama"}

# Tasks with a local-first path, and the most paid share each may show over 24h.
#
# ⚠ THESE THRESHOLDS ARE PROVISIONAL AND DELIBERATELY LOOSE (S141). Measured
# 2026-09-09 over 7 days: catalogue 3 paid / 49 = 6.1%, routing 11 / 52 = 21.2%.
# The values the S140 handoff proposed (10% and 25%) sit just ABOVE those, which
# means they would have stayed silent through the very defect found the same
# morning -- the halftime jobs escalating because a 4,000-token budget truncated
# the local model's reply (fixed, 44b31fe). A threshold set at the level the
# system is already running at blesses the status quo; it is a check that can
# only ever fire for a catastrophe.
#
# They are kept loose FOR NOW so this alert does not cry wolf in its first days
# (T9), and the real work is the ZERO_LOCAL rule below, which needs no tuning.
# RE-TIGHTEN from post-fix data once three clean runs exist -- worklisted.
LOCAL_FIRST_TASKS = {
    "halftime_catalogue":    0.10,
    "halftime_routing":      0.25,
    "intake:promise_detect": 0.10,
    "billsnow:draft":        0.0,
}

# Below this many calls in the window, a share is noise: 1 paid row out of 3 is
# 33% and means nothing. The ZERO_LOCAL rule still applies at any sample size,
# because "this task ran and NOTHING went local" is not a sampling artefact.
FALLBACK_MIN_ROWS = 8


def _ledger_rows(path, hours=24, now=None):
    """Rows from the spend ledger inside the window. Never raises."""
    from datetime import timedelta, timezone
    now = now or datetime.now(timezone.utc)
    cut = now - timedelta(hours=hours)
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    # spend ledger says "ts", truncation ledger says "at"
                    ts = datetime.fromisoformat(d.get("ts") or d.get("at") or "")
                except Exception:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cut:
                    out.append(d)
    except Exception:
        return []
    return out


def fallback_verdict(rows, tasks=None, min_rows=FALLBACK_MIN_ROWS):
    """(line, should_notify) from ledger rows. PURE -- no I/O, so it is testable.

    Two rules, deliberately different in nature:
      * ZERO_LOCAL -- the task ran and not ONE call went to a local provider.
        That is the S137 shape and it fires at ANY sample size, because it is
        not a rate, it is an absence.
      * OVER -- the paid share exceeded this task's threshold, on a sample big
        enough to mean something.
    A task with no rows at all is silent: it did not run in the window, which is
    a scheduling question, not a fallback question, and other checks own it.
    """
    tasks = LOCAL_FIRST_TASKS if tasks is None else tasks
    seen, alarms = [], []
    for task, thr in sorted(tasks.items()):
        local = sum(1 for r in rows if r.get("task") == task
                    and r.get("provider") in LOCAL_PROVIDERS)
        paid = sum(1 for r in rows if r.get("task") == task
                   and r.get("provider") not in LOCAL_PROVIDERS)
        total = local + paid
        if not total:
            continue
        share = paid / total
        seen.append(f"{task} {paid}/{total} paid ({share:.0%})")
        if local == 0 and paid >= 1:
            alarms.append(f"{task}: ALL {paid} call(s) went PAID, none local "
                          f"-- the local path is not being used at all")
        elif share > thr and total >= min_rows:
            alarms.append(f"{task}: {share:.0%} paid ({paid}/{total}) "
                          f"over its {thr:.0%} threshold")
    if not seen:
        return ("local fallback: no local-first task ran in the last 24h", False)
    if alarms:
        return ("local fallback: " + "; ".join(alarms), True)
    return ("local fallback: " + ", ".join(seen), False)


def truncation_verdict(rows):
    """(line, should_notify) for cut-off LOCAL replies. PURE, so it is testable.

    S141. `finish_reason == "length"` means the model was still talking when we
    stopped it. On a PAID model that is a quality call to make. On a LOCAL one
    it is a bill: the halftime jobs read an unparseable local reply as "the
    local model could not do this" and escalate to a cloud provider, so every
    reply WE truncate buys a paid call. On 2026-09-09 that ran all day -- the
    reasoning tokens of a thinking model ate a 4,000-token budget before the
    JSON began -- and nothing recorded it. It had to be reproduced by hand.

    Any local truncation at all notifies. There is no sensible non-zero
    allowance: it is never right for us to cut our own free model off and then
    pay someone else the same question.
    """
    local = [r for r in rows if (r.get("provider") or "") in LOCAL_PROVIDERS]
    if not local:
        return ("truncation: no local reply was cut off in the last 24h", False)
    by_task = {}
    for r in local:
        by_task[r.get("task") or "(untagged)"] = by_task.get(
            r.get("task") or "(untagged)", 0) + 1
    detail = ", ".join(f"{t} x{n}" for t, n in sorted(by_task.items()))
    return (f"truncation: {len(local)} LOCAL repl(ies) cut off at max_tokens "
            f"({detail}) -- each one likely bought a PAID call", True)


def check_local_truncation(creds=None, ledger=None, now=None):
    """(line, should_notify). Reads this box's truncation ledger. Never raises."""
    path = ledger or (HERE / "logs" / "llm_truncations.jsonl")
    return truncation_verdict(_ledger_rows(path, now=now))


def check_local_fallback_rate(creds=None, ledger=None, now=None):
    """(line, should_notify). Reads THIS box's spend ledger. Never raises."""
    path = ledger or (HERE / "out" / "llm-spend-ledger.jsonl")
    return fallback_verdict(_ledger_rows(path, now=now))


def check_detached_jobs(creds=None):
    """(line, should_notify). S141, watcher audit item 5.

    A job a session launched and stopped watching can die -- OOM, a reboot, a
    dropped ssh -- and leave behind exactly what a SLOW job leaves behind,
    which is nothing. detached.py has such jobs write a marker on start and
    update it on exit; this is the thing that reads them, so a marker nobody
    looks at is still looked at. Never raises."""
    try:
        sys.path.insert(0, str(HERE))
        import detached
        problems, ok = detached.sweep_verdict(detached.load_markers())
    except Exception as e:  # noqa: BLE001
        return (f"detached jobs: sweep failed ({type(e).__name__}: {e})", False)
    if problems:
        return ("detached jobs: " + "; ".join(problems[:3])
                + (f" (+{len(problems)-3} more)" if len(problems) > 3 else ""),
                True)
    return (f"detached jobs: {ok} marker(s), none unaccounted for", False)


# ── Endpoint config vs REALITY (S141, watcher audit item 4) ──────────────────
# `serve-tp2.sh` says what we ASK the engine for. It does not say what the
# engine DID. vLLM resolves several of those flags against what the hardware
# and the installed wheels actually support, and when the answer is "no" it
# picks something else and logs one INFO line:
#
#   Using FLASHINFER attention backend out of potential backends:
#     ['FLASHINFER', 'TRITON_ATTN']
#
# The list is the point. A box that quietly resolved to TRITON_ATTN would serve
# every request, pass the watchdog's completion probe, pass access_check, and
# be slower and differently-numeric than the one we benchmarked -- with nothing
# anywhere saying so. That is the same silhouette as the S139 crash-loop (nvcc
# missing from the Ray workers' PATH), minus the crash that made it visible.
#
# So this reads the ENGINE's own boot lines, not the launch script and not the
# unit file, and compares them against what we believe we are running.
ENDPOINT_UNIT = "vllm-tp2"
ENDPOINT_EXPECT = {
    # what we benchmarked and what the plan's numbers assume (S139, S141)
    "backend": "FLASHINFER",
    "kv_cache_dtype": "float8",
}


def parse_endpoint_boot(text):
    """Boot log -> {backend, kv_cache_dtype, kv_tokens, alternatives}. PURE.

    Reads the LAST occurrence of each fact, which is the most recent boot.
    Missing keys stay None: an absent line is an unknown, never a pass (T76).
    """
    out = {"backend": None, "kv_cache_dtype": None, "kv_tokens": None,
           "alternatives": []}
    for line in (text or "").splitlines():
        m = re.search(r"Using (\w+) attention backend out of potential "
                      r"backends: \[([^\]]*)\]", line)
        if m:
            out["backend"] = m.group(1)
            out["alternatives"] = [x.strip().strip("'\"")
                                   for x in m.group(2).split(",") if x.strip()]
        m = re.search(r"kv_cache_dtype=torch\.(\w+)", line)
        if m:
            out["kv_cache_dtype"] = m.group(1)
        m = re.search(r"GPU KV cache size: ([\d,]+) tokens", line)
        if m:
            out["kv_tokens"] = int(m.group(1).replace(",", ""))
    return out


def endpoint_config_verdict(facts, expect=None):
    """(line, should_notify) comparing resolved facts to what we expect. PURE."""
    expect = ENDPOINT_EXPECT if expect is None else expect
    if facts is None:
        return ("endpoint config: no TP=2 endpoint on this box", False)
    if not any(facts.get(k) for k in ("backend", "kv_cache_dtype", "kv_tokens")):
        return ("endpoint config: UNREADABLE — the boot log gave no engine "
                "facts, so nothing was verified (not 'fine')", True)
    bad = []
    got_backend = facts.get("backend")
    if got_backend and got_backend.upper() != expect["backend"].upper():
        alts = ", ".join(facts.get("alternatives") or []) or "?"
        bad.append(f"attention backend resolved to {got_backend}, not "
                   f"{expect['backend']} (candidates were: {alts}) — every "
                   f"request still succeeds, and the benchmarks no longer apply")
    got_kv = (facts.get("kv_cache_dtype") or "")
    if got_kv and expect["kv_cache_dtype"] not in got_kv:
        bad.append(f"kv cache dtype is {got_kv}, not {expect['kv_cache_dtype']} "
                   f"— the fp8 KV pool (1.46x) is not in effect")
    detail = (f"backend={got_backend or '?'}, kv={got_kv or '?'}, "
              f"pool={facts.get('kv_tokens') or '?'} tokens")
    if bad:
        return ("endpoint config DRIFTED: " + "; ".join(bad) + f" [{detail}]", True)
    return (f"endpoint config: as configured ({detail})", False)


def _endpoint_boot_log():
    """The current boot's log for the endpoint unit, or None if not this box."""
    import subprocess
    try:
        r = subprocess.run(["systemctl", "--user", "show", ENDPOINT_UNIT,
                            "-p", "ActiveEnterTimestamp", "--value"],
                           capture_output=True, text=True, timeout=20)
        since = (r.stdout or "").strip()
        if r.returncode != 0 or not since:
            return None
        j = subprocess.run(["journalctl", "--user", "-u", ENDPOINT_UNIT,
                            "--since", since, "--no-pager"],
                           capture_output=True, text=True, timeout=60)
        return j.stdout if j.returncode == 0 else None
    except Exception:
        return None


def check_endpoint_config(creds=None):
    """(line, should_notify). Never raises."""
    log = _endpoint_boot_log()
    if log is None:
        return endpoint_config_verdict(None)
    return endpoint_config_verdict(parse_endpoint_boot(log))


# S141. How long a STANDING version-drift condition stays quiet between
# reminders. Not daily (that is nagging, and it gets the channel muted) and not
# never (that is amnesia, which is how CIRRUS sat three days on a model that
# would not load).
RUNTIME_NAG_DAYS = 7


def _days_since(stamp):
    """Whole days since a 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' stamp, or None."""
    if not stamp:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return (datetime.now() - datetime.strptime(stamp, fmt)).days
        except Exception:
            continue
    return None


def check_local_runtime():
    """(line, should_notify). Never raises — a drift check must not break the
    health run it rides along with."""
    try:
        cur = _installed_ollama()
        if not cur:
            return ("ollama: NOT INSTALLED or unreadable on this box", False)
        latest = _latest_ollama()
        if not latest:
            # Say so. An unreachable release API is not "up to date" — that
            # silent-zero reading is the failure this whole file guards against.
            return (f"ollama {cur} installed; latest UNKNOWN (release API "
                    f"unreachable) — drift not checked", False)
        if _ver_tuple(cur) >= _ver_tuple(latest):
            return (f"ollama {cur} — current (latest {latest})", False)

        line = f"ollama {cur} is BEHIND latest {latest}"
        # ── S141, watcher audit item 3: once-only vs. nagging ───────────────
        # The original rule notified once per NEW upstream release and then
        # went quiet, for a good reason that is written above this line in
        # every previous version: a finding repeated every morning while nobody
        # acts is how an alert channel gets muted (T9), and this one repeats for
        # weeks by design because the fix is a deliberate upgrade.
        #
        # T78 says the opposite and is also right: a STANDING CONDITION that
        # stops being mentioned has been silently accepted. CIRRUS sat on
        # ollama 0.24.0 while its configured model could not load, and the
        # drift line had long since gone quiet.
        #
        # Both are right in different regimes, so the answer is neither side --
        # it is a CADENCE. Speak on the day a new release lands, then hold for
        # a week, then speak again while it is still true, saying how long it
        # has been. Daily is nagging; never is amnesia; weekly-with-an-age is
        # a reminder.
        #
        # Note this is only correct for a standing CONDITION. The sibling
        # checks -- a new cloud model, a changed registry digest -- are EVENTS,
        # true once, and they correctly stay once-only.
        prev = {}
        try:
            prev = json.loads(RUNTIME_STATE.read_text())
        except Exception:
            pass
        new_release = prev.get("notified_latest") != latest
        since = _days_since(prev.get("at"))
        due = (since is not None and since >= RUNTIME_NAG_DAYS)
        notify = new_release or due
        first_seen = prev.get("first_seen") or datetime.now().strftime("%Y-%m-%d")
        if new_release:
            first_seen = datetime.now().strftime("%Y-%m-%d")
        else:
            age = _days_since(first_seen)
            if age:
                line += f" — still behind after {age}d"
        if notify:
            try:
                RUNTIME_STATE.parent.mkdir(parents=True, exist_ok=True)
                RUNTIME_STATE.write_text(json.dumps(
                    {"notified_latest": latest, "installed": cur,
                     "first_seen": first_seen,
                     "at": datetime.now().strftime("%Y-%m-%d %H:%M")}, indent=2) + "\n")
            except Exception:
                pass
        return (line, notify)
    except Exception as e:  # noqa: BLE001
        return (f"ollama drift check failed: {type(e).__name__}: {e}", False)


MODEL_DRIFT_STATE = HERE / "logs" / "model-drift.json"
OLLAMA_TAGS = "http://127.0.0.1:11434/api/tags"
OLLAMA_REGISTRY = "https://registry.ollama.ai/v2/%s/manifests/%s"


def _registry_digest(name):
    """sha256 of the registry's CURRENT manifest for this tag, or "" if unknown.

    Ollama's registry sits behind Cloudflare and does NOT return the
    Docker-Content-Digest header, so the digest has to be computed from the
    manifest bytes. That is what the digest IS — sha256 of the canonical
    manifest — so this is the real comparison, not an approximation of one.
    """
    import hashlib
    if ":" not in name:
        return ""
    repo, tag = name.split(":", 1)
    path = repo if "/" in repo else "library/" + repo
    try:
        req = urllib.request.Request(
            OLLAMA_REGISTRY % (path, tag),
            headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json",
                     "User-Agent": "cirrus-modelhealth"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return hashlib.sha256(r.read()).hexdigest()
    except Exception:
        return ""


def check_model_drift():
    """(line, should_notify) — are our LOCAL model tags still what the registry
    ships? Never raises.

    Buddy, 2026-09-01: "can we also make sure our agents are keeping track of our
    model releases too." check_local_runtime() watches the ollama RUNTIME; this
    watches the MODELS running on it. An ollama tag is mutable — `qwen2.5:14b`
    can be rebuilt upstream and the copy on disk silently becomes months old,
    which is exactly the state CIRRUS was in (three-month-old weights) with
    nothing able to say so.

    Verified against a control before shipping: qwen3.8:27b, pulled the same day,
    reports CURRENT. A check that flagged everything would be worse than none.
    """
    try:
        with urllib.request.urlopen(OLLAMA_TAGS, timeout=20) as r:
            models = (json.loads(r.read()) or {}).get("models") or []
    except Exception:
        return ("models: no local ollama to inspect", False)
    if not models:
        return ("models: ollama is running but holds no models", False)

    stale, unknown, current = [], [], 0
    for m in models:
        name = m.get("name") or ""
        local = (m.get("digest") or "").replace("sha256:", "")
        remote = _registry_digest(name)
        if not remote:
            # An unreachable registry is NOT "up to date". Same rule as the
            # runtime check: silence and agreement must not render the same.
            unknown.append(name)
        elif local and local != remote:
            stale.append(name)
        else:
            current += 1

    parts = ["%d current" % current]
    if stale:
        parts.append("%d STALE (%s)" % (len(stale), ", ".join(sorted(stale)[:4])))
    if unknown:
        parts.append("%d unchecked (%s)" % (len(unknown), ", ".join(sorted(unknown)[:3])))
    line = "models: " + " · ".join(parts)

    if not stale:
        return (line, False)

    # Notify once per (model, new digest), not daily. Refreshing a tag is a
    # deliberate `ollama pull` on a box serving live jobs, so this can sit
    # unactioned for a while — and a nightly repeat would train it to be ignored.
    prev = {}
    try:
        prev = json.loads(MODEL_DRIFT_STATE.read_text())
    except Exception:
        pass
    seen = prev.get("notified") or {}
    fresh = [n for n in stale if seen.get(n) != _registry_digest(n)]
    if fresh:
        try:
            MODEL_DRIFT_STATE.parent.mkdir(parents=True, exist_ok=True)
            for n in stale:
                seen[n] = _registry_digest(n)
            MODEL_DRIFT_STATE.write_text(json.dumps(
                {"notified": seen,
                 "at": datetime.now().strftime("%Y-%m-%d %H:%M")}, indent=2) + "\n")
        except Exception:
            pass
    return (line, bool(fresh))


CLOUD_MODELS_STATE = HERE / "logs" / "cloud-models.json"

# Non-text modalities. A new image/audio/embedding model is a real release but
# not one that affects any lane we run, and reporting it trains the channel to
# be ignored.
_MODALITY_NOISE = ("image", "tts", "audio", "video", "embed", "robotics",
                   "transcribe", "whisper", "dall-e", "moderation", "realtime",
                   "computer-use", "vision", "lyria", "nano-banana", "veo",
                   "imagen", "sora", "rerank", "guard")

_LIST_ENDPOINTS = {
    "anthropic": ("https://api.anthropic.com/v1/models",
                  lambda k: {"x-api-key": k, "anthropic-version": "2023-06-01"}),
    "openai":    ("https://api.openai.com/v1/models",
                  lambda k: {"Authorization": "Bearer " + k}),
    "grok":      ("https://api.x.ai/v1/models",
                  lambda k: {"Authorization": "Bearer " + k}),
    "deepseek":  ("https://api.deepseek.com/v1/models",
                  lambda k: {"Authorization": "Bearer " + k}),
}


def _list_cloud_models(provider, creds):
    """Model ids a provider currently offers this key, or None if unreachable.

    None and empty-set are DELIBERATELY different: an unreachable provider must
    not read as "nothing new". Keys are used to build the request and are never
    returned or printed.
    """
    key = creds.get(_KEY_FIELD_CLOUD.get(provider, ""))
    if not key:
        return None
    try:
        if provider == "gemini":
            url = ("https://generativelanguage.googleapis.com/v1beta/models?key="
                   + urllib.parse.quote(key))
            req = urllib.request.Request(url, headers={"User-Agent": "cirrus-modelhealth"})
        else:
            url, hdrs = _LIST_ENDPOINTS[provider]
            req = urllib.request.Request(url, headers=hdrs(key))
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
        rows = d.get("data") or d.get("models") or []
        return {(m.get("id") or m.get("name", "")).split("/")[-1] for m in rows if m}
    except Exception:
        return None


_KEY_FIELD_CLOUD = {
    "anthropic": "anthropic_api_key", "gemini": "gemini_api_key",
    "openai": "openai_api_key", "grok": "grok_api_key",
    "deepseek": "deepseek_api_key",
}


def check_configured_model_delisted(creds):
    """(line, should_notify) — is a model we RUN no longer offered by its provider?

    S146, 2026-09-10, and it was live the day it was written. Buddy forwarded a
    DeepSeek notice: v4.1 is out, v4 unsupported after the 14th. We run
    `deepseek-v4-flash`. The provider's own /v1/models offered this key exactly
    `deepseek-flash` and `deepseek-v4-pro` -- ours was already gone from the
    list, four days ahead of the cutoff.

    Nothing here would have said so. There were two checks and neither asks this
    question:

      * check_cloud_model_releases() reports ids that are NEW. Additions, not
        removals. It ran that same evening and said "NEW: openai gpt-live-1".
      * test_model() makes a tiny live call, and a deprecated-but-not-yet-retired
        model ANSWERS. modelhealth printed "healthy: deepseek=deepseek-v4-flash"
        while the model was already delisted.

    So the working check and the release check agreed, and both were looking past
    the thing that will break on the 15th. T78 in one line: installed != fresh !=
    current-version != WORKS. "Works today" is not "supported tomorrow", and the
    provider says which is which by listing it or not.

    None from _list_cloud_models means UNREACHABLE and is reported as unchecked,
    never as delisted -- accusing a provider of retiring a model because the
    network blinked would get this muted in a week.
    """
    try:
        delisted, unchecked, checked = [], [], 0
        for prov in sorted(_KEY_FIELD_CLOUD):
            if not creds.get(_KEY_FIELD_CLOUD[prov]):
                continue
            cur = (creds.get(MODEL_FIELD.get(prov, "")) or "").strip()
            if not cur:
                continue
            ids = _list_cloud_models(prov, creds)
            if ids is None:
                unchecked.append(prov)
                continue
            checked += 1
            if cur not in ids:
                offered = ", ".join(sorted(ids)[:6]) or "(nothing)"
                delisted.append(f"{prov}={cur} NOT offered (this key sees: {offered})")

        parts = []
        if delisted:
            parts.append("DELISTED — a model we RUN is no longer offered: "
                         + "; ".join(delisted)
                         + ". It may still answer until the provider retires it; "
                           "re-pin before it stops.")
        if unchecked:
            parts.append("unchecked (unreachable): " + ", ".join(unchecked))
        if not parts:
            parts.append(f"all {checked} configured cloud model(s) still offered")
        return ("delist: " + " · ".join(parts), bool(delisted))
    except Exception as e:
        return ("delist: check failed: %s" % type(e).__name__, False)


def selftest_delist() -> int:
    """S146. check_configured_model_delisted must FIRE, and must not fire on a
    provider it merely could not reach -- accusing a provider of retiring a model
    because the network blinked is how a check gets muted.

    The live run is not a test: it happened to have two delisted models that
    night. A green run against a healthy fleet proves nothing at all.
    """
    bad = 0

    def ck(name, cond):
        nonlocal bad
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        bad += 0 if cond else 1

    real = globals()["_list_cloud_models"]
    creds = {"deepseek_api_key": "x", "deepseek_model": "deepseek-v4-flash"}
    try:
        globals()["_list_cloud_models"] = lambda p, c: {"deepseek-flash", "deepseek-v4-pro"}
        line, notify = check_configured_model_delisted(creds)
        ck("a delisted model FIRES", notify is True)
        ck("...and names it", "deepseek-v4-flash" in line)
        ck("...and shows what the key is offered instead", "deepseek-flash" in line)

        globals()["_list_cloud_models"] = lambda p, c: {"deepseek-v4-flash", "deepseek-flash"}
        line, notify = check_configured_model_delisted(creds)
        ck("a model still offered does NOT fire", notify is False)

        globals()["_list_cloud_models"] = lambda p, c: None
        line, notify = check_configured_model_delisted(creds)
        ck("an UNREACHABLE provider does not fire", notify is False)
        ck("...and says it was unchecked, not that it is fine",
           "unchecked" in line.lower())

        def boom(p, c):
            raise RuntimeError("x")
        globals()["_list_cloud_models"] = boom
        line, notify = check_configured_model_delisted(creds)
        ck("it never raises", isinstance(line, str) and notify is False)
    finally:
        globals()["_list_cloud_models"] = real

    print()
    print("all delist selftests passed" if not bad else f"{bad} FAILED")
    return 1 if bad else 0


def check_cloud_model_releases(creds):
    """(line, should_notify) — has a provider shipped a model we have not seen?

    Buddy, 2026-09-01, after the local half landed: track cloud releases too.

    The rule is deliberately a FACT, not a judgement. "Is this model better than
    ours?" is unanswerable without benchmarking it — and this session twice
    proved rank does not predict fitness for our prompts (a 72B reasoned no
    better than a 14B). So this reports only: **this model id is new to this key
    since we last looked.** Deciding whether to adopt it stays a human call, made
    against the bench suite.

    Two things keep it quiet enough to stay useful:
      * SAME FAMILY ONLY, derived from the model we actually run — `gpt-4.1`
        gives "gpt", so `babbage-002` and `chatgpt-image-latest` never qualify.
        Derived from config, not a hardcoded list, so it follows a re-pin.
      * FIRST RUN SEEDS SILENTLY. Without that, run one reports ~200 "new"
        models across five providers and is switched off the same morning.
    """
    try:
        prev = {}
        try:
            prev = json.loads(CLOUD_MODELS_STATE.read_text())
        except Exception:
            pass
        seen = prev.get("seen") or {}

        fresh, unchecked, seeded, checked = {}, [], [], 0
        for prov in sorted(_KEY_FIELD_CLOUD):
            if not creds.get(_KEY_FIELD_CLOUD[prov]):
                continue
            ids = _list_cloud_models(prov, creds)
            if ids is None:
                unchecked.append(prov)
                continue
            checked += 1
            cur_model = creds.get(MODEL_FIELD.get(prov, ""), "") or ""
            fam = cur_model.split("-")[0].lower()
            rel = sorted(i for i in ids
                         if fam and i.lower().startswith(fam)
                         and not any(n in i.lower() for n in _MODALITY_NOISE))
            if prov not in seen:
                seeded.append(prov)          # baseline only — never notify
            else:
                new = [i for i in rel if i not in seen[prov]]
                if new:
                    fresh[prov] = new
            seen[prov] = rel

        try:
            CLOUD_MODELS_STATE.parent.mkdir(parents=True, exist_ok=True)
            CLOUD_MODELS_STATE.write_text(json.dumps(
                {"seen": seen, "at": datetime.now().strftime("%Y-%m-%d %H:%M")},
                indent=2) + "\n")
        except Exception:
            pass

        parts = ["%d provider(s) checked" % checked]
        if seeded:
            parts.append("baseline seeded for %s (first run — not an alert)"
                         % ", ".join(seeded))
        if fresh:
            parts.append("NEW: " + "; ".join(
                "%s %s" % (p, ", ".join(v[:3])) for p, v in sorted(fresh.items())))
        elif not seeded and checked:
            # `checked` is load-bearing: with every provider unreachable this
            # said "nothing new", which is a claim we had not earned. Silence
            # and agreement must not render the same.
            parts.append("nothing new")
        if unchecked:
            parts.append("%d unchecked (%s)" % (len(unchecked), ", ".join(unchecked)))
        return ("cloud: " + " · ".join(parts), bool(fresh))
    except Exception as e:  # noqa: BLE001 — a drift check must not break the health run
        return ("cloud: release check failed: %s" % type(e).__name__, False)


def main():
    creds = load()
    providers = L.available(creds)
    healthy, healed, broken, errored, needs_funding = [], [], [], [], []

    for p in providers:
        field = MODEL_FIELD.get(p)
        if not field:
            continue
        model = creds.get(field) or ""
        # S91: when the field is unset, `model` is "" and every line below
        # rendered as "healthy: anthropic=" — a check reporting a pass without
        # naming what it inspected (docs/TOOLING-TRAPS.md, the S71 class). It is
        # not cosmetic: with claude_model AND claude_dev_model both empty on
        # CIRRUS, llm_providers._anthropic falls back to its hardcoded
        # "claude-sonnet-5", so the probe was live-testing a model nobody
        # configured and naming neither it nor the fact that it had defaulted.
        # Label the condition instead of hiding it. Deliberately NOT resolving
        # the provider's fallback here: duplicating that logic is how the two
        # copies drift, and pinning a model is a cost decision, not a repair.
        shown = model or "(unset — provider default)"
        ok, err = test_model(p, creds, model)
        if ok:
            healthy.append(f"{p}={shown}")
            continue
        if not MODEL_ERR.search(err):
            # billing/credits exhaustion is distinct from a transient auth/network
            # blip — flag it so Buddy knows to check funding / auto-refill.
            if BILLING_ERR.search(err):
                needs_funding.append(f"{p}={shown}: {err[:140]}")
            else:
                errored.append(f"{p}={shown}: {err[:120]}")   # auth/network — no change
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
            broken.append(f"{p}={shown}: no working replacement found ({err[:80]})")

    runtime_line, runtime_notify = check_local_runtime()
    models_line, models_notify = check_model_drift()
    cloud_line, cloud_notify = check_cloud_model_releases(creds)
    dl_line, dl_notify = check_configured_model_delisted(creds)   # S146
    local_line, local_notify = check_local_model_loads(creds)     # S137
    fb_line, fb_notify = check_local_fallback_rate(creds)         # S141
    tr_line, tr_notify = check_local_truncation(creds)            # S141
    ep_line, ep_notify = check_endpoint_config(creds)             # S141
    dj_line, dj_notify = check_detached_jobs(creds)               # S141

    stamp = f"{node_name()} {datetime.now():%Y-%m-%d %H:%M}"
    print(f"[{stamp}] model-health {'(dry-run)' if DRY else ''}")
    print(f"  local:   {local_line}")
    print(f"  spend:   {fb_line}")
    print(f"  cutoff:  {tr_line}")
    print(f"  engine:  {ep_line}")
    print(f"  detach:  {dj_line}")
    print(f"  runtime: {runtime_line}")
    print(f"  {models_line}")
    print(f"  {dl_line}")
    print(f"  {cloud_line}")
    for label, items in (("healthy", healthy), ("healed", healed),
                         ("broken", broken), ("needs_funding", needs_funding),
                         ("errored", errored)):
        for it in items:
            print(f"  {label}: {it}")

    # Notify only when something needs attention or changed.
    if (healed or broken or errored or needs_funding or runtime_notify or models_notify
            or cloud_notify or local_notify or fb_notify or tr_notify
            or ep_notify or dj_notify or dl_notify):
        lines = [f"🩺 *{node_name()} model-health*"]
        if local_notify:
            # First, because it is the one that costs money every hour it stands.
            lines += ["*LOCAL MODEL CANNOT LOAD — every local call is going to the paid model:*",
                      f"• {local_line}",
                      "_Told EVERY run until it loads: this is an outage, not drift. "
                      "On CIRRUS the fix was the runner's cirrus-ollama-upgrade "
                      "(args.mode=apply); on CUMULUS check `ollama ps` / the unit._"]
        if fb_notify:
            # S141. The model may LOAD perfectly and still not be getting the
            # work -- a fallback chain that never recovers, a budget that
            # truncates its reply, a config pointing elsewhere. That is
            # invisible to every other check here and shows up only as spend.
            lines += ["*WORK IS GOING TO A PAID MODEL that should be local:*",
                      f"• {fb_line}",
                      "_Read `runner llm-spend-report` for the per-task rows. "
                      "'ALL n call(s) went PAID' means the local path is not "
                      "being used at all -- the S137 shape._"]
        if tr_notify:
            lines += ["*WE CUT OUR OWN LOCAL MODEL OFF mid-reply:*",
                      f"• {tr_line}",
                      "_A truncated local reply is unparseable, so the job "
                      "escalates and PAYS for the same question. Raise that "
                      "call's max_tokens -- a reasoning model spends its budget "
                      "thinking before it writes anything (S141, 44b31fe)._"]
        if ep_notify:
            lines += ["*THE ENGINE IS NOT RUNNING WHAT WE ASKED FOR:*",
                      f"• {ep_line}",
                      "_Every request still succeeds, which is why nothing else "
                      "catches this. Compare `~/tp2fp8/serve-tp2.sh` with the "
                      "boot log: `journalctl --user -u vllm-tp2 --since \"$(systemctl "
                      "--user show vllm-tp2 -p ActiveEnterTimestamp --value)\"`._"]
        if dj_notify:
            lines += ["*A DETACHED JOB IS UNACCOUNTED FOR:*",
                      f"• {dj_line}",
                      "_'NEVER FINISHED' means the process is gone and no exit "
                      "code was written — the S140 shape, where a dropped ssh "
                      "left a 0-byte summary and nothing said so. Logs are "
                      "beside the marker in ~/.cowork-detached/._"]
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
        if dl_notify:
            lines += ["*a model we RUN is no longer offered by its provider:*",
                      f"• {dl_line}",
                      "_It may still answer right up until the provider pulls it — "
                      "that is why 'healthy' does not cover this. Re-pin before the "
                      "cutoff, not after._"]
        if cloud_notify:
            lines += ["*a provider shipped a model we have not seen:*", f"• {cloud_line}",
                      "_Reported as a FACT, not a recommendation — whether it beats "
                      "what we run is a bench question. Told once per new id._"]
        if models_notify:
            lines += ["*local model(s) behind the registry:*", f"• {models_line}",
                      "_`ollama pull <tag>` refreshes one. Told once per new "
                      "upstream build, not daily._"]
        if runtime_notify:
            lines += ["*local runtime is behind:*", f"• {runtime_line}",
                      "_Report only — upgrading the runtime that serves the digest "
                      "is a decision, not a self-heal. You are told once per new "
                      "upstream release, not daily._"]
        tg("\n".join(lines))

    # Run-status ledger (best-effort).
    try:
        import job_status
        note = (f"{len(healthy)} ok, {len(healed)} healed, {len(broken)} broken, "
                f"{len(needs_funding)} needs-funding, {len(errored)} err; "
                f"{local_line}; {runtime_line}; {models_line}; {cloud_line}")
        job_status.record("modelhealth",
                          ok=(not broken and not errored and not needs_funding
                              and not local_notify),
                          note=note)
    except Exception:
        pass

    sys.exit(1 if (broken or errored or needs_funding or local_notify) else 0)


def _selftest_runtime(ck):
    """S91 — the drift comparator and the once-per-release notify rule."""
    ck("a newer installed version is not 'behind'",
       _ver_tuple("0.33.2") >= _ver_tuple("0.24.0"))
    ck("0.24.0 IS behind 0.33.2 (the real case)",
       _ver_tuple("0.24.0") < _ver_tuple("0.33.2"))
    ck("equal versions are current", _ver_tuple("1.2.3") >= _ver_tuple("1.2.3"))
    # 9 vs 10 must not compare as strings, or "0.9.0" reads as newer than "0.10.0".
    ck("version compare is NUMERIC, not lexical",
       _ver_tuple("0.9.0") < _ver_tuple("0.10.0"))
    ck("a short version still parses", _ver_tuple("1") == (1, 0, 0))
    ck("junk does not raise", _ver_tuple("not.a.version") == (0, 0, 0))

    # The unreachable-API case must NOT read as "up to date". A silent zero here
    # is the exact failure this file exists to catch, one layer down.
    import tempfile as _tf
    global RUNTIME_STATE
    _saved_state, _saved_latest = RUNTIME_STATE, globals()["_latest_ollama"]
    _saved_installed = globals()["_installed_ollama"]
    with _tf.TemporaryDirectory() as td:
        RUNTIME_STATE = Path(td) / "runtime-drift.json"
        try:
            globals()["_installed_ollama"] = lambda: "0.24.0"
            globals()["_latest_ollama"] = lambda: ""
            line, notify = check_local_runtime()
            ck("an unreachable release API says UNKNOWN, not 'current'",
               "UNKNOWN" in line and not notify)

            globals()["_latest_ollama"] = lambda: "0.33.2"
            line, notify = check_local_runtime()
            ck("being behind is reported AND notified the first time",
               "BEHIND" in line and notify)
            line, notify = check_local_runtime()
            ck("  ...but NOT notified again for the same release (no daily nagging)",
               "BEHIND" in line and not notify)

            globals()["_latest_ollama"] = lambda: "0.34.0"
            line, notify = check_local_runtime()
            ck("  ...and IS notified again when a NEW release appears", notify)

            globals()["_installed_ollama"] = lambda: "0.34.0"
            line, notify = check_local_runtime()
            ck("once upgraded it reports current and stops notifying",
               "current" in line and not notify)

            globals()["_installed_ollama"] = lambda: ""
            line, notify = check_local_runtime()
            ck("a missing ollama is said out loud, not skipped",
               "NOT INSTALLED" in line and not notify)
        finally:
            RUNTIME_STATE = _saved_state
            globals()["_latest_ollama"] = _saved_latest
            globals()["_installed_ollama"] = _saved_installed


def _selftest_local_load(ck):
    """S137 — the load probe. urllib is swapped for a fake the same way the S94
    drift test does it, EXCEPT `error` stays the real urllib.error so the
    function's `except urllib.error.HTTPError` still matches the class the fake
    raises. Each outcome is paired with its inverse: a probe that always says
    LOADS, or always FAILS, cannot pass all four."""
    import io as _io
    import types as _types
    _real_urllib = globals()["urllib"]

    class _Resp:
        def read(self): return b'{"choices":[{"message":{"content":"OK"}}]}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake(urlopen):
        return _types.SimpleNamespace(
            request=_types.SimpleNamespace(urlopen=urlopen, Request=lambda *a, **k: None),
            error=_real_urllib.error)

    _creds = {"ollama_url": "http://o:11434", "ollama_model": "qwen3.8:27b"}
    try:
        globals()["urllib"] = _fake(lambda *a, **k: _Resp())
        line, notify = check_local_model_loads(_creds)
        ck("a 200 from the configured model reads LOADS and does not notify",
           "LOADS" in line and "qwen3.8:27b" in line and not notify)

        def _500(*a, **k):
            raise _real_urllib.error.HTTPError(
                "http://o", 500, "Internal Server Error", {},
                _io.BytesIO(b'{"error":{"message":"unable to load model: /blobs/sha256-f5f1"}}'))
        globals()["urllib"] = _fake(_500)
        line, notify = check_local_model_loads(_creds)
        ck("an HTTP 500 reads FAILS TO LOAD, quotes the server's reason, and NOTIFIES "
           "(the CIRRUS 2026-09-05..08 case)",
           "FAILS TO LOAD" in line and "500" in line and "unable to load model" in line and notify)

        def _timeout(*a, **k):
            raise TimeoutError("timed out")
        globals()["urllib"] = _fake(_timeout)
        line, notify = check_local_model_loads(_creds)
        ck("a timeout reads FAILS TO LOAD and notifies (not serving is not serving)",
           "FAILS TO LOAD" in line and "TimeoutError" in line and notify)

        line, notify = check_local_model_loads({})
        ck("a box with no ollama_url is 'not configured' -- reported, not alarmed",
           "not configured" in line and not notify)
    finally:
        globals()["urllib"] = _real_urllib


def _selftest_fallback(ck):
    """S141 — the local->paid fallback alert (watcher audit item 1).

    Every shape is exercised, including the S137 outage replayed: a task whose
    calls ALL went to a paid provider. That one fires at any sample size on
    purpose -- it is an absence, not a rate, and waiting for a quorum is how
    three days went by.
    """
    def rows(task, local=0, paid=0, lp="vllm", pp="anthropic"):
        return ([{"task": task, "provider": lp}] * local
                + [{"task": task, "provider": pp}] * paid)

    def verdict(rws):
        return fallback_verdict(rws)

    _, n = verdict(rows("intake:promise_detect", local=0, paid=12))
    ck("fallback: S137 replayed (every call billed, none local) ALERTS", n is True)
    line, n = verdict(rows("intake:promise_detect", local=0, paid=1))
    ck("fallback: ...and it alerts on a ONE-row sample too, being an absence",
       n is True and "ALL 1 call(s) went PAID" in line)
    _, n = verdict(rows("halftime_catalogue", local=49, paid=0))
    ck("fallback: an all-local day is quiet", n is False)
    line, n = verdict(rows("halftime_catalogue", local=30, paid=20))
    ck("fallback: a graded breach on a real sample alerts",
       n is True and "over its 10% threshold" in line)
    _, n = verdict(rows("halftime_catalogue", local=2, paid=1))
    ck("fallback: 1-of-3 paid is noise, not an alarm (T9)", n is False)
    _, n = verdict([])
    ck("fallback: a task that did not run is silent, not a failure", n is False)
    _, n = verdict(rows("business-idea-gate:council", local=0, paid=58))
    ck("fallback: a council task is cloud BY DESIGN and is never flagged",
       n is False)
    line, n = verdict(rows("halftime_routing", local=20, paid=0, lp="ollama"))
    ck("fallback: ollama counts as LOCAL, not paid",
       n is False and "0/20 paid" in line)
    # The quiet line must still carry the NUMBER -- a check whose healthy output
    # says nothing gives you no baseline to tighten against later.
    line, _ = verdict(rows("halftime_catalogue", local=45, paid=4))
    ck("fallback: the quiet line still reports the observed rate",
       "4/49 paid" in line and "8%" in line)


def _selftest_truncation(ck):
    """S141 — the cut-off-local-reply alert."""
    def r(prov, task="halftime_catalogue"):
        return {"provider": prov, "task": task, "at": "x"}
    line, n = truncation_verdict([])
    ck("truncation: a day with no cut-off replies is quiet", n is False)
    line, n = truncation_verdict([r("vllm"), r("vllm"), r("ollama")])
    ck("truncation: ANY local cut-off alerts -- there is no safe allowance",
       n is True and "3 LOCAL" in line)
    line, n = truncation_verdict([r("vllm")])
    ck("truncation: even ONE alerts, and names the task",
       n is True and "halftime_catalogue" in line)
    line, n = truncation_verdict([r("anthropic"), r("openai")])
    ck("truncation: a PAID model hitting its cap is NOT this alert's business",
       n is False)


def _selftest_nag(ck):
    """S141 — a standing drift condition speaks, holds, then speaks again."""
    import tempfile, json as _j
    from pathlib import Path as _P
    _g = globals()
    sv_state, sv_inst, sv_latest = (_g["RUNTIME_STATE"], _g["_installed_ollama"],
                                    _g["_latest_ollama"])
    d = _P(tempfile.mkdtemp())
    try:
        _g["RUNTIME_STATE"] = d / "runtime-drift.json"       # T32: never live
        _g["_installed_ollama"] = lambda: "0.24.0"
        _g["_latest_ollama"] = lambda: "0.33.3"

        line, n = check_local_runtime()
        ck("nag: a newly-seen release notifies", n is True and "BEHIND" in line)
        line, n = check_local_runtime()
        ck("nag: the very next run is QUIET -- daily repetition is what mutes "
           "a channel", n is False)

        # ...but a week later it is still true, and silence would be amnesia.
        st = _j.loads((d / "runtime-drift.json").read_text())
        old = (datetime.now() - timedelta(days=RUNTIME_NAG_DAYS + 1))
        st["at"] = old.strftime("%Y-%m-%d %H:%M")
        st["first_seen"] = old.strftime("%Y-%m-%d")
        (d / "runtime-drift.json").write_text(_j.dumps(st))
        line, n = check_local_runtime()
        ck("nag: after the hold it speaks again", n is True)
        ck("nag: ...and says HOW LONG it has been standing",
           "still behind after" in line)

        # A box that is current says nothing at all.
        _g["_installed_ollama"] = lambda: "0.33.3"
        line, n = check_local_runtime()
        ck("nag: an up-to-date box is silent", n is False and "current" in line)

        # An unreachable release API is a MISSING measurement, not 'current'.
        _g["_installed_ollama"] = lambda: "0.24.0"
        _g["_latest_ollama"] = lambda: None
        line, n = check_local_runtime()
        ck("nag: an unreachable release API says UNKNOWN, never 'up to date'",
           "UNKNOWN" in line)
    finally:
        _g["RUNTIME_STATE"], _g["_installed_ollama"], _g["_latest_ollama"] = (
            sv_state, sv_inst, sv_latest)


def _selftest_endpoint_config(ck):
    """S141 — the endpoint must be judged on what the ENGINE resolved."""
    REAL = ("INFO [cuda.py:486] Using FLASHINFER attention backend out of "
            "potential backends: ['FLASHINFER', 'TRITON_ATTN']\n"
            "INFO [flashinfer.py:907] FlashInfer resolved query dtypes: "
            "prefill=torch.bfloat16, decode=torch.bfloat16, decode_backend=xqa, "
            "kv_cache_dtype=torch.float8_e4m3fn, arch=sm121\n"
            "INFO [kv_cache_utils.py:1869] GPU KV cache size: 588,913 tokens, "
            "Maximum concurrency for 32,768 tokens per request: 17.97x\n")
    f = parse_endpoint_boot(REAL)
    ck("endpoint: the real 2026-09-08 boot log parses",
       f["backend"] == "FLASHINFER" and "float8" in f["kv_cache_dtype"]
       and f["kv_tokens"] == 588913)
    ck("endpoint: ...and the alternatives it could have picked are captured",
       "TRITON_ATTN" in f["alternatives"])
    line, n = endpoint_config_verdict(f)
    ck("endpoint: a correctly-resolved engine is quiet", n is False
       and "as configured" in line)

    # The silent fallback this check exists for: everything still serves.
    drift = REAL.replace("Using FLASHINFER attention", "Using TRITON_ATTN attention")
    line, n = endpoint_config_verdict(parse_endpoint_boot(drift))
    ck("endpoint: a silent backend fallback is CAUGHT", n is True
       and "TRITON_ATTN" in line)
    ck("endpoint: ...and says the benchmarks no longer apply",
       "benchmarks no longer apply" in line)

    # KVDTYPE quietly back to auto -- the S138 rollback lever, applied by accident.
    drift2 = REAL.replace("kv_cache_dtype=torch.float8_e4m3fn",
                          "kv_cache_dtype=torch.bfloat16")
    line, n = endpoint_config_verdict(parse_endpoint_boot(drift2))
    ck("endpoint: a KV cache silently back on bf16 is CAUGHT",
       n is True and "not float8" in line)

    # T76: nothing parsed is a MISSING measurement, not a clean bill.
    line, n = endpoint_config_verdict(parse_endpoint_boot("nothing useful here"))
    ck("endpoint: an unreadable boot log ALERTS, never reads as fine",
       n is True and "UNREADABLE" in line)
    line, n = endpoint_config_verdict(None)
    ck("endpoint: a box with no endpoint is silent, not a failure", n is False)


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
    # S91 — the local-runtime drift check rides along here.
    def ck(name, cond):
        nonlocal fails
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        fails += 0 if cond else 1
    _selftest_runtime(ck)
    _selftest_local_load(ck)     # S137: does the configured local model LOAD?
    _selftest_fallback(ck)       # S141: is the WORK actually going there?
    _selftest_truncation(ck)     # S141: did WE cut the local model off?
    _selftest_nag(ck)            # S141: does a standing condition keep speaking?
    _selftest_endpoint_config(ck)  # S141: did the engine resolve what we asked?

    # ── S92: the self-heal must not undo a human's tier decision ─────────────
    _MODELS = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-2.0-flash",
               "gemini-2.5-pro", "gemini-pro-latest", "gemini-3.1-pro-preview",
               "gemini-3-pro-image", "text-embedding-004"]

    def _cands(current):
        # Patch THIS module's global, not `import model_health`'s. Run as
        # __main__ that import yields a SECOND module object, so patching it
        # leaves the _get these functions actually call untouched — the test
        # would then hit the live API and quietly prove nothing.
        saved = globals()["_get"]
        globals()["_get"] = lambda url: {"models": [
            {"name": "models/" + m, "supportedGenerationMethods": ["generateContent"]}
            for m in _MODELS]}
        try:
            return candidates_gemini({"gemini_api_key": "x", "gemini_model": current})
        finally:
            globals()["_get"] = saved

    pro = _cands("gemini-2.5-pro")
    ck("a failing PRO model is never healed down to flash (Buddy's 2026-09-01 call)",
       pro and not any("flash" in m for m in pro))
    ck("  ...and a pinned version is preferred over a rolling alias (T55)",
       pro and not pro[0].endswith("latest"))
    ck("  ...and a stable config is not promoted onto a preview",
       not any("preview" in m for m in pro))

    fl = _cands("gemini-2.5-flash")
    ck("a failing FLASH model still heals within flash", fl and all("flash" in m for m in fl))
    ck("  ...pinned first there too", fl and not fl[0].endswith("latest"))
    ck("  ...but the alias is still available as a last resort",
       any(m.endswith("latest") for m in fl))

    prev = _cands("gemini-3.1-pro-preview")
    ck("a preview config MAY heal onto another preview (it is already there)",
       all("flash" not in m for m in prev))
    ck("image/embedding/robotics models are never candidates",
       not any(("image" in m or "embedding" in m) for m in _cands("gemini-2.5-pro") + fl))

    # ── S94: model-release drift ─────────────────────────────────────────────
    import tempfile as _tf2
    global MODEL_DRIFT_STATE
    _sv = (MODEL_DRIFT_STATE, globals()["_registry_digest"], globals()["urllib"])

    class _FakeResp:
        def __init__(self, payload): self._p = json.dumps(payload).encode()
        def read(self): return self._p
        def __enter__(self): return self
        def __exit__(self, *a): return False

    with _tf2.TemporaryDirectory() as td:
        MODEL_DRIFT_STATE = Path(td) / "model-drift.json"
        _tags = {"models": [{"name": "a:1", "digest": "sha256:aaaa"},
                            {"name": "b:1", "digest": "sha256:bbbb"}]}
        globals()["urllib"] = type("U", (), {"request": type("R", (), {
            "urlopen": staticmethod(lambda *a, **k: _FakeResp(_tags)),
            "Request": staticmethod(lambda *a, **k: None)})})
        try:
            globals()["_registry_digest"] = lambda n: {"a:1": "aaaa", "b:1": "bbbb"}[n]
            line, notify = check_model_drift()
            ck("all tags matching the registry reports current, notifies nothing",
               "2 current" in line and not notify)

            globals()["_registry_digest"] = lambda n: {"a:1": "aaaa", "b:1": "zzzz"}[n]
            line, notify = check_model_drift()
            ck("a rebuilt upstream tag is reported STALE and notified once",
               "STALE" in line and "b:1" in line and notify)
            line, notify = check_model_drift()
            ck("  ...and NOT notified again for the same upstream build",
               "STALE" in line and not notify)

            globals()["_registry_digest"] = lambda n: {"a:1": "aaaa", "b:1": "yyyy"}[n]
            line, notify = check_model_drift()
            ck("  ...but IS notified again when upstream moves AGAIN", notify)

            globals()["_registry_digest"] = lambda n: ""
            line, notify = check_model_drift()
            ck("an unreachable registry reports UNCHECKED, never 'current'",
               "unchecked" in line and "0 current" in line and not notify)
        finally:
            MODEL_DRIFT_STATE, globals()["_registry_digest"], globals()["urllib"] = _sv

    # ── S95: cloud model releases ────────────────────────────────────────────
    import tempfile as _tf3
    global CLOUD_MODELS_STATE
    _sv3 = (CLOUD_MODELS_STATE, globals()["_list_cloud_models"])
    _CREDS = {"anthropic_api_key": "x", "claude_model": "claude-haiku-4-5-20251001",
              "openai_api_key": "x", "openai_model": "gpt-4.1"}
    with _tf3.TemporaryDirectory() as td:
        CLOUD_MODELS_STATE = Path(td) / "cloud-models.json"
        catalog = {"anthropic": {"claude-haiku-4-5-20251001", "claude-opus-5"},
                   "openai": {"gpt-4.1", "babbage-002", "chatgpt-image-latest"}}
        globals()["_list_cloud_models"] = lambda p, c: catalog.get(p)
        try:
            line, notify = check_cloud_model_releases(_CREDS)
            ck("first run SEEDS the baseline and never alerts (else ~200 'new')",
               "baseline seeded" in line and not notify)
            line, notify = check_cloud_model_releases(_CREDS)
            ck("second run with no change says nothing new",
               "nothing new" in line and not notify)

            catalog["anthropic"] = catalog["anthropic"] | {"claude-fable-5-1"}
            line, notify = check_cloud_model_releases(_CREDS)
            ck("a genuinely new model in OUR family is reported and notified",
               notify and "claude-fable-5-1" in line)
            line, notify = check_cloud_model_releases(_CREDS)
            ck("  ...and not reported again on the next run", not notify)

            catalog["openai"] = catalog["openai"] | {"gpt-image-2", "davinci-003"}
            line, notify = check_cloud_model_releases(_CREDS)
            ck("an out-of-family model (davinci) is ignored", "davinci" not in line)
            ck("  ...and an in-family IMAGE model is ignored too",
               "gpt-image-2" not in line and not notify)

            globals()["_list_cloud_models"] = lambda p, c: None
            line, notify = check_cloud_model_releases(_CREDS)
            ck("an unreachable provider reports UNCHECKED, never 'nothing new'",
               "unchecked" in line and "nothing new" not in line and not notify)
        finally:
            CLOUD_MODELS_STATE, globals()["_list_cloud_models"] = _sv3

    # S146: chained in so `model_health.py --selftest` covers it too. A test
    # nothing invokes is not a test -- the dev-loop's gate 2 runs exactly this
    # entry point.
    fails += selftest_delist()

    print("PASS" if not fails else f"{fails} FAILURE(S)")
    return 1 if fails else 0


if __name__ == "__main__":
    # S91: accepted only a bare `selftest`, while every other module in this
    # repo (dev_agent, dev_loop, supervisor/tools) uses `--selftest` — so the
    # obvious invocation ran MAIN against a live box instead of the tests.
    if "selftest" in sys.argv or "--selftest" in sys.argv:
        sys.exit(selftest())
    main()
