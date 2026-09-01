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
        # Notify once per NEW upstream release, not once per day. A finding that
        # repeats every morning while nobody acts is how an alert channel gets
        # muted -- and this one would repeat for weeks by design, since the fix
        # is a deliberate upgrade rather than a self-heal.
        prev = {}
        try:
            prev = json.loads(RUNTIME_STATE.read_text())
        except Exception:
            pass
        notify = prev.get("notified_latest") != latest
        if notify:
            try:
                RUNTIME_STATE.parent.mkdir(parents=True, exist_ok=True)
                RUNTIME_STATE.write_text(json.dumps(
                    {"notified_latest": latest, "installed": cur,
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

    stamp = f"{node_name()} {datetime.now():%Y-%m-%d %H:%M}"
    print(f"[{stamp}] model-health {'(dry-run)' if DRY else ''}")
    print(f"  runtime: {runtime_line}")
    print(f"  {models_line}")
    print(f"  {cloud_line}")
    for label, items in (("healthy", healthy), ("healed", healed),
                         ("broken", broken), ("needs_funding", needs_funding),
                         ("errored", errored)):
        for it in items:
            print(f"  {label}: {it}")

    # Notify only when something needs attention or changed.
    if healed or broken or errored or needs_funding or runtime_notify or models_notify or cloud_notify:
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
                f"{runtime_line}; {models_line}; {cloud_line}")
        job_status.record("modelhealth",
                          ok=(not broken and not errored and not needs_funding),
                          note=note)
    except Exception:
        pass

    sys.exit(1 if (broken or errored or needs_funding) else 0)


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

    print("PASS" if not fails else f"{fails} FAILURE(S)")
    return 1 if fails else 0


if __name__ == "__main__":
    # S91: accepted only a bare `selftest`, while every other module in this
    # repo (dev_agent, dev_loop, supervisor/tools) uses `--selftest` — so the
    # obvious invocation ran MAIN against a live box instead of the tests.
    if "selftest" in sys.argv or "--selftest" in sys.argv:
        sys.exit(selftest())
    main()
