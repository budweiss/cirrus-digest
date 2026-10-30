#!/usr/bin/env python3
"""cloudflared_update.py -- keep THIS box's cloudflared connector current (S515).

cloudflared's own updater covers neither connector: CIRRUS runs a brew install
(com.cirrus.tunnel) and cumulus1 runs a bare GitHub .deb under --no-autoupdate
(its packaged cloudflared-update.timer stays disabled). T160: both sat months
behind until an advisory found them. So once a night, per box, an hour apart
(each tunnel is the other box's fallback route -- never restart both in one run):

  1. read the RUNNING version from build_info on the metrics port, not the
     binary on disk (brew swaps the binary under a live process);
  2. if behind, refuse unless the OTHER box's tunnel answers -- it is our way back;
  3. upgrade from a verified download: brew checks its bottle's sha256; the .deb
     is checked against the sha256 GitHub publishes for the release asset;
  4. restart the connector, then require the new version AND a new connectorId
     with ready connections on the metrics port, AND our own API answering
     through the tunnel (its 403 body, which Cloudflare's edge never sends);
  5. on any failure put the previous binary back, restart, and record the
     version as failed so the next night does not retry it. model_health's
     check_cloudflared keeps reporting the drift and shows this job's last result.

Usage: cloudflared_update.py [--check | --force | --selftest]
  --check     say what a run would do; changes nothing
  --force     reinstall + restart + verify even when current (proves the path)
  --selftest  exercise the decision and rollback logic against fakes in a tempdir
"""
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOGS = HERE / "logs"
RELEASES = "https://api.github.com/repos/cloudflare/cloudflared/releases"
UA = "cowork-cloudflared-update"
# Our cirrus_api's reply to a token-less /status. Reaching it proves the request
# crossed the tunnel to the origin; a Cloudflare 302/403/530 never carries it.
MARKER = "Invalid or missing API token"
BOXES = {
    "darwin": {"name": "cirrus", "binary": "/opt/homebrew/bin/cloudflared",
               "self": "https://cirrus.cirrustask.com/status",
               "peer": "https://cumulus.cirrustask.com/status",
               "restart": ["sudo", "-n", "/bin/launchctl", "kickstart", "-k",
                           "system/com.cirrus.tunnel"]},
    "linux": {"name": "cumulus1", "binary": "/usr/bin/cloudflared",
              "self": "https://cumulus.cirrustask.com/status",
              "peer": "https://cirrus.cirrustask.com/status",
              "restart": ["sudo", "-n", "systemctl", "restart", "cloudflared"]},
}
BREW = "/opt/homebrew/bin/brew"
BREW_ENV = dict(os.environ, HOME=str(Path.home()), HOMEBREW_NO_AUTO_UPDATE="1",
                HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK="1", HOMEBREW_NO_ENV_HINTS="1",
                HOMEBREW_NO_ANALYTICS="1",
                PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")


def vt(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "0"))


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:   # /ready is 503 and /status is 403 by design
        return e.read()


def _run(cmd, timeout=600, env=None):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])} rc={r.returncode}: "
                           f"{(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout


