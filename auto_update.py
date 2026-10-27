#!/usr/bin/env python3
"""auto_update.py -- tiered auto-update agent, STEP 1: the dry run (S516).

Spec: docs/AUTO-UPDATE-AGENT-PLAN.md (Buddy approved 2026-10-27). Nothing on
any box applied OS updates by itself (T160), so the plan is a deterministic
nightly updater per machine, tiered by config/auto-update-policy.json. Step 1
runs for a week and CHANGES NOTHING: it lists what is pending on this machine
(apt, snaps, brew, softwareupdate), sorts each update into a tier with the
reason, and writes logs/auto-update/<date>.json plus one summary line. The
week measures the noise and whether the blocklist is right before step 2
applies anything.

  tier1     auto, nightly (step 2)        noble-security userland; brew patch/minor
  tier2     auto, after the canary        glibc / systemd / dbus / openssl
  tier3     report only, Buddy's yes      majors, downgrades, third-party, macOS
  blocked   never auto-applied here       kernel / NVIDIA / CUDA / firmware, holds
  external  another updater owns it       cloudflared_update.py, snapd, Apple

"Changes nothing" is enforced, not promised: every subprocess goes through
_run(), which refuses any argv that is not on the exact read-only list below.
The selftest proves the refusal.

Usage: auto_update.py [--machine NAME] [--out DIR] [--no-record] | --selftest
"""
import fnmatch
import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
POLICY = HERE / "config" / "auto-update-policy.json"
TIERS = ("tier1", "tier2", "tier3", "blocked", "external")
BREW = "/opt/homebrew/bin/brew"
APT_STAMP = Path("/var/lib/apt/periodic/update-success-stamp")
BREW_API = Path.home() / "Library/Caches/Homebrew/api/formula_names.txt"  # rewritten on each API refresh

# Exact argv shapes, not prefixes: ("snap", "refresh") is a refresh, and only
# the trailing "--list" makes it a read.
READ_ONLY = {
    ("apt", "list", "--upgradable"),
    ("dpkg-query", "-W", r"--showformat=${Package}\t${source:Package}\n"),
    ("apt-mark", "showhold"),
    ("snap", "refresh", "--list"),
    (BREW, "outdated", "--json=v2"),
    ("softwareupdate", "-l"),
    ("sw_vers", "-productVersion"),
}
# apt-cache madison plus package names. A name cannot start with "-", so no
# option (-o Dir::... etc.) can ride in on the package list.
MADISON = ("apt-cache", "madison")
PKG_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")


def _allowed(argv):
    t = tuple(argv)
    if t in READ_ONLY:
        return True
    return t[:2] == MADISON and len(t) > 2 and all(PKG_RE.match(a) for a in t[2:])


def _run(argv, timeout=600):
    if not _allowed(argv):
        raise PermissionError(f"refused: not a read-only command: {' '.join(argv[:4])}")
    env = dict(os.environ, LC_ALL="C", HOME=str(Path.home()), HOMEBREW_NO_AUTO_UPDATE="1",
               HOMEBREW_NO_ENV_HINTS="1", HOMEBREW_NO_ANALYTICS="1")
    r = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(argv[:3])} rc={r.returncode}: {r.stderr.strip()[:160]}")
    return r.stdout


def _glob(value, patterns):
    """The first pattern `value` matches, or None."""
    return next((p for p in patterns if fnmatch.fnmatchcase(value, p)), None)


def _age_days(path):
    try:
        return round((datetime.now().timestamp() - path.stat().st_mtime) / 86400, 1)
    except OSError:
        return None


# ── parsers (pure) ──────────────────────────────────────────────────────────
APT_ROW = re.compile(r"^([^/\s]+)/(\S*) (\S+) \S+ \[upgradable from: ([^\]]+)\]")


def parse_apt_list(text):
    """`apt list --upgradable` -> {name: {"suites": [...], "to": v, "from": v}}."""
    rows = {}
    for line in text.splitlines():
        m = APT_ROW.match(line)
        if m:
            rows[m.group(1)] = {"suites": m.group(2).split(","), "to": m.group(3),
                                "from": m.group(4)}
    return rows


def parse_madison(text):
    """`apt-cache madison` -> {name: [(version, host, suite)]}. Binary rows only.

    A flat repo (NVIDIA's CUDA repo) has no suite: "URL  Packages" -> suite "".
    """
    out = {}
    for line in text.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3 or not parts[2].endswith("Packages"):
            continue
        toks = parts[2].split()
        suite = toks[1].split("/")[0] if len(toks) > 2 else ""
        out.setdefault(parts[0], []).append((parts[1], urlparse(toks[0]).hostname or "", suite))
    return out


def parse_sources(text):
    """dpkg-query Package<TAB>source -> {binary: source}."""
    return dict(line.split("\t", 1) for line in text.splitlines() if "\t" in line)


