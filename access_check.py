#!/usr/bin/env python3
"""access_check — runs ON cumulus1, watches whether all three boxes are reachable.

S101 (Buddy): "schedule it on cumulus1 too", after a CIRRUS outage that ran for
most of a day unnoticed.

WHY THIS EXISTS ALONGSIDE cirrus_deadman.py, rather than duplicating it:
  cirrus_deadman answers ONE question -- "is CIRRUS answering?" -- and pages on
  it. This answers "WHICH LAYER is broken, and is anything else down?", which the
  deadman structurally cannot:
    * it probes ONE url, so it cannot tell a dead tunnel from a dead box. On
      2026-09-04 CIRRUS was perfectly healthy on the LAN while its tunnel was
      dead; knowing that immediately would have saved hours.
    * NOTHING watches cumulus2 at all. It joined the estate on 2026-09-03 and no
      monitor has ever looked at it.
    * nothing watches cumulus1's own outbound health, which is what actually
      broke on CIRRUS.

cumulus1 is the right host: always on, on CIRRUS's LAN, and with its own
independent tunnel, so it can see both the inside and the outside of the estate.

    python3 access_check.py            # check, record, alert on a transition
    python3 access_check.py --status   # print, send nothing
    python3 access_check.py selftest
    python3 access_check.py --endpoint # ONLY the TP=2 endpoint checks (the 10-min watchdog)

S125 (CUMULUS2-TP2-PLAN.md Phase C): this is also the home of the TP=2 vLLM
endpoint's health -- "is /v1/models answering", "does a REAL completion come
back" (the NCCL-deadlock mode keeps /v1/models alive while both GPUs sit at
100%), and "does cumulus1 still see two Ray nodes". Two consecutive failed
completions restart the user unit; every transition is one Telegram line.
The checks only run when vllm-tp2.service is ENABLED on this box, so a box
without the endpoint reports "absent", never "down".
"""

import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
STATE_PATH = PROJECT_DIR / "logs/access-check-state.json"
CREDS_PATH = PROJECT_DIR / "config/credentials.json"
TIMEOUT = 15

# Cloudflare EDGE errors: the edge generates these BECAUSE it cannot reach the
# origin, so they are not the origin answering. Same set cirrus_deadman uses;
# the 2026-09-04 outage was a 530 read as proof of life.
CF_ORIGIN_UNREACHABLE = {520, 521, 522, 523, 524, 525, 526, 527, 530}

# (label, kind, target). LAN targets prove the BOX is alive; public targets prove
# the ROUTE to it is alive. Having both is the whole point -- one without the
# other cannot attribute a failure to a layer.
TARGETS = [
    ("cirrus-lan",    "tcp",    ("192.168.0.202", 22)),
    ("cirrus-public", "https",  "https://cirrus.cirrustask.com/status"),
    ("cumulus2-tail", "tcp",    ("100.87.241.34", 22)),
    ("self-outbound", "https",  "https://1.1.1.1/"),
]


def check_tcp(host_port):
    host, port = host_port
    try:
        s = socket.create_connection((host, port), timeout=TIMEOUT)
        s.close()
        return True, "connected"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:70]}"


def check_https(url):
    req = urllib.request.Request(url, headers={"User-Agent": "cowork-access-check/1"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        if e.code in CF_ORIGIN_UNREACHABLE:
            return False, f"HTTP {e.code} — CLOUDFLARE cannot reach the origin"
        return True, f"HTTP {e.code} (origin answered)"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:70]}"


def run_checks(targets=None):
    targets = targets if targets is not None else TARGETS
    out = {}
    for label, kind, target in targets:
        ok, detail = (check_tcp(target) if kind == "tcp" else check_https(target))
        out[label] = {"ok": ok, "detail": detail}
    return out


