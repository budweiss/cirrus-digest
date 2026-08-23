#!/usr/bin/env python3
"""devloop_yield.py — the dev-loop's fitness function.

S74, 2026-08-23. Buddy chose "dev-loop yield" as the metric the self-improvement
loop optimises against:

    shipped changes that survive 7 days without revert  ÷  builds attempted

WHY A METRIC AT ALL
-------------------
Every working recursive-self-improvement system in the field (Weco's AIDE²,
Recursive Improve, Goal-MD — see docs/RSI-RESEARCH-AND-PLAN.md) needs one input
above all others: something measurable to select on. "Keep it only if better"
is meaningless without a "better".

Our dev-loop had a human tap instead, and a tap cannot be optimised against —
which is exactly why it idled four consecutive nights in August 2026. This is
the number that replaces the tap for changes the metric can judge. Client-facing
work stays human-gated regardless; a metric must never be allowed to ship mail
to Bill.

THE PART THAT MATTERS MORE THAN THE RATIO
-----------------------------------------
A single yield number tells you the loop is bad without telling you WHY, and an
outer loop needs a gradient, not a verdict. So the failures are split by FAULT:

  harness fault   the agent could not build because OUR TOOLING failed it —
                  it was handed a truncated file, or not handed the file at all.
                  Fixable by us, and the loop cannot fix it alone.
  proposal fault  the idea was out of scope or referenced something that does
                  not exist. Fixable upstream, in what gets proposed.
  judgment        built fine, then discarded by a human. The build worked; the
                  idea was not wanted.

This split was not theoretical. On the first run, 2 of 4 build failures were
harness faults — the loop was asking a model to rewrite files it had never been
shown. That is a bug in the loop's own harness, found by measuring it.

HONESTY
-------
n is small (8 builds over 5 weeks at the time of writing). This prints the
sample size next to every ratio, and refuses to draw a trend from fewer than 20.
A fitness function that reports confident numbers on 8 samples would poison
every decision downstream of it.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.abspath(__file__))
BUILDS = os.path.join(REPO, "logs", "dev-loop", "builds.json")
LEDGER = os.path.join(REPO, "logs", "dev-loop", "yield-ledger.jsonl")
SURVIVE_DAYS = 7
MIN_N_FOR_TREND = 20

# Harness faults name a failure of OUR tooling to supply the agent what it
# needed. Matched on the model's own words, which are stable because they are
# quoting a rule it was given ("Rule 2 requires returning the COMPLETE new
# content"). Anything unmatched is deliberately counted as a proposal fault —
# we under-claim harness bugs rather than inflate them.
_HARNESS = re.compile(
    r"truncated|was not provided|not provided to me|cuts off mid|"
    r"cannot safely reconstruct|missing the (rest|majority)", re.I)


def _sha_reverted(sha):
    """Is this shipped commit still standing? -> (reverted: bool, why: str)"""
    if not sha:
        return False, "no sha recorded"
    try:
        r = subprocess.run(["git", "-C", REPO, "cat-file", "-e", sha + "^{commit}"],
                           capture_output=True, timeout=20)
        if r.returncode != 0:
            return True, "commit no longer in the repo"
        r = subprocess.run(["git", "-C", REPO, "log", "--oneline",
                            "--grep", f"[Rr]evert.*{sha[:7]}", "-n", "5"],
                           capture_output=True, text=True, timeout=20)
        if r.stdout.strip():
            return True, "explicit revert commit found"
        r = subprocess.run(["git", "-C", REPO, "merge-base", "--is-ancestor",
                            sha, "HEAD"], capture_output=True, timeout=20)
        if r.returncode != 0:
            return True, "not an ancestor of HEAD (rewritten or dropped)"
        return False, "still in HEAD"
    except Exception as e:
        return False, f"could not verify ({e})"


def compute():
    try:
        builds = json.load(open(BUILDS))
    except Exception as e:
        return None, f"cannot read {BUILDS}: {e}"

    now = datetime.now()
    rows = []
    for b in builds:
        st = b.get("status")
        err = str(b.get("error") or "")
        fault = ""
        if st == "cannot-build":
            fault = "harness" if _HARNESS.search(err) else "proposal"
        elif st == "discarded":
            fault = "judgment"

        survived = None
        why = ""
        if st == "shipped":
            reverted, why = _sha_reverted(b.get("shipped_sha"))
            try:
                age = now - datetime.strptime(b.get("created", "")[:19],
                                              "%Y-%m-%d %H:%M:%S")
            except Exception:
                age = timedelta(days=999)
            if age < timedelta(days=SURVIVE_DAYS):
                survived = None            # too young to count either way
                why = f"only {age.days}d old — not yet judged"
            else:
                survived = not reverted
        rows.append(dict(id=b.get("id"), status=st, fault=fault,
                         survived=survived, why=why))
    return rows, None


def main():
    rows, err = compute()
    if err:
        print(f"!! {err}")
        return 1

    n = len(rows)
    shipped = [r for r in rows if r["status"] == "shipped"]
    judged = [r for r in shipped if r["survived"] is not None]
    survived = [r for r in judged if r["survived"]]
    harness = [r for r in rows if r["fault"] == "harness"]
    proposal = [r for r in rows if r["fault"] == "proposal"]
    judgment = [r for r in rows if r["fault"] == "judgment"]

    print("== dev-loop yield ==")
    print(f"  builds attempted        : {n}")
    print(f"  shipped                 : {len(shipped)}")
    print(f"  shipped AND judged (>{SURVIVE_DAYS}d): {len(judged)}")
    print(f"  survived without revert : {len(survived)}")
    if judged:
        print(f"\n  YIELD = {len(survived)}/{n} = {len(survived)/n:.0%}"
              f"   (of judged: {len(survived)}/{len(judged)})")
    else:
        print("\n  YIELD = not yet computable — nothing shipped is old enough to judge")

    print("\n== where the loop LOSES — the actionable part ==")
    print(f"  harness fault  : {len(harness)}  <- OUR tooling failed the agent")
    for r in harness:
        print(f"      {r['id']}")
    print(f"  proposal fault : {len(proposal)}  <- the idea was out of scope or invented")
    for r in proposal:
        print(f"      {r['id']}")
    print(f"  judgment       : {len(judgment)}  <- built fine, human said no")

    if harness:
        print(f"\n  ** {len(harness)}/{len(harness)+len(proposal)} build failures are HARNESS faults."
              "\n     The loop cannot fix these itself — it is being asked to rewrite"
              "\n     files it was never shown, or shown truncated. Fix the harness"
              "\n     before optimising anything else; this is free yield. **")

    print(f"\n  SAMPLE SIZE: n={n}."
          + ("" if n >= MIN_N_FOR_TREND else
             f" Below {MIN_N_FOR_TREND} — report the number, do NOT read a trend"
             " into it. A fitness function that sounds confident on a small n"
             " poisons every decision downstream."))

    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "n": n, "shipped": len(shipped), "judged": len(judged),
            "survived": len(survived), "harness_fault": len(harness),
            "proposal_fault": len(proposal), "judgment": len(judgment),
            "yield": (len(survived) / n) if n else None,
        }) + "\n")
    print(f"\n  appended to {LEDGER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