def parse_snaps(text):
    """`snap refresh --list` -> [{"name", "to"}]; header row skipped."""
    rows = [l.split() for l in text.splitlines() if l.strip()]
    return [{"name": r[0], "to": r[1]} for r in rows[1:] if len(r) >= 2 and rows[0][0] == "Name"]


SU_ITEM = re.compile(r"Title: (.*?), Version: ([^,]+),")


def parse_softwareupdate(text):
    """`softwareupdate -l` -> [{"title", "version", "restart"}]."""
    return [{"title": m.group(1), "version": m.group(2).strip(),
             "restart": "Action: restart" in line}
            for line in text.splitlines() for m in [SU_ITEM.search(line)] if m]


def service_formulae(plist_dirs):
    """{formula: [plist]} for Homebrew formulae a launchd job runs directly.

    Read from the live plists every run, not a hand list (a copy rots). A PATH
    entry like /opt/homebrew/bin:... names no formula and is skipped: the
    match stops at the colon.
    """
    found = {}
    for d in plist_dirs:
        for p in sorted(Path(d).glob("*.plist")):
            try:
                text = p.read_text(errors="replace")
            except OSError:
                continue
            for path in set(re.findall(r"/opt/homebrew/[^<\s:\"]+", text)):
                m = re.match(r"/opt/homebrew/(?:opt|Cellar)/([^/]+)", path)
                if not m and path.startswith("/opt/homebrew/bin/"):
                    m = re.search(r"/Cellar/([^/]+)/", os.path.realpath(path))
                if m:
                    found.setdefault(m.group(1), []).append(p.name)
    return found


# ── classifiers (pure) ──────────────────────────────────────────────────────
def classify_apt(name, row, madison, sources, holds, pol, mach):
    """(tier, reason) for one pending apt package. First match wins."""
    a, elig = pol["apt"], "/".join(pol["apt"]["eligible_suites"])
    if name in holds:
        return "blocked", "held with apt-mark"
    if name in pol["external"]:
        return "external", pol["external"][name]
    g = _glob(name, a["blocked_name_globs"])
    if g:
        return "blocked", f"name matches {g} (kernel/NVIDIA/CUDA/firmware blocklist)"
    src = sources.get(name, "")
    g = _glob(src, a["blocked_source_globs"]) if src else None
    if g:
        return "blocked", f"source package {src} matches {g}"
    # The suite NAME cannot be trusted: NVIDIA's Spark and DGX repos publish
    # under "noble-updates" too. Only the host serving the candidate version
    # says whose package it is.
    cands = [(h, s) for v, h, s in madison.get(name, []) if v == row["to"]]
    if not cands:
        return "blocked", f"no repo found serving {row['to']} (fail closed)"
    nv = [h for h, _ in cands if _glob(h, a["blocked_origin_hosts"])]
    if nv:
        return "blocked", f"from the NVIDIA repo {nv[0]}"
    other = [h for h, _ in cands if not _glob(h, a["ubuntu_hosts"])]
    if other:
        return "tier3", f"third-party repo {other[0]}; only {elig} is auto-eligible"
    suites = sorted({s for _, s in cands})
    if not any(s in a["eligible_suites"] for s in suites):
        return "tier3", f"{','.join(suites)} is not {elig}; report only"
    if src in a["tier2_sources"]:
        return "tier2", f"reboot-class: {a['tier2_sources'][src]}; {mach['tier2_note']}"
    return "tier1", f"{elig} userland"


def _vt(v):
    """'8.1.1_2' -> ((8, 1, 1), 2). Brew's _N suffix is a rebuild of the same version."""
    base, _, rev = v.partition("_")
    return tuple(int(x) for x in re.findall(r"\d+", base)), int(rev) if rev.isdigit() else 0


def _cmp(a, b):
    """-1/0/1 comparing version tuples, padding so 1.5 == 1.5.0."""
    n = max(len(a), len(b))
    a, b = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def classify_brew(item, pol, mach, services):
    """(tier, reason) for one outdated formula or cask."""
    name, new = item["name"], item["current_version"]
    if name in pol["external"]:
        return "external", pol["external"][name]
    if name in pol["brew"]["deny"]:
        return "tier3", pol["brew"]["deny"][name]
    if item.get("cask"):
        return "tier3", "cask: brew Tier 1 covers formulae only"
    if item.get("pinned"):
        return "tier3", "pinned in brew"
    olds = [_vt(v) for v in item["installed_versions"]]
    n = _vt(new)
    if not n[0] or not all(o[0] for o in olds):
        return "tier3", "version not comparable"
    o = max(olds, key=lambda t: (t[0] + (0,) * 8)[:8] + (t[1],))
    c = _cmp(n[0], o[0])
    if c < 0 or (c == 0 and n[1] < o[1]):
        return "tier3", f"DOWNGRADE: brew proposes {new}, older than the installed version"
    if n[0][0] != o[0][0]:
        return "tier3", f"major upgrade {o[0][0]} -> {n[0][0]}"
    if mach.get("service_formulae_tier3") and name in services:
        return "tier3", f"a launchd job runs it ({', '.join(sorted(services[name]))})"
    return "tier1", "rebuild of the same version" if c == 0 else "patch/minor upgrade"


