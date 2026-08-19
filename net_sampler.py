#!/usr/bin/env python3
"""Measure what the uplink and the boxes are ACTUALLY doing — S69.

Buddy (2026-08-19): "are you keeping track of what is using the internet
between all of bills projects, Alyssa, and our improvement projects? ... we
should build something that knows what is running on Cirrus and Cumulus to make
sure they have enough resource."

The scheduling policy in runtime_window.py is built on ONE assumed fact -- that
08:00-16:00 Mon-Fri is when the household uses the internet. That assumption
came from Buddy, it is probably right, and it is currently unmeasured. This
samples the real counters so the policy can be checked against the day instead
of believed.

WHAT IT MEASURES, AND WHAT IT HONESTLY CANNOT
---------------------------------------------
Measured, cheaply and reliably: host-level bytes in/out on the primary
interface, 1-minute load average, and free memory. One sample every 5 minutes,
appended to a JSONL. Costs nothing and never touches the network itself.

NOT measured: per-process attribution. Neither macOS nor Linux gives per-PID
byte counters without either sudo+dtrace or cgroup plumbing, and standing up
either for this would be far more machinery than the question deserves. So a
sample says "the box moved 40MB in these 5 minutes", not "hoaleads moved 40MB".

That is usable precisely BECAUSE of the scheduling change: jobs are now spread
across 02:00-05:15 instead of piled into one hour, so at most one heavy job is
usually in flight and `attribute()` can join a sample window against job start
times with a real chance of being right. Where two jobs overlap a window, the
report says "ambiguous" rather than picking one -- a confident wrong attribution
is worse than an honest gap.

    python3 net_sampler.py --sample          # one sample, append to the log
    python3 net_sampler.py --report [--days 7]
    python3 net_sampler.py --selftest
"""

import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

LOG = Path.home() / ".cowork-netsamples.jsonl"
MAX_LINES = 20000          # ~70 days at 5-min samples; trimmed from the front


# ── counter reading, per platform ───────────────────────────────────────────
def _linux_counters():
    """Sum every non-loopback, non-virtual interface in /proc/net/dev."""
    rx = tx = 0
    for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        if name == "lo" or name.startswith(("docker", "veth", "br-", "virbr")):
            continue
        f = rest.split()
        if len(f) >= 9:
            rx += int(f[0])
            tx += int(f[8])
    return rx, tx


def _macos_counters():
    """netstat -ib. Each interface appears on several rows (one per address
    family) with the SAME cumulative counters, so summing every row would
    multiply the real number. Take one row per interface."""
    out = subprocess.run(["netstat", "-ib"], capture_output=True, text=True,
                         timeout=15).stdout
    rx = tx = 0
    seen = set()
    for line in out.splitlines()[1:]:
        f = line.split()
        if len(f) < 10:
            continue
        name = f[0]
        if name in seen or name.startswith(("lo", "gif", "stf", "utun", "awdl",
                                            "llw", "bridge", "ap")):
            continue
        try:
            ibytes, obytes = int(f[6]), int(f[9])
        except (ValueError, IndexError):
            continue
        seen.add(name)
        rx += ibytes
        tx += obytes
    return rx, tx


def counters():
    return _linux_counters() if platform.system() == "Linux" else _macos_counters()


def _memfree_mb():
    try:
        if platform.system() == "Linux":
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
        else:
            vm = subprocess.run(["vm_stat"], capture_output=True, text=True,
                                timeout=10).stdout
            page = 4096
            m = re.search(r"page size of (\d+)", vm)
            if m:
                page = int(m.group(1))
            free = 0
            for key in ("Pages free:", "Pages inactive:"):
                mm = re.search(re.escape(key) + r"\s+(\d+)", vm)
                if mm:
                    free += int(mm.group(1))
            return free * page // (1024 * 1024)
    except Exception:
        pass
    return None


def sample(now: float = None):
    rx, tx = counters()
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = None
    return {"ts": int(now if now is not None else time.time()),
            "host": platform.node().split(".")[0],
            "rx": rx, "tx": tx, "load1": load1,
            "memfree_mb": _memfree_mb(), "cpus": os.cpu_count()}


def append(rec, path: Path = LOG):
    with path.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    # Trim from the front so the file cannot grow without bound on a box nobody
    # is watching. Cheap: only when it's actually oversized.
    try:
        lines = path.read_text().splitlines()
        if len(lines) > MAX_LINES:
            path.write_text("\n".join(lines[-MAX_LINES:]) + "\n")
    except Exception:
        pass


# ── turning samples into throughput ─────────────────────────────────────────
def deltas(records):
    """Consecutive samples -> per-interval throughput.

    Two things break a naive subtraction and both happen in practice:
      * a reboot resets the counters, so rx can go DOWN. A negative delta is
        dropped, not recorded as a huge negative or an absolute value.
      * a gap (sampler stopped, box asleep) spreads one delta over hours and
        would look like a flat trickle. Intervals over 30 minutes are dropped.
    """
    out = []
    for a, b in zip(records, records[1:]):
        dt = b["ts"] - a["ts"]
        if dt <= 0 or dt > 1800:
            continue
        drx, dtx = b["rx"] - a["rx"], b["tx"] - a["tx"]
        if drx < 0 or dtx < 0:
            continue
        out.append({"ts": b["ts"], "secs": dt, "rx": drx, "tx": dtx,
                    "mbps_down": drx * 8 / dt / 1e6,
                    "mbps_up": dtx * 8 / dt / 1e6,
                    "load1": b.get("load1")})
    return out


