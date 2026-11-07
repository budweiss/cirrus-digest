#!/usr/bin/env python3
"""tailscale_heal.py -- keep THIS box on the tailnet, and say so (S579).

Spec: Cowork docs/TAILSCALE-SELFHEAL-SPEC.md. Every 5 min and at boot on
CIRRUS (com.cirrus.tailscaleheal), cumulus1 (cirrus-tailscaleheal.timer, via
tailscale_heal_cumulus.py) and cumulus2 (user cowork-tailscaleheal.timer,
--no-record). cumulus2 has no Telegram and no secrets: cumulus1 reads its
state over the private link, relays its messages, and logs it back in.

Mode, read from logs/tailscale-heal.mode (default shadow):
  shadow   classify, record, alert "[shadow] would ...", repair nothing
  repair   + restart a dead daemon, undo `tailscale down`
  relogin  + log a logged-out node back in with tailscale_oauth_secret
logs/tailscale-heal.hold (first line: ISO expiry, or empty = 24 h from mtime)
pauses repairs and alerts on this box.

Never logs or sends raw tailscale output: a logged-out node's status carries a
login URL (T192). Only named JSON fields are read. The secret travels on stdin
(--auth-key=file:/dev/stdin), never argv, never a file.

Usage: tailscale_heal.py [--host NAME] [--no-record] [--check] [--now]
       tailscale_heal.py --remote          (cumulus2 forced command; verb from
                                            SSH_ORIGINAL_COMMAND: status|relogin)
       tailscale_heal.py --selftest
"""
import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOGS = HERE / "logs"
CREDS = HERE / "config" / "credentials.json"

EXPECT = ("cirrus", "cumulus1", "cumulus2")
TAG = "tag:server"
LAUNCHD = "system/homebrew.mxcl.tailscale"
PLIST = "/Library/LaunchDaemons/homebrew.mxcl.tailscale.plist"
C2_ADDR = "buddy@192.168.100.11"                  # cumulus2 over the C1-C2 private link
C2_KEY = Path.home() / ".ssh" / "tailscale_heal_c2"
OAUTH_SUFFIX = "?ephemeral=false&preauthorized=true"  # oauthkey defaults: ephemeral=TRUE

HOSTS = {
    # flags: what `tailscale up --reset` must restate (spec §5.4). accept-dns
    # stays on everywhere: CIRRUS's `ssh cumulus1` resolves through MagicDNS.
    "cirrus":   {"ts": "/opt/homebrew/bin/tailscale", "svc": "launchd",
                 "flags": ["--hostname=cirrus", "--operator=buddy"],
                 "observe": ("cumulus1",), "speaks": True, "hygiene": False},
    "cumulus1": {"ts": "/usr/bin/tailscale", "svc": "systemd",
                 "flags": ["--hostname=cumulus1"],
                 "observe": ("cirrus", "cumulus2"), "speaks": True, "hygiene": True,
                 "relay": "cumulus2"},
    "cumulus2": {"ts": "/usr/bin/tailscale", "svc": "systemd",
                 "flags": ["--hostname=cumulus2"],
                 "observe": (), "speaks": False, "hygiene": False},
}

CONFIRM_S = 60          # a fault must be seen twice, this far apart
YOUNG_S = 180           # a daemon younger than this is still settling
STARTING_MAX_S = 600    # "Starting" longer than this = control unreachable
H, D = 3600, 86400
LIMITS = {              # kind: (per hour, per day, pause after N straight failures, pause s)
    "restart": (3, 12, 3, 6 * H),
    "up":      (3, 12, 3, 6 * H),
    "relogin": (1, 3, 3, D),
}
DAILY_CAP = 6           # Telegram messages per box per 24 h

_SCRUB = [(re.compile(r"https://login\.tailscale\.com/a/\S+"), "[login-url]"),
          (re.compile(r"tskey-[A-Za-z0-9_?=&-]+"), "[tskey]")]


def scrub(s):
    s = s or ""
    for rx, rep in _SCRUB:
        s = rx.sub(rep, s)
    return s


def run(cmd, timeout=30, stdin=None):
    """(rc, stdout, scrubbed stderr). stdout is for parsing only, never logged."""
    try:
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, r.stdout, scrub(r.stderr).strip()[:200]
    except FileNotFoundError:
        return 127, "", "not found: " + cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %ss" % timeout


def now_s():
    return time.time()


def hhmm(t):
    return datetime.fromtimestamp(t).strftime("%H:%M")


