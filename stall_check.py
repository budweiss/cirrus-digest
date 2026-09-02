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
import subprocess
import sys
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.abspath(__file__))

# S96: reuse job_status's node detection and CUMULUS address rather than
# restating them here -- a second copy of "which box am I" is how the two
# drift apart, and this check is worthless if it asks the wrong box.
try:
    from job_status import _here, REMOTE_HOST
except Exception:                                  # pragma: no cover
    def _here(): return "CIRRUS"
    REMOTE_HOST = "buddy@192.168.0.204"

OK, STALL, UNKNOWN = "ok", "stall", "unknown"
# S75. A finding that is REAL, KNOWN and DELIBERATELY NOT BEING FIXED needs its
# own state. Reporting it as STALLED every morning is how the whole panel stops
# being read (T9) — but silencing it entirely loses the finding. ACCEPTED keeps
# it visible, records WHO decided and WHEN to look again, and does not count
# toward the alarm total.
ACCEPTED = "accepted"

# key -> (who/when decided, why, review-on). The review date is the point: an
# acceptance with no expiry is just a silence with extra steps.
ACCEPTED_FINDINGS = {
    "variance[business_ideas]": (
        "Buddy, 2026-08-23",
        "business-ideas scoring stays AS IS; the fit_score finding is recorded "
        "for information, not as a change to make",
        "2026-09-23",
    ),
}


def _apply_acceptance(r):
    """Downgrade a STALL that Buddy has explicitly accepted, and say so.

    Never hides it: the finding still prints, with who accepted it and when to
    revisit. Past the review date it reverts to a STALL, because an acceptance
    that never expires is indistinguishable from having forgotten.
    """
    entry = ACCEPTED_FINDINGS.get(r["name"])
    if not entry or r["state"] != STALL:
        return r
    who, why, review_on = entry
    today = datetime.now().strftime("%Y-%m-%d")
    if today > review_on:
        r["msg"] = (f"{r['msg']} — ACCEPTANCE EXPIRED (was accepted by {who}, "
                    f"review was due {review_on}); decide again")
        return r
    return _res(ACCEPTED, r["name"],
                f"{r['msg']} — ACCEPTED by {who}: {why}. Review on {review_on}")


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


