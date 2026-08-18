#!/usr/bin/env python3
"""
search_usage.py  (S67, 2026-08-18)
===============================================================================
Count every web-search request, by provider, by caller, by day.

WHY THIS EXISTS
---------------
Buddy asked "does it make sense we spent our initial allowance this quickly?"
and the honest answer was: we could not tell. Nothing counted search requests.
The Brave bill had to be reverse-engineered from log lines, and roughly half of
August's requests came from code paths that log nothing per call -- most notably
`privacy_monitor._brave_search()`, which turned out to be the single largest
consumer (~416 requests per sweep) and was completely invisible.

A cost you cannot attribute is a cost you cannot manage. This closes that.

DESIGN
------
* **Never raises.** A counter that can break a search is worse than no counter.
  Every entry point is wrapped; failures are swallowed. This mirrors
  `job_status.py`'s rule -- monitoring must not break the thing it monitors.
* **Counts REQUESTS, not successes.** A 429, a timeout and an empty result set
  all still cost a request against the quota (or would have). Counting only
  successful calls is how you end up with an estimate that reads 30% low.
* **Per-caller attribution.** "Brave: 3,000 requests" is not actionable.
  "privacy_monitor: 1,248, daily_digest: 468" tells you exactly what to change.
* **Bounded file.** Days older than RETAIN_DAYS are dropped on write, so this
  never becomes another log that grows until someone notices.

Read it with the runner command `brave-usage`, which prefers these real counts
over its older log-scraping estimate.
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path.home() / "projects/cirrus-digest"
if not PROJECT_DIR.exists():                       # CUMULUS layout
    PROJECT_DIR = Path.home() / "cirrus-digest"

USAGE_PATH = PROJECT_DIR / "logs/search-usage.json"
RETAIN_DAYS = 120

# Rough unit costs, only so the report can show dollars alongside counts.
# Not billing truth -- the provider dashboard is. Update if a plan changes.
COST_PER_1K = {"brave": 5.00, "gemini": 0.0, "ddg": 0.0}


def record(provider: str, caller: str, outcome: str = "ok", n: int = 1) -> None:
    """Count n search requests. Never raises.

    outcome: "ok" | "empty" | "error" | "quota". All of them count as requests,
    because all of them consumed one -- the split exists so a spike in "quota"
    or "error" is visible rather than looking like reduced usage.
    """
    try:
        day = datetime.now().strftime("%Y-%m-%d")
        data = _load()
        bucket = data.setdefault(day, {}).setdefault(provider, {}).setdefault(
            caller, {"ok": 0, "empty": 0, "error": 0, "quota": 0})
        bucket[outcome] = bucket.get(outcome, 0) + n
        _save(data)
    except Exception:
        pass


def _load() -> dict:
    try:
        return json.loads(USAGE_PATH.read_text())
    except Exception:
        return {}


def _save(data: dict) -> None:
    cutoff = (datetime.now() - timedelta(days=RETAIN_DAYS)).strftime("%Y-%m-%d")
    for day in [d for d in data if d < cutoff]:
        data.pop(day, None)
    USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = USAGE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, USAGE_PATH)


def totals(days: int = 31) -> dict:
    """{provider: {caller: {outcome: n}}} over the last `days` days."""
    since = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    out = {}
    for day, providers in _load().items():
        if day < since:
            continue
        for provider, callers in providers.items():
            for caller, counts in callers.items():
                dst = out.setdefault(provider, {}).setdefault(
                    caller, {"ok": 0, "empty": 0, "error": 0, "quota": 0})
                for k, v in counts.items():
                    dst[k] = dst.get(k, 0) + v
    return out


def requests_for(provider: str, days: int = 31) -> int:
    return sum(sum(c.values()) for c in totals(days).get(provider, {}).values())


def report(days: int = 31) -> str:
    data = totals(days)
    if not data:
        return ("No search-usage data yet. The counter records from its first\n"
                "call onward -- it cannot reconstruct history.")
    lines = [f"== Search requests, last {days} day(s) — MEASURED, not estimated =="]
    for provider in sorted(data, key=lambda p: -sum(
            sum(c.values()) for c in data[p].values())):
        total = sum(sum(c.values()) for c in data[provider].values())
        cost = total * COST_PER_1K.get(provider, 0.0) / 1000
        money = f"  ~${cost:.2f}" if cost else ""
        lines.append(f"\n{provider}: {total} request(s){money}")
        for caller in sorted(data[provider], key=lambda c: -sum(data[provider][c].values())):
            c = data[provider][caller]
            n = sum(c.values())
            detail = ", ".join(f"{k} {v}" for k, v in c.items() if v)
            lines.append(f"   {n:6d}  {caller:22} ({detail})")
    return "\n".join(lines)


def _selftest() -> bool:
    """Offline test against a temp file — never touches the real counter."""
    global USAGE_PATH
    import tempfile
    real = USAGE_PATH
    tmpdir = tempfile.mkdtemp()
    USAGE_PATH = Path(tmpdir) / "usage.json"
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))

    try:
        record("brave", "daily_digest", "ok", 3)
        record("brave", "privacy_monitor", "ok", 416)
        record("brave", "daily_digest", "quota")
        record("gemini", "daily_digest", "ok", 2)

        ck("counts every outcome as a request", requests_for("brave") == 420)
        t = totals()
        ck("attributes per caller", t["brave"]["privacy_monitor"]["ok"] == 416)
        ck("quota outcomes are counted, not dropped",
           t["brave"]["daily_digest"]["quota"] == 1)
        ck("providers are kept separate", requests_for("gemini") == 2)
        ck("report names the biggest consumer first",
           "privacy_monitor" in report().split("daily_digest")[0])

        # Must never raise, whatever happens to the file.
        USAGE_PATH = Path("/nonexistent-dir-xyz/usage.json")
        try:
            record("brave", "x")
            ck("unwritable path does not raise", True)
        except Exception:
            ck("unwritable path does not raise", False)
    finally:
        USAGE_PATH = real

    bad = 0
    for name, ok in checks:
        print(("  ok   " if ok else "  FAIL ") + name)
        bad += 0 if ok else 1
    print("\n" + ("all search_usage selftests passed" if not bad else f"{bad} FAILED"))
    return bad == 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(0 if _selftest() else 1)
    days = 31
    for a in sys.argv[1:]:
        if a.isdigit():
            days = int(a)
    print(report(days))
