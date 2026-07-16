#!/usr/bin/env python3
"""
cirrus_watchdog.py — CIRRUS-side service watchdog + self-heal (Session 35).

The CIRRUS twin of the MacBook's heartbeat.py: every 30 min
(com.cirrus.watchdog LaunchAgent) it checks the com.cirrus.* services,
repairs what it can, and — because it runs ON the box that owns the Telegram
credentials — alerts Buddy DIRECTLY even when the bot itself is the casualty
(tonight's failure mode: bot crash-looping on malformed credentials.json,
discovered only by a silent /help).

Checks per service:
  com.cirrus.bot     loaded + process alive; if dead: is credentials.json
                     valid JSON? (reports exact parse position, never contents)
  com.cirrus.api     GET http://127.0.0.1:5001/status with the local token
  com.cirrus.tunnel  loaded + cloudflared process present
  com.cirrus.daily / com.cirrus.devloop   loaded (scheduled jobs — no PID
                     expected between runs)

Repair: launchctl kickstart -k (persistent services only). 3 consecutive
failed repairs → stop retrying, alert HUMAN NEEDED with the diagnosis.
Never touches credentials; never reinstalls plists (launchctl state only).

Reporting: appends src="cirrus" to logs/heartbeats.json (same file the
MacBook reports into → /status shows both) and Telegrams Buddy on any
repair/degradation (deduped per episode via logs/watchdog-state.json).

Manual run:  cd ~/projects/cirrus-digest && python3 cirrus_watchdog.py
Self-test:   python3 cirrus_watchdog.py selftest   (offline, no services)
"""

import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path.home() / "projects/cirrus-digest"
CREDS_PATH  = PROJECT_DIR / "config/credentials.json"
STATE_PATH  = PROJECT_DIR / "logs/watchdog-state.json"
HB_PATH     = PROJECT_DIR / "logs/heartbeats.json"
LOG_PATH    = PROJECT_DIR / "logs/watchdog.log"

PERSISTENT = {"com.cirrus.bot", "com.cirrus.api", "com.cirrus.tunnel"}
SCHEDULED  = {"com.cirrus.daily", "com.cirrus.devloop"}
MAX_REPAIRS = 3


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── independent Telegram send (works when the bot is down) ────────────────────
def telegram(text: str) -> bool:
    try:
        creds = json.loads(CREDS_PATH.read_text())
        token, chat = creds["telegram_bot_token"], creds["telegram_user_id"]
    except Exception as e:
        log(f"telegram unavailable (creds: {e})")
        return False
    try:
        data = json.dumps({"chat_id": int(chat), "text": text,
                           "parse_mode": "Markdown"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data,
            headers={"Content-Type": "application/json",
                     "User-Agent": "CirrusWatchdog/1.0"})
        urllib.request.urlopen(req, timeout=30).read()
        return True
    except Exception as e:
        log(f"telegram send failed: {e}")
        return False


# ── checks ────────────────────────────────────────────────────────────────────
def launchctl_state():
    """label -> (pid_or_None, exit_code) for com.cirrus.* loaded agents."""
    out = subprocess.run(["launchctl", "list"], capture_output=True,
                         text=True).stdout
    st = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2].startswith("com.cirrus."):
            pid = None if parts[0] == "-" else parts[0]
            try:
                code = int(parts[1])
            except ValueError:
                code = 0
            st[parts[2]] = (pid, code)
    return st


def creds_diagnosis() -> str:
    """'' if credentials.json parses; else the parse error (position only)."""
    try:
        json.loads(CREDS_PATH.read_text())
        return ""
    except Exception as e:
        return f"credentials.json INVALID: {e}"


def api_ok() -> bool:
    try:
        creds = json.loads(CREDS_PATH.read_text())
        tok = creds.get("api_token", "")
        req = urllib.request.Request(
            f"http://127.0.0.1:5001/status?token={tok}",
            headers={"User-Agent": "CirrusWatchdog/1.0"})
        body = urllib.request.urlopen(req, timeout=15).read().decode()
        return '"ok"' in body
    except Exception:
        return False


def cloudflared_running() -> bool:
    r = subprocess.run(["pgrep", "-x", "cloudflared"], capture_output=True)
    return r.returncode == 0


def kickstart(svc: str):
    uid = os.getuid()
    return subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{svc}"],
                          capture_output=True, text=True).returncode == 0


