#!/usr/bin/env python3
"""stall_check.py — find the things that should be moving and aren't.

S74, 2026-08-23. Buddy, after a day in which six separate problems were found by
measuring rather than by monitoring: *"do we need to build something that finds
these stuck processes on a regular interval?"*

THE PATTERN THIS EXISTS FOR
---------------------------
Every one of those six had the same shape — **a number that should move, and
doesn't**:

    Bill's CRM        2,444 leads, 0 state transitions, ever
    fit_score         9 on 27 of 35 rows — no variance at all
    dev-loop          0 builds across 4 consecutive nights
    outcome column    0 rows
    ssh-restart       reported success, did nothing, for a day
    scheduled reboot  fired, logged, never rebooted

None of them threw an error. Nothing crashed. Every log looked fine. That is
exactly why they survived for weeks — a stall is invisible to error-based
monitoring, because a stall is not an error.

THE RULE THIS FILE OBEYS ABOVE ALL OTHERS
-----------------------------------------
**"Could not check" must never render as "healthy."** That single confusion is
what let all six persist. A missing file, an unreadable DB, an absent table —
each returns UNKNOWN and says so loudly, never OK. On 2026-08-22 `placement-audit`
reported 15 healthy units as MISSING because it could not reach a box, and
`tm-freshness` called a dead backup healthy because it answered a question about
the past. Both are the same mistake in the other direction.

WHAT IT CANNOT DO
-----------------
It only watches what someone thought to list. Of the six above it would have
caught four; it would NOT have caught the dense-72B being 19x slower than a
model beside it, or `ssh-restart` silently no-opping — those needed someone to
measure something nobody had measured. **No detector replaces occasionally
checking whether an old claim is still true.**

Usage:  stall_check.py            human-readable
        stall_check.py --brief    one line per finding, for the morning brief
Exit 0 = nothing stalled. 1 = something stalled or unknown.
"""
import argparse
import glob
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.abspath(__file__))

OK, STALL, UNKNOWN = "ok", "stall", "unknown"


def _res(state, name, msg):
    return {"state": state, "name": name, "msg": msg}


def _age_days(ts_str):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            d = datetime.strptime(str(ts_str)[:19], fmt)
            if d.tzinfo:
                d = d.replace(tzinfo=None)
            return (datetime.now() - d).days
        except Exception:
            continue
    return None


# ── watchers ────────────────────────────────────────────────────────────────
def check_devloop_builds(days=3):
    p = os.path.join(REPO, "logs", "dev-loop", "builds.json")
    if not os.path.exists(p):
        return _res(UNKNOWN, "dev-loop builds", f"cannot read {p} — NOT the same as 'no builds'")
    try:
        b = json.load(open(p))
    except Exception as e:
        return _res(UNKNOWN, "dev-loop builds", f"unreadable ({e})")
    ages = [a for a in (_age_days(x.get("created")) for x in b) if a is not None]
    if not ages:
        return _res(UNKNOWN, "dev-loop builds", "no parseable build dates")
    newest = min(ages)
    if newest > days:
        return _res(STALL, "dev-loop builds",
                    f"no build attempted in {newest} days — the loop is idle, "
                    "usually because nothing is approved")
    return _res(OK, "dev-loop builds", f"last build {newest}d ago")


def check_yield_ledger(days=2):
    p = os.path.join(REPO, "logs", "dev-loop", "yield-ledger.jsonl")
    if not os.path.exists(p):
        return _res(UNKNOWN, "yield ledger", "never written — devloop_yield.py has not run")
    try:
        last = [l for l in open(p) if l.strip()][-1]
        age = _age_days(json.loads(last).get("ts"))
    except Exception as e:
        return _res(UNKNOWN, "yield ledger", f"unreadable ({e})")
    if age is None:
        return _res(UNKNOWN, "yield ledger", "no parseable timestamp")
    if age > days:
        return _res(STALL, "yield ledger",
                    f"last measured {age}d ago — the fitness function is not being computed")
    return _res(OK, "yield ledger", f"measured {age}d ago")


def _kb_dbs():
    return sorted(glob.glob(os.path.join(REPO, "data", "entity_kb", "*.db")))