def _question_attempts(project):
    """How many client questions actually reached this KB? -> dict or None.

    None means the ledger does not exist, which is NOT the same as zero
    attempts and must not be reported as if it were.
    """
    path = os.path.join(REPO, "logs", "kb_question_attempts.jsonl")
    if not os.path.exists(path):
        return None
    out = {"attempts": 0, "no_match": 0, "ambiguous": 0, "recorded": 0}
    try:
        with open(path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("project") != project:
                    continue
                out["attempts"] += 1
                m = d.get("matches")
                if m == 0:
                    out["no_match"] += 1
                elif isinstance(m, int) and m > 1:
                    out["ambiguous"] += 1
                if d.get("outcome_recorded"):
                    out["recorded"] += 1
    except Exception:
        return None
    return out


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
            # S75: zero outcomes has TWO very different causes, and calling both
            # a stall every morning is how a check gets ignored (T9). The
            # outcome only fires when exactly one entity matches a client's
            # question, so distinguish "nobody has asked yet" (no fault, and
            # nothing to fix) from "they asked and matching never worked"
            # (a real fault, and invisible until now).
            att = _question_attempts(proj)
            if att is None:
                out.append(_res(UNKNOWN, f"outcomes[{proj}]",
                                "zero outcomes, and no attempt ledger to say "
                                "whether the signal has had any opportunity"))
            elif att["attempts"] == 0:
                out.append(_res(UNKNOWN, f"outcomes[{proj}]",
                                "zero outcomes, but the client has asked about "
                                "0 entities — the signal has had NO opportunity "
                                "yet, so this is waiting, not stalled"))
            else:
                out.append(_res(STALL, f"outcomes[{proj}]",
                                f"{att['attempts']} client question(s) reached the "
                                f"KB and NONE produced an outcome "
                                f"({att['no_match']} matched nothing, "
                                f"{att['ambiguous']} were ambiguous) — matching "
                                f"is broken, not merely quiet"))
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
            out[-1] = _apply_acceptance(out[-1])
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




# ── S96: the check that would have caught 2026-09-02 ─────────────────────────
# Every other check in this file watches a NUMBER that should move. On
# 2026-09-02 two jobs failed a different way: the process was alive and doing
# nothing. cirrus_daily sat on one socket for 6h47m; pedagogy_daily (Alyssa's
# digest) for 2h47m on 279ms of CPU. Nothing here looked at running processes,
# so nothing saw either.
#
# WHY THE EXISTING LEDGER CHECK WAS NOT ENOUGH. A hung job never finishes, so it
# never calls job_status.record(), so its ledger entry stays at YESTERDAY. That
# does eventually read as OVERDUE -- at the 26h cadence. For a 06:00 daily job
# that threshold is crossed around 08:00, and the morning brief goes out at
# 07:30. The brief that morning reported pedagogy as healthy while it was hung,
# and the failure would not have surfaced until the NEXT day. A monitor that
# reports yesterday's outage tomorrow is not a monitor.
#
# This asks a question with a same-morning answer: is anything stuck RIGHT NOW?
#
# Two signals, one per box, each native to how that box fails:
#
#   CIRRUS (launchd) has no start-timeout mechanism at all, so a hung job just
#   runs. We look at elapsed process time directly.
#
#   CUMULUS (systemd) now bounds every oneshot job at 30 minutes (S96 drop-ins,
#   runner oneshot-timeouts), so a hang there self-terminates into `failed`.
#   That is a much better signal than elapsed time -- it is unambiguous, it
#   appears within 30 minutes, and it needs no per-job duration table. We also
#   flag a oneshot still in `activating`, which is what the pre-drop-in hang
#   looked like, so this keeps working if a unit ever loses its ceiling.
#
# CEILING: 4 hours, and it is deliberately generous. Real CIRRUS runs measured
# 2026-09-02: daily 37min, the weekly digest 2h12m. A tight ceiling would fire
# on the healthy weekly digest every Sunday and be muted within a month (T9).
# 4h is above every real run and still caught today's 6h47m stall five times
# over -- at the 07:30 brief, a job that began at 02:00 is 5h30m in.
_STUCK_CEILING_MIN = 240

# Only OUR jobs. Matching on "python" alone would flag the API, the bot, and
# every editor the box happens to be running.
_JOB_PATTERNS = ("cirrus_daily.py", "cirrus_digest.py", "pedagogy_daily.py",
                 "business_idea_scan.py", "business_idea_feeds.py",
                 "business_idea_ideate.py", "privacy_monitor.py",
                 "vendor_mail.py", "model_health.py", "dev_agent.py",
                 "morning_brief.py", "alopecia_collect.py", "alopecia_brief.py",
                 "halftime_catalogue.py", "halftime_routing.py",
                 "opportunity_scout.py", "entity_kb.py")


def _etime_to_min(et):
    """ps `etime` -> whole minutes. Formats: MM:SS, HH:MM:SS, D-HH:MM:SS."""
    try:
        days, _, rest = et.rpartition("-")
        bits = [int(x) for x in rest.split(":")]
        if len(bits) == 2:
            h, m, sec = 0, bits[0], bits[1]
        elif len(bits) == 3:
            h, m, sec = bits
        else:
            return None
        return int(days or 0) * 1440 + h * 60 + m
    except (ValueError, TypeError):
        return None


def check_stuck_jobs(ceiling_min=_STUCK_CEILING_MIN):
    """Is any scheduled job stuck RIGHT NOW? Local processes + remote units."""
    out = []

    # ── local box: a job process running past the ceiling ────────────────────
    # `etime`, NOT `etimes`. macOS ps has no `etimes` (that is procps/Linux) and
    # -- the part that matters -- it rejects the keyword on stderr, EXITS 0, and
    # still prints the remaining columns. The first version of this check used
    # `etimes` and would have parsed a path fragment as the elapsed time, found
    # nothing, and reported "nothing stuck" forever on the box it was written
    # for. A missing column must be UNKNOWN, never OK (T8).
    try:
        ps = subprocess.run(["ps", "-axo", "pid=,etime=,command="],
                            capture_output=True, text=True, timeout=20)
        if ps.returncode != 0 or "keyword not found" in (ps.stderr or ""):
            out.append(_res(UNKNOWN, "stuck jobs (local)",
                            f"ps did not give an elapsed-time column "
                            f"(rc={ps.returncode}, stderr={(ps.stderr or '').strip()[:80]!r}) "
                            "— could not look, which is NOT 'nothing stuck'"))
        else:
            stuck, parsed = [], 0
            for line in ps.stdout.splitlines():
                parts = line.split(None, 2)
                if len(parts) < 3:
                    continue
                pid, et, cmd = parts
                mins = _etime_to_min(et)
                if mins is None:
                    continue
                parsed += 1
                if not any(p in cmd for p in _JOB_PATTERNS):
                    continue
                if mins >= ceiling_min:
                    script = next(p for p in _JOB_PATTERNS if p in cmd)
                    stuck.append(f"{script} pid {pid} running {mins//60}h{mins%60:02d}m")
            if parsed == 0:
                # ps printed rows but not one elapsed time parsed: the column is
                # not what we think it is. Say so instead of reporting clean.
                out.append(_res(UNKNOWN, "stuck jobs (local)",
                                "ps returned rows but no parseable elapsed time — "
                                "the output format changed; this check is blind"))
            elif stuck:
                out.append(_res(STALL, "stuck jobs (local)",
                                "; ".join(stuck) +
                                f" — past the {ceiling_min//60}h ceiling. A job "
                                "alive this long is blocked, not working; check "
                                "its CPU time and open sockets."))
            else:
                out.append(_res(OK, "stuck jobs (local)",
                                f"no job process past {ceiling_min//60}h"))
    except Exception as e:
        out.append(_res(UNKNOWN, "stuck jobs (local)", f"could not check ({e})"))

    # ── CUMULUS: failed or stuck-activating oneshot units ────────────────────
    # Only meaningful from CIRRUS; on CUMULUS itself the local check above is
    # the one that applies, and asking a box about itself over ssh would be
    # both wrong and slow.
    if _here() != "CIRRUS":
        return out
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", REMOTE_HOST,
             "systemctl list-units --type=service --state=failed,activating "
             "--no-legend --plain 2>/dev/null | awk '{print $1, $3, $4}'"],
            capture_output=True, text=True, timeout=45)
        if r.returncode != 0:
            out.append(_res(UNKNOWN, "stuck jobs (CUMULUS)",
                            "could not reach CUMULUS — this is NOT 'nothing failed'"))
            return out
        bad = []
        for line in r.stdout.splitlines():
            f = line.split()
            if len(f) < 2:
                continue
            unit, active = f[0], f[1]
            if not unit.startswith(("cirrus-", "alopecia-", "halftime-",
                                    "opportunity-", "cumulus-", "entity-kb-")):
                continue
            bad.append(f"{unit} [{active}]")
        if bad:
            out.append(_res(STALL, "stuck jobs (CUMULUS)",
                            "; ".join(bad) + " — a failed unit is a job that did "
                            "NOT produce its output today. `activating` means it "
                            "is hung with no ceiling (see runner oneshot-timeouts)."))
        else:
            out.append(_res(OK, "stuck jobs (CUMULUS)", "no failed or hung unit"))
    except Exception as e:
        out.append(_res(UNKNOWN, "stuck jobs (CUMULUS)",
                        f"could not check ({e}) — not the same as 'nothing failed'"))
    return out


