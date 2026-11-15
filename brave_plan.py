#!/usr/bin/env python3
"""
brave_plan.py  (S629, 2026-11-15)
===============================================================================
Buddy, 11-15: "Instead of increasing the monthly plan, just switch to the
prepaid till the following month."

The Brave Search account has two plans:
  * Postpaid  - key "Cirrus_Brave_API", monthly usage limit $50.
  * Prepaid   - key "Default", paid in advance.
Every search (cirrus_daily.brave_search, privacy_monitor) reads `brave_api_key`
from config/credentials.json, so changing plans means changing that one field.

Both boxes keep both keys in their encrypted credentials:
    brave_key_postpaid, brave_key_prepaid      (brave_api_key = the active one)

Every 30 minutes on CIRRUS (com.cirrus.braveplan) the controller:
  * reads this month's counted Brave searches on CIRRUS and CUMULUS1
    (logs/search-usage.json, $5 per 1,000);
  * moves both boxes to Prepaid when that reaches SWITCH_AT_USD, or when Brave
    refused the Postpaid key for funds/limit (spend_guard paused "brave"), and
    clears that pause so searches keep using Brave;
  * moves both boxes back to Postpaid on the 1st of the month.
Our own count is the main trigger because in October Brave did not refuse us
past the old $25 limit, and the error Brave sends at its limit is unverified.
Switches go into Telewarden's weekly recap (no message). A failed run records
job_status "braveplan" not ok, which jobs_check and Telewarden pick up.

Never prints a key. Keys are compared only in memory. The encrypted file is
replaced atomically, the previous one kept as credentials.json.age.bak-braveplan.

Usage:
  python3 brave_plan.py                       # controller (CIRRUS, launchd)
  python3 brave_plan.py --dry-run             # report the decision, change nothing
  python3 brave_plan.py --local status MONTH  # this box: JSON, no key values
  python3 brave_plan.py --local use postpaid|prepaid
  python3 brave_plan.py --local probe         # one search with the live key
  python3 brave_plan.py --local init-postpaid # copy brave_api_key -> brave_key_postpaid
  python3 brave_plan.py --local store-prepaid # key on stdin -> brave_key_prepaid
  python3 brave_plan.py --selftest
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))
import spend_guard  # noqa: E402

AGE = PROJECT / "config/credentials.json.age"
LIVE = PROJECT / "config/credentials.json"
USAGE = PROJECT / "logs/search-usage.json"
STATE = PROJECT / "logs/brave-plan.json"
SWITCH_AT_USD = 50.0          # the Postpaid plan's monthly limit (PAID-ACCESS-REGISTRY)
C1 = "buddy@192.168.0.204"    # same LAN route as telewarden.C1
C1_DIR = "cirrus-digest"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
PROBE_URL = "https://api.search.brave.com/res/v1/web/search?q=python+documentation&count=1"
PLANS = ("postpaid", "prepaid")


# ── this box ─────────────────────────────────────────────────────────────────
def _host():
    return socket.gethostname().split(".")[0]


def _secrets():
    for name in ("cirrus-secrets", "cumulus-secrets"):
        d = Path.home() / ".config" / name
        if (d / "age-identity.txt").exists():
            return d / "age-identity.txt", (d / "age-recipient.txt").read_text().strip()
    raise RuntimeError("no age identity on this host")


def _crypto():
    import creds_crypto       # on-box copy, deployed by the rotate-creds scripts
    return creds_crypto


def read_age():
    return _crypto().decrypt_json(AGE, _secrets()[0])


def write_age(creds):
    tmp = AGE.with_name(AGE.name + ".tmp-braveplan")
    _crypto().encrypt_json(creds, _secrets()[1], tmp)
    shutil.copy2(AGE, AGE.with_name(AGE.name + ".bak-braveplan"))
    os.replace(tmp, AGE)


def _refresh_live():
    """CIRRUS re-decrypts every 15 min, so do it now; cumulus1's loop does it every 10 s."""
    script = PROJECT / "materialize_credentials_cirrus.sh"
    if script.exists():
        subprocess.run([str(script)], capture_output=True, timeout=60, check=True)


def _live_key():
    try:
        return json.loads(LIVE.read_text()).get("brave_api_key", "")
    except (OSError, ValueError):
        return None


def _valid(k):
    return isinstance(k, str) and len(k) >= 20 and k.isascii() and k == k.strip()


def plan_of(creds):
    k = creds.get("brave_api_key")
    for plan in PLANS:
        if _valid(k) and k == creds.get("brave_key_" + plan):
            return plan
    return "unknown"


def month_requests(month, path=None):
    """Brave requests this box counted in `month` (YYYY-MM). Raises if unreadable."""
    u = json.loads(Path(path or USAGE).read_text())
    return sum(sum(c.values()) for d, p in u.items() if d.startswith(month)
               for c in p.get("brave", {}).values())


