#!/usr/bin/env python3
"""Ask the other foundation models the same 24 questions, then diff.

Buddy: "Reach out to the other foundational models asking the 24 questions and
see if they give different results."

DESIGN NOTES
------------
* **All 24 in ONE prompt per provider.** 24 questions x 5 providers as separate
  calls would be 120 paid calls; batching makes it 5. Same answers, ~4% of the
  cost.
* **The models are NOT told our answers.** Anchoring a second opinion on the
  first is how you get agreement instead of information -- the same rule the
  `second-opinion` skill states.
* **Disagreement is the product**, not consensus. A question where four models
  split four ways is telling us it is a coin flip regardless of what anyone
  says confidently.
* Every call is ledgered by llm_providers under task `immaculate_poll`, so the
  spend shows up in llm-spend-report like any other.

Read-only toward the CRM: this WRITES nothing. It prints a comparison and, with
--record, ledgers each model's answers as signal events against the questions.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import immaculate_store as store   # noqa: E402
import entity_kb                   # noqa: E402
import llm_providers as L          # noqa: E402

SYSTEM = """You are answering a Pittsburgh Steelers season-prediction contest
for the 2026 NFL season. It is 9 September 2026; the season starts 13 September.

Answer all 24 questions. For EACH, choose exactly one option from that
question's own list, and give a one-line reason.

Return ONLY a JSON array, no prose. Each element:
{"n": <question number>, "answer": "<exactly one of the listed options>",
 "why": "<one short sentence>"}

Rules:
- The answer MUST be copied from that question's option list, verbatim.
- Where a question says "pick a number", give the number as a string.
- Do not hedge, do not give two answers, do not explain outside "why".
- If you genuinely do not know, still pick the single most likely option."""


def build_prompt():
    lines = ["The 2026 Pittsburgh Steelers questions:", ""]
    for num, when, q, opts, *_ in store.QUESTIONS:
        lines.append(f"{num}. ({when}) {q}")
        lines.append(f"   Options: {opts}")
    lines += ["", "Useful context you may not have:",
              "- Mike Tomlin has left; Mike McCarthy is the head coach.",
              "- Aaron Rodgers is the starting QB.",
              "- 2025: Steelers 10-7 and won the AFC North; Ravens 8-9 with",
              "  Lamar Jackson missing 4 games injured; Bengals 6-11; Browns 5-12.",
              "- 2026 win totals: BAL 11.5, CIN 9.5, PIT 8.5, CLE 5.5.",
              "- New in 2026: RB Rico Dowdle, WR Michael Pittman Jr."]
    return "\n".join(lines)


def parse(raw):
    """-> {n: (answer, why)} or {} if the reply is unusable."""
    import re
    if not raw:
        return {}
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except Exception:
        return {}
    out = {}
    for item in data if isinstance(data, list) else []:
        try:
            out[int(item["n"])] = (str(item.get("answer", "")).strip(),
                                   str(item.get("why", "")).strip()[:120])
        except Exception:
            continue
    return out


def poll(providers, creds, max_tokens=6000):
    prompt = build_prompt()
    results = {}
    for prov in providers:
        try:
            raw = L.call(prov, SYSTEM, prompt, creds,
                         max_tokens=max_tokens, retries=1,
                         task="immaculate_poll")
            got = parse(raw)
            if got:
                results[prov] = got
                print(f"  {prov:10s} answered {len(got)}/24")
            else:
                print(f"  {prov:10s} UNUSABLE reply ({len(raw or '')} chars)")
        except Exception as e:
            print(f"  {prov:10s} FAILED: {type(e).__name__}: {str(e)[:70]}")
    return results


def compare(results):
    """Print ours vs theirs, loudest disagreement first."""
    rows = []
    for num, when, q, opts, ours, conf, basis, _vol in store.QUESTIONS:
        theirs = {p: r[num][0] for p, r in results.items() if num in r}
        agree = sum(1 for a in theirs.values() if a.lower() == ours.lower())
        rows.append((agree, len(theirs), num, when, q, ours, conf, theirs))

    rows.sort(key=lambda r: (r[0] / r[1] if r[1] else 1, -r[2]))
    print(f"\n{'#':>3} {'ours':14s} {'agree':>7s}  others")
    for agree, n, num, when, q, ours, conf, theirs in rows:
        others = ", ".join(f"{p[:4]}:{a}" for p, a in sorted(theirs.items()))
        flag = "  <-- OUTVOTED" if n and agree * 2 < n else ""
        print(f"{num:>3} {ours:14s} {agree}/{n:<5}  {others[:96]}{flag}")
    return rows


def main():
    creds = json.loads((HERE / "config/credentials.json").read_text())
    providers = [p for p in ("anthropic", "openai", "gemini", "grok", "deepseek")
                 if L.has_provider(p, creds)] if hasattr(L, "has_provider") else \
                ["anthropic", "openai", "gemini", "grok", "deepseek"]
    print(f"polling: {', '.join(providers)}")
    results = poll(providers, creds)
    if not results:
        print("\nNo model answered usably — nothing to compare. This is a "
              "FAILED poll, not agreement.")
        return 1
    rows = compare(results)

    if "--record" in sys.argv:
        for agree, n, num, *_rest in rows:
            theirs = _rest[-1]
            entity_kb.add_signal(
                store.PROJECT, f"q{num:02d}", "model_poll",
                f"{agree}/{n} of the other models agree; " +
                "; ".join(f"{p}={a}" for p, a in sorted(theirs.items())))
        print("\nrecorded to the CRM as model_poll signals")
    return 0


if __name__ == "__main__":
    if "selftest" in sys.argv:
        # offline: the prompt and the parser, no network
        fails = 0
        def ck(nm, c):
            global fails
            print(f"  [{'OK ' if c else 'FAIL'}] {nm}")
            fails += 0 if c else 1
        p = build_prompt()
        ck("the prompt carries all 24 questions",
           all(f"\n{n}. (" in "\n" + p for n in range(1, 25)))
        ck("...and their option lists", "PIT / ATL / No Points" in p)
        # The anchoring risk is our REASONING, not the option lists -- "50-59"
        # is legitimately one of Q11's options and must be shown. What must
        # never appear is why we chose what we chose.
        ck("...and leaks none of our own rationale (anchoring)",
           not any(b[:30] in p for *_, b, _v in store.QUESTIONS if len(b) > 30))
        ck("...and never labels an option as ours",
           "our answer" not in p.lower() and "recommend" not in p.lower())
        g = parse('noise [{"n":1,"answer":"PIT","why":"home"},'
                  '{"n":24,"answer":"Ravens","why":"lamar"}] trailing')
        ck("parses JSON wrapped in prose", g.get(1, ("",))[0] == "PIT"
           and g.get(24, ("",))[0] == "Ravens")
        ck("an unusable reply parses to EMPTY, never a false answer",
           parse("I think the Steelers") == {} and parse("") == {})
        print(f"\n{'ALL PASS' if not fails else f'{fails} FAILURE(S)'}")
        sys.exit(1 if fails else 0)
    sys.exit(main())