# ── main pass ────────────────────────────────────────────────────────────────
def check_and_heal():
    """Returns (status, notes:list). status: ok | repaired | degraded."""
    try:
        state = json.loads(STATE_PATH.read_text())
    except Exception:
        state = {}
    st = launchctl_state()
    findings, repairs = [], []

    def svc_problem(svc):
        if svc not in st:
            return "not loaded in launchctl"
        pid, code = st[svc]
        if svc in PERSISTENT and pid is None:
            # bot special-case: the usual killer is a broken creds file —
            # kickstarting into a crash-loop helps nobody; diagnose instead.
            if svc == "com.cirrus.bot":
                d = creds_diagnosis()
                if d:
                    return f"process dead + {d}"
            return f"process not running (last exit {code})"
        # Only treat a non-zero LAST exit code as a problem when the process
        # is NOT currently running. launchctl reports the previous exit
        # status even while a healthy replacement runs — and kickstart -k
        # itself leaves -15 (SIGTERM) behind, so checking it on a live pid
        # created a self-perpetuating 30-min kill loop (bot+api restarted
        # every pass, 2026-07-15/16 — Session 39 fix).
        if pid is None and code not in (0,):
            return f"last exit code {code}"
        return ""

    for svc in sorted(PERSISTENT | SCHEDULED):
        problem = svc_problem(svc)
        # deeper functional checks even when launchctl looks fine
        if not problem and svc == "com.cirrus.api" and not api_ok():
            problem = "launchctl OK but /status not answering on :5001"
        if not problem and svc == "com.cirrus.tunnel" and not cloudflared_running():
            problem = "launchctl OK but no cloudflared process"
        if not problem:
            state[svc] = {"fails": 0}
            continue

        fails = state.get(svc, {}).get("fails", 0)
        findings.append(f"{svc}: {problem}")
        if "credentials.json INVALID" in problem:
            # unrepairable by us — human must fix the file (vi). Don't loop.
            state[svc] = {"fails": fails + 1}
            continue
        if fails >= MAX_REPAIRS:
            findings.append(f"{svc}: {fails} repairs failed — HUMAN NEEDED")
            continue
        if svc in PERSISTENT or svc in SCHEDULED:
            ok = kickstart(svc)
            # verify: re-list; persistent services should have a PID shortly
            import time as _t
            _t.sleep(3)
            st2 = launchctl_state()
            healthy = svc in st2 and (svc in SCHEDULED or st2[svc][0] is not None)
            repairs.append(f"{svc}: kickstart {'OK' if ok and healthy else 'did not stick'}")
            state[svc] = {"fails": 0 if (ok and healthy) else fails + 1,
                          "last_repair": datetime.now().isoformat()}

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")

    if not findings:
        return "ok", []
    bad = any("HUMAN NEEDED" in f or "INVALID" in f for f in findings) or \
          any("did not stick" in r for r in repairs)
    return ("degraded" if bad else "repaired"), findings + repairs


def record_heartbeat(status, notes):
    try:
        hb = json.loads(HB_PATH.read_text()) if HB_PATH.exists() else {}
    except Exception:
        hb = {}
    now = datetime.now().isoformat(timespec="seconds")
    entry = hb.get("cirrus", {})
    history = entry.get("history", [])
    note = "; ".join(notes)[:400] if notes else "all services healthy"
    history.append({"ts": now, "status": status, "note": note[:120]})
    hb["cirrus"] = {"ts": now, "status": status, "note": note,
                    "history": history[-20:]}
    HB_PATH.parent.mkdir(parents=True, exist_ok=True)
    HB_PATH.write_text(json.dumps(hb, indent=2) + "\n")


def alert_if_needed(status, notes):
    """Telegram on non-ok, deduped: only when the note-set changes."""
    if status == "ok":
        return
    try:
        state = json.loads(STATE_PATH.read_text())
    except Exception:
        state = {}
    sig = "|".join(sorted(notes))[:300]
    if state.get("_last_alert_sig") == sig:
        return   # same problem already reported this episode
    state["_last_alert_sig"] = sig
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")
    icon = "🔧" if status == "repaired" else "⚠️"
    telegram(f"{icon} *CIRRUS watchdog* ({status}):\n" +
             "\n".join(f"• {n}" for n in notes[:8]))


def clear_alert_episode(status):
    if status != "ok":
        return
    try:
        state = json.loads(STATE_PATH.read_text())
    except Exception:
        return
    if state.pop("_last_alert_sig", None) is not None:
        STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")
        telegram("✅ *CIRRUS watchdog*: all services healthy again.")


def main():
    log("watchdog pass start")
    status, notes = check_and_heal()
    record_heartbeat(status, notes)
    alert_if_needed(status, notes)
    clear_alert_episode(status)
    log(f"watchdog pass done: {status}" + (f" — {'; '.join(notes)[:200]}" if notes else ""))


# ── offline self-test ─────────────────────────────────────────────────────────
def _selftest():
    ok = fail = 0

    def check(name, cond):
        nonlocal ok, fail
        ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")

    import tempfile
    global PROJECT_DIR, CREDS_PATH, STATE_PATH, HB_PATH, LOG_PATH
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        PROJECT_DIR, CREDS_PATH = tdp, tdp / "credentials.json"
        STATE_PATH, HB_PATH, LOG_PATH = (tdp / "state.json", tdp / "hb.json",
                                         tdp / "wd.log")
        # creds diagnosis: invalid file reports position, not contents
        CREDS_PATH.write_text('{"a": 1,\n "b": }')
        d = creds_diagnosis()
        check("creds_diagnosis flags invalid JSON", "INVALID" in d)
        check("creds_diagnosis leaks no values", "1" not in d.split("line")[0].replace("credentials.json", ""))
        CREDS_PATH.write_text('{"a": 1}')
        check("creds_diagnosis passes valid JSON", creds_diagnosis() == "")

        # heartbeat record round-trip
        record_heartbeat("repaired", ["com.cirrus.bot: kickstart OK"])
        hb = json.loads(HB_PATH.read_text())
        check("heartbeat row written (src=cirrus)", hb["cirrus"]["status"] == "repaired")
        record_heartbeat("ok", [])
        hb = json.loads(HB_PATH.read_text())
        check("heartbeat history accumulates", len(hb["cirrus"]["history"]) == 2)

        # alert dedupe signature
        STATE_PATH.write_text("{}")
        state = json.loads(STATE_PATH.read_text())
        state["_last_alert_sig"] = "x|y"
        STATE_PATH.write_text(json.dumps(state))
        check("dedupe state persists", "_last_alert_sig" in json.loads(STATE_PATH.read_text()))

    print(f"\n{ok} passed, {fail} failed")
    return fail == 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(0 if _selftest() else 1)
    main()