def local_status(month):
    c = read_age()
    pp, pr = c.get("brave_key_postpaid"), c.get("brave_key_prepaid")
    return {"host": _host(), "active": plan_of(c),
            "has_postpaid": _valid(pp), "has_prepaid": _valid(pr),
            "keys_differ": _valid(pp) and _valid(pr) and pp != pr,
            "live_matches": _live_key() == c.get("brave_api_key"),
            "requests": month_requests(month), "paused": spend_guard.is_paused("brave")}


def local_use(plan, wait_s=60):
    if plan not in PLANS:
        raise ValueError(f"plan must be one of {PLANS}")
    c = read_age()
    target = c.get("brave_key_" + plan)
    if not _valid(target):
        raise RuntimeError(f"no {plan} key stored on this host")
    result = "already"
    if c.get("brave_api_key") != target:
        c["brave_api_key"] = target
        write_age(c)
        if read_age().get("brave_api_key") != target:
            raise RuntimeError("the re-read encrypted file does not show the switch")
        result = "switched"
    _refresh_live()
    deadline = time.time() + wait_s
    while _live_key() != target:
        if time.time() > deadline:
            raise RuntimeError(f"live credentials did not show {plan} within {wait_s} s")
        time.sleep(5)
    return {"host": _host(), "result": result, "active": plan,
            "resumed": spend_guard.resume("brave")}


def local_probe():
    """One search with the live key: ok / funds / http NNN / error."""
    req = urllib.request.Request(PROBE_URL, headers={
        "Accept": "application/json", "X-Subscription-Token": _live_key() or ""})
    try:
        urllib.request.urlopen(req, timeout=20).read(1)
        return {"host": _host(), "probe": "ok"}
    except urllib.error.HTTPError as e:
        body = e.read(500).decode("utf-8", "replace")
        funds = spend_guard.is_funds_error(f"{e.code} {body}")
        return {"host": _host(), "probe": "funds" if funds else f"http {e.code}"}
    except Exception as e:  # noqa: BLE001 -- reported, not raised
        return {"host": _host(), "probe": "error " + type(e).__name__}


def local_init_postpaid():
    c = read_age()
    if _valid(c.get("brave_key_postpaid")):
        return {"host": _host(), "postpaid": "already stored",
                "is_active": c.get("brave_key_postpaid") == c.get("brave_api_key")}
    if not _valid(c.get("brave_api_key")):
        raise RuntimeError("brave_api_key is not a usable key; nothing to copy")
    c["brave_key_postpaid"] = c["brave_api_key"]
    write_age(c)
    ok = read_age().get("brave_key_postpaid") == c["brave_api_key"]
    return {"host": _host(), "postpaid": "stored" if ok else "NOT VERIFIED"}


def local_store_prepaid(stdin=None):
    key = (stdin if stdin is not None else sys.stdin.read()).strip()
    if not _valid(key):
        raise RuntimeError("no usable key on stdin")
    c = read_age()
    if key in (c.get("brave_api_key"), c.get("brave_key_postpaid")):
        raise RuntimeError("that is the Postpaid key, not the Prepaid one")
    c["brave_key_prepaid"] = key
    write_age(c)
    ok = read_age().get("brave_key_prepaid") == key
    return {"host": _host(), "prepaid": "stored and verified" if ok else "NOT VERIFIED"}


# ── controller (CIRRUS) ──────────────────────────────────────────────────────
def decide(month, state, statuses, cost_per_1k, switch_at=SWITCH_AT_USD):
    """-> (want, why, usd). Pure, so the selftest covers every branch."""
    usd = sum(s["requests"] for s in statuses) * cost_per_1k / 1000
    if state.get("prepaid_month") == month:
        return "prepaid", "already on Prepaid this month", usd
    if usd >= switch_at:
        return "prepaid", f"Postpaid use ${usd:.2f} reached ${switch_at:.0f}", usd
    if any(s["paused"] for s in statuses) and all(s["active"] == "postpaid" for s in statuses):
        return "prepaid", "Brave refused the Postpaid key", usd
    return "postpaid", f"Postpaid use ${usd:.2f} of ${switch_at:.0f}", usd


def _remote(args):
    r = subprocess.run(SSH + ["-n", C1, f"cd {C1_DIR} && python3 brave_plan.py --local {args}"],
                       capture_output=True, text=True, timeout=180)
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    if r.returncode != 0 or not lines:
        raise RuntimeError(f"cumulus1 '--local {args.split()[0]}' failed (rc {r.returncode})")
    return json.loads(lines[-1])