def classify_apple(item, installed, mach):
    """(tier, reason) for one softwareupdate item."""
    inst, new = _vt(installed)[0], _vt(item["version"])[0]
    if item["title"].startswith("macOS") and new and inst and new[0] > inst[0]:
        return "tier3", f"macOS major upgrade {inst[0]} -> {new[0]}"
    tier, why = mach["apple"]
    kind = "macOS minor" if item["title"].startswith("macOS") else "Apple update"
    return tier, f"{kind}: {why}"


# ── collection (the only part that touches the machine; read-only) ──────────
def collect_apt(run, pol, mach):
    rows = parse_apt_list(run(["apt", "list", "--upgradable"]))
    mad = parse_madison(run(list(MADISON) + sorted(rows))) if rows else {}
    src = parse_sources(run(list(next(c for c in READ_ONLY if c[0] == "dpkg-query"))))
    holds = set(run(["apt-mark", "showhold"]).split())
    items = []
    for name, row in sorted(rows.items()):
        tier, why = classify_apt(name, row, mad, src, holds, pol, mach)
        items.append({"source": "apt", "name": name, "from": row["from"], "to": row["to"],
                      "suites": row["suites"], "source_pkg": src.get(name, ""),
                      "tier": tier, "reason": why})
    return items


def collect_snap(run, pol, mach):
    tier, why = pol["snap"]
    return [{"source": "snap", "name": s["name"], "from": "", "to": s["to"],
             "tier": tier, "reason": why}
            for s in parse_snaps(run(["snap", "refresh", "--list"]))]


def collect_brew(run, pol, mach, services):
    data = json.loads(run([BREW, "outdated", "--json=v2"]))
    items = []
    for kind in ("formulae", "casks"):
        for f in data.get(kind, []):
            f = dict(f, cask=(kind == "casks"))
            tier, why = classify_brew(f, pol, mach, services)
            items.append({"source": "cask" if f["cask"] else "brew", "name": f["name"],
                          "from": ",".join(f["installed_versions"]), "to": f["current_version"],
                          "tier": tier, "reason": why})
    return items


def collect_apple(run, pol, mach):
    installed = run(["sw_vers", "-productVersion"]).strip()
    items = []
    for u in parse_softwareupdate(run(["softwareupdate", "-l"])):
        tier, why = classify_apple(u, installed, mach)
        items.append({"source": "macos", "name": u["title"], "from": installed,
                      "to": u["version"], "restart": u["restart"], "tier": tier, "reason": why})
    return items


def collectors(mach, run):
    """[(label, thunk)] for this machine's kind."""
    if mach["kind"] == "apt":
        return [("apt", lambda p: collect_apt(run, p, mach)),
                ("snap", lambda p: collect_snap(run, p, mach))]
    svc = service_formulae(["/Library/LaunchDaemons", Path.home() / "Library/LaunchAgents"])
    return [("brew", lambda p: collect_brew(run, p, mach, svc)),
            ("macos", lambda p: collect_apple(run, p, mach))]


def which_machine(pol, host=None):
    host = (host or socket.gethostname()).split(".")[0]
    return next((k for k, m in pol["machines"].items() if host in m["hostnames"]), None)


def dry_run(machine, pol, out_dir, steps, record=None, now=None):
    """Classify everything pending, write the report, return (report, exit_code)."""
    now = now or datetime.now()
    mach = pol["machines"][machine]
    items, errors = [], []
    for label, step in steps:
        try:
            items += step(pol)
        except Exception as e:  # noqa: BLE001 -- one broken source must not hide the others
            errors.append(f"{label}: {type(e).__name__}: {e}"[:240])
    counts = {t: sum(1 for i in items if i["tier"] == t) for t in TIERS}
    by_source = {}
    for i in items:
        by_source.setdefault(i["source"], dict.fromkeys(TIERS, 0))[i["tier"]] += 1
    notes = {}
    if mach["kind"] == "apt":
        apt = [i for i in items if i["source"] == "apt"]
        notes = {"apt_pending": len(apt),
                 "apt_security_suite": sum(1 for i in apt if any("-security" in s for s in i["suites"])),
                 "apt_lists_age_days": _age_days(APT_STAMP)}
    else:
        notes = {"brew_api_age_days": _age_days(BREW_API)}
    summary = (f"auto-update dry run {machine}: {len(items)} pending -- "
               + ", ".join(f"{t} {counts[t]}" for t in TIERS if counts[t] or t != "external")
               + (f"; ERRORS: {'; '.join(errors)}" if errors else "")
               + "; changed nothing")
    report = {"machine": machine, "at": now.isoformat(timespec="seconds"), "dry_run": True,
              "summary": summary, "counts": counts, "by_source": by_source, "notes": notes,
              "errors": errors, "items": items}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{now:%Y-%m-%d}.json").write_text(json.dumps(report, indent=1) + "\n")
    with open(out_dir / "summary.log", "a") as f:
        f.write(f"{report['at']} {summary}\n")
    if record and mach.get("ledger"):
        record(ok=not errors, note=summary)
    return report, (1 if errors else 0)