class Live:
    """Everything that touches the box, the network or the tunnel."""

    def __init__(self, box, work):
        self.box, self.work = box, work
        self.darwin = sys.platform == "darwin"

    def connector(self):
        """(version, ready_connections, connector_id) of the RUNNING process."""
        for port in range(20241, 20246):
            try:
                m = re.search(r'^build_info\{[^}]*version="([0-9][0-9.]*)"',
                              _get(f"http://127.0.0.1:{port}/metrics", 3).decode(
                                  errors="replace"), re.M)
                if not m:
                    continue
                ready = json.loads(_get(f"http://127.0.0.1:{port}/ready", 3) or b"{}")
                return (m.group(1), int(ready.get("readyConnections") or 0),
                        ready.get("connectorId") or "")
            except Exception:
                continue
        return ("", 0, "")

    def binary_version(self):
        try:
            out = subprocess.run([self.box["binary"], "--version"], capture_output=True,
                                 text=True, timeout=20)
            m = re.search(r"version ([0-9][0-9.]*)", out.stdout + out.stderr)
            return m.group(1) if m else ""
        except Exception:
            return ""

    def target(self):
        """(newest version this box's channel offers, source note). On CIRRUS
        that is brew's stable, which can trail the GitHub release by hours."""
        if self.darwin:
            _run([BREW, "update", "--quiet"], env=BREW_ENV)
            info = json.loads(_run([BREW, "info", "--json=v2", "cloudflared"], env=BREW_ENV))
            return info["formulae"][0]["versions"]["stable"], "brew stable"
        rel = json.loads(_get(f"{RELEASES}/latest", 30))
        arch = _run(["dpkg", "--print-architecture"]).strip()
        name = f"cloudflared-linux-{arch}.deb"
        asset = next((a for a in rel.get("assets", []) if a.get("name") == name), None)
        if not asset or not (asset.get("digest") or "").startswith("sha256:"):
            raise RuntimeError(f"release {rel.get('tag_name')} has no sha256 for {name}")
        self.asset = asset
        return rel["tag_name"].lstrip("v"), "GitHub release"

    def answers(self, url):
        try:
            return MARKER in _get(url, 20).decode(errors="replace")
        except Exception:
            return False

    def install(self, version, reinstall):
        if self.darwin:
            _run([BREW, "reinstall" if reinstall else "upgrade", "cloudflared"],
                 timeout=900, env=BREW_ENV)
            return "brew (bottle sha256 checked by brew)"
        deb = self.work / self.asset["name"].replace(".deb", f"-{version}.deb")
        h = hashlib.sha256()
        req = urllib.request.Request(self.asset["browser_download_url"],
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=120) as r, open(deb, "wb") as f:
            for chunk in iter(lambda: r.read(1 << 20), b""):
                h.update(chunk)
                f.write(chunk)
        want = self.asset["digest"].split(":", 1)[1]
        if h.hexdigest() != want:
            deb.unlink()
            raise RuntimeError(f"sha256 mismatch for {deb.name}: got {h.hexdigest()}")
        fields = _run(["dpkg-deb", "-f", str(deb), "Package", "Version"])
        if "Package: cloudflared" not in fields or f"Version: {version}" not in fields:
            raise RuntimeError(f"{deb.name} is not cloudflared {version}: {fields!r}")
        _run(["sudo", "-n", "dpkg", "-i", str(deb)], timeout=300)
        return f"{deb.name} sha256 {want[:12]}… matches GitHub"

    def restart(self):
        _run(self.box["restart"], timeout=120)

    def place(self, src, dest):
        """Copy src over the binary the connector executes."""
        if self.darwin:
            _run(["install", "-m", "0755", str(src), dest])
        else:
            _run(["sudo", "-n", "install", "-m", "0755", str(src), dest])

    def sleep(self, s):
        time.sleep(s)


def verify(ops, version, old_id, wait=90):
    """True once the NEW process runs `version` with ready connections and our
    own API answers through the tunnel."""
    deadline = time.monotonic() + wait
    while True:
        v, ready, cid = ops.connector()
        if v == version and ready >= 1 and cid and cid != old_id:
            break
        if time.monotonic() > deadline:
            return False, f"connector shows version={v or '?'} ready={ready} new_id={cid != old_id}"
        ops.sleep(3)
    for _ in range(12):
        if ops.answers(ops.box["self"]):
            return True, f"{version}, {ready} connections, {ops.box['self']} answers"
        ops.sleep(5)
    return False, f"connector up but {ops.box['self']} does not answer through the tunnel"


