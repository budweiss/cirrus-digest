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
# MONITOR_ONLY (S49): scheduled agents we watch for loaded + clean-exit and ALERT
# on trouble, but NEVER kickstart — several of these send client email, and a
# repair kickstart would fire an off-schedule send. Loaded/exit monitoring only;
# run-success is tracked separately by job_status.py / jobs_check.py.
# S57: billsnow / billnewdev / pedagogy were CUT OVER to CUMULUS and intentionally
# unloaded on CIRRUS — dropped from this set so the watchdog doesn't false-alarm
# "not loaded" for jobs that no longer live here. Their run-success is now watched
# from CUMULUS's ledger via the morning brief (job_status node-aware pull); their
# service health on CUMULUS is managed by systemd (see the CUMULUS-watchdog TODO).
MONITOR_ONLY = {"com.cirrus.morningbrief", "com.cirrus.stratusreview",
                "com.cirrus.privacymon"}
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
    """label -> (pid_or_None, exit_code) for every loaded com.cirrus.* job.

    S71: reads BOTH domains. `launchctl list` only reports the caller's own
    bootstrap namespace, so once jobs began converting to system LaunchDaemons a
    single call could no longer see all of them — and the watchdog would have
    reported healthy daemons as "not loaded in launchctl" and tried to repair
    things that were fine. A watchdog that is blind to half the machine is worse
    than none, because it manufactures confidence.
    """
    # THREE sources, because no single one sees the whole machine during the
    # LaunchAgent -> LaunchDaemon migration, and being blind in either direction
    # makes the watchdog cry wolf:
    #   * as a GUI agent, it could not see converted daemons  (seen live 11:19,
    #     reporting a healthy com.cirrus.api as "not loaded" and then failing to
    #     "repair" it);
    #   * as a system daemon, `launchctl list` returns the SYSTEM domain, so the
    #     not-yet-converted GUI agents vanish instead (seen live 11:24, same
    #     false alarm about com.cirrus.daily / devloop / morningbrief).
    # Reading all three costs nothing and is correct at every point of the
    # migration, including both ends of it.
    def _dump(*args):
        try:
            return subprocess.run(["launchctl", *args], capture_output=True,
                                  text=True, timeout=20).stdout
        except Exception:
            return ""

    out = _dump("list")
    sysout = _dump("print", "system")
    guiout = _dump("print", f"gui/{os.getuid()}")
    st = {}
    for line in (sysout + "\n" + guiout).splitlines():
        parts = line.split()
        # rows look like:  <pid|-> <status> com.cirrus.foo
        if len(parts) == 3 and parts[2].startswith("com.cirrus."):
            pid = None if parts[0] in ("-", "0") else parts[0]
            try:
                code = int(parts[1])
            except ValueError:
                code = 0
            st[parts[2]] = (pid, code)
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


def launchctl_target(label: str) -> str:
    """Which domain actually holds this job — system or the GUI session?

    S71: the hardcoded gui/<uid>/<label> was right only while every com.cirrus.*
    job was a user LaunchAgent AND the watchdog was one too. A converted job
    lives in `system`, and after a reboot with nobody logged in gui/<uid> does
    not exist at all. Falls back to the GUI domain, so nothing changes for
    agents that have not been converted yet.
    """
    try:
        if subprocess.run(["launchctl", "print", f"system/{label}"],
                          capture_output=True, timeout=10).returncode == 0:
            return f"system/{label}"
    except Exception:
        pass
    return f"gui/{os.getuid()}/{label}"


def kickstart(svc: str):
    return subprocess.run(["launchctl", "kickstart", "-k", launchctl_target(svc)],
                          capture_output=True, text=True).returncode == 0


# ── main pass ────────────────────────────────────────────────────────────────
# ── S83: log error-RATE watch ────────────────────────────────────────────────
# The gap this closes, precisely. On 2026-08-24 a second cirrus_bot started (an
# ignored `selftest` argument) and 409-Conflicted the launchd-owned one at ~60
# errors/min for four days. This watchdog saw com.cirrus.bot loaded with a live
# PID and called it healthy — and it was healthy. There were simply TWO of it,
# and the only evidence anywhere was ~290,000 "HTTP Error 409: Conflict" lines
# in a log that nothing read.
#
# So watch the RATE, not the presence. A box where every service is up and one
# log is screaming is a broken box, and until now nothing here could say so.
LOG_WATCH = {
    # log file      -> error lines per hour that count as a fault
    "bot.log":        30,   # steady state ~0; the 409 storm ran at ~3600/h
    "watchdog.log":   30,
    "digest.log":    120,   # legitimately logs per-source 403s and timeouts
                            # DURING a run and still completes — a threshold
                            # under that would cry wolf every Sunday, and a
                            # check that cries wolf gets switched off
}
LOG_ERR_RX     = re.compile(r"error|failed|conflict|traceback", re.I)
_LOG_TS_RX     = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")
LOG_TAIL_BYTES = 512 * 1024