# ── S125: the TP=2 vLLM endpoint (cumulus1 127.0.0.1:8000 + cumulus2 rank) ──
VLLM_URL = "http://127.0.0.1:8000"
VLLM_MODEL = "qwen3.8-27b-fp8"
VLLM_UNIT = "vllm-tp2.service"
RAY_ENV = str(Path.home() / "tp2fp8/env.sh")
COMPLETION_TIMEOUT = 90       # a 16-token reply takes ~2 s; 90 s means "hung"
RESTART_AFTER_FAILS = 2       # consecutive completion failures before a restart


def _user_env():
    """systemctl --user from a SYSTEM unit needs the user manager's socket."""
    env = dict(os.environ)
    uid = os.getuid()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
    return env


def _user_systemctl(*args, runner=None):
    runner = runner or (lambda cmd, env: subprocess.run(
        cmd, capture_output=True, text=True, timeout=60, env=env))
    r = runner(["systemctl", "--user", *args], _user_env())
    return (r.returncode == 0), (r.stdout or "").strip() or (r.stderr or "").strip()


def endpoint_expected(runner=None):
    """True only when vllm-tp2.service is ENABLED here. Absent != down."""
    ok, out = _user_systemctl("is-enabled", VLLM_UNIT, runner=runner)
    return ok and out == "enabled"


def check_vllm_models(url=VLLM_URL, opener=None):
    opener = opener or urllib.request.urlopen
    try:
        with opener(urllib.request.Request(url.rstrip("/") + "/v1/models"), timeout=TIMEOUT) as r:
            ids = [m.get("id") for m in json.loads(r.read().decode()).get("data", [])]
        return (VLLM_MODEL in ids), f"models: {ids}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:70]}"