def _record(name, ok, note):
    try:
        import job_status
        job_status.record(name, ok=ok, note=note)
    except Exception:  # noqa: BLE001 -- best-effort, as in model_health
        pass


def main(argv, record=None):
    """record: the ledger writer. Each box's entry point names its own row
    (auto_update_cumulus.py on CUMULUS); this file is CIRRUS's."""
    if "--selftest" in argv:
        return selftest()
    pol = json.loads(POLICY.read_text())
    machine = argv[argv.index("--machine") + 1] if "--machine" in argv else which_machine(pol)
    if machine not in pol["machines"]:
        print(f"auto_update: unknown machine {machine!r} (host {socket.gethostname()}); "
              f"refusing to guess -- pass --machine", file=sys.stderr)
        return 2
    out = Path(argv[argv.index("--out") + 1]) if "--out" in argv else HERE / "logs" / "auto-update"
    report, rc = dry_run(machine, pol, out, collectors(pol["machines"][machine], _run),
                         record=None if "--no-record" in argv else (
                             record or (lambda ok, note: _record("autoupdate", ok, note))))
    print(report["summary"])
    return rc


# ── selftest ────────────────────────────────────────────────────────────────
FIX_APT = """\
Listing...
linux-image-nvidia-hwe-24.04/noble-updates,noble-security 7.0.0-1019.19~24.04.2 arm64 [upgradable from: 6.17.0-1029.29]
nvidia-driver-580-open/noble-updates,noble-security 580.178.04-0ubuntu0.24.04.1 arm64 [upgradable from: 580.173.02-0ubuntu0.24.04.1]
libnvidia-compute-580/noble-updates,noble-security 580.178.04-0ubuntu0.24.04.1 arm64 [upgradable from: 580.173.02-0ubuntu0.24.04.1]
cuda-toolkit-config-common/unknown 13.4.92-1 all [upgradable from: 13.3.29-1]
libcuda1/noble-updates,noble-security 580.178.04-0ubuntu0.24.04.1 arm64 [upgradable from: 580.173.02-0ubuntu0.24.04.1]
libnccl2/unknown 2.28.3-1+cuda13.0 arm64 [upgradable from: 2.27.7-1+cuda13.0]
firmware-sof-signed/noble-updates,noble-security 2023.12.1-1ubuntu1.1 all [upgradable from: 2023.12.1-1ubuntu1]
dkms/noble-updates,noble-security 3.0.11-1ubuntu13.1 all [upgradable from: 3.0.11-1ubuntu13]
xserver-xorg-video-nvidia-580/noble-updates,noble-security 580.178.04-0ubuntu0.24.04.1 arm64 [upgradable from: 580.173.02-0ubuntu0.24.04.1]
dgx-release/noble-updates 7.6.0 all [upgradable from: 7.5.0]
spark-security-thing/noble-security 2.0 all [upgradable from: 1.0]
nv-common-apis/unknown 26.08-2 all [upgradable from: 26.03-2]
ghost-pkg/noble-security 9.9 arm64 [upgradable from: 9.8]
tailscale/unknown 1.104.2 arm64 [upgradable from: 1.102.2]
nodejs/nodistro 24.22.1-1nodesource1 arm64 [upgradable from: 24.19.0-1nodesource1]
vim/noble-updates 2:9.1.0016-1ubuntu7.10 arm64 [upgradable from: 2:9.1.0016-1ubuntu7.9]
python3-jinja2/noble-apps-security 3.1.2-1ubuntu1.3+esm1 all [upgradable from: 3.1.2-1ubuntu1.3]
libc6/noble-updates,noble-security 2.39-0ubuntu8.9 arm64 [upgradable from: 2.39-0ubuntu8.8]
libc-bin/noble-updates,noble-security 2.39-0ubuntu8.9 arm64 [upgradable from: 2.39-0ubuntu8.8]
systemd/noble-updates,noble-security 255.4-1ubuntu8.18 arm64 [upgradable from: 255.4-1ubuntu8.16]
udev/noble-updates,noble-security 255.4-1ubuntu8.18 arm64 [upgradable from: 255.4-1ubuntu8.16]
dbus/noble-updates,noble-security 1.14.10-4ubuntu4.2 arm64 [upgradable from: 1.14.10-4ubuntu4.1]
openssl/noble-updates,noble-security 3.0.13-0ubuntu3.16 arm64 [upgradable from: 3.0.13-0ubuntu3.12]
libssl3t64/noble-updates,noble-security 3.0.13-0ubuntu3.16 arm64 [upgradable from: 3.0.13-0ubuntu3.12]
curl/noble-updates,noble-security 8.5.0-2ubuntu10.9 arm64 [upgradable from: 8.5.0-2ubuntu10.8]
libnvidia-egl-wayland1/unknown 1:1.1.23-1ubuntu1 arm64 [upgradable from: 1:1.1.21-1ubuntu1]
zlib1g/noble-security 1:1.3.dfsg-3.1ubuntu2.2 arm64 [upgradable from: 1:1.3.dfsg-3.1ubuntu2.1]
heldpkg/noble-security 2.0 arm64 [upgradable from: 1.0]
cloudflared/noble-security 2026.10.0 arm64 [upgradable from: 2026.9.0]
"""
_U = "http://ports.ubuntu.com/ubuntu-ports"
_S = "https://repo.download.nvidia.com/spark/ubuntu/arm64"
FIX_MADISON = "\n".join(f" {n} | {v} | {u} {s} arm64 Packages" for n, v, u, s in [
    ("firmware-sof-signed", "2023.12.1-1ubuntu1.1", _U, "noble-security/main"),
    ("dkms", "3.0.11-1ubuntu13.1", _U, "noble-security/main"),
    ("xserver-xorg-video-nvidia-580", "580.178.04-0ubuntu0.24.04.1", _U, "noble-security/restricted"),
    ("dgx-release", "7.6.0", _S, "noble-updates/common"),
    ("spark-security-thing", "2.0", _S, "noble-security/common"),
    ("tailscale", "1.104.2", "https://pkgs.tailscale.com/stable/ubuntu", "noble/main"),
    ("tailscale", "1.102.2", "https://pkgs.tailscale.com/stable/ubuntu", "noble/main"),
    ("nodejs", "24.22.1-1nodesource1", "https://deb.nodesource.com/node_24.x", "nodistro/main"),
    ("vim", "2:9.1.0016-1ubuntu7.10", _U, "noble-updates/main"),
    ("python3-jinja2", "3.1.2-1ubuntu1.3+esm1", "https://esm.ubuntu.com/apps/ubuntu", "noble-apps-security/main"),
    ("libc6", "2.39-0ubuntu8.9", _U, "noble-updates/main"),
    ("libc6", "2.39-0ubuntu8.9", _U, "noble-security/main"),
    ("libc-bin", "2.39-0ubuntu8.9", _U, "noble-security/main"),
    ("systemd", "255.4-1ubuntu8.18", _U, "noble-security/main"),
    ("udev", "255.4-1ubuntu8.18", _U, "noble-security/main"),
    ("dbus", "1.14.10-4ubuntu4.2", _U, "noble-security/main"),
    ("openssl", "3.0.13-0ubuntu3.16", _U, "noble-security/main"),
    ("libssl3t64", "3.0.13-0ubuntu3.16", _U, "noble-security/main"),
    ("curl", "8.5.0-2ubuntu10.9", _U, "noble-updates/main"),
    ("curl", "8.5.0-2ubuntu10.9", "http://us.archive.ubuntu.com/ubuntu", "noble-security/main"),
    ("curl", "8.5.0-2ubuntu10.8", _U, "noble-security/main"),
    ("zlib1g", "1:1.3.dfsg-3.1ubuntu2.2", _U, "noble-security/main"),
    ("heldpkg", "2.0", _U, "noble-security/main"),
]) + """
 nv-common-apis |    26.08-2 | https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/sbsa  Packages
 libnvidia-egl-wayland1 | 1:1.1.23-1ubuntu1 | https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/sbsa  Packages
 zlib1g | 1:1.3.dfsg-3.1ubuntu2.2 | http://ports.ubuntu.com/ubuntu-ports noble-security/main Sources
"""
FIX_SOURCES = "\n".join("\t".join(p) for p in [
    ("xserver-xorg-video-nvidia-580", "nvidia-graphics-drivers-580"), ("libc6", "glibc"),
    ("libc-bin", "glibc"), ("systemd", "systemd"), ("udev", "systemd"), ("dbus", "dbus"),
    ("openssl", "openssl"), ("libssl3t64", "openssl"), ("curl", "curl"), ("zlib1g", "zlib"),
    ("dgx-release", "dgx-release"), ("spark-security-thing", "spark-security-thing")])