def log_error_rates(now=None, log_dir=None):
    """Findings for every watched log whose last-hour error rate is over its
    threshold. Reads only the TAIL — bot.log reached 290k lines, and a check
    that must read all of it is a check that gets turned off.

    A MISSING log is reported, never skipped as clean (T8): "could not look"
    and "looked and it was fine" are different answers.
    """
    now = now or datetime.now()
    d = Path(log_dir) if log_dir else (PROJECT_DIR / "logs")
    findings = []
    for name, limit in sorted(LOG_WATCH.items()):
        fp = d / name
        try:
            with open(fp, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - LOG_TAIL_BYTES))
                text = f.read().decode("utf-8", "replace")
        except FileNotFoundError:
            findings.append(f"{name}: expected log is MISSING — cannot check it")
            continue
        except Exception as e:
            findings.append(f"{name}: unreadable ({e}) — cannot check it")
            continue
        n = 0
        for line in text.splitlines():
            m = _LOG_TS_RX.match(line)
            if not m:
                continue          # includes the partial first line of the tail
            try:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if 0 <= (now - ts).total_seconds() <= 3600 and LOG_ERR_RX.search(line):
                n += 1
        if n > limit:
            findings.append(f"{name}: {n} error lines in the last hour "
                            f"(limit {limit}) — something is failing quietly")
    return findings


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

    for svc in sorted(PERSISTENT | SCHEDULED | MONITOR_ONLY):
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
        if svc in MONITOR_ONLY:
            # Alert only — NEVER kickstart (a repair would fire an off-schedule
            # client send). Buddy checks the job log / job_status for the cause.
            findings.append(f"{svc}: monitor-only, not auto-repaired — check the job log")
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
    bad = any("HUMAN NEEDED" in f or "INVALID" in f or "monitor-only" in f
              for f in findings) or \
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
    # S83: every service can be UP while something screams into a log nobody
    # reads. Fold that in here so it travels the same heartbeat/alert path,
    # including the existing per-episode dedupe.
    lognotes = log_error_rates()
    if lognotes:
        status = "degraded"
        notes = list(notes) + lognotes
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

    # S57: the cut-over jobs must NOT be watched on CIRRUS (they moved to CUMULUS).
    moved = {"com.cirrus.billsnow", "com.cirrus.billnewdev", "com.cirrus.pedagogy"}
    check("moved jobs dropped from MONITOR_ONLY", not (MONITOR_ONLY & moved))

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

        # ---- S83: log error-rate watch ----
        ld = tdp / "logs"; ld.mkdir()
        t = datetime.now()

        def _write(name, count, minutes_ago, text="HTTP Error 409: Conflict"):
            stamp = t - __import__("datetime").timedelta(minutes=minutes_ago)
            (ld / name).write_text("".join(
                "[%s] API error (getUpdates): %s\n"
                % (stamp.strftime("%Y-%m-%d %H:%M:%S"), text)
                for _ in range(count)))

        _write("bot.log", 200, 10)                 # 200 errors, 10 min ago
        (ld / "watchdog.log").write_text("")
        (ld / "digest.log").write_text("")
        f = log_error_rates(now=t, log_dir=ld)
        check("log watch: a screaming log is reported",
              any("bot.log" in x and "200 error lines" in x for x in f))

        # the whole point of a RATE: yesterday's storm is not today's fault
        _write("bot.log", 200, 60 * 26)
        f = log_error_rates(now=t, log_dir=ld)
        check("log watch: errors OLDER than the window do not fire",
              not any("bot.log" in x for x in f))

        # thresholds are per-log: 50 is a fault for the bot, normal for a digest
        _write("bot.log", 50, 5)
        _write("digest.log", 50, 5, text="Fetch error: 403 Forbidden")
        f = log_error_rates(now=t, log_dir=ld)
        check("log watch: per-log threshold — 50 trips bot.log",
              any("bot.log" in x for x in f))
        check("log watch: per-log threshold — 50 does NOT trip digest.log",
              not any("digest.log" in x for x in f))

        # T8: a log we could not read must not read as clean
        (ld / "bot.log").unlink()
        f = log_error_rates(now=t, log_dir=ld)
        check("log watch: a MISSING log is reported, not silently clean",
              any("bot.log" in x and "MISSING" in x for x in f))

        # and a genuinely quiet box stays quiet
        for n in ("bot.log", "watchdog.log", "digest.log"):
            (ld / n).write_text("[%s] all good\n" % t.strftime("%Y-%m-%d %H:%M:%S"))
        check("log watch: a quiet box produces NO findings",
              log_error_rates(now=t, log_dir=ld) == [])

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