def run(ops, state, check=False, force=False, wait=90):
    """One pass. Returns (exit_code, record). `state` is the previous record."""
    running, _, old_id = ops.connector()
    binary = ops.binary_version()
    rec = {"at": datetime.now().isoformat(timespec="seconds"), "box": ops.box["name"],
           "running": running, "binary": binary, "failed_version": state.get("failed_version", "")}
    if not running:
        rec.update(result="unreadable", detail="no build_info on 127.0.0.1:20241-20245; not touching it")
        return 1, rec
    target, source = ops.target()
    rec["target"] = f"{target} ({source})"
    if vt(running) >= vt(target) and not force:
        rec.update(result="current", failed_version="", detail=f"{source} offers {target}")
        return 0, rec
    if target == rec["failed_version"] and not force:
        rec.update(result="skipped", detail=f"{target} was rolled back before; not retrying it")
        return 0, rec
    if not ops.answers(ops.box["peer"]):
        rec.update(result="deferred", detail=f"peer tunnel {ops.box['peer']} does not answer -- "
                   "it is the way back if this restart fails")
        return 0, rec
    if check:
        rec.update(result="would-upgrade", detail=f"{running} -> {target}; peer route answers")
        return 0, rec

    prev = ops.work / "cloudflared.prev"
    real = os.path.realpath(ops.box["binary"])
    # S542: brew's binary is read-only and copy2 keeps the mode, so the copy
    # kept by the last upgrade refused this overwrite and blocked every upgrade.
    prev.unlink(missing_ok=True)
    shutil.copy2(real, prev)
    rec["from"], rec["to"] = binary, target
    restarted = False
    try:
        rec["download"] = ops.install(target, reinstall=vt(binary) >= vt(target))
        got = ops.binary_version()
        if got != target:
            raise RuntimeError(f"installed binary reports {got or '?'}, wanted {target}")
        restarted = True
        ops.restart()
        ok, detail = verify(ops, target, old_id, wait)
    except Exception as e:  # noqa: BLE001
        ok, detail = False, f"{type(e).__name__}: {e}"
    if not ok and not restarted:
        # Download/install failed before any restart: the old process is still
        # serving. Put the old binary back on disk; no tunnel blip, and no
        # failed-version mark, since a network error deserves another night.
        try:
            ops.place(prev, os.path.realpath(ops.box["binary"]))
        except Exception as e:  # noqa: BLE001
            detail += f"; restoring the binary failed too: {e}"
        rec.update(result="install-failed", detail=detail)
        return 1, rec
    if ok:
        rec.update(result="upgraded" if not force else "reinstalled", detail=detail,
                   failed_version="")
        return 0, rec

    # Roll back: the old binary over whatever the connector now executes.
    rec["failure"] = detail
    try:
        real = os.path.realpath(ops.box["binary"])
        ops.place(prev, real)
        _, _, cur_id = ops.connector()
        ops.restart()
        ok2, detail2 = verify(ops, binary, cur_id, wait)
    except Exception as e:  # noqa: BLE001
        ok2, detail2 = False, f"{type(e).__name__}: {e}"
    rec.update(result="rolled-back" if ok2 else "ROLLBACK-FAILED", detail=detail2,
               failed_version=target)
    return 1, rec


