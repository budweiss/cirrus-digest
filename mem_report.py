#!/usr/bin/env python3
"""mem_report.py — how much memory each scheduled project actually uses (S111).

WHY NOT systemd's MemoryPeak, which is the obvious answer
---------------------------------------------------------
Buddy asked to "turn on MemoryPeak for the client jobs". It is already on
(`DefaultMemoryAccounting=yes`), and it still cannot answer the question:

  1. On systemd 255 `MemoryPeak` is read from the unit's cgroup, which is
     DESTROYED when a oneshot exits. Every finished job reads `[not set]`.
     Retention landed in systemd 256; CUMULUS is on 255.
  2. Even while running it measures the WRONG PROCESS. The job is a small
     Python process; the memory is the MODEL, held by `ollama.service` in its
     own cgroup.
  3. And that cgroup under-reports too. On GB10 the model sits in UNIFIED
     memory and the GPU allocation is not charged to the cgroup:
     ollama.service memory.peak said 26.3 GB while the system-wide peak for
     the same window was 46.3 GB.

So this reads the system-wide figure sysstat is already collecting and
attributes it to whichever job owned the clock. Jobs are serialised by design
(`window-audit` enforces no same-start overlaps), which is what makes the
attribution honest — and where two jobs DO overlap, this says so rather than
crediting the peak to one of them.

    python3 mem_report.py [--date YYYY-MM-DD] [--json]
    python3 mem_report.py --selftest
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta

# The scheduled app units whose memory is worth attributing. Vendor and
# infrastructure units are deliberately absent: this answers "what do the
# PROJECTS cost", not "what is running".
UNITS = [
    "halftime-catalogue", "halftime-routing", "cirrus-pedagogy",
    "cirrus-hoaleads", "cirrus-billsnow", "cirrus-billnewdev",
    "alopecia-collect", "alopecia-brief", "opportunity-scout",
    "entity-kb-weekly-digest", "cumulus-daily-brief", "cirrus-modelhealth",
    "accesscheck", "cirrus-devloop",
]

_SAR_RE = re.compile(
    r"^(\d{2}:\d{2}:\d{2})\s+(AM|PM)\s+\d+\s+\d+\s+(\d+)\s")


def parse_sar(text):
    """[(minutes_since_midnight, gb_used)] from `sar -r` output.

    The 12-hour clock is the trap: a naive split gives every column an
    off-by-one and `kbmemused` becomes `%memused`. Pinned in the selftest with
    a real captured line.
    """
    out = []
    for line in (text or "").splitlines():
        m = _SAR_RE.match(line.strip())
        if not m:
            continue
        hhmmss, ampm, kb_used = m.group(1), m.group(2), int(m.group(3))
        h, mnt, _s = (int(x) for x in hhmmss.split(":"))
        if ampm == "PM" and h != 12:
            h += 12
        elif ampm == "AM" and h == 12:
            h = 0
        out.append((h * 60 + mnt, kb_used / 1048576.0))
    return out


def peak_in(samples, start_min, end_min):
    """Highest sample inside [start, end]. None when nothing was sampled there.

    None is NOT zero. sysstat samples every 10 minutes, so a job shorter than
    the gap can fall entirely between two samples — reporting 0 GB for it would
    be inventing a measurement.
    """
    hits = [gb for (t, gb) in samples if start_min <= t <= end_min]
    return max(hits) if hits else None


def overlaps(windows):
    """[(unit_a, unit_b)] for windows that share clock time.

    Attribution is only honest while jobs are serialised, so the report has to
    be able to say when they were not.
    """
    bad = []
    items = sorted(windows.items(), key=lambda kv: kv[1][0])
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            (ua, (sa, ea)), (ub, (sb, eb)) = items[i], items[j]
            if sb < ea and sa < eb:
                bad.append((ua, ub))
    return bad


def _run(argv):
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=30).stdout


def windows_for(day, units=UNITS, runner=_run):
    """{unit: (start_min, end_min)} from the journal, for `day`."""
    out = {}
    for u in units:
        try:
            txt = runner(["journalctl", "-u", f"{u}.service",
                          "--since", f"{day} 00:00:00",
                          "--until", f"{day} 23:59:59",
                          "-o", "short-iso", "--no-pager"])
        except Exception:
            continue
        stamps = []
        for line in txt.splitlines():
            m = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):", line)
            if m:
                stamps.append(int(m.group(2)) * 60 + int(m.group(3)))
        if stamps:
            out[u] = (min(stamps), max(stamps))
    return out


def build(day, sar_text, wins):
    rows = []
    samples = parse_sar(sar_text)
    baseline = min((gb for _t, gb in samples), default=None)
    for unit, (s, e) in sorted(wins.items(), key=lambda kv: kv[1][0]):
        pk = peak_in(samples, s, e)
        rows.append({
            "unit": unit,
            "start": f"{s // 60:02d}:{s % 60:02d}",
            "end": f"{e // 60:02d}:{e % 60:02d}",
            "minutes": e - s,
            "peak_gb": None if pk is None else round(pk, 1),
            "over_baseline_gb": (None if pk is None or baseline is None
                                 else round(pk - baseline, 1)),
        })
    return {"date": day, "baseline_gb": (None if baseline is None
                                         else round(baseline, 1)),
            "rows": rows, "overlaps": overlaps(wins)}


def render(rep):
    out = [f"== memory by scheduled job — {rep['date']} =="]
    if rep["baseline_gb"] is not None:
        out.append(f"   idle baseline: {rep['baseline_gb']} GB")
    out.append("")
    if not rep["rows"]:
        out.append("   no scheduled job ran on this date")
    for r in rep["rows"]:
        pk = "not sampled" if r["peak_gb"] is None else f"{r['peak_gb']:5.1f} GB"
        ob = ("" if r["over_baseline_gb"] is None
              else f"  (+{r['over_baseline_gb']:.1f} over idle)")
        out.append(f"   {r['start']}-{r['end']}  {r['unit']:26} {pk}{ob}")
    if rep["rows"] and all(r["peak_gb"] is None for r in rep["rows"]):
        out.append("")
        out.append("   ⚠️  NOTHING was sampled in any window — sysstat may not be "
                   "collecting. That is not 'the jobs used no memory'.")
    for a, b in rep["overlaps"]:
        out.append(f"   ⚠️  {a} and {b} OVERLAP — the peak cannot be attributed "
                   f"to either one alone")
    return "\n".join(out)


def selftest():
    bad = 0

    def ck(label, cond):
        nonlocal bad
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        if not cond:
            bad += 1

    # A real captured line — the 12-hour clock and column order are the trap.
    real = ("12:10:02 AM  86136260 123208432   2687996      2.11     54936  "
            "37444828   4577780      3.17  19000572  18887932      6944")
    s = parse_sar(real)
    ck("a real sar row parses to (minutes, GB)", len(s) == 1)
    ck("...kbmemused is read, NOT %memused — the off-by-one the 12-hour "
       "clock causes", abs(s[0][1] - 2.56) < 0.05)
    ck("...12:10 AM is minute 10, not 730", s[0][0] == 10)
    pm = parse_sar(real.replace("12:10:02 AM", "01:30:00 PM"))
    ck("...and 01:30 PM is minute 810", pm[0][0] == 13 * 60 + 30)
    noon = parse_sar(real.replace("12:10:02 AM", "12:30:00 PM"))
    ck("...12:30 PM is 12:30, not 00:30 — the noon/midnight case",
       noon[0][0] == 12 * 60 + 30)
    ck("a header line is not a sample", parse_sar("kbmemfree kbavail") == [])

    smp = [(0, 2.5), (30, 40.0), (60, 3.0)]
    ck("peak_in finds the max inside the window", peak_in(smp, 20, 40) == 40.0)
    ck("...and ignores samples outside it", peak_in(smp, 50, 70) == 3.0)
    ck("a window with NO samples is None, never 0 — a job shorter than the "
       "10-minute sample gap must not read as 'used nothing'",
       peak_in(smp, 5, 9) is None)

    ck("overlapping windows are reported",
       overlaps({"a": (0, 30), "b": (20, 50)}) == [("a", "b")])
    ck("...and adjacent, non-overlapping ones are NOT — or the warning fires "
       "on every healthy day and gets ignored",
       overlaps({"a": (0, 30), "b": (30, 50)}) == [])

    rep = build("2026-01-01", real, {"j": (0, 30)})
    ck("build attributes a peak to the job that owned the clock",
       rep["rows"][0]["peak_gb"] == 2.6)
    ck("...and renders without raising", "j" in render(rep))
    empty = build("2026-01-01", "", {"j": (0, 30)})
    ck("a day with no sar data says so LOUDLY rather than printing zeros",
       "NOTHING was sampled" in render(empty))

    print("\nALL PASS" if not bad else f"\n{bad} FAILED")
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    d = datetime.strptime(a.date, "%Y-%m-%d")
    argv = ["sar", "-r"]
    if d.date() != datetime.now().date():
        argv += ["-f", f"/var/log/sysstat/sa{d.strftime('%d')}"]
    try:
        sar_text = _run(argv)
    except Exception as e:
        print(f"sar unavailable: {e}")
        return 1
    rep = build(a.date, sar_text, windows_for(a.date))
    print(json.dumps(rep, indent=2) if a.json else render(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