def check_kb_outcomes(days=7):
    dbs = _kb_dbs()
    if not dbs:
        return [_res(UNKNOWN, "kb outcomes", "no entity_kb databases found")]
    out = []
    for db in dbs:
        proj = os.path.basename(db)[:-3]
        try:
            c = sqlite3.connect(db)
            cols = {r[1] for r in c.execute("PRAGMA table_info(entity_events)")}
            if "outcome" not in cols:
                out.append(_res(UNKNOWN, f"outcomes[{proj}]",
                                "no outcome column — migration has not run here"))
                continue
            row = c.execute("SELECT MAX(outcome_at) FROM entity_events "
                            "WHERE outcome IS NOT NULL").fetchone()
            n = c.execute("SELECT COUNT(*) FROM entity_events "
                          "WHERE outcome IS NOT NULL").fetchone()[0]
        except Exception as e:
            out.append(_res(UNKNOWN, f"outcomes[{proj}]", f"unreadable ({e})"))
            continue
        if not n:
            out.append(_res(STALL, f"outcomes[{proj}]",
                            "ZERO outcomes ever recorded — nothing downstream can learn"))
            continue
        age = _age_days(row[0]) if row and row[0] else None
        if age is not None and age > days:
            out.append(_res(STALL, f"outcomes[{proj}]",
                            f"{n} total but none in {age}d — the feedback signal stopped"))
        else:
            out.append(_res(OK, f"outcomes[{proj}]", f"{n} recorded, newest {age}d ago"))
    return out


def check_score_variance(min_rows=10):
    """A scored field with no variance is not scoring anything. This is the
    generalisation of the fit_score finding: 9 on 27 of 35 rows."""
    dbs = _kb_dbs()
    if not dbs:
        return [_res(UNKNOWN, "score variance", "no entity_kb databases found")]
    out = []
    for db in dbs:
        proj = os.path.basename(db)[:-3]
        try:
            c = sqlite3.connect(db)
            rows = [r[0] for r in c.execute("SELECT state_json FROM entities")]
        except Exception as e:
            out.append(_res(UNKNOWN, f"variance[{proj}]", f"unreadable ({e})"))
            continue
        vals = {}
        for sj in rows:
            try:
                d = json.loads(sj or "{}")
            except Exception:
                continue
            for k, v in d.items():
                if k.endswith("_score") and isinstance(v, (int, float)):
                    vals.setdefault(k, []).append(v)
        if not vals:
            out.append(_res(OK, f"variance[{proj}]", "no scored fields"))
            continue
        flat = []
        for k, vs in vals.items():
            if len(vs) < min_rows:
                continue
            # Threshold set from the REAL case, not a guess. The first version
            # used >90% and would have missed the finding that motivated this
            # whole check: business-ideas `fit_score` is 9 on 27 of 35 rows —
            # 77%. A field that answers the same way three times in four is not
            # discriminating, whatever its theoretical range. Tested against
            # that exact data before shipping.
            dom = max(set(vs), key=vs.count)
            share = vs.count(dom) / len(vs)
            if len(set(vs)) <= 1:
                flat.append(f"{k}: one value ({dom}) across all {len(vs)} rows")
            elif share >= 0.70:
                flat.append(f"{k}: {share:.0%} of {len(vs)} rows are {dom}")
        if flat:
            out.append(_res(STALL, f"variance[{proj}]",
                            "; ".join(flat) + " — a dimension with no variance is not selecting"))
        else:
            out.append(_res(OK, f"variance[{proj}]", f"{len(vals)} scored field(s) vary"))
    return out


def check_council_diversity(n=10):
    """If one provider wins every decision, the council is theatre."""
    p = os.path.join(REPO, "logs", "dev-loop", "builds.json")
    if not os.path.exists(p):
        return _res(UNKNOWN, "council diversity", "no builds.json")
    try:
        b = json.load(open(p))
    except Exception as e:
        return _res(UNKNOWN, "council diversity", f"unreadable ({e})")
    judges = [x.get("judge") for x in b if x.get("judge")]
    if len(judges) < n:
        return _res(UNKNOWN, "council diversity",
                    f"only {len(judges)} judged decision(s) — below {n}, no verdict")
    recent = judges[-n:]
    if len(set(recent)) == 1:
        return _res(STALL, "council diversity",
                    f"{recent[0]} won all of the last {n} — the others are not contributing")
    return _res(OK, "council diversity", f"{len(set(recent))} distinct judges in last {n}")


