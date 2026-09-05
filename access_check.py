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
"""

import json
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
    results = run_checks()
    verdict, detail = attribute(results)
    down = sorted(k for k, v in results.items() if not v["ok"])
    bad = bool(down)

    state, action = decide(_load_state(), bad)
    report = {"when": datetime.now().isoformat(timespec="seconds"),
              "verdict": verdict, "detail": detail, "down": down,
              "results": results, "action": action}
    print(json.dumps(report, indent=1))

    if status_only:
        return 0
    _save_state(state)

    if action == "alert":
        telegram(f"ACCESS CHECK: {detail}\ndown: {', '.join(down)}")
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

    for d, ok_ in checks:
        print(f"  {'PASS' if ok_ else 'FAIL'}  {d}")
    bad = [d for d, o in checks if not o]
    print(f"\n{len(checks) - len(bad)} passed, {len(bad)} failed")
    return 0 if not bad else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(selftest())
    sys.exit(main())
