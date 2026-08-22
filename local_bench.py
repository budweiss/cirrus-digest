#!/usr/bin/env python3
"""local_bench.py — can the LOCAL model do the work we currently pay a cloud for?

S73, 2026-08-22.

WHY THIS EXISTS
---------------
Both boxes hold a 47 GB `qwen2.5:72b`. Neither ever calls it. Over seven days
CIRRUS made 1,218 cloud LLM calls — claude-sonnet-5 594, gemini-2.5-flash 312,
grok-3-mini 312 — because `llm_providers.py` had no local backend at all.

The obvious reaction is "route some of that locally and save money". That is the
wrong frame: the spend is ~$4/week. The reasons that actually matter are that
client material (Bill's leads, Aggie's properties, Alyssa's coursework) leaves
the building on every call, and that we depend on three external services being
up and not rate-limiting us.

But "the local model could probably handle the easy ones" is a guess, and this
project has spent a whole day proving what guesses are worth. So: measure.

WHAT IT MEASURES, AND WHAT IT DELIBERATELY DOES NOT
--------------------------------------------------
It replays the SAME prompt through the local model and through the cloud
providers, and reports, per prompt:

  * did the local model answer at all, and how fast
  * length agreement       — a local answer 10x shorter is usually a refusal or
                             a truncation, not a concise win
  * structural agreement   — if the cloud returned JSON, is the local reply also
                             valid JSON with the same top-level keys? For the
                             prefilter/classification work that IS the contract,
                             and it is objective.
  * verdict agreement      — for yes/no or category answers, do they match

It does NOT try to score "which answer is better". An LLM grading LLM output is
a hall of mirrors, and the honest version of that question needs Buddy reading a
sample. This produces the sample.

USAGE
    local_bench.py --list                     what prompt sets are available
    local_bench.py --set prefilter --n 10     run 10 prompts through both
    local_bench.py --set prefilter --local-only   no cloud calls, no spend

Nothing here writes to the KB, sends mail, or changes routing. It is read-only
against the system and additive-only in what it produces (a report file).
"""
import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

REPO = os.path.dirname(os.path.abspath(__file__))
CREDS = os.path.join(REPO, "config", "credentials.json")
OUT_DIR = os.path.join(REPO, "out")
OLLAMA = "http://127.0.0.1:11434"


def load_creds():
    try:
        return json.load(open(CREDS))
    except Exception as e:
        print(f"!! cannot read credentials: {e}")
        return {}


def local_models():
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=10) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except Exception:
        return []