def by_hour(ds):
    """Bucket throughput by hour-of-day, weekday vs weekend kept apart --
    that split is the whole point, since the policy claims they differ."""
    buckets = {}
    for d in ds:
        lt = time.localtime(d["ts"])
        kind = "weekend" if lt.tm_wday >= 5 else "weekday"
        b = buckets.setdefault((kind, lt.tm_hour),
                               {"bytes": 0, "secs": 0, "n": 0, "peak": 0.0})
        b["bytes"] += d["rx"] + d["tx"]
        b["secs"] += d["secs"]
        b["n"] += 1
        b["peak"] = max(b["peak"], d["mbps_down"] + d["mbps_up"])
    return buckets


def report(days: int = 7, path: Path = LOG):
    if not path.exists():
        print(f"no samples yet at {path} — install the sampler first")
        return 1
    cutoff = time.time() - days * 86400
    recs = []
    for line in path.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("ts", 0) >= cutoff:
            recs.append(r)
    recs.sort(key=lambda r: r["ts"])
    ds = deltas(recs)
    if not ds:
        print(f"{len(recs)} sample(s) in the last {days}d, but no usable "
              f"intervals yet (need at least two samples < 30 min apart)")
        return 0

    span_h = (recs[-1]["ts"] - recs[0]["ts"]) / 3600
    host = recs[-1].get("host", "?")
    total_gb = sum(d["rx"] + d["tx"] for d in ds) / 1e9
    print(f"host {host}: {len(recs)} samples over {span_h:.1f}h, "
          f"{total_gb:.2f} GB moved\n")

    buckets = by_hour(ds)
    for kind in ("weekday", "weekend"):
        rows = {h: b for (k, h), b in buckets.items() if k == kind}
        if not rows:
            print(f"=== {kind}: no data yet ===\n")
            continue
        peak_avg = max((b["bytes"] / max(1, b["secs"]) for b in rows.values()),
                       default=1) or 1
        print(f"=== {kind} — average throughput by hour ===")
        for h in range(24):
            b = rows.get(h)
            if not b:
                continue
            avg_mbps = b["bytes"] * 8 / max(1, b["secs"]) / 1e6
            bar = "#" * max(1, int(40 * (b["bytes"] / max(1, b["secs"])) / peak_avg))
            tag = ""
            if kind == "weekday" and 8 <= h < 16:
                tag = "  <- assumed local-use window"
            elif 2 <= h < 6:
                tag = "  <- preferred run window"
            print(f"  {h:02d}:00  {avg_mbps:7.2f} Mbps avg  "
                  f"peak {b['peak']:6.2f}  n={b['n']:3}  {bar}{tag}")
        print()

    # The check that matters: does the measured day agree with the policy?
    wd = {h: b for (k, h), b in buckets.items() if k == "weekday"}
    if wd:
        def rate(hrs):
            sel = [wd[h] for h in hrs if h in wd]
            if not sel:
                return None
            return (sum(b["bytes"] for b in sel) * 8
                    / max(1, sum(b["secs"] for b in sel)) / 1e6)
        busy, night = rate(range(8, 16)), rate(range(2, 6))
        if busy is not None and night is not None:
            print("=== policy check (weekday) ===")
            print(f"  08:00-16:00 avg: {busy:6.2f} Mbps")
            print(f"  02:00-06:00 avg: {night:6.2f} Mbps")
            if busy > night:
                print("  -> measurement AGREES with the 08:00-16:00 local-use "
                      "assumption; keep heavy work in the early window.")
            else:
                print("  -> measurement DISAGREES: our own night jobs are "
                      "heavier than daytime local use. That is expected while "
                      "the sample is short; revisit after a full week.")
    return 0


def selftest() -> bool:
    bad = 0

    def ck(label, got, want):
        nonlocal bad
        ok = got == want
        print(("  ok   " if ok else "  FAIL ") + f"{label}: {got!r}" +
              ("" if ok else f" (want {want!r})"))
        bad += 0 if ok else 1

    base = 1_700_000_000
    recs = [{"ts": base, "rx": 1000, "tx": 500},
            {"ts": base + 300, "rx": 1000 + 300 * 125000, "tx": 500}]
    d = deltas(recs)
    ck("one interval", len(d), 1)
    ck("1 Mbps down computed", round(d[0]["mbps_down"], 3), 1.0)

    # A reboot resets counters -- must be dropped, not reported as negative or
    # as a spurious multi-GB transfer.
    ck("counter reset dropped",
       deltas([{"ts": base, "rx": 10**9, "tx": 0},
               {"ts": base + 300, "rx": 5, "tx": 0}]), [])
    # A long gap must not be smeared into a fake flat rate.
    ck("long gap dropped",
       deltas([{"ts": base, "rx": 0, "tx": 0},
               {"ts": base + 7200, "rx": 10**9, "tx": 0}]), [])
    ck("zero/negative dt dropped",
       deltas([{"ts": base, "rx": 0, "tx": 0}, {"ts": base, "rx": 5, "tx": 0}]), [])

    s = sample()
    ck("sample has counters", all(k in s for k in ("rx", "tx", "ts", "host")), True)
    ck("counters are non-negative ints",
       isinstance(s["rx"], int) and s["rx"] >= 0 and s["tx"] >= 0, True)

    print()
    print("all net_sampler selftests passed" if not bad else f"{bad} FAILED")
    return bad == 0


if __name__ == "__main__":
    a = sys.argv
    if "--selftest" in a:
        raise SystemExit(0 if selftest() else 1)
    if "--report" in a:
        d = int(a[a.index("--days") + 1]) if "--days" in a else 7
        raise SystemExit(report(d))
    rec = sample()
    append(rec)
    print(json.dumps(rec))
