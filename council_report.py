#!/usr/bin/env python3
"""council_report.py — is a council member earning its place?

S74, 2026-08-23. Buddy added DeepSeek as a fifth council member and said:
"let's spend this week to see if DeepSeek makes sense to run on cumulus when the
second DGX Spark is added."

A week of running only tells you something if something is counting. Today's
whole lesson was that three separate systems here could not improve because
nothing measured them — so this exists before the week starts, not after.

WHAT IT ANSWERS
  * how often does each provider PARTICIPATE (it is in `members`)
  * how often is it CHOSEN as judge — the council's own verdict on who was most
    useful, and the closest thing we have to a quality signal
  * what does it COST per call, and how slow is it
  * does it FAIL or return empty — a member that quietly returns nothing still
    counts as present and would skew every vote

WHAT IT DOES NOT CLAIM
  Judge-selection is not proof of quality. It is one signal, produced by the
  same family of models being judged. It is reported as what it is, alongside
  the sample size, and the report refuses to rank providers on fewer than 10
  decisions. A confident ranking off three data points would be worse than no
  report at all — it would get acted on.

Usage:  council_report.py [--days 7]
"""
import argparse
import collections
import json
import os
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.abspath(__file__))
SPEND = os.path.join(REPO, "out", "llm-spend-ledger.jsonl")
BUILDS = os.path.join(REPO, "logs", "dev-loop", "builds.json")
MIN_N_TO_RANK = 10


def load_spend(days):
    cut = (datetime.now() - timedelta(days=days)).isoformat()
    rows = []
    try:
        for line in open(SPEND):
            try:
                d = json.loads(line)
            except Exception:
                continue
            if str(d.get("ts", "")) >= cut:
                rows.append(d)
    except FileNotFoundError:
        pass
    return rows


def load_judges(days):
    """Council decisions with their member list and chosen judge."""
    out = []
    try:
        builds = json.load(open(BUILDS))
    except Exception:
        return out
    cut = datetime.now() - timedelta(days=days)
    for b in builds:
        try:
            when = datetime.strptime(str(b.get("created", ""))[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if when < cut:
            continue
        m, j = b.get("members"), b.get("judge")
        if m:
            out.append((m, j))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args()

    spend = load_spend(a.days)
    judges = load_judges(a.days)

    print(f"== council report — last {a.days} days ==\n")
    if not spend:
        print("  no LLM calls recorded in the window. Nothing to report yet.")
        return 0

    calls = collections.Counter()
    cost = collections.Counter()
    out_tok = collections.Counter()
    empty = collections.Counter()
    for r in spend:
        p = r.get("provider") or r.get("model") or "?"
        calls[p] += 1
        cost[p] += float(r.get("cost") or 0)
        ot = int(r.get("out_tok") or 0)
        out_tok[p] += ot
        if ot == 0:
            empty[p] += 1

    print("  provider          calls    cost$   avg out_tok   zero-output")
    for p, n in calls.most_common():
        avg = (out_tok[p] / n) if n else 0
        flag = "  <-- returning nothing" if empty[p] and empty[p] / n > 0.2 else ""
        print(f"  {p:16} {n:6}  {cost[p]:7.2f}   {avg:10.0f}   {empty[p]:>5}{flag}")

    print(f"\n  === judge selection (council decisions: {len(judges)}) ===")
    if not judges:
        print("  none recorded yet — the dev-loop has not run a council decision")
        print("  in this window. This is the signal that matters most, so the")
        print("  report is incomplete until the loop actually builds something.")
    else:
        chosen = collections.Counter(j for _, j in judges if j)
        present = collections.Counter(p for m, _ in judges for p in m)
        print("  provider          present   chosen   chosen-when-present")
        for p in sorted(present, key=lambda x: -present[x]):
            rate = (chosen[p] / present[p]) if present[p] else 0
            print(f"  {p:16} {present[p]:7}  {chosen[p]:7}   {rate:17.0%}")
        if len(judges) < MIN_N_TO_RANK:
            print(f"\n  n={len(judges)} — BELOW {MIN_N_TO_RANK}. Do not rank providers on this.")
            print("  Report the numbers; a confident ranking here would get acted on")
            print("  and would be wrong.")

    print("\n  NOTE: judge-selection is NOT proof of quality — it is one signal,")
    print("  produced by the same family of models being judged. Read it")
    print("  alongside cost and latency, not instead of them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