FIX_SNAP = """\
Name         Version  Rev   Size   Publisher    Notes
firefox      159.0-1  9136  275MB  mozilla**    -
thunderbird  159.0-1  1313  227MB  canonical**  -
"""
FIX_SU = """\
Software Update Tool

Finding available software
Software Update found the following new or updated software:
* Label: Safari27.2TahoeAuto-27.2
\tTitle: Safari, Version: 27.2, Size: 250130KiB, Recommended: YES, \n\
* Label: macOS Tahoe 26.7.2-25G332
\tTitle: macOS Tahoe 26.7.2, Version: 26.7.2, Size: 3868257KiB, Recommended: YES, Action: restart, \n\
* Label: macOS 27.2-26B123
\tTitle: macOS 27.2, Version: 27.2, Size: 11319983KiB, Recommended: YES, Action: restart, \n\
"""


def _brew_fixture():
    def f(name, old, new, **kw):
        return dict({"name": name, "installed_versions": [old], "current_version": new,
                     "pinned": False}, **kw)
    return {"formulae": [
        f("ffmpeg", "8.1.1", "9.0.2_1"), f("lame", "3.100", "4.0"),
        f("openssl@3", "3.6.2", "3.5.9"), f("tailscale", "1.102.3", "1.104.2"),
        f("ollama", "0.34.0", "0.40.11_1"), f("llama.cpp", "0.6.0_1", "0.7.0"),
        f("mlx", "0.32.1", "0.32.4"), f("mlx-c", "0.6.0_4", "0.8.0"),
        f("age", "1.3.1", "1.3.2"), f("merve", "1.2.2_2", "1.2.2_4"),
        f("ca-certificates", "2026-09-25", "2026-10-20"), f("cloudflared", "2026.9.0", "2026.10.0"),
        f("gh", "2.100.0", "2.103.3", pinned=True), f("node", "26.8.2", "26.12.1_1"),
        f("readline", "1.5.0", "1.5_1"), f("weird", "HEAD-abc", "HEAD-def"),
        f("x265", "4.2", "4.3")],
        "casks": [f("claude-code", "2.1.236", "2.1.308")]}