def _load_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def run(now=None, dry=False, local=None, remote=None, log=None):
    """One pass. local/remote/log are injectable for the selftest."""
    now = now or datetime.now()
    month = now.strftime("%Y-%m")
    local = local or {"status": local_status, "use": local_use, "probe": local_probe}
    remote = remote or {"status": lambda m: _remote(f"status {m}"),
                        "use": lambda p: _remote(f"use {p}"),
                        "probe": lambda: _remote("probe")}
    boxes = {"cirrus": local, "cumulus1": remote}
    import search_usage
    cost = search_usage.COST_PER_1K.get("brave", 5.0)
    state = _load_state()
    st = {name: b["status"](month) for name, b in boxes.items()}
    missing = [n for n, s in st.items() if not (s["has_postpaid"] and s["has_prepaid"] and s["keys_differ"])]
    if missing:
        raise RuntimeError("both Brave keys are not stored on " + ", ".join(missing))
    want, why, usd = decide(month, state, list(st.values()), cost)
    actions = []
    for name, s in st.items():
        if s["active"] != want:
            actions.append(f"{name}: {s['active']} -> {want}")
            if not dry:
                boxes[name]["use"](want)
        elif want == "prepaid" and s["paused"]:
            # A process that started before the switch can re-pause Brave with the
            # old key. Clear it only if the Prepaid key itself still works.
            p = boxes[name]["probe"]() if not dry else {"probe": "skipped"}
            actions.append(f"{name}: paused on Prepaid, probe {p['probe']}")
            if p["probe"] == "ok":
                boxes[name]["use"]("prepaid")          # idempotent; clears the pause
    if not dry:
        changed = any("->" in a for a in actions)
        if want == "prepaid" and state.get("prepaid_month") != month:
            state["prepaid_month"] = month
        state.update({"month": month, "active": want, "usd": round(usd, 2),
                      "checked": now.isoformat(timespec="seconds")})
        if changed:
            state.setdefault("history", []).append(
                {"t": now.isoformat(timespec="seconds"), "to": want, "why": why})
            state["history"] = state["history"][-24:]
            (log or _log_change)(f"Brave: switched both servers to the {want.title()} plan ({why})")
        STATE.write_text(json.dumps(state, indent=2) + "\n")
    return {"month": month, "want": want, "why": why, "usd": round(usd, 2), "actions": actions}


def _log_change(text):
    try:
        import telewarden
        telewarden.log_change(text)
    except Exception as e:  # noqa: BLE001 -- the switch itself already happened
        print("recap line not written:", type(e).__name__)


def main(argv):
    if "--selftest" in argv:
        return selftest()
    if "--local" in argv:
        a = argv[argv.index("--local") + 1:]
        ops = {"status": lambda: local_status(a[1]), "use": lambda: local_use(a[1]),
               "probe": local_probe, "init-postpaid": local_init_postpaid,
               "store-prepaid": local_store_prepaid}
        print(json.dumps(ops[a[0]]()))
        return 0
    dry = "--dry-run" in argv
    try:
        r = run(dry=dry)
        line = f"{r['month']}: {r['why']}; " + ("; ".join(r["actions"]) or f"stays on {r['want']}")
        print(datetime.now().strftime("[%Y-%m-%d %H:%M] ") + ("DRY RUN " if dry else "") + line)
        ok, note = True, line
    except Exception as e:  # noqa: BLE001 -- recorded for jobs_check
        print(datetime.now().strftime("[%Y-%m-%d %H:%M] ") + "FAILED: " + str(e)[:300])
        ok, note = False, "failed: " + str(e)[:200]
    if not dry:
        import job_status
        job_status.record("braveplan", ok, note)
    return 0 if ok else 1


