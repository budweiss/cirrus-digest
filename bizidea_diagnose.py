#!/usr/bin/env python3
"""Why does the business-idea scan admit nothing? — S68 diagnostic.

The 35-day backfill processed 214 emails and admitted ZERO:
    emails 214 -> prefiltered 191 (89%) -> scored_low 30 -> admitted 0

Two very different failures produce that same number, and they have OPPOSITE
fixes, so guessing is worthless:
  (a) the local prefilter is eating good material at 89%, or
  (b) the council's relevance bar is set too high for what survives.

This walks a sample through the real decision chain and prints each verdict, so
the funnel is visible instead of inferred. Read-only: no KB writes, no seen-state
updates, no sends.

    python3 bizidea_diagnose.py [--n 40] [--days 35]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import business_idea_scan as B


def main(n: int = 40, days: int = 35):
    creds = B.load_creds() if hasattr(B, "load_creds") else {}
    try:
        import json
        creds = json.loads((Path.home() / "projects/cirrus-digest/config/credentials.json").read_text())
    except Exception as e:
        print("no creds:", e)

    # ignore_seen: the backfill already claimed these Message-IDs, so a normal
    # fetch returns nothing. The whole point here is to re-examine what was
    # already decided.
    msgs = B.fetch_business_emails(creds, lookback_days=days, max_per_sender=n,
                                   ignore_seen=True)
    print(f"fetched {len(msgs)} email(s) over {days} days\n")

    kept = dropped = 0
    passed = []
    for m in msgs[:n]:
        title = m.get("subject", "")[:90]
        body = m.get("body", "")
        ok, why = B.prefilter_local(title, body)
        if ok:
            kept += 1
            passed.append(m)
            print(f"  PASS  {title}")
        else:
            dropped += 1
            print(f"  drop  {title}")

    print(f"\nprefilter: {kept} passed / {dropped} dropped "
          f"({100*dropped/max(1,kept+dropped):.0f}% rejected)")

    # For everything that survived, show the COUNCIL's score and reasoning --
    # this is the half that decides whether the bar or the prefilter is wrong.
    print(f"\n=== council relevance on the {len(passed)} survivor(s) "
          f"(RELEVANCE_MIN = {B.RELEVANCE_MIN}) ===")
    for m in passed[:8]:
        try:
            score, why, label = B._relevance(m.get("subject", ""), m.get("body", ""), creds)
            verdict = "ADMIT" if score >= B.RELEVANCE_MIN else "below bar"
            print(f"  [{score}/10 {verdict}] {m.get('subject','')[:70]}")
            print(f"        idea: {label}")
            print(f"        why:  {(why or '')[:180]}")
        except Exception as e:
            print(f"  ERR {type(e).__name__}: {e}")


if __name__ == "__main__":
    a = sys.argv
    n = int(a[a.index("--n") + 1]) if "--n" in a else 40
    d = int(a[a.index("--days") + 1]) if "--days" in a else 35
    main(n, d)