def check_vllm_completion(url=VLLM_URL, opener=None):
    """A REAL generation, tiny. /v1/models answering is not the same thing."""
    opener = opener or urllib.request.urlopen
    body = json.dumps({"model": VLLM_MODEL, "max_tokens": 16,
                       "messages": [{"role": "user", "content": "Say OK."}],
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with opener(req, timeout=COMPLETION_TIMEOUT) as r:
            d = json.loads(r.read().decode())
        txt = (d["choices"][0]["message"].get("content") or "").strip()
        return bool(txt), f"completion: {txt[:20]!r}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:70]}"


def check_ray_nodes(want=2, runner=None):
    """cumulus1 must still see BOTH GPUs; a lost worker shows here first."""
    runner = runner or (lambda cmd, env: subprocess.run(
        cmd, capture_output=True, text=True, timeout=60, env=env))
    try:
        r = runner(["bash", "-c", f"source {RAY_ENV}; ray status 2>&1"], _user_env())
        out = r.stdout or ""
        import re
        m = re.search(r"/([0-9.]+) GPU", out)
        seen = float(m.group(1)) if m else 0.0
        return seen >= want, f"cluster GPUs: {seen:g} of {want}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:70]}"


def run_endpoint_checks(expected=None, checks=None):
    """{} when the endpoint is not expected here (nothing to judge)."""
    expected = endpoint_expected() if expected is None else expected
    if not expected:
        return {}
    checks = checks or {"vllm-models": check_vllm_models,
                        "vllm-completion": check_vllm_completion,
                        "ray-nodes": check_ray_nodes}
    return {label: dict(zip(("ok", "detail"), fn())) for label, fn in checks.items()}


def endpoint_verdict(ep, cumulus2_ok=None):
    """Blame the right LAYER, as attribute() does for CIRRUS."""
    if not ep:
        return "absent", "TP=2 endpoint not enabled on this box"
    ray_ok = ep.get("ray-nodes", {}).get("ok")
    models_ok = ep.get("vllm-models", {}).get("ok")
    comp_ok = ep.get("vllm-completion", {}).get("ok")
    if ray_ok and models_ok and comp_ok:
        return "ok", "TP=2 endpoint serving (models, completion, 2 Ray nodes)"
    if ray_ok is False and cumulus2_ok is False:
        return "cumulus2", "cumulus2 is DOWN (ssh and Ray both gone) — the box, not vLLM"
    if ray_ok is False:
        return "ray", "cumulus2 answers ssh but its Ray worker is gone — the worker unit, not the box"
    if models_ok and comp_ok is False:
        return "hung", ("vLLM answers /v1/models but a real completion FAILS — "
                        "the NCCL-deadlock shape; restart the unit")
    return "vllm", "vLLM endpoint down on cumulus1 (Ray is fine)"


def endpoint_decide(prev, comp_ok):
    """Consecutive completion failures -> restart once per streak. Pure."""
    streak = 0 if comp_ok else int(prev.get("vllm_fail_streak", 0)) + 1
    restart = (not comp_ok) and streak == RESTART_AFTER_FAILS
    return {"vllm_fail_streak": streak}, restart


def endpoint_unit_active(runner=None):
    """Only a unit that IS running can be 'hung'. A stopped one was stopped by
    someone (or crashed, and systemd's own Restart= handles that) -- starting
    it from here could load 42 GB onto cumulus1 in the middle of a client job."""
    ok, out = _user_systemctl("is-active", VLLM_UNIT, runner=runner)
    return out in ("active", "activating")


def restart_endpoint(runner=None):
    if not endpoint_unit_active(runner=runner):
        return False, "unit not active — reported, not restarted"
    return _user_systemctl("restart", VLLM_UNIT, runner=runner)


def attribute(results):
    """Turn per-target results into a LAYER verdict. This is the part the
    deadman cannot do, and the reason a one-url probe cost a day."""
    lan = results.get("cirrus-lan", {}).get("ok")
    pub = results.get("cirrus-public", {}).get("ok")
    if lan and pub:
        return "ok", "CIRRUS healthy on both the LAN and its public route"
    if lan and pub is False:
        return "tunnel", ("CIRRUS is ALIVE on the LAN but its PUBLIC ROUTE is "
                          "down — the tunnel/connector, not the box")
    if lan is False and pub:
        return "odd", "public route answers but the LAN does not — investigate"
    if lan is False and pub is False:
        return "box", "CIRRUS unreachable on BOTH paths — the box itself"
    return "unknown", "incomplete results"


def _load_state():
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(s):
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(s, indent=1))
    except Exception:
        pass


def decide(prev, bad_now):
    """Alert only on a TRANSITION. A monitor that repeats itself every 30
    minutes trains you to ignore it, which is how the next real one is missed."""
    was_bad = bool(prev.get("bad"))
    if bad_now and not was_bad:
        return {"bad": True}, "alert"
    if bad_now and was_bad:
        return {"bad": True}, "none"
    if (not bad_now) and was_bad:
        return {"bad": False}, "recovered"
    return {"bad": False}, "none"


def telegram(msg):
    try:
        creds = json.loads(CREDS_PATH.read_text())
        token, user = creds.get("telegram_bot_token"), creds.get("telegram_user_id")
        if not token or not user:
            return False
        import urllib.parse
        data = urllib.parse.urlencode({"chat_id": user, "text": msg}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20)
        return True
    except Exception:
        return False


def main():
    status_only = "--status" in sys.argv
    endpoint_only = "--endpoint" in sys.argv
    results = {} if endpoint_only else run_checks()
    verdict, detail = ("skipped", "endpoint-only run") if endpoint_only else attribute(results)

    # S125: the TP=2 endpoint, judged separately so its verdict names its layer
    ep = run_endpoint_checks()
    c2 = results.get("cumulus2-tail", {}).get("ok") if results else None
    ep_verdict, ep_detail = endpoint_verdict(ep, c2)
    results.update(ep)
    down = sorted(k for k, v in results.items() if not v["ok"])
    bad = bool(down)

    prev = _load_state()
    state, action = decide(prev, bad)
    ep_state, do_restart = endpoint_decide(
        prev, ep.get("vllm-completion", {}).get("ok", True)) if ep else ({}, False)
    state.update(ep_state)
    report = {"when": datetime.now().isoformat(timespec="seconds"),
              "verdict": verdict, "detail": detail,
              "endpoint": ep_verdict, "endpoint_detail": ep_detail,
              "down": down, "results": results, "action": action,
              "restart": do_restart}
    print(json.dumps(report, indent=1))

    if status_only:
        return 0
    _save_state(state)

    if do_restart:
        ok, out = restart_endpoint()
        telegram(f"TP=2 ENDPOINT: {ep_detail}\nrestarted {VLLM_UNIT}: "
                 f"{'ok' if ok else 'FAILED — ' + out[:120]}")
    if action == "alert":
        telegram(f"ACCESS CHECK: {detail}"
                 + (f"\nendpoint: {ep_detail}" if ep_verdict not in ("ok", "absent") else "")
                 + f"\ndown: {', '.join(down)}")
    elif action == "recovered":
        telegram("ACCESS CHECK: all routes recovered")

    try:
        import job_status
        job_status.record("accesscheck", not bad,
                          detail if bad else "all routes ok")
    except Exception as e:
        print(f"job_status.record failed: {e}")
    return 0


def selftest():
    checks = []

    def ck(d, cond):
        checks.append((d, cond))

    # attribution — the whole reason this exists beside the deadman
    ok = {"cirrus-lan": {"ok": True}, "cirrus-public": {"ok": True}}
    ck("both up -> ok", attribute(ok)[0] == "ok")
    tun = {"cirrus-lan": {"ok": True}, "cirrus-public": {"ok": False}}
    ck("LAN up + public down -> blames the TUNNEL, not the box",
       attribute(tun)[0] == "tunnel")
    ck("...and says so in words", "not the box" in attribute(tun)[1])
    box = {"cirrus-lan": {"ok": False}, "cirrus-public": {"ok": False}}
    ck("both down -> blames the BOX", attribute(box)[0] == "box")

    # THE 2026-09-04 REGRESSION: a Cloudflare edge error is NOT the origin.
    real = urllib.request.urlopen

    def fake_code(code):
        def f(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)
        return f
    try:
        for code in (520, 522, 530):
            urllib.request.urlopen = fake_code(code)
            ck(f"HTTP {code} reads as DOWN", check_https("https://x.invalid/")[0] is False)
        for code in (401, 403, 404):
            urllib.request.urlopen = fake_code(code)
            ck(f"HTTP {code} (origin answered) reads as UP",
               check_https("https://x.invalid/")[0] is True)
    finally:
        urllib.request.urlopen = real

    # alerting only on transitions
    s, a = decide({}, True);            ck("first failure alerts", a == "alert")
    s2, a2 = decide(s, True);           ck("repeat failure stays silent", a2 == "none")
    s3, a3 = decide(s2, False);         ck("recovery announces once", a3 == "recovered")
    s4, a4 = decide(s3, False);         ck("staying healthy is silent", a4 == "none")

    # run_checks must not explode on an unreachable target
    r = run_checks([("x", "tcp", ("127.0.0.1", 1))])
    ck("an unreachable target is recorded, not raised", r["x"]["ok"] is False)

    ck("cumulus2 is actually watched (nothing else watches it)",
       any(t[0] == "cumulus2-tail" for t in TARGETS))

    # ── S125: the TP=2 endpoint ───────────────────────────────────────────
    class _R:
        def __init__(self, rc, out): self.returncode, self.stdout, self.stderr = rc, out, ""
    ck("endpoint is NOT expected when the unit is not enabled (absent != down)",
       endpoint_expected(runner=lambda c, e: _R(1, "disabled")) is False)
    ck("...and IS expected when it is",
       endpoint_expected(runner=lambda c, e: _R(0, "enabled\n")) is True)
    ck("run_endpoint_checks returns {} when not expected — nothing to judge",
       run_endpoint_checks(expected=False) == {})

    class _Resp:
        def __init__(self, payload): self._p = json.dumps(payload).encode()
        def read(self): return self._p
        def __enter__(self): return self
        def __exit__(self, *a): return False
    good_models = lambda req, timeout=None: _Resp({"data": [{"id": VLLM_MODEL}]})
    ck("/v1/models with our model reads as UP",
       check_vllm_models(opener=good_models)[0] is True)
    ck("/v1/models WITHOUT our model reads as DOWN (wrong model loaded)",
       check_vllm_models(opener=lambda r, timeout=None: _Resp({"data": [{"id": "other"}]}))[0] is False)
    ck("a completion with text reads as UP",
       check_vllm_completion(opener=lambda r, timeout=None: _Resp(
           {"choices": [{"message": {"content": "OK"}}]}))[0] is True)
    ck("an EMPTY completion reads as DOWN (hung engine returns nothing)",
       check_vllm_completion(opener=lambda r, timeout=None: _Resp(
           {"choices": [{"message": {"content": ""}}]}))[0] is False)

    def boom(req, timeout=None):
        raise TimeoutError("timed out")
    ck("a completion timeout is recorded as DOWN, not raised",
       check_vllm_completion(opener=boom)[0] is False)
    ck("ray status with 2 GPUs reads as UP",
       check_ray_nodes(runner=lambda c, e: _R(0, " 0.0/2.0 GPU\n"))[0] is True)
    ck("ray status with 1 GPU reads as DOWN (worker lost)",
       check_ray_nodes(runner=lambda c, e: _R(0, " 0.0/1.0 GPU\n"))[0] is False)

    up = {"vllm-models": {"ok": True}, "vllm-completion": {"ok": True}, "ray-nodes": {"ok": True}}
    ck("all three up -> ok", endpoint_verdict(up)[0] == "ok")
    hung = dict(up, **{"vllm-completion": {"ok": False}})
    ck("models up + completion down -> HUNG (the deadlock shape)",
       endpoint_verdict(hung)[0] == "hung")
    lost = dict(up, **{"ray-nodes": {"ok": False}})
    ck("Ray node lost while cumulus2 ssh answers -> blames the WORKER UNIT",
       endpoint_verdict(lost, cumulus2_ok=True)[0] == "ray")
    ck("Ray node lost AND cumulus2 ssh dead -> blames the BOX",
       endpoint_verdict(lost, cumulus2_ok=False)[0] == "cumulus2")
    ck("nothing expected -> absent, never down", endpoint_verdict({})[0] == "absent")

    st, r1 = endpoint_decide({}, False)
    ck("first completion failure: no restart yet", r1 is False and st["vllm_fail_streak"] == 1)
    st, r2 = endpoint_decide(st, False)
    ck("SECOND consecutive failure: restart", r2 is True)
    st, r3 = endpoint_decide(st, False)
    ck("third: no second restart in the same streak", r3 is False)
    st, r4 = endpoint_decide(st, True)
    ck("a success clears the streak", r4 is False and st["vllm_fail_streak"] == 0)
    calls = []

    def _active_runner(c, e):
        calls.append(c)
        return _R(0, "active\n") if "is-active" in c else _R(0, "")
    ok_, _ = restart_endpoint(runner=_active_runner)
    ck("a HUNG (active) unit is restarted through the USER manager",
       ok_ and any(x[:2] == ["systemctl", "--user"] and "restart" in x and VLLM_UNIT in x
                   for x in calls))
    calls.clear()

    def _stopped_runner(c, e):
        calls.append(c)
        return _R(3, "inactive\n") if "is-active" in c else _R(0, "")
    ok_, why = restart_endpoint(runner=_stopped_runner)
    ck("a STOPPED unit is NOT started by the watchdog (could load 42 GB mid-job)",
       ok_ is False and "not active" in why
       and not any("restart" in x for x in calls))

    for d, ok_ in checks:
        print(f"  {'PASS' if ok_ else 'FAIL'}  {d}")
    bad = [d for d, o in checks if not o]
    print(f"\n{len(checks) - len(bad)} passed, {len(bad)} failed")
    return 0 if not bad else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(selftest())
    sys.exit(main())