def dur(s):
    s = int(s)
    return "%dh %02dm" % (s // H, (s % H) // 60) if s >= H else "%d min" % max(1, s // 60)


# ── reading the box ──────────────────────────────────────────────────────────
def label(dns):
    return (dns or "").split(".")[0].lower()


def node(p):
    ips = p.get("TailscaleIPs") or []
    return {"host": (p.get("HostName") or "").lower(), "dns": label(p.get("DNSName")),
            "online": bool(p.get("Online")), "ip": ips[0] if ips else "",
            "tags": list(p.get("Tags") or []), "keyexp": p.get("KeyExpiry") or "",
            "lastseen": (p.get("LastSeen") or "")[:10]}


def facts(st):
    """Named fields only. AuthURL, keys and capabilities are never copied."""
    st = st or {}
    return {"state": st.get("BackendState") or "unknown",
            "self": node(st.get("Self") or {}),
            "peers": [node(p) for p in (st.get("Peer") or {}).values()],
            "health": [scrub(h)[:120] for h in (st.get("Health") or [])][:3]}


def service(cfg):
    """{'up': bool, 'loaded': bool, 'age': seconds or None, 'detail': str}."""
    if cfg["svc"] == "launchd":
        rc, out, _ = run(["launchctl", "print", LAUNCHD], 15)
        if rc != 0:
            return {"up": False, "loaded": False, "age": None, "detail": "launchd job not loaded"}
        m = re.search(r"^\s*pid = (\d+)", out, re.M)
        if not m:
            return {"up": False, "loaded": True, "age": None, "detail": "loaded, no pid"}
        rc, et, _ = run(["ps", "-o", "etime=", "-p", m.group(1)], 10)
        return {"up": True, "loaded": True, "age": _etime(et), "detail": "running"}
    rc, out, _ = run(["systemctl", "show", "tailscaled", "--timestamp=unix",
                      "-p", "ActiveState", "-p", "SubState", "-p", "ActiveEnterTimestamp"], 15)
    kv = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    act = kv.get("ActiveState", "unknown")
    m = re.match(r"@(\d+)", kv.get("ActiveEnterTimestamp", ""))
    age = now_s() - int(m.group(1)) if m else None
    return {"up": act == "active", "loaded": True, "age": age,
            "detail": "%s/%s" % (act, kv.get("SubState", "?"))}


def _etime(s):
    """ps etime [[dd-]hh:]mm:ss -> seconds."""
    s = (s or "").strip()
    if not s:
        return None
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = [int(x) for x in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return days * D + parts[0] * H + parts[1] * 60 + parts[2]


def probe(cfg):
    rc, out, err = run([cfg["ts"], "status", "--json"], 20)
    st = None
    if rc == 0 and out.strip():
        try:
            st = json.loads(out)
        except ValueError:
            st = None
    return {"facts": facts(st) if st is not None else None, "err": err,
            "svc": service(cfg), "busy": _updating()}


def _updating():
    """True while a package manager or tailscale's updater may be restarting it."""
    rc, out, _ = run(["pgrep", "-fl", "dpkg|apt-get|tailscale update|brew upgrade"], 5)
    return rc == 0 and bool(out.strip())


def tcp_ok(host, port=443, timeout=5):
    try:
        socket.create_connection((host, port), timeout).close()
        return True
    except OSError:
        return False


# ── classification (pure) ────────────────────────────────────────────────────
def classify(p, starting_since=None, now=None, net=None):
    """(state, detail). States: OK TRANSIENT DAEMON_DOWN STOPPED NEEDS_LOGIN
    NEEDS_MACHINE_AUTH CONTROL_UNREACHABLE NETWORK_DOWN."""
    now = now or now_s()
    svc, f = p["svc"], p["facts"]
    young = svc.get("age") is not None and svc["age"] < YOUNG_S
    if f is None:
        if p.get("busy") or (svc["up"] and young):
            return "TRANSIENT", "daemon (re)starting"
        return "DAEMON_DOWN", svc["detail"] + ("; " + p["err"] if p.get("err") else "")
    st = f["state"]
    if st == "Stopped":
        return "STOPPED", "WantRunning=false"
    if st == "NeedsLogin":
        return "NEEDS_LOGIN", "logged out"
    if st == "NeedsMachineAuth":
        return "NEEDS_MACHINE_AUTH", "waiting for device approval"
    if st in ("Starting", "NoState"):
        if young or p.get("busy") or starting_since is None or now - starting_since < STARTING_MAX_S:
            return "TRANSIENT", st
    elif st == "Running" and f["self"]["online"]:
        return "OK", "Running"
    elif st != "Running":
        return "TRANSIENT", st
    # Running but not connected to control, or stuck Starting.
    net = net or (lambda: (tcp_ok("controlplane.tailscale.com"), tcp_ok("1.1.1.1")))
    ctl, inet = net()
    if not inet and not ctl:
        return "NETWORK_DOWN", "no internet from this box"
    return "CONTROL_UNREACHABLE", ("control plane unreachable (%s)" % st
                                   + ("; " + "; ".join(f["health"]) if f["health"] else ""))


def pick(f, name):
    """The record for an expected name: DNS label first, then an ONLINE record
    with that HostName (a stale duplicate keeps the HostName, ab2dfc1)."""
    nodes = f["peers"] + [f["self"]]
    for n in nodes:
        if n["dns"] == name:
            return n
    same = [n for n in nodes if n["host"] == name]
    return next((n for n in same if n["online"]), same[0] if same else None)


def hygiene(f, me):
    """[(key, text)] warnings. Never repaired."""
    out = []
    nodes = f["peers"] + [f["self"]]
    if f["self"]["dns"] and f["self"]["dns"] != me:
        out.append(("name:" + me, "%s is registered as %s, not %s: a stale record holds the "
                    "name. Remove it in the admin console." % (me, f["self"]["dns"], me)))
    for n in nodes:
        if n["host"] in EXPECT and n["dns"] != n["host"] and not n["online"]:
            out.append(("dup:" + n["dns"], "stale record %s (%s), offline since %s. Remove it at "
                        "login.tailscale.com/admin/machines." % (n["dns"], n["ip"], n["lastseen"] or "?")))
    for name in EXPECT:
        n = pick(f, name)
        if n and n["keyexp"] and TAG not in n["tags"]:
            out.append(("exp:" + name, "key expiry is still on for %s (expires %s). Tagging it "
                        "tag:server turns it off." % (name, n["keyexp"][:10])))
    extra = sorted(n["dns"] for n in nodes if TAG in n["tags"] and n["dns"] not in EXPECT)
    if extra:
        out.append(("extra-tag", "UNEXPECTED tag:server node(s): %s. If you did not add them, "
                    "revoke the OAuth client now." % ", ".join(extra)))
    return out


def auth_value(secret):
    """The --auth-key value for a stored secret, or None if it is not usable.
    An OAuth client secret gets ephemeral=false, preauthorized=true FORCED: the
    CLI's default is an ephemeral node, deleted when it goes offline."""
    s = (secret or "").strip()
    if s.startswith("tskey-client-"):
        return s.split("?", 1)[0] + OAUTH_SUFFIX
    if s.startswith("tskey-auth-") and "?" not in s:
        return s
    return None


# ── state ────────────────────────────────────────────────────────────────────
def load_state(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def save_state(path, st):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1, sort_keys=True))
    os.replace(str(tmp), str(path))


def held(path, now=None):
    """Expiry epoch if a hold is in force, else None."""
    now = now or now_s()
    try:
        first = Path(path).read_text().strip().splitlines()
        mtime = Path(path).stat().st_mtime
    except OSError:
        return None
    until = mtime + D
    if first and first[0].strip():
        try:
            until = datetime.fromisoformat(first[0].strip()).timestamp()
        except ValueError:
            pass
    return until if until > now else None


def allowed(state, kind, now=None):
    """(ok, why). Rate limits from LIMITS; a pause after repeated failures."""
    now = now or now_s()
    a = state.setdefault("attempts", {}).setdefault(kind, {"t": [], "fails": 0, "paused": 0})
    a["t"] = [t for t in a["t"] if now - t < D]
    per_h, per_d, _, _ = LIMITS[kind.split(":")[0]]
    if a["paused"] > now:
        return False, "paused until %s after repeated failures" % hhmm(a["paused"])
    if sum(1 for t in a["t"] if now - t < H) >= per_h or len(a["t"]) >= per_d:
        return False, "rate limit reached"
    return True, ""


def attempted(state, kind, ok, now=None):
    now = now or now_s()
    a = state.setdefault("attempts", {}).setdefault(kind, {"t": [], "fails": 0, "paused": 0})
    a["t"].append(now)
    if ok:
        a["fails"] = 0
        return
    a["fails"] += 1
    _, _, after, pause = LIMITS[kind.split(":")[0]]
    if a["fails"] >= after:
        a["paused"], a["fails"] = now + pause, 0


# ── alerts ───────────────────────────────────────────────────────────────────
def episodes(state, active, now=None, me="this box"):
    """active: {key: (text, threshold_s, remind_s)}. Returns messages to send:
    first notice once past threshold, reminders, and one RECOVERED per episode
    that was announced."""
    now = now or now_s()
    eps = state.setdefault("eps", {})
    out = []
    for key, (text, thr, remind) in sorted(active.items()):
        e = eps.setdefault(key, {"since": now, "sent": 0})
        if now - e["since"] < thr:
            continue
        if not e["sent"]:
            out.append((key, text))
        elif now - e["sent"] >= remind:
            out.append((key, text + " (still, since %s)" % hhmm(e["since"])))
    for key in [k for k in eps if k not in active]:
        e = eps.pop(key)
        if e["sent"]:
            what = me if key.startswith("self") else key.split(":", 1)[-1]
            out.append((None, "Tailscale RECOVERED: %s (%s) after %s."
                        % (what, key.split(":")[0], dur(now - e["since"]))))
    return out


def deliver(state, msgs, send, now=None):
    """Send under DAILY_CAP; mark episodes sent only when the send worked."""
    now = now or now_s()
    log = [t for t in state.get("sent_log", []) if now - t < D]
    for key, text in msgs:
        if len(log) >= DAILY_CAP:
            state["suppressed"] = state.get("suppressed", 0) + 1
            continue
        if state.get("suppressed"):
            text += " (+%d suppressed)" % state["suppressed"]
        if send(text):
            log.append(now)
            state["suppressed"] = 0
            if key and key in state.get("eps", {}):
                state["eps"][key]["sent"] = now
    state["sent_log"] = log


def telegram(text, creds=None):
    """Plain text on purpose: no Markdown 400s (S576)."""
    try:
        creds = creds if creds is not None else json.loads(CREDS.read_text())
    except (OSError, ValueError):
        return False
    tok, uid = creds.get("telegram_bot_token"), str(creds.get("telegram_user_id") or "").strip()
    if not tok or not uid:
        return False
    data = urllib.parse.urlencode({"chat_id": uid, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % tok,
                               data=data, timeout=15).read()
        return True
    except Exception:  # noqa: BLE001 -- the exception text can carry the URL
        return False


# ── repairs ──────────────────────────────────────────────────────────────────
def wait_running(cfg, secs=60):
    end = now_s() + secs
    while now_s() < end:
        p = probe(cfg)
        if p["facts"] and p["facts"]["state"] == "Running":
            return p
        time.sleep(5)
    return probe(cfg)


def repair_daemon(cfg):
    if cfg["svc"] == "launchd":
        svc = service(cfg)
        cmd = (["sudo", "-n", "launchctl", "kickstart", "-k", LAUNCHD] if svc["loaded"]
               else ["sudo", "-n", "launchctl", "bootstrap", "system", PLIST])
        rc, _, err = run(cmd, 30)
    else:
        run(["sudo", "-n", "systemctl", "reset-failed", "tailscaled"], 15)
        rc, _, err = run(["sudo", "-n", "systemctl", "start", "tailscaled"], 60)
    return rc, err


def repair_up(cfg, tags=(), _run=None):
    """Undo `tailscale down`. A BARE `tailscale up` is refused ("requires
    mentioning all non-default flags"): measured on cumulus2 2026-11-07, when a
    scheduled bare `up` left it off the tailnet. So restate the pinned flags,
    and the tag once the box is tagged. No --reset: nothing else is touched."""
    cmd = ["sudo", "-n", cfg["ts"], "up"] + cfg["flags"]
    if TAG in (tags or ()):
        cmd.append("--advertise-tags=" + TAG)
    rc, _, err = (_run or run)(cmd + ["--timeout=60s"], 90)
    return rc, err


def relogin(cfg, value):
    """value comes from auth_value(); refused here again so no caller can ever
    hand `tailscale up` an empty key (that starts an INTERACTIVE login)."""
    if not value or not value.startswith("tskey-"):
        return 2, "refused: no usable secret"
    cmd = (["sudo", "-n", cfg["ts"], "up", "--reset", "--auth-key=file:/dev/stdin",
            "--advertise-tags=" + TAG] + cfg["flags"] + ["--timeout=90s"])
    rc, _, err = run(cmd, 120, stdin=value)
    return rc, err


def secret():
    try:
        return json.loads(CREDS.read_text()).get("tailscale_oauth_secret")
    except (OSError, ValueError):
        return None


# ── cumulus2's voice (runs on cumulus1) ──────────────────────────────────────
def c2(verb, stdin=None, timeout=40):
    rc, out, err = run(["ssh", "-i", str(C2_KEY), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                        "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=yes",
                        C2_ADDR, verb], timeout, stdin=stdin)
    try:
        return json.loads(out) if rc == 0 else {"error": err or "rc %d" % rc}
    except ValueError:
        return {"error": "unparseable reply"}


# ── one run ──────────────────────────────────────────────────────────────────
MSG = {"DAEMON_DOWN": "the Tailscale daemon is not running",
       "STOPPED": "someone ran `tailscale down`",
       "NEEDS_LOGIN": "logged out of Tailscale",
       "NEEDS_MACHINE_AUTH": "waiting for device approval in the admin console",
       "CONTROL_UNREACHABLE": "cannot reach Tailscale's control plane",
       "NETWORK_DOWN": "has no internet"}


def heal(me, cfg, mode, state, p, now):
    """Confirm, repair within limits. Returns (state_name, detail, healed_text or None)."""
    sname, detail = classify(p, state.get("starting_since"), now)
    if p["facts"] and p["facts"]["state"] in ("Starting", "NoState"):
        state.setdefault("starting_since", now)
    else:
        state.pop("starting_since", None)
    if sname in ("OK", "TRANSIENT"):
        return sname, detail, None
    time.sleep(CONFIRM_S)
    p = probe(cfg)
    sname, detail = classify(p, state.get("starting_since"), now_s())
    if sname in ("OK", "TRANSIENT"):
        return sname, detail, None
    kind = {"DAEMON_DOWN": "restart", "STOPPED": "up", "NEEDS_LOGIN": "relogin"}.get(sname)
    if kind is None:
        return sname, detail, None
    need = "relogin" if kind == "relogin" else "repair"
    if mode == "shadow" or (need == "relogin" and mode != "relogin"):
        state["would"] = {"restart": "restart the daemon", "up": "run tailscale up",
                          "relogin": "log back in as tag:server"}[kind]
        return sname, detail, None
    if kind == "relogin" and not cfg["speaks"]:
        state["defer"] = 900                   # cumulus1 logs this box in (relay)
        return sname, detail + "; waiting for cumulus1 to log it back in", None
    ok_, why = allowed(state, kind)
    if not ok_:
        return sname, detail + "; repair " + why, None
    t0 = now_s()
    if kind == "restart":
        rc, err = repair_daemon(cfg)
    elif kind == "up":
        rc, err = repair_up(cfg, p["facts"]["self"]["tags"] if p["facts"] else ())
    else:
        val = auth_value(secret())
        if not val:
            attempted(state, kind, False)
            return sname, detail + "; no usable tailscale_oauth_secret on this box", None
        rc, err = relogin(cfg, val)
    p2 = wait_running(cfg, 60 if kind != "relogin" else 30)
    s2, _ = classify(p2, None, now_s())
    good = s2 == "OK"
    attempted(state, kind, good)
    if not good:
        return sname, "%s; %s failed (rc %s%s)" % (detail, kind, rc, (": " + err) if err else ""), None
    f = p2["facts"]["self"]
    text = "HEALED on %s: %s; %s. Back as %s %s after %s." % (
        me, MSG[sname], {"restart": "restarted it", "up": "brought it back up",
                         "relogin": "logged back in as tag:server"}[kind],
        f["dns"], f["ip"], dur(now_s() - t0))
    if kind == "relogin" and state.get("last_ip") and f["ip"] != state["last_ip"]:
        text += " NEW IP (was %s): update anything hard-coded." % state["last_ip"]
    if kind == "relogin" and f["dns"] != me:
        text += " It came back as %s: a stale record holds the name." % f["dns"]
    return "OK", "healed", text


def observe(me, cfg, f, state, c2_view, now):
    """Peer conditions this box is the designated observer for."""
    active = {}
    for name in cfg["observe"]:
        n = pick(f, name)
        if name == "cumulus2" and c2_view and not c2_view.get("error"):
            if c2_view.get("hold") or c2_view.get("state") not in ("OK", "TRANSIENT", None):
                continue                      # it speaks for itself, via the relay
        if n is None:
            why = "ABSENT from the tailnet (removed or renamed?)"
        elif not n["online"]:
            why = "offline (not connected to control)"
        else:
            rc, _, _ = run([cfg["ts"], "ping", "--c", "3", "--timeout", "5s", n["ip"]], 30)
            if rc == 0:
                continue
            why = "online per control but not answering tailscale ping"
        active["peer:" + name] = ("DOWN: %s, seen from %s: %s. Its own check should report "
                                  "too; if it has not, the box or its check is down." % (name, me, why),
                                  1800, 12 * H)
    return active


def relay(state, c2_view, mode, send_list, now):
    """cumulus1: forward cumulus2's own messages; log it back in when asked."""
    active = {}
    if c2_view.get("error"):
        active["relay:cumulus2"] = ("cannot read cumulus2's heal state over the private link: %s"
                                    % scrub(c2_view["error"])[:100], 900, 12 * H)
        return active
    if not c2_view.get("t"):
        active["relay:cumulus2"] = ("cumulus2's heal check has never run", 900, 12 * H)
    elif now - c2_view["t"] > 20 * 60:
        active["relay:cumulus2"] = ("cumulus2's heal check has not run for %s"
                                    % dur(now - c2_view["t"]), 0, 12 * H)
    last = state.get("c2_last_event", 0)
    for ev in c2_view.get("events", []):
        if ev["id"] > last:
            send_list.append((None, ev["text"]))
            last = ev["id"]
    state["c2_last_event"] = last
    if c2_view.get("state") == "NEEDS_LOGIN" and not c2_view.get("hold") and mode == "relogin":
        ok_, why = allowed(state, "relogin:cumulus2", now)
        val = auth_value(secret())
        if ok_ and val:
            r = c2("relogin", stdin=val, timeout=150)
            attempted(state, "relogin:cumulus2", bool(r.get("ok")), now)
    return active


def main(argv, record=None):
    if "--selftest" in argv:
        return selftest()
    if "--remote" in argv:
        return remote(os.environ.get("SSH_ORIGINAL_COMMAND", "").strip())
    me = argv[argv.index("--host") + 1] if "--host" in argv else socket.gethostname().split(".")[0].lower()
    if me not in HOSTS:
        print("tailscale_heal: unknown host %r; pass --host" % me, file=sys.stderr)
        return 2
    cfg = HOSTS[me]
    LOGS.mkdir(parents=True, exist_ok=True)
    if "--check" in argv:
        p = probe(cfg)
        sname, detail = classify(p)
        f = p["facts"]
        print("%s: %s (%s)" % (me, sname, detail))
        if f:
            print("self: %s %s tags=%s" % (f["self"]["dns"], f["self"]["ip"], f["self"]["tags"]))
            for k, t in hygiene(f, me):
                print("hygiene:", t)
        print("mode:", _mode(), "| hold:", "yes" if held(LOGS / "tailscale-heal.hold") else "no")
        return 0
    lock = open(str(LOGS / "tailscale-heal.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0                               # a run is already in progress
    spath = LOGS / "tailscale-heal-state.json"
    state = load_state(spath)
    now = now_s()
    mode = _mode()
    if "--now" in argv:
        state.pop("attempts", None)
    hold = held(LOGS / "tailscale-heal.hold", now)
    state.pop("would", None)
    state.pop("defer", None)
    p = probe(cfg)
    if hold:
        sname, detail = classify(p, state.get("starting_since"), now)
        healed = None
    else:
        sname, detail, healed = heal(me, cfg, mode, state, p, now)
        if healed:
            p = probe(cfg)
    now = now_s()
    f = p["facts"]
    if sname == "OK" and f:
        state["last_ip"], state["last_keyexp"] = f["self"]["ip"], f["self"]["keyexp"]
    tag = "[shadow] " if mode == "shadow" else ""
    active, msgs = {}, []
    if healed:
        msgs.append((None, "Tailscale " + healed))
    if sname not in ("OK", "TRANSIENT"):
        would = state.get("would")
        text = "Tailscale %s on %s: %s (%s)." % ("NEEDS YOU" if not would else "FAULT", me,
                                                 MSG.get(sname, sname), detail)
        if would:
            text += " Would %s; mode is %s." % (would, mode)
        thr = 1800 if sname in ("CONTROL_UNREACHABLE", "NETWORK_DOWN") else state.get("defer", 0)
        active["self:" + sname] = (text, thr, (6 if thr >= 1800 else 12) * H)
    c2_view = None
    if cfg.get("relay"):
        c2_view = c2("status")
        active.update(relay(state, c2_view, mode, msgs, now))
    if sname == "OK" and f:
        active.update(observe(me, cfg, f, state, c2_view, now))
        if cfg["hygiene"]:
            for key, text in hygiene(f, me):
                active["hyg:" + key] = ("Tailscale reminder: " + text,
                                        0, (24 if key == "extra-tag" else 7 * 24) * H)
    if not hold:                              # a hold freezes episodes: no RECOVERED either
        msgs += [(k, t if t.startswith("Tailscale") else "Tailscale " + t)
                 for k, t in episodes(state, active, now, me)]
    msgs = [(k, t if t.startswith("[shadow]") else tag + t) for k, t in msgs]
    if cfg["speaks"]:
        deliver(state, msgs, telegram, now)
    else:
        evs = state.setdefault("events", [])
        nid = state.get("next_event", 1)
        for k, t in msgs:
            evs.append({"id": nid, "text": t})
            if k and k in state.get("eps", {}):
                state["eps"][k]["sent"] = now
            nid += 1
        state["events"], state["next_event"] = evs[-50:], nid
    state.update({"t": now, "state": sname, "detail": detail, "mode": mode,
                  "hold": bool(hold)})
    save_state(spath, state)
    note = "%s %s; mode=%s%s%s" % (
        sname, (f["self"]["dns"] + " " + f["self"]["ip"]) if f else detail, mode,
        "; HELD" if hold else "",
        ("; c2 " + (c2_view.get("state") or c2_view.get("error", "?"))) if c2_view else "")
    ok = sname in ("OK", "TRANSIENT") and not any(k.startswith("relay:") for k in active)
    _log("%s %s" % ("ok" if ok else "FAIL", scrub(note)))
    if record and "--no-record" not in argv:
        record(ok, scrub(note)[:200])
    print(scrub(note))
    return 0


def _mode():
    try:
        m = (LOGS / "tailscale-heal.mode").read_text().strip()
    except OSError:
        return "shadow"
    return m if m in ("shadow", "repair", "relogin") else "shadow"


def _log(line):
    path = LOGS / "tailscale-heal.log"
    try:
        if path.exists() and path.stat().st_size > 1_000_000:
            path.write_text("\n".join(path.read_text().splitlines()[-2000:]) + "\n")
        with open(str(path), "a") as o:
            o.write("%s %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), line))
    except OSError:
        pass


def remote(verb):
    """cumulus2's forced command. Two verbs only; anything else is refused."""
    me, cfg = "cumulus2", HOSTS["cumulus2"]
    spath = LOGS / "tailscale-heal-state.json"
    if verb == "status":
        st = load_state(spath)
        out = {k: st.get(k) for k in ("t", "state", "detail", "mode", "events")}
        out["events"] = out["events"] or []
        out["hold"] = bool(held(LOGS / "tailscale-heal.hold"))
        print(json.dumps(out))
        return 0
    if verb == "relogin":
        val = auth_value(sys.stdin.read())
        lock = open(str(LOGS / "tailscale-heal.lock"), "w")
        fcntl.flock(lock, fcntl.LOCK_EX)
        st = load_state(spath)
        sname, _ = classify(probe(cfg))
        ok_, why = allowed(st, "relogin")
        if sname != "NEEDS_LOGIN" or held(LOGS / "tailscale-heal.hold") or not ok_ or not val:
            print(json.dumps({"ok": False, "why": why or "not NEEDS_LOGIN, held, or no key"}))
            return 0
        rc, err = relogin(cfg, val)
        s2, _ = classify(wait_running(cfg, 30))
        attempted(st, "relogin", s2 == "OK")
        if s2 == "OK":
            evs = st.setdefault("events", [])
            nid = st.get("next_event", 1)
            evs.append({"id": nid, "text": "Tailscale (cumulus2) HEALED: was logged out; cumulus1 "
                        "logged it back in as tag:server."})
            st["next_event"] = nid + 1
        save_state(spath, st)
        print(json.dumps({"ok": s2 == "OK", "rc": rc, "err": err}))
        return 0
    print(json.dumps({"error": "refused"}))
    return 2


def _record(name, ok, note):
    try:
        import job_status
        job_status.record(name, ok=ok, note=note)
    except Exception:  # noqa: BLE001 -- best-effort, as in model_health
        pass


# ── selftest: fixtures only; never a live file, never the network ────────────
def selftest():
    import tempfile
    fails = []

    def ck(label_, cond):
        print(("  ok   " if cond else "  FAIL ") + label_)
        if not cond:
            fails.append(label_)

    def st_json(state="Running", online=True, peers=(), dns="cirrus", tags=None, keyexp=None):
        s = {"HostName": "cirrus", "DNSName": dns + ".tail6abce6.ts.net.", "Online": online,
             "TailscaleIPs": ["100.76.30.41"], "Tags": tags,
             "PublicKey": "nodekey:SECRETSHAPE"}
        if keyexp:
            s["KeyExpiry"] = keyexp
        return {"BackendState": state, "AuthURL": "https://login.tailscale.com/a/abc123XYZ",
                "Self": s, "Health": ["visit https://login.tailscale.com/a/zzzSECRET now"],
                "Peer": {str(i): pp for i, pp in enumerate(peers)}}

    def peer(host, dns, online, ip, keyexp=None, tags=None, ls=""):
        d = {"HostName": host, "DNSName": dns + ".tail6abce6.ts.net.", "Online": online,
             "TailscaleIPs": [ip], "Tags": tags, "LastSeen": ls}
        if keyexp:
            d["KeyExpiry"] = keyexp
        return d

    up = {"up": True, "loaded": True, "age": 9999, "detail": "running"}
    # Today's real shape (2026-11-07): live cirrus + stale cirrus-1.
    today = st_json(peers=[peer("cumulus1", "cumulus1", True, "100.88.81.60"),
                           peer("cumulus2", "cumulus2", True, "100.87.241.34"),
                           peer("cirrus", "cirrus-1", False, "100.76.83.127",
                                "2027-03-27T15:00:12Z", ls="2026-10-01T12:15:00Z")],
                    keyexp="2027-05-06T13:44:44Z")
    f = facts(today)
    blob = json.dumps(f)
    ck("facts never copies AuthURL, login codes or node keys",
       "abc123XYZ" not in blob and "zzzSECRET" not in blob and "nodekey" not in blob)
    ck("classify: Running+online is OK", classify({"svc": up, "facts": f})[0] == "OK")
    ck("pick: the live record wins over the stale duplicate", pick(f, "cirrus")["dns"] == "cirrus")
    ck("pick: by HostName, an online record beats an offline one",
       pick(facts(st_json(dns="x", online=False, peers=[
           peer("cirrus", "cirrus-1", False, "1"), peer("cirrus", "cirrus-2", True, "2")])),
           "cirrus")["ip"] == "2")
    hy = dict(hygiene(f, "cumulus1"))
    ck("hygiene reports today's stale cirrus-1", "dup:cirrus-1" in hy)
    ck("hygiene reports key expiry still on for untagged cirrus", "exp:cirrus" in hy)
    ck("...and not for nodes with no KeyExpiry", "exp:cumulus1" not in hy)
    tagged = facts(st_json(tags=[TAG], keyexp="2027-05-06T00:00:00Z",
                           peers=[peer("rogue", "rogue", True, "100.1.1.1", tags=[TAG])]))
    hy2 = dict(hygiene(tagged, "cirrus"))
    ck("tagged node with a KeyExpiry is not nagged", "exp:cirrus" not in hy2)
    ck("an unexpected tag:server node is flagged", "extra-tag" in hy2)
    ck("self registered as cirrus-1 is flagged",
       "name:cirrus" in dict(hygiene(facts(st_json(dns="cirrus-1")), "cirrus")))

    down = {"up": False, "loaded": False, "age": None, "detail": "launchd job not loaded"}
    ck("no status + daemon gone = DAEMON_DOWN",
       classify({"svc": down, "facts": None, "err": "x"})[0] == "DAEMON_DOWN")
    ck("no status + young daemon = TRANSIENT",
       classify({"svc": dict(up, age=20), "facts": None})[0] == "TRANSIENT")
    ck("no status during dpkg/update = TRANSIENT",
       classify({"svc": down, "facts": None, "busy": True})[0] == "TRANSIENT")
    for bs, want in (("Stopped", "STOPPED"), ("NeedsLogin", "NEEDS_LOGIN"),
                     ("NeedsMachineAuth", "NEEDS_MACHINE_AUTH")):
        ck("BackendState %s -> %s" % (bs, want),
           classify({"svc": up, "facts": facts(st_json(bs))})[0] == want)
    off = facts(st_json(online=False))
    ck("Running + offline + internet ok = CONTROL_UNREACHABLE",
       classify({"svc": up, "facts": off}, net=lambda: (False, True))[0] == "CONTROL_UNREACHABLE")
    ck("Running + offline + no internet = NETWORK_DOWN",
       classify({"svc": up, "facts": off}, net=lambda: (False, False))[0] == "NETWORK_DOWN")
    stt = facts(st_json("Starting"))
    ck("Starting briefly = TRANSIENT",
       classify({"svc": up, "facts": stt}, starting_since=1000, now=1100)[0] == "TRANSIENT")
    ck("Starting for 11 min = CONTROL_UNREACHABLE",
       classify({"svc": up, "facts": stt}, starting_since=1000, now=1000 + 660,
                net=lambda: (False, True))[0] == "CONTROL_UNREACHABLE")
    ck("health strings are scrubbed", "zzzSECRET" not in " ".join(off["health"]))

    ck("auth_value: OAuth secret gets ephemeral=false&preauthorized=true",
       auth_value(" tskey-client-kAbc-XYZ\n") == "tskey-client-kAbc-XYZ" + OAUTH_SUFFIX)
    ck("auth_value: a stored ephemeral=true suffix is REPLACED",
       auth_value("tskey-client-k-X?ephemeral=true") == "tskey-client-k-X" + OAUTH_SUFFIX)
    ck("auth_value: empty / garbage / None refused",
       auth_value("") is None and auth_value("hello") is None and auth_value(None) is None)
    ck("relogin refuses an unusable value before calling tailscale",
       relogin(HOSTS["cirrus"], "")[0] == 2 and relogin(HOSTS["cirrus"], None)[0] == 2)
    ck("scrub removes login URLs and keys",
       scrub("x https://login.tailscale.com/a/ab12 tskey-client-k-Q?ephemeral=false y")
       == "x [login-url] [tskey] y")

    s = {}
    for i in range(3):
        ck("restart allowed #%d" % (i + 1), allowed(s, "restart", 1000 + i)[0])
        attempted(s, "restart", False, 1000 + i)
    ck("restart paused after 3 straight failures", not allowed(s, "restart", 1010)[0])
    ck("...and allowed again 6 h later", allowed(s, "restart", 1010 + 6 * H + 1)[0])
    s = {}
    attempted(s, "relogin", True, 1000)
    ck("relogin: at most one per hour", not allowed(s, "relogin", 1000 + 600)[0])
    ck("per-target kinds share LIMITS by prefix", allowed({}, "relogin:cumulus2", 5)[0])

    s = {}
    act = {"peer:x": ("DOWN x", 1800, 12 * H)}
    ck("no alert before threshold", episodes(s, act, 1000) == [])
    m = episodes(s, act, 1000 + 1800)
    ck("first alert at threshold", m and m[0][1] == "DOWN x")
    deliver(s, m, lambda t: True, 1000 + 1800)
    ck("no repeat before the reminder", episodes(s, act, 1000 + 3600) == [])
    ck("reminder after 12 h", "still" in episodes(s, act, 1000 + 1800 + 12 * H)[0][1])
    ck("RECOVERED once the condition clears", "RECOVERED" in episodes(s, {}, 1000 + 13 * H)[0][1])
    s2 = {}
    episodes(s2, {"peer:y": ("y", 1800, H)}, 0)
    ck("an episode never announced ends silently", episodes(s2, {}, 100) == [])
    s3, sent = {}, []
    deliver(s3, [(None, "m%d" % i) for i in range(8)], lambda t: sent.append(t) or True, 50)
    ck("daily cap holds at %d" % DAILY_CAP, len(sent) == DAILY_CAP and s3["suppressed"] == 2)
    s4 = {"eps": {"k": {"since": 0, "sent": 0}}}
    deliver(s4, [("k", "x")], lambda t: False, 10)
    ck("a failed send leaves the episode unsent (retried next run)", s4["eps"]["k"]["sent"] == 0)

    with tempfile.TemporaryDirectory() as td:
        hp = Path(td) / "hold"
        ck("no hold file = not held", held(hp, 1000) is None)
        hp.write_text("")
        ck("empty hold = 24 h from mtime", held(hp) is not None)
        hp.write_text("2000-01-01T00:00:00\n")
        ck("expired hold = not held", held(hp) is None)
        sp = Path(td) / "s.json"
        save_state(sp, {"a": 1})
        ck("state round-trips", load_state(sp) == {"a": 1})
    ck("etime parses dd-hh:mm:ss and mm:ss",
       _etime("2-03:04:05") == 2 * D + 3 * H + 245 and _etime("01:30") == 90)
    ck("every host pins --hostname for the re-login",
       all(any(x.startswith("--hostname=" + h) for x in c["flags"]) for h, c in HOSTS.items()))
    ck("no host advertises --ssh or turns accept-dns off (S579 decisions)",
       not any(x.startswith(("--ssh", "--accept-dns")) for c in HOSTS.values() for x in c["flags"]))
    ck("unknown remote verb refused", _quiet(lambda: remote("rm -rf /")) == 2)
    seen = []
    repair_up(HOSTS["cumulus2"], [], _run=lambda c, t: seen.append(c) or (0, "", ""))
    repair_up(HOSTS["cirrus"], [TAG], _run=lambda c, t: seen.append(c) or (0, "", ""))
    ck("undo-down restates the pinned flags (a bare `up` is refused, 11-07)",
       "--hostname=cumulus2" in seen[0] and "--reset" not in seen[0]
       and not any(x.startswith("--advertise-tags") for x in seen[0]))
    ck("...and the tag once the box is tagged",
       "--operator=buddy" in seen[1] and "--advertise-tags=" + TAG in seen[1])
    print("selftest: %d failed" % len(fails))
    return 1 if fails else 0


def _quiet(fn):
    import io
    import contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        return fn()


if __name__ == "__main__":
    sys.exit(main(sys.argv, record=lambda ok, note: _record("tailscaleheal", ok, note)))