# ── selftest (temp files and fakes only; never the live credentials) ─────────
def selftest():
    import io
    import tempfile
    from contextlib import redirect_stdout
    global AGE, LIVE, USAGE, STATE
    fails = 0

    def ck(label, cond):
        nonlocal fails
        print(f"  [{'OK ' if cond else 'FAIL'}] {label}")
        fails += 0 if cond else 1

    tmp = Path(tempfile.mkdtemp())
    AGE, LIVE, USAGE, STATE = (tmp / "c.age", tmp / "c.json", tmp / "u.json", tmp / "s.json")
    spend_guard.LEDGER = tmp / "pause.json"
    os.environ["SPEND_GUARD_NO_ALERT"] = "1"
    POST, PRE = "BSA3" + "p" * 27, "BSAB" + "q" * 27
    store = {"creds": {"brave_api_key": POST}}
    g = globals()
    saved = {k: g[k] for k in ("read_age", "write_age", "_refresh_live")}
    g["read_age"] = lambda: json.loads(json.dumps(store["creds"]))

    def fake_write(c):
        store["creds"] = json.loads(json.dumps(c))
    g["write_age"] = fake_write
    g["_refresh_live"] = lambda: LIVE.write_text(json.dumps(store["creds"]))
    try:
        LIVE.write_text(json.dumps(store["creds"]))
        USAGE.write_text(json.dumps({"2026-10-31": {"brave": {"x": {"ok": 9000}}},
                                     "2026-11-01": {"brave": {"x": {"ok": 4000, "error": 10}}},
                                     "2026-11-02": {"gemini": {"x": {"ok": 50}}}}))
        ck("month_requests counts only that month's Brave rows", month_requests("2026-11") == 4010)

        out = io.StringIO()
        with redirect_stdout(out):
            print(json.dumps(local_init_postpaid()))
            print(json.dumps(local_store_prepaid(PRE)))
        ck("init + store keep both keys", store["creds"].get("brave_key_postpaid") == POST
           and store["creds"].get("brave_key_prepaid") == PRE)
        try:
            local_store_prepaid(POST)
            ck("refuses the Postpaid key as Prepaid", False)
        except RuntimeError:
            ck("refuses the Postpaid key as Prepaid", True)
        s = local_status("2026-11")
        ck("status reads postpaid, both keys, live in step", s["active"] == "postpaid"
           and s["has_prepaid"] and s["keys_differ"] and s["live_matches"])

        spend_guard.pause("brave", "limit")
        with redirect_stdout(out):
            print(json.dumps(local_use("prepaid", wait_s=1)))
        ck("use prepaid switches, refreshes live and clears the pause",
           store["creds"]["brave_api_key"] == PRE and json.loads(LIVE.read_text())["brave_api_key"] == PRE
           and not spend_guard.is_paused("brave"))
        ck("no key value ever printed", POST not in out.getvalue() and PRE not in out.getvalue())

        st = lambda r, paused=False, active="postpaid": {"requests": r, "paused": paused, "active": active}
        ck("under $50 stays Postpaid", decide("2026-11", {}, [st(4000), st(5000)], 5.0)[0] == "postpaid")
        ck("$50 across both boxes switches", decide("2026-11", {}, [st(4000), st(6000)], 5.0)[0] == "prepaid")
        ck("a Brave refusal switches early", decide("2026-11", {}, [st(10), st(10, True)], 5.0)[0] == "prepaid")
        ck("stays Prepaid for the rest of the month",
           decide("2026-11", {"prepaid_month": "2026-11"}, [st(0, False, "prepaid")] * 2, 5.0)[0] == "prepaid")
        ck("back to Postpaid on the 1st",
           decide("2026-12", {"prepaid_month": "2026-11"}, [st(0, False, "prepaid")] * 2, 5.0)[0] == "postpaid")

        # Controller pass with fakes for both boxes.
        calls, logs = [], []
        box = lambda name, active, req, paused=False, probe="ok": {
            "status": lambda m: {"host": name, "active": active, "has_postpaid": True, "has_prepaid": True,
                                 "keys_differ": True, "live_matches": True, "requests": req, "paused": paused},
            "use": lambda p: calls.append((name, p)), "probe": lambda: {"probe": probe}}
        r = run(datetime(2026, 11, 20), local=box("cirrus", "postpaid", 6000),
                remote=box("cumulus1", "postpaid", 4500), log=logs.append)
        ck("controller switches both boxes at $52.50", calls == [("cirrus", "prepaid"), ("cumulus1", "prepaid")]
           and r["want"] == "prepaid" and len(logs) == 1)
        calls.clear()
        run(datetime(2026, 11, 21), local=box("cirrus", "prepaid", 0, paused=True),
            remote=box("cumulus1", "prepaid", 0, paused=True, probe="funds"), log=logs.append)
        ck("stale pause cleared only where the Prepaid probe works", calls == [("cirrus", "prepaid")])
        calls.clear()
        run(datetime(2026, 12, 1, 0, 30), local=box("cirrus", "prepaid", 0),
            remote=box("cumulus1", "prepaid", 0), log=logs.append)
        ck("December 1 returns both to Postpaid", calls == [("cirrus", "postpaid"), ("cumulus1", "postpaid")])
        try:
            bad = box("cumulus1", "postpaid", 0)
            bad["status"] = lambda m: dict(box("cumulus1", "postpaid", 0)["status"](m), has_prepaid=False)
            run(datetime(2026, 11, 5), local=box("cirrus", "postpaid", 0), remote=bad, log=logs.append)
            ck("a box without the Prepaid key fails loudly", False)
        except RuntimeError as e:
            ck("a box without the Prepaid key fails loudly", "cumulus1" in str(e))
        calls.clear()
        STATE.write_text("{}")         # fresh month state, so the dry run decides on the count
        r = run(datetime(2026, 11, 22), dry=True, local=box("cirrus", "postpaid", 9000),
                remote=box("cumulus1", "postpaid", 2000), log=logs.append)
        ck("dry run changes nothing", calls == [] and r["want"] == "prepaid")
    finally:
        g.update(saved)
    print("PASS" if not fails else f"{fails} FAILURE(S)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