def call_local(model, system, user, max_tokens=2048, timeout=300):
    """Returns (text, seconds, error). Never raises — a failure is a datapoint."""
    body = json.dumps({
        "model": model,
        "stream": False,
        "options": {"num_predict": max_tokens},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
        return d.get("message", {}).get("content", ""), time.time() - t0, None
    except Exception as e:
        return "", time.time() - t0, str(e)[:200]


def call_cloud(provider, system, user, creds, max_tokens=2048):
    """Returns (text, seconds, error). Uses the box's own provider module."""
    sys.path.insert(0, REPO)
    try:
        import llm_providers
    except Exception as e:
        return "", 0.0, f"cannot import llm_providers: {e}"
    t0 = time.time()
    try:
        return llm_providers.call(provider, system, user, creds, max_tokens), time.time() - t0, None
    except Exception as e:
        return "", time.time() - t0, str(e)[:200]


# ── prompt sets ─────────────────────────────────────────────────────────────
# These mirror the SHAPE of the real cheap-tier work: short, structured,
# high-volume judgments. Deliberately not the council's long-form generation —
# that is where a frontier model most likely still earns its cost, and pretending
# otherwise would be the kind of flattering benchmark that proves nothing.
SETS = {
    "prefilter": {
        "why": "business_idea_scan.py's per-email keep/drop judgment — 312 calls/wk shape",
        "system": ("You classify newsletter emails for a business-opportunity scanner. "
                   "Reply ONLY with JSON: {\"keep\": true|false, \"reason\": \"<10 words\"}"),
        "prompts": [
            "Subject: The 5 AI tools I use daily\nFrom: a productivity newsletter\nBody: A roundup of consumer AI apps with affiliate links.",
            "Subject: USPTO opens bulk trademark data API\nFrom: IP law weekly\nBody: The patent office published a free bulk download endpoint for filings.",
            "Subject: Our Series B\nFrom: a startup mailing list\nBody: We raised $40M to build AI sales agents.",
            "Subject: State contractor boards now publish license data as CSV\nFrom: construction trade digest\nBody: Twelve states standardised public registry exports.",
            "Subject: Weekend reading\nFrom: a general tech newsletter\nBody: Seven long-form links about culture and technology.",
        ],
    },
    "extract": {
        "why": "extract_actions.py's action-item pull — structured extraction from prose",
        "system": ("Extract action items. Reply ONLY with JSON: "
                   "{\"actions\": [\"...\"]}. Empty list if none."),
        "prompts": [
            "The team agreed Bob will send the contract Friday and Sue will review the pricing model before the board call.",
            "Nothing was decided; we will revisit next quarter.",
            "Install the new certificate before it expires on the 30th, and notify the two client contacts once it is live.",
        ],
    },
    "classify": {
        "why": "routing/triage shape — single-label answers, the cheapest tier",
        "system": "Reply with exactly one word from: infrastructure, client, research, noise.",
        "prompts": [
            "The backup drive failed to mount after a reboot.",
            "Alyssa asked for a fourth-grade version of the podcast digest.",
            "New paper on speculative decoding for local inference.",
            "Unsubscribe confirmation from a mailing list.",
        ],
    },
}


def structural_match(cloud_text, local_text):
    """Do both parse as JSON with the same top-level keys? (None = not JSON work)"""
    def parse(t):
        t = (t or "").strip()
        if t.startswith("```"):
            t = t.split("```")[1] if "```" in t[3:] else t.strip("`")
            t = t[4:] if t.lower().startswith("json") else t
        try:
            return json.loads(t.strip())
        except Exception:
            return None
    c, l = parse(cloud_text), parse(local_text)
    if c is None:
        return None                      # cloud did not return JSON; not applicable
    if l is None:
        return False                     # cloud did, local did not — a real failure
    return set(c.keys()) == set(l.keys()) if isinstance(c, dict) and isinstance(l, dict) else True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", dest="setname", default="prefilter")
    ap.add_argument("--model", default=None, help="local model (default: largest qwen present)")
    ap.add_argument("--cloud", default="anthropic")
    ap.add_argument("--local-only", action="store_true", help="no cloud calls, no spend")
    ap.add_argument("--n", type=int, default=0, help="limit prompts")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    if a.list:
        print("prompt sets:")
        for k, v in SETS.items():
            print(f"  {k:12} {len(v['prompts'])} prompts — {v['why']}")
        print("\nlocal models present:", ", ".join(local_models()) or "(none — is ollama running?)")
        return 0

    have = local_models()
    if not have:
        print("!! no local models reachable at " + OLLAMA)
        return 1
    model = a.model or next((m for m in ("qwen2.5:72b", "qwen2.5:14b", "llama3.2:3b") if m in have), have[0])

    s = SETS.get(a.setname)
    if not s:
        print(f"!! unknown set {a.setname!r}; try --list")
        return 2
    prompts = s["prompts"][:a.n] if a.n else s["prompts"]
    creds = {} if a.local_only else load_creds()

    print(f"set        : {a.setname} — {s['why']}")
    print(f"local model: {model}")
    print(f"cloud      : {'(skipped)' if a.local_only else a.cloud}")
    print(f"prompts    : {len(prompts)}\n")

    rows, lat = [], []
    for i, p in enumerate(prompts, 1):
        lt, lsec, lerr = call_local(model, s["system"], p, timeout=300)
        lat.append(lsec)
        if a.local_only:
            ct, csec, cerr = "", 0.0, None
        else:
            ct, csec, cerr = call_cloud(a.cloud, s["system"], p, creds)
        struct = None if a.local_only else structural_match(ct, lt)
        rows.append(dict(n=i, prompt=p[:60], local=lt.strip()[:200], cloud=ct.strip()[:200],
                         lsec=round(lsec, 1), csec=round(csec, 1),
                         lerr=lerr, cerr=cerr, struct=struct))
        mark = {True: "match", False: "MISMATCH", None: "-"}[struct]
        print(f"  [{i}] local {lsec:5.1f}s  cloud {csec:5.1f}s  structure: {mark}")
        if lerr:
            print(f"      !! local error: {lerr}")
        if cerr:
            print(f"      !! cloud error: {cerr}")

    ok = [r for r in rows if not r["lerr"]]
    struct_rows = [r for r in rows if r["struct"] is not None]
    print("\n== summary ==")
    print(f"  local answered      : {len(ok)}/{len(rows)}")
    if lat:
        print(f"  local latency       : median {statistics.median(lat):.1f}s  max {max(lat):.1f}s")
    if struct_rows:
        m = sum(1 for r in struct_rows if r["struct"])
        print(f"  structural agreement: {m}/{len(struct_rows)}"
              "   (cloud returned JSON; did local return the same shape?)")
    print("\n  NOTE: this does not score which answer is BETTER. That needs a human")
    print("        reading the pairs below — which is what the report file is for.")

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(OUT_DIR, f"local-bench-{a.setname}-{stamp}.json")
    with open(path, "w") as f:
        json.dump(dict(set=a.setname, model=model, cloud=a.cloud, rows=rows), f, indent=2)
    print(f"\n  pairs written to: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