def selftest():
    import shutil
    import tempfile
    fails = []

    def ck(name, cond):
        print(("  ok    " if cond else "  FAIL  ") + name)
        if not cond:
            fails.append(name)

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # T32: load the REAL policy rules, but from a copy -- the selftest never
        # opens a live file for writing and never reads one it could disturb.
        shutil.copy(POLICY, td / "policy.json")
        pol = json.loads((td / "policy.json").read_text())
        c1, c2 = pol["machines"]["cumulus1"], pol["machines"]["cumulus2"]
        rows = parse_apt_list(FIX_APT)
        mad, src = parse_madison(FIX_MADISON), parse_sources(FIX_SOURCES)

        def apt(name, mach=c2):
            return classify_apt(name, rows[name], mad, src, {"heldpkg"}, pol, mach)

        print("parsers")
        ck("apt list: every fixture row parsed", len(rows) == FIX_APT.count("[upgradable"))
        ck("apt list: an epoch version keeps its epoch",
           rows["libnvidia-egl-wayland1"]["to"] == "1:1.1.23-1ubuntu1")
        ck("apt list: a two-suite row keeps both suites",
           rows["libc6"]["suites"] == ["noble-updates", "noble-security"])
        ck("madison: a flat repo row has an empty suite",
           mad["nv-common-apis"] == [("26.08-2", "developer.download.nvidia.com", "")])
        ck("madison: Sources rows are ignored", len(mad["zlib1g"]) == 1)
        ck("snap: two refreshes, header skipped",
           [s["name"] for s in parse_snaps(FIX_SNAP)] == ["firefox", "thunderbird"])
        ck("snap: 'All snaps up to date' is no refresh", parse_snaps("All snaps up to date.\n") == [])
        su = parse_softwareupdate(FIX_SU)
        ck("softwareupdate: three items, two need a restart",
           len(su) == 3 and sum(u["restart"] for u in su) == 2)

        print("apt blocklist: every family")
        for name in ("linux-image-nvidia-hwe-24.04", "nvidia-driver-580-open",
                     "libnvidia-compute-580", "cuda-toolkit-config-common", "libcuda1",
                     "libnccl2", "firmware-sof-signed", "dkms"):
            ck(f"{name} is blocked by name", apt(name)[0] == "blocked" and "name matches" in apt(name)[1])
        ck("xserver-xorg-video-nvidia-580 (noble-security!) is blocked by its SOURCE",
           apt("xserver-xorg-video-nvidia-580") == ("blocked",
               "source package nvidia-graphics-drivers-580 matches nvidia*"))
        ck("NVIDIA repo serving 'noble-updates' is blocked by host",
           apt("dgx-release") == ("blocked", "from the NVIDIA repo repo.download.nvidia.com"))
        ck("NVIDIA repo serving 'noble-security' is STILL blocked -- the suite name is not trusted",
           apt("spark-security-thing")[0] == "blocked")
        ck("NVIDIA flat CUDA repo (no suite) is blocked",
           apt("nv-common-apis") == ("blocked", "from the NVIDIA repo developer.download.nvidia.com"))
        ck("a candidate no repo serves fails CLOSED", apt("ghost-pkg")[0] == "blocked")
        ck("an apt-mark hold is blocked", apt("heldpkg") == ("blocked", "held with apt-mark"))

        print("apt origins")
        ck("tailscale from its own repo is tier3 third-party",
           apt("tailscale")[0] == "tier3" and "pkgs.tailscale.com" in apt("tailscale")[1])
        ck("nodesource is tier3 third-party", apt("nodejs")[0] == "tier3")
        ck("noble-updates only (not security) is tier3", apt("vim")[0] == "tier3")
        ck("ESM apps-security is tier3: only noble-security is eligible",
           apt("python3-jinja2")[0] == "tier3")
        ck("cloudflared belongs to cloudflared_update.py", apt("cloudflared")[0] == "external")

        print("apt tier 2 (reboot-class, by source) and tier 1")
        for name in ("libc6", "libc-bin", "systemd", "udev", "dbus", "openssl", "libssl3t64"):
            ck(f"{name} is tier2", apt(name)[0] == "tier2")
        ck("cumulus1 tier2 carries the 3-day canary gate", "3 days" in apt("libc6", c1)[1])
        ck("curl (security, also on an archive mirror) is tier1", apt("curl") == ("tier1", "noble-security userland"))
        ck("zlib1g is tier1", apt("zlib1g")[0] == "tier1")

        print("brew")
        mac, cirrus = pol["machines"]["macbook"], pol["machines"]["cirrus"]
        svc = {"node": ["ai.openclaw.gateway.plist"]}
        bf = _brew_fixture()
        b = {f["name"]: classify_brew(dict(f, cask=False), pol, cirrus, svc) for f in bf["formulae"]}
        ck("ffmpeg 8 -> 9 is a tier3 major", b["ffmpeg"] == ("tier3", "major upgrade 8 -> 9"))
        ck("lame 3 -> 4 is a tier3 major", b["lame"][0] == "tier3" and "major" in b["lame"][1])
        ck("openssl@3 3.6.2 -> 3.5.9 is a tier3 DOWNGRADE",
           b["openssl@3"][0] == "tier3" and "DOWNGRADE" in b["openssl@3"][1])
        ck("tailscale is denylisted (T68)", b["tailscale"][0] == "tier3" and "T68" in b["tailscale"][1])
        for name in ("ollama", "llama.cpp", "mlx", "mlx-c"):
            ck(f"{name} (model stack) is denylisted even as a minor", b[name][0] == "tier3")
        ck("age 1.3.1 -> 1.3.2 is tier1", b["age"] == ("tier1", "patch/minor upgrade"))
        ck("x265 4.2 -> 4.3 is tier1", b["x265"][0] == "tier1")
        ck("a _N rebuild is tier1", b["merve"] == ("tier1", "rebuild of the same version"))
        ck("a date version bump within a year is tier1", b["ca-certificates"][0] == "tier1")
        ck("1.5.0 -> 1.5_1 is a rebuild, not a downgrade",
           b["readline"] == ("tier1", "rebuild of the same version"))
        ck("cloudflared belongs to cloudflared_update.py", b["cloudflared"][0] == "external")
        ck("a pinned formula is tier3", b["gh"] == ("tier3", "pinned in brew"))
        ck("an unparseable version is tier3", b["weird"] == ("tier3", "version not comparable"))
        ck("CIRRUS: a formula a launchd job runs is tier3", b["node"][0] == "tier3")
        ck("MacBook: the same minor is tier1 (policy flag off)",
           classify_brew(dict(bf["formulae"][13], cask=False), pol, mac, svc)[0] == "tier1")
        ck("a cask is tier3", classify_brew(dict(bf["casks"][0], cask=True), pol, mac, svc)[0] == "tier3")
        ck("multiple installed versions compare against the newest",
           classify_brew({"name": "x", "installed_versions": ["1.0", "2.1"],
                          "current_version": "2.0"}, pol, mac, {})[1].startswith("DOWNGRADE"))

        print("macOS")
        tahoe, mac27, safari = su[1], su[2], su[0]
        ck("CIRRUS 26.5.2 -> 26.7.2 is tier3 (monthly reboot)",
           classify_apple(tahoe, "26.5.2", cirrus)[0] == "tier3")
        ck("CIRRUS -> macOS 27 is a tier3 major",
           classify_apple(mac27, "26.5.2", cirrus) == ("tier3", "macOS major upgrade 26 -> 27"))
        ck("MacBook 26.7.2 is external (Apple auto-install)",
           classify_apple(tahoe, "26.6.2", mac)[0] == "external")
        ck("MacBook -> macOS 27 is still tier3", classify_apple(mac27, "26.6.2", mac)[0] == "tier3")
        ck("Safari follows the machine's Apple rule",
           classify_apple(safari, "26.5.2", cirrus)[0] == "tier3"
           and classify_apple(safari, "26.6.2", mac)[0] == "external")

        print("changes nothing: the read-only gate")
        for argv in [["apt", "upgrade"], ["apt-get", "install", "-y", "curl"],
                     ["apt", "list", "--upgradable", "-o", "x"], ["snap", "refresh"],
                     ["snap", "refresh", "firefox"], [BREW, "upgrade"], ["brew", "outdated", "--json=v2"],
                     ["softwareupdate", "-i", "-a"], ["softwareupdate", "-l", "-i"],
                     ["apt-cache", "madison", "-o", "Dir::Etc=/tmp"], ["apt-cache", "madison"],
                     ["sh", "-c", "apt list --upgradable"]]:
            ck(f"refuses {' '.join(argv)}", not _allowed(argv))
        for argv in READ_ONLY:
            ck(f"allows {' '.join(argv)[:40]}", _allowed(argv))
        ck("allows madison with package names", _allowed(["apt-cache", "madison", "libc6", "g++-13"]))
        ran = []
        real, subprocess.run = subprocess.run, lambda *a, **k: ran.append(a)
        try:
            _run(["apt-get", "upgrade", "-y"])
            refused = False
        except PermissionError:
            refused = True
        except Exception:  # noqa: BLE001 -- it ran the fake: report, do not crash
            refused = False
        finally:
            subprocess.run = real
        ck("_run refuses BEFORE executing anything", refused and not ran)

        print("service formulae from live-shaped plists")
        pl = td / "LaunchAgents"
        pl.mkdir()
        (pl / "a.plist").write_text("<string>/opt/homebrew/opt/node/bin/node</string>"
                                    "<string>/opt/homebrew/bin:/usr/bin</string>")
        (pl / "b.plist").write_text("<string>/opt/homebrew/opt/tailscale/bin/tailscaled</string>")
        ck("opt/<formula> paths are found; a PATH entry is not",
           service_formulae([pl]) == {"node": ["a.plist"], "tailscale": ["b.plist"]})

        print("end to end (temp dir, fake collectors)")
        recorded = []
        steps = [("apt", lambda p: collect_apt(lambda argv: {
                    "apt": FIX_APT, "apt-cache": FIX_MADISON, "dpkg-query": FIX_SOURCES,
                    "apt-mark": "heldpkg\n"}[argv[0]], p, c1)),
                 ("snap", lambda p: collect_snap(lambda argv: FIX_SNAP, p, c1))]
        when = datetime(2026, 10, 27, 1, 30)
        rep, rc = dry_run("cumulus1", pol, td / "out", steps,
                          record=lambda **k: recorded.append(k), now=when)
        written = json.loads((td / "out" / "2026-10-27.json").read_text())
        ck("report written to <date>.json", written["summary"] == rep["summary"] and rc == 0)
        ck("every pending item is classified exactly once",
           sum(rep["counts"].values()) == len(rows) + 2 == len(rep["items"]))
        ck("summary.log got one line",
           (td / "out" / "summary.log").read_text().count("\n") == 1)
        ck("a ledger machine records exactly once, ok", recorded == [{"ok": True, "note": rep["summary"]}])
        ck("security-suite count matches the model_health way of counting",
           rep["notes"]["apt_security_suite"] == sum(
               1 for r in rows.values() if any("-security" in s for s in r["suites"])))
        recorded.clear()
        bad = [("apt", lambda p: (_ for _ in ()).throw(RuntimeError("apt list rc=100"))),
               ("snap", lambda p: collect_snap(lambda argv: FIX_SNAP, p, c2))]
        rep2, rc2 = dry_run("cumulus2", pol, td / "out2", bad,
                            record=lambda **k: recorded.append(k), now=when)
        ck("a failed collector is reported, the rest still run, exit 1",
           rc2 == 1 and "apt list rc=100" in rep2["summary"] and rep2["counts"]["tier1"] == 2)
        ck("cumulus2 has no ledger reader, so it does not record", not recorded)
        ck("hostname picks the machine; an unknown host is refused",
           which_machine(pol, "cumulus2") == "cumulus2"
           and which_machine(pol, "Buddyss-MacBook-Pro-2.local") == "macbook"
           and which_machine(pol, "stranger") is None)

    print(f"\n{'FAILED: ' + str(len(fails)) if fails else 'all passed'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