def main(argv):
    if "--selftest" in argv:
        return selftest()
    box = BOXES.get(sys.platform)
    if not box or not os.path.exists(box["binary"]):
        print(f"cloudflared_update: no connector binary on this box "
              f"({box and box['binary']}) -- nothing to do")
        return 0
    work = LOGS / "cloudflared-update"
    work.mkdir(parents=True, exist_ok=True)
    state_path = LOGS / "cloudflared-update.json"
    with open(work / ".lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("cloudflared_update: another run holds the lock")
            return 1
        try:
            state = json.loads(state_path.read_text())
        except Exception:
            state = {}
        check = "--check" in argv
        try:
            code, rec = run(Live(box, work), state, check=check, force="--force" in argv)
        except Exception as e:  # noqa: BLE001 -- a target/network error changed nothing
            code, rec = 1, {"at": datetime.now().isoformat(timespec="seconds"),
                            "box": box["name"], "result": "error",
                            "detail": f"{type(e).__name__}: {e}",
                            "failed_version": state.get("failed_version", "")}
    line = json.dumps(rec)
    print(line)
    if not check:
        state_path.write_text(json.dumps(rec, indent=1) + "\n")
        with open(LOGS / "cloudflared-update.log", "a") as f:
            f.write(line + "\n")
        # S517: feed the daily missed/failed-job monitor, not only our own log.
        import job_status
        job_status.record("cloudflaredupdate", ok=(code == 0 and rec.get("result") in
                          {"current", "upgraded", "reinstalled"}),
                          note=rec.get("result", "unknown") + (
                              ": " + rec.get("detail", "").split(":")[0]
                              if rec.get("result") == "error" else ""))
    return code


class Fake:
    """Stands in for Live: a fake binary file, a fake connector, no network."""

    def __init__(self, tmp, running="1.0.1", target="1.0.2", peer=True, comes_up=True):
        self.work = Path(tmp)
        self.bin = self.work / "cloudflared"
        self.bin.write_text("v" + running)
        self.box = {"name": "fake", "binary": str(self.bin), "self": "self", "peer": "peer"}
        self.proc = (running, 4, "id-0")
        self.tgt, self.peer, self.comes_up, self.restarts = target, peer, comes_up, 0

    def connector(self):
        return self.proc

    def binary_version(self):
        return self.bin.read_text()[1:]

    def target(self):
        return self.tgt, "fake"

    def answers(self, url):
        return self.peer if url == "peer" else self.proc[1] > 0

    def install(self, version, reinstall):
        self.bin.write_text("v" + version)
        return "fake"

    def restart(self):
        self.restarts += 1
        v = self.binary_version()
        up = self.comes_up or v != self.tgt
        self.proc = (v, 4 if up else 0, f"id-{self.restarts}")

    def place(self, src, dest):
        shutil.copy2(src, dest)

    def sleep(self, s):
        pass


def selftest():
    # wait=0 everywhere: a fake that never comes up fails at once, not after 90s.
    cases = []
    with tempfile.TemporaryDirectory() as t:
        f = Fake(t)
        (f.work / "cloudflared.prev").write_text("old")
        os.chmod(f.work / "cloudflared.prev", 0o555)
        code, r = run(f, {}, wait=0)
        cases.append(("read-only kept copy does not block the next upgrade",
                      code == 0 and r["result"] == "upgraded"))
    with tempfile.TemporaryDirectory() as t:
        f = Fake(t, running="1.0.2")
        code, r = run(f, {}, wait=0)
        cases.append(("current -> no restart", code == 0 and r["result"] == "current" and f.restarts == 0))
    with tempfile.TemporaryDirectory() as t:
        f = Fake(t, peer=False)
        code, r = run(f, {}, wait=0)
        cases.append(("peer down -> deferred, no restart",
                      r["result"] == "deferred" and f.restarts == 0 and f.binary_version() == "1.0.1"))
    with tempfile.TemporaryDirectory() as t:
        f = Fake(t)
        code, r = run(f, {}, check=True, wait=0)
        cases.append(("--check changes nothing",
                      r["result"] == "would-upgrade" and f.restarts == 0 and f.binary_version() == "1.0.1"))
    with tempfile.TemporaryDirectory() as t:
        f = Fake(t)
        code, r = run(f, {}, wait=0)
        cases.append(("upgrade verified", code == 0 and r["result"] == "upgraded"
                      and f.proc[0] == "1.0.2" and f.restarts == 1))
    with tempfile.TemporaryDirectory() as t:
        f = Fake(t, comes_up=False)
        code, r = run(f, {}, wait=0)
        cases.append(("no reconnect -> old binary back, version marked failed",
                      code == 1 and r["result"] == "rolled-back" and f.binary_version() == "1.0.1"
                      and f.proc[0] == "1.0.1" and r["failed_version"] == "1.0.2"))
        code2, r2 = run(f, r, wait=0)
        cases.append(("failed version not retried", r2["result"] == "skipped" and f.restarts == 2))
    with tempfile.TemporaryDirectory() as t:
        f = Fake(t, running="1.0.2")
        code, r = run(f, {}, force=True, wait=0)
        cases.append(("--force reinstalls + verifies when current",
                      code == 0 and r["result"] == "reinstalled" and f.restarts == 1))
    with tempfile.TemporaryDirectory() as t:
        f = Fake(t)
        f.install = lambda version, reinstall: (_ for _ in ()).throw(RuntimeError("sha256 mismatch"))
        code, r = run(f, {}, wait=0)
        cases.append(("install failure -> no restart, not marked failed",
                      code == 1 and r["result"] == "install-failed" and f.restarts == 0
                      and not r["failed_version"] and f.binary_version() == "1.0.1"))
    for name, ok in cases:
        print(("PASS " if ok else "FAIL ") + name)
    return 0 if all(ok for _, ok in cases) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