def run_all():
    res = []
    res.append(check_devloop_builds())
    res.append(check_yield_ledger())
    res += check_kb_outcomes()
    res += check_score_variance()
    res.append(check_council_diversity())
    res.append(check_prompt_cache())
    res.append(check_link_extraction())
    res += check_stuck_jobs()
    return res


def selftest():
    """S96. Pins check_stuck_jobs by FAKING the two system calls it makes.

    This file had no selftest at all, so dev_agent's gate 2 reported "selftest"
    for it while inspecting nothing. More to the point, the first draft of
    check_stuck_jobs used `ps -o etimes` -- a Linux-only keyword that macOS ps
    rejects on stderr while EXITING 0 and printing the other columns. On CIRRUS
    it would have parsed a path fragment as the elapsed time, matched nothing,
    and reported "no job process past 4h" every morning forever. Case 2 is that
    exact failure, kept as a test so it cannot come back silently.

    Every case asserts the direction that matters: a check which cannot see must
    say UNKNOWN, never OK.
    """
    import types
    ok = fail = 0

    def ck(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1; print(f"  PASS {name}")
        else:
            fail += 1; print(f"  FAIL {name}")

    real_run, real_here = subprocess.run, _here
    try:
        def with_ps(stdout, stderr="", rc=0, ssh=None):
            def f(cmd, **kw):
                if cmd[0] == "ps":
                    return types.SimpleNamespace(returncode=rc, stderr=stderr, stdout=stdout)
                return ssh or types.SimpleNamespace(returncode=0, stderr="", stdout="")
            return f

        globals()["_here"] = lambda: "CUMULUS"      # local leg only

        subprocess.run = with_ps(
            "  501 07:12:44 /usr/bin/python3 /home/x/cirrus_daily.py\n"
            "  999 00:03:11 /usr/bin/python3 /home/x/morning_brief.py\n")
        r = check_stuck_jobs()[0]
        ck("a 7h job is STALLED", r["state"] == STALL and "7h12m" in r["msg"])
        ck("...and a 3-minute job beside it is NOT flagged",
           "morning_brief" not in r["msg"])

        # The macOS `etimes` trap: rc 0, keyword rejected, columns shift.
        subprocess.run = with_ps("    1 /sbin/launchd\n",
                                 stderr="ps: etimes: keyword not found\n")
        r = check_stuck_jobs()[0]
        ck("a rejected ps keyword is UNKNOWN, not OK", r["state"] == UNKNOWN)

        subprocess.run = with_ps("  501 /usr/bin/python3 /home/x/cirrus_daily.py\n")
        r = check_stuck_jobs()[0]
        ck("rows with no parseable elapsed time are UNKNOWN, not OK",
           r["state"] == UNKNOWN)

        subprocess.run = with_ps("  9 00:01:00 /bin/zsh\n")
        r = check_stuck_jobs()[0]
        ck("a quiet box is OK", r["state"] == OK)

        # Remote leg.
        globals()["_here"] = lambda: "CIRRUS"
        subprocess.run = with_ps("  9 00:01:00 /bin/zsh\n",
                                 ssh=types.SimpleNamespace(returncode=255, stderr="x", stdout=""))
        r = check_stuck_jobs()[1]
        ck("unreachable CUMULUS is UNKNOWN, not 'nothing failed'", r["state"] == UNKNOWN)

        subprocess.run = with_ps(
            "  9 00:01:00 /bin/zsh\n",
            ssh=types.SimpleNamespace(returncode=0, stderr="", stdout=
                "cirrus-pedagogy.service failed failed\n"
                "snap.mesa-2404.component-monitor.service failed failed\n"))
        r = check_stuck_jobs()[1]
        ck("a failed OUR unit is STALLED", r["state"] == STALL and "cirrus-pedagogy" in r["msg"])
        ck("...and a vendor unit is not reported as ours", "snap.mesa" not in r["msg"])

        subprocess.run = with_ps(
            "  9 00:01:00 /bin/zsh\n",
            ssh=types.SimpleNamespace(returncode=0, stderr="", stdout=
                "cirrus-pedagogy.service activating start\n"))
        r = check_stuck_jobs()[1]
        ck("a oneshot stuck in `activating` is STALLED (the pre-drop-in shape)",
           r["state"] == STALL and "activating" in r["msg"])

        for et, exp in [("05:30", 5), ("01:05:30", 65), ("2-03:04:05", 3064),
                        ("bogus", None), ("", None)]:
            ck(f"etime {et!r} -> {exp}", _etime_to_min(et) == exp)
    finally:
        subprocess.run, globals()["_here"] = real_run, real_here

    print(f"\n{ok} passed, {fail} failed")
    return 1 if fail else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", action="store_true")
    # T57: the subcommand is argv[0], never `"--selftest" in sys.argv` -- a
    # wrapper's flag namespace is not its payload's.
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
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
        mark = {OK: "  ok     ", STALL: "  STALL  ", UNKNOWN: "  unknown",
                ACCEPTED: "  accept "}[r["state"]]
        print(f"{mark} {r['name']:22} {r['msg']}")
    accepted = [r for r in res if r["state"] == ACCEPTED]
    print(f"\n  {len(res)} signal(s): "
          f"{len(res)-len(stalls)-len(unknown)-len(accepted)} ok, "
          f"{len(stalls)} stalled, {len(unknown)} UNCHECKED, "
          f"{len(accepted)} accepted")
    if unknown:
        print("\n  UNCHECKED is not OK. Each one is a signal we cannot see, which is")
        print("  the exact condition that let six problems survive for weeks.")
    return 1 if (stalls or unknown) else 0


if __name__ == "__main__":
    raise SystemExit(main())
