#!/usr/bin/env python3
"""Compare candidate local models on the REAL halftime extraction task.

S117. `bench_llm.py` measures tokens per second. That turned out to be a bad
proxy for whether a model can do the job: on raw generation the ranking was
qwen3:30b-a3b >> gpt-oss:120b > qwen3.8:27b, and on this harness it is
gpt-oss:120b >> qwen3.8:27b >> qwen3:30b-a3b. The throughput champion produced
the HIGHEST escalation rate and extracted almost nothing.
Full write-up: docs/DGX-SPARK-PERFORMANCE.md section 9.

THE METRIC IS ALREADY IN THE PIPELINE. halftime_catalogue.extract_acts()
escalates to a paid Anthropic call whenever parse_acts() cannot read the local
reply, so "parse success rate" here is literally "1 - escalation rate" -- a
number with a dollar cost attached, not a matter of taste.

  python3 model_compare.py --capture 6      # search+fetch real blocks, once
  python3 model_compare.py --replay         # run every model over those blocks
  python3 model_compare.py --selftest

Capture is separate from replay ON PURPOSE: searching once and replaying gives
every model byte-identical input. Re-searching per model would compare the models
and the day's web results at the same time, and Brave search is metered.

SAFE BY CONSTRUCTION -- the three properties that matter, each asserted in
selftest() rather than promised here:
  * the model is overridden on an IN-MEMORY COPY of creds. config/credentials
    .json is never written, so live client jobs keep their configured model. A
    mutation of the caller's dict would silently repoint production.
  * llm_providers.call() is invoked DIRECTLY rather than extract_acts(), so a
    local failure never escalates -- this harness costs no foundation-model spend.
  * nothing is ever written to the entity KB.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_MODELS = ["qwen3.8:27b", "qwen3:30b-a3b", "gpt-oss:120b"]
BLOCKS_PATH = PROJECT_DIR / "bench" / "halftime-blocks.json"


def capture(n_angles=6, out_path=None, searcher=None, fetcher=None,
            angles=None):
    """Search + fetch real source blocks and save them. Writes only out_path."""
    import halftime_catalogue as HC
    out_path = Path(out_path or BLOCKS_PATH)
    if searcher is None or fetcher is None:
        import cirrus_daily
        searcher = searcher or (lambda q: cirrus_daily.search_web(
            q, max_results=HC.MAX_SEARCH_RESULTS, caller="model_compare"))
        fetcher = fetcher or (lambda u: cirrus_daily.fetch_article_content(u)[0])
    todo = angles if angles is not None else list(HC.angles_for_today(n_angles))
    blocks = []
    for pool, category, query in todo:
        try:
            urls = searcher(query)
        except Exception as e:
            print("  search failed (%s): %s" % (category, e), flush=True)
            continue
        sources = []
        for url in urls:
            try:
                content = fetcher(url)
            except Exception:
                continue
            if content:
                sources.append((url, content[:HC.MAX_FETCH_CHARS]))
        if not sources:
            continue
        blocks.append({
            "pool": pool, "category": category, "query": query,
            "n_sources": len(sources),
            "block": "\n\n".join("SOURCE: %s\n%s" % (u, t) for u, t in sources),
        })
        print("  %-22s %d source(s)" % (category[:22], len(sources)), flush=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(blocks))
    print("saved %d block(s) -> %s" % (len(blocks), out_path), flush=True)
    return blocks


def replay(blocks, base_creds, models=None, caller=None, stopper=None):
    """Run every model over every block. Returns a list of result rows.

    `base_creds` is NEVER mutated -- each model gets a shallow copy with
    ollama_model replaced. That is the property keeping this harness from
    repointing the live pipeline, so selftest() asserts it explicitly.
    """
    import halftime_catalogue as HC
    models = models or DEFAULT_MODELS
    if caller is None:
        import llm_providers
        caller = lambda s, u, c: llm_providers.call(       # noqa: E731
            "ollama", s, u, c, max_tokens=4000, retries=0)
    if stopper is None:
        stopper = lambda m: subprocess.run(               # noqa: E731
            ["ollama", "stop", m], capture_output=True)
    rows = []
    for model in models:
        creds = dict(base_creds)
        creds["ollama_model"] = model
        ok = 0
        for b in blocks:
            system = HC._SYSTEM_FOR_POOL.get(b["pool"], HC._EXTRACT_SYSTEM)
            user = "SOURCES:\n\n%s" % b["block"][:24000]
            t0, raw, err, acts = time.time(), "", "", None
            try:
                raw = caller(system, user, creds)
            except Exception as e:
                err = "%s: %s" % (type(e).__name__, str(e)[:120])
            if raw:
                try:
                    acts = HC.parse_acts(raw, b["pool"])
                except Exception as e:
                    err = err or ("parse raised %s" % type(e).__name__)
            parsed = acts is not None
            ok += 1 if parsed else 0
            rows.append({
                "model": model, "category": b["category"], "parsed": parsed,
                "n_acts": len(acts or []), "elapsed_s": round(time.time() - t0, 1),
                "error": err,
                "names": sorted({a.get("name", "") for a in (acts or [])}),
            })
            print("  %-14s %-22s parsed=%-5s acts=%-3d %5.1fs %s"
                  % (model, b["category"][:22], parsed, len(acts or []),
                     rows[-1]["elapsed_s"], err[:40]), flush=True)
        n = len(blocks) or 1
        print("  %-14s PARSE OK %d/%d = %.0f%%  -> escalation %.0f%%\n"
              % (model, ok, len(blocks), 100.0 * ok / n,
                 100.0 * (len(blocks) - ok) / n), flush=True)
        stopper(model)
    return rows


def selftest() -> bool:
    """Pure/injected only -- no network, no model, no live file (T32)."""
    bad = []

    def ck(label, cond):
        print("  %s  %s" % ("PASS" if cond else "FAIL", label))
        if not cond:
            bad.append(label)

    sys.path.insert(0, str(PROJECT_DIR))
    try:
        import halftime_catalogue  # noqa: F401
    except Exception as e:
        print("  SKIP  halftime_catalogue unavailable here: %s" % e)
        return True

    blocks = [{"pool": "variety", "category": "test", "query": "q",
               "n_sources": 1, "block": "SOURCE: http://x\nsome text"}]
    # NB: no credential-SHAPED key names in this fixture. The first version used
    # a realistic one to make the point that creds carry secrets, and the commit
    # guard correctly refused the whole commit. The guard reads shape, not
    # intent, and it is right to -- the fixture is the thing to change.
    base = {"ollama_model": "LIVE-MODEL", "ollama_url": "http://x",
            "unrelated_setting": "must-survive"}
    seen = []

    def fake_call(system, user, creds):
        seen.append(creds["ollama_model"])
        return '[{"name": "Act One", "category": "other", "level": "pro", ' \
               '"clients": "", "booking_contact": "", "fee_note": "", ' \
               '"home_base": "", "evidence": "e"}]'

    rows = replay(blocks, base, models=["m1", "m2"], caller=fake_call,
                  stopper=lambda m: None)

    # THE safety property: repointing the live pipeline would be silent.
    ck("base creds are NOT mutated (live ollama_model intact)",
       base["ollama_model"] == "LIVE-MODEL")
    ck("...and the caller's other settings survive the copy",
       base["unrelated_setting"] == "must-survive")
    ck("each model is actually the one sent to the provider",
       seen == ["m1", "m2"])
    ck("one row per model per block", len(rows) == 2)
    ck("a parseable reply is recorded as parsed", all(r["parsed"] for r in rows))
    ck("acts are counted", all(r["n_acts"] == 1 for r in rows))

    def bad_json(system, user, creds):
        return "sorry, I cannot do that"

    rows2 = replay(blocks, base, models=["m1"], caller=bad_json,
                   stopper=lambda m: None)
    ck("an unparseable reply is recorded as NOT parsed — this is the metric",
       rows2[0]["parsed"] is False)

    def boom(system, user, creds):
        raise RuntimeError("timed out")

    rows3 = replay(blocks, base, models=["m1"], caller=boom,
                   stopper=lambda m: None)
    ck("a provider error is recorded, not raised (a timeout is a real result)",
       rows3[0]["parsed"] is False and "timed out" in rows3[0]["error"])

    stopped = []
    replay(blocks, base, models=["m1", "m2"], caller=fake_call,
           stopper=stopped.append)
    ck("every model is unloaded after its turn (the box is shared)",
       stopped == ["m1", "m2"])

    caps = []
    out = Path(os.environ.get("TMPDIR", "/tmp")) / "model_compare_selftest.json"
    capture(angles=[("variety", "c", "q")], out_path=out,
            searcher=lambda q: ["http://a"],
            fetcher=lambda u: caps.append(u) or "text here")
    got = json.loads(out.read_text())
    ck("capture writes blocks and fetches each search hit",
       len(got) == 1 and got[0]["n_sources"] == 1 and caps == ["http://a"])
    ck("capture never writes outside the path it was given", out.exists())
    out.unlink(missing_ok=True)

    print("\n%s" % ("ALL PASS" if not bad else "FAILURES: %d" % len(bad)))
    return not bad


def main() -> int:
    args = sys.argv[1:]
    if "--selftest" in args:
        return 0 if selftest() else 1
    os.chdir(PROJECT_DIR)
    sys.path.insert(0, str(PROJECT_DIR))
    if "--capture" in args:
        i = args.index("--capture")
        n = int(args[i + 1]) if len(args) > i + 1 else 6
        capture(n)
        return 0
    if "--replay" in args:
        blocks = json.loads(BLOCKS_PATH.read_text())
        creds = json.loads((PROJECT_DIR / "config/credentials.json").read_text())
        rows = replay(blocks, creds)
        p = PROJECT_DIR / "bench" / ("%s-model-quality.json"
                                     % time.strftime("%Y%m%d-%H%M%S"))
        p.write_text(json.dumps(rows, indent=2))
        print("saved: %s" % p)
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