def check_prompt_cache(min_calls=20):
    """Is prompt caching actually READING BACK, or only ever writing?

    S75. Caching was switched on for Anthropic after a live probe proved the
    mechanism works. That is NOT proof our workload benefits: a cache only pays
    when the prefix repeats byte-identically between calls. If every call writes
    a cache and none ever reads one, we are paying the 1.25x write premium for
    nothing — strictly WORSE than leaving it off.

    This exists so that a fix I made cannot quietly turn out to be a cost
    increase while everyone assumes it was a saving.
    """
    path = os.path.join(REPO, "logs", "llm_cache_usage.jsonl")
    if not os.path.exists(path):
        return _res(UNKNOWN, "prompt cache",
                    f"no ledger at {path} — NOT the same as 'no caching problem'")
    reqs = writes = reads = 0
    try:
        with open(path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if not d.get("cache_requested"):
                    continue
                reqs += 1
                writes += d.get("cache_write") or 0
                reads += d.get("cache_read") or 0
    except Exception as e:
        return _res(UNKNOWN, "prompt cache", f"ledger unreadable ({e})")

    if reqs < min_calls:
        return _res(UNKNOWN, "prompt cache",
                    f"only {reqs} cache-eligible call(s) — below {min_calls}, "
                    "too few to judge")
    if reads == 0:
        return _res(STALL, "prompt cache",
                    f"{reqs} calls wrote {writes:,} cached tokens and read back "
                    "ZERO — prefixes are not repeating, so caching is currently "
                    "a COST INCREASE, not a saving")
    ratio = reads / max(1, reads + writes)
    return _res(OK, "prompt cache",
                f"{reads:,} read vs {writes:,} written ({ratio:.0%} reuse) "
                f"over {reqs} calls")


def check_link_extraction(max_fail_ratio=0.40):
    """Are we spending most of the fetch budget on links we cannot read?

    S75. The weekly report carried "960 failed" for weeks with no cause
    attached, and it turned out ~85% were not fetch failures at all: the page
    returned HTTP 200 and the parser found no article body. Causes are recorded
    now, so this watches the ratio rather than waiting for someone to notice a
    big number again.
    """
    files = sorted(glob.glob(os.path.join(REPO, "digests", "metrics", "daily-*.json")),
                   reverse=True)[:3]
    if not files:
        return _res(UNKNOWN, "link extraction",
                    "no daily metrics files — cannot tell, which is not 'fine'")
    ok = failed = 0
    reasons = {}
    for fp in files:
        try:
            d = json.load(open(fp))
        except Exception:
            continue
        ok += d.get("links_ok", 0) or 0
        failed += d.get("links_failed", 0) or 0
        for k, v in d.items():
            if k.startswith("linkfail_") and isinstance(v, int):
                reasons[k[len("linkfail_"):]] = reasons.get(k[len("linkfail_"):], 0) + v
    total = ok + failed
    if total == 0:
        return _res(UNKNOWN, "link extraction",
                    "metrics files present but no link counts in them")
    ratio = failed / total
    if not reasons and failed:
        return _res(UNKNOWN, "link extraction",
                    f"{failed} of {total} failed but NO cause recorded — the "
                    "reason field is not being written")
    top = sorted(reasons.items(), key=lambda kv: -kv[1])[:3]
    detail = ", ".join(f"{v} {k}" for k, v in top)
    if ratio > max_fail_ratio:
        return _res(STALL, "link extraction",
                    f"{failed} of {total} link fetches unusable ({ratio:.0%}) — {detail}")
    return _res(OK, "link extraction",
                f"{failed} of {total} unusable ({ratio:.0%}) — {detail}" if reasons
                else f"{failed} of {total} unusable ({ratio:.0%})")


def run_all():
    res = []
    res.append(check_devloop_builds())
    res.append(check_yield_ledger())
    res += check_kb_outcomes()
    res += check_score_variance()
    res.append(check_council_diversity())
    res.append(check_prompt_cache())
    res.append(check_link_extraction())
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", action="store_true")
    a = ap.parse_args()
    res = run_all()
    stalls = [r for r in res if r["state"] == STALL]
    unknown = [r for r in res if r["state"] == UNKNOWN]

    if a.brief:
        for r in stalls:
            print(f"- ❌ STALLED {r['name']}: {r['msg']}")
        for r in unknown:
            print(f"- ⚠️ UNCHECKED {r['name']}: {r['msg']}")
        if not stalls and not unknown:
            print(f"- ✅ nothing stalled ({len(res)} signals checked)")
        return 1 if (stalls or unknown) else 0

    print("== stall check ==\n")
    for r in res:
        mark = {OK: "  ok     ", STALL: "  STALL  ", UNKNOWN: "  unknown"}[r["state"]]
        print(f"{mark} {r['name']:22} {r['msg']}")
    print(f"\n  {len(res)} signal(s): {len(res)-len(stalls)-len(unknown)} ok, "
          f"{len(stalls)} stalled, {len(unknown)} UNCHECKED")
    if unknown:
        print("\n  UNCHECKED is not OK. Each one is a signal we cannot see, which is")
        print("  the exact condition that let six problems survive for weeks.")
    return 1 if (stalls or unknown) else 0


if __name__ == "__main__":
    raise SystemExit(main())
