#!/usr/bin/env python3
"""bench_llm.py — repeatable LLM throughput benchmark for CUMULUS (S115).

Buddy: "can we use the same tools ... to test our throughput ... where we can
save the results and compare after we make scheduled enhancements or adjustments
to our settings?"

WHAT IT MEASURES, AND WHY THIS WAY
----------------------------------
Ziskind's central practical finding is that SINGLE-REQUEST tokens/sec is the
wrong benchmark for this hardware: the Spark's throughput multiplies under
concurrency. Our own jobs are the proof of the problem -- halftime-routing makes
49 model calls ONE AT A TIME and takes 59 minutes. So this sweeps CONCURRENCY,
not just raw speed.

Timings come from the server (`eval_count`/`eval_duration`,
`prompt_eval_count`/`prompt_eval_duration`), not from wall-clock guessing.

THREE THINGS IT REFUSES TO GET WRONG
------------------------------------
1. WARM-UP. A cold model reports `load_duration` inside `total_duration` -- 8.2s
   of a 9.0s call, measured 2026-09-07. Benchmarking that measures disk, not the
   GPU. One discarded request per model before anything is recorded.
2. CONCURRENCY THAT DID NOT HAPPEN. If the server serialises requests (the
   default when OLLAMA_NUM_PARALLEL is unset -- which is our current state),
   "concurrency 8" silently becomes 8 sequential calls and the throughput number
   is a lie. So it computes an OVERLAP RATIO (summed server time / wall time)
   and flags any level where the requests did not actually overlap.
3. FAILURES ARE NEVER AVERAGED AWAY. A level with errors reports them; it does
   not quietly mean over the survivors.

    python3 bench_llm.py --label before-parallel
    python3 bench_llm.py --compare bench/<a>.json bench/<b>.json
    python3 bench_llm.py --selftest
"""

import argparse
import json
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "bench"
ENDPOINT = os.environ.get("BENCH_ENDPOINT", "http://localhost:11434/api/generate")
DEFAULT_LEVELS = [1, 2, 4, 8, 16]

# Fixed, representative of what our jobs actually send: a chunk of source text
# and an extraction instruction. Held constant so runs are comparable.
PROMPT = (
    "Read the notes below and list every named performer, one per line.\n\n"
    "NOTES: The halftime slot featured a BMX freestyle team from Ohio, a "
    "pyrotechnics vendor contracted through the stadium, and a projection "
    "mapping company. The drone display was cancelled. A local drumline "
    "opened the second half.\n\nANSWER:"
)


def _post(payload, timeout=300):
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def one_call(model, num_predict, results, idx, runner=None):
    """One request. Records server-side timings, or the error."""
    call = runner or _post
    t0 = time.time()
    try:
        d = call({"model": model, "prompt": PROMPT, "stream": False,
                  "options": {"num_predict": num_predict}})
        results[idx] = {
            "ok": True,
            "wall_s": time.time() - t0,
            "gen_tokens": d.get("eval_count") or 0,
            "gen_s": (d.get("eval_duration") or 0) / 1e9,
            "prefill_tokens": d.get("prompt_eval_count") or 0,
            "prefill_s": (d.get("prompt_eval_duration") or 0) / 1e9,
            "load_s": (d.get("load_duration") or 0) / 1e9,
        }
    except Exception as e:
        results[idx] = {"ok": False, "wall_s": time.time() - t0,
                        "error": f"{type(e).__name__}: {str(e)[:120]}"}


def summarise(rows, wall_s):
    """-> dict. Never averages over failures, and detects fake concurrency."""
    ok = [r for r in rows if r.get("ok")]
    bad = [r for r in rows if not r.get("ok")]
    if not ok:
        return {"requests": len(rows), "failed": len(bad), "usable": 0,
                "note": "every request failed — no number is reported"}
    gen_tok = sum(r["gen_tokens"] for r in ok)
    gen_s = sum(r["gen_s"] for r in ok)
    pre_tok = sum(r["prefill_tokens"] for r in ok)
    pre_s = sum(r["prefill_s"] for r in ok)
    server_s = sum(r["wall_s"] for r in ok)
    # If the server ran them one after another, summed time ~= wall time and the
    # ratio is ~1. Real concurrency pushes it toward the request count.
    #
    # S115: overlap ALONE is not enough, and the first live run proved it.
    # With OLLAMA_NUM_PARALLEL=4 the overlap ratio reached 4.5x while aggregate
    # throughput stayed FLAT at ~30 tok/s -- the requests were interleaved but
    # the machine did no more work per second. That is queueing dressed as
    # concurrency, and the flag called it "concurrent". The caller needs the
    # THROUGHPUT question answered, so it is computed here and compared against
    # the single-request rate by the caller.
    overlap = (server_s / wall_s) if wall_s > 0 else 0.0
    return {
        "requests": len(rows), "failed": len(bad), "usable": len(ok),
        "wall_s": round(wall_s, 2),
        "aggregate_gen_tok_s": round(gen_tok / wall_s, 1) if wall_s else None,
        "per_request_gen_tok_s": round(gen_tok / gen_s, 1) if gen_s else None,
        "prefill_tok_s": round(pre_tok / pre_s, 1) if pre_s else None,
        "median_latency_s": round(statistics.median(r["wall_s"] for r in ok), 2),
        "overlap_ratio": round(overlap, 2),
        "concurrency_real": overlap >= 1.5 or len(rows) == 1,
        "errors": [r["error"] for r in bad][:3],
    }


def env_value(raw):
    """Value from a `systemctl show -p Environment` line.

    It returns "KEY=VALUE"; keeping the whole string made a saved result read
    `OLLAMA_NUM_PARALLEL=OLLAMA_NUM_PARALLEL=4`. These files are the permanent
    record of what a measurement was taken under, so the field has to be clean.
    """
    raw = (raw or "").strip().strip('"')
    if not raw:
        return "unset"
    return raw.split("=", 1)[-1].strip('"') or "unset"


def environment(model):
    def sh(cmd):
        try:
            return subprocess.run(cmd, shell=True, capture_output=True,
                                  text=True, timeout=15).stdout.strip()
        except Exception:
            return ""
    return {
        "host": socket.gethostname(),
        "model": model,
        "when": datetime.now().isoformat(timespec="seconds"),
        "ollama_version": sh("ollama --version 2>/dev/null | head -1"),
        # WITHOUT these two, comparing runs is meaningless -- the whole point is
        # to see what a settings change did.
        # `systemctl show` returns "KEY=VALUE"; keeping the whole string made a
        # saved result read "OLLAMA_NUM_PARALLEL=OLLAMA_NUM_PARALLEL=4". Take
        # the value only -- these files are the permanent record of what a
        # measurement was taken under.
        "OLLAMA_NUM_PARALLEL": os.environ.get("OLLAMA_NUM_PARALLEL")
            or env_value(sh("systemctl show ollama.service -p Environment"
                            " --value | tr ' ' '\\n'"
                            " | grep OLLAMA_NUM_PARALLEL")),
        "endpoint": ENDPOINT,
    }


def run(model, levels, num_predict, label, runner=None, warmup=True):
    if warmup:
        w = {}
        one_call(model, 8, w, 0, runner)      # discard: pays load_duration
    out = {"label": label, "env": environment(model),
           "num_predict": num_predict, "levels": {}}
    for n in levels:
        rows, threads = {}, []
        t0 = time.time()
        for i in range(n):
            t = threading.Thread(target=one_call,
                                 args=(model, num_predict, rows, i, runner))
            t.start(); threads.append(t)
        for t in threads:
            t.join()
        out["levels"][str(n)] = summarise([rows[k] for k in sorted(rows)],
                                          time.time() - t0)
    return out


def scaling_verdict(levels):
    """Did aggregate throughput actually RISE with concurrency?

    The question the benchmark exists to answer. Overlap says requests were
    interleaved; this says whether that bought anything. Measured 2026-09-07:
    overlap 4.5x, throughput +0% -- interleaved and worthless.
    """
    base = levels.get("1", {}).get("aggregate_gen_tok_s")
    out = {}
    for n, s in levels.items():
        agg = s.get("aggregate_gen_tok_s")
        if base in (None, 0) or agg is None or n == "1":
            out[n] = None
        else:
            out[n] = round((agg - base) / base * 100, 1)
    return out


def render(rep):
    e = rep["env"]
    L = [f"== bench {rep.get('label','')} — {e['host']} — {e['when']} ==",
         f"   model {e['model']}   OLLAMA_NUM_PARALLEL={e['OLLAMA_NUM_PARALLEL']}"
         f"   num_predict={rep['num_predict']}", ""]
    gain = scaling_verdict(rep["levels"])
    L.append("   conc  agg tok/s  per-req  prefill  median s  overlap  vs conc-1")
    for n, s in sorted(rep["levels"].items(), key=lambda kv: int(kv[0])):
        if not s.get("usable"):
            L.append(f"   {n:>4}  ALL {s['requests']} REQUESTS FAILED — no number")
            continue
        flag = "" if s["concurrency_real"] else "  <-- NOT concurrent"
        g = gain.get(n)
        gs = "  base" if g is None else f"{g:+6.0f}%"
        L.append("   {:>4}  {:>9}  {:>7}  {:>7}  {:>8}  {:>6}x  {}{}".format(
            n, s["aggregate_gen_tok_s"], s["per_request_gen_tok_s"],
            s["prefill_tok_s"], s["median_latency_s"], s["overlap_ratio"],
            gs, flag))
        if s["failed"]:
            L.append(f"         {s['failed']} of {s['requests']} FAILED: "
                     f"{s['errors'][0][:70]}")
    _g = [v for v in gain.values() if v is not None]
    if _g and max(_g) < 10:
        L += ["", "   ⚠️  Concurrency bought NOTHING — aggregate throughput did not",
              "      rise above the single-request rate at any level. Requests",
              "      were interleaved but the machine did no more work per",
              "      second. Generation here is memory-bandwidth bound, and",
              "      ollama's NUM_PARALLEL does not change that; only kernel-",
              "      level batching (vLLM) or a smaller/quantised model would."]
    if any(not s.get("concurrency_real", True) for s in rep["levels"].values()):
        L += ["", "   ⚠️  A level marked NOT concurrent means the server ran the",
              "      requests one after another. The aggregate figure there is",
              "      not a concurrency result. Check OLLAMA_NUM_PARALLEL."]
    return "\n".join(L)


def compare(a, b):
    ra, rb = json.loads(Path(a).read_text()), json.loads(Path(b).read_text())
    L = [f"== {ra.get('label','A')}  ->  {rb.get('label','B')} ==",
         f"   {ra['env']['when']}  ->  {rb['env']['when']}",
         f"   NUM_PARALLEL {ra['env']['OLLAMA_NUM_PARALLEL']}"
         f" -> {rb['env']['OLLAMA_NUM_PARALLEL']}"]
    if ra["env"]["model"] != rb["env"]["model"]:
        L.append(f"   ⚠️  DIFFERENT MODELS ({ra['env']['model']} vs "
                 f"{rb['env']['model']}) — not a like-for-like comparison")
    L.append("")
    L.append("   conc     before      after     change")
    for n in sorted(set(ra["levels"]) & set(rb["levels"]), key=int):
        x = ra["levels"][n].get("aggregate_gen_tok_s")
        y = rb["levels"][n].get("aggregate_gen_tok_s")
        if x is None or y is None:
            L.append(f"   {n:>4}  {'n/a':>9}  {'n/a':>9}   (a level had no usable result)")
            continue
        pct = ((y - x) / x * 100) if x else 0
        L.append(f"   {n:>4}  {x:>9}  {y:>9}   {pct:+6.0f}%")
    return "\n".join(L)


def selftest():
    bad = 0

    def ck(label, cond):
        nonlocal bad
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        if not cond:
            bad += 1

    def fake(delay=0.05, gen=20, pre=100):
        def _r(payload):
            time.sleep(delay)
            return {"eval_count": gen, "eval_duration": int(delay * 1e9),
                    "prompt_eval_count": pre,
                    "prompt_eval_duration": int(delay * 1e9),
                    "load_duration": 0}
        return _r

    # Fake concurrency is the failure this tool exists to prevent.
    seq = summarise([{"ok": True, "wall_s": 1.0, "gen_tokens": 10, "gen_s": 1.0,
                      "prefill_tokens": 5, "prefill_s": 0.1, "load_s": 0}
                     for _ in range(4)], wall_s=4.0)
    ck("4 requests taking 4s wall = SERIALISED, flagged not-concurrent",
       seq["overlap_ratio"] == 1.0 and seq["concurrency_real"] is False)
    par = summarise([{"ok": True, "wall_s": 1.0, "gen_tokens": 10, "gen_s": 1.0,
                      "prefill_tokens": 5, "prefill_s": 0.1, "load_s": 0}
                     for _ in range(4)], wall_s=1.1)
    ck("...while 4 requests in 1.1s wall IS concurrent — the inverse",
       par["overlap_ratio"] > 3 and par["concurrency_real"] is True)
    ck("a single request is never flagged as fake concurrency",
       summarise([{"ok": True, "wall_s": 1.0, "gen_tokens": 10, "gen_s": 1.0,
                   "prefill_tokens": 5, "prefill_s": 0.1, "load_s": 0}],
                 wall_s=1.0)["concurrency_real"] is True)

    # Failures must never be averaged away.
    mixed = summarise([{"ok": True, "wall_s": 1.0, "gen_tokens": 10, "gen_s": 1.0,
                        "prefill_tokens": 5, "prefill_s": 0.1, "load_s": 0},
                       {"ok": False, "wall_s": 0.1, "error": "boom"}], wall_s=1.0)
    ck("a failed request is reported, not silently dropped",
       mixed["failed"] == 1 and mixed["usable"] == 1 and mixed["errors"])
    allbad = summarise([{"ok": False, "wall_s": 0.1, "error": "boom"}] * 3,
                       wall_s=0.3)
    ck("when EVERY request fails, NO throughput number is reported",
       allbad["usable"] == 0 and "aggregate_gen_tok_s" not in allbad)
    ck("...and that renders as a loud line, not a blank row",
       "ALL 3 REQUESTS FAILED" in render(
           {"label": "x", "num_predict": 8, "levels": {"3": allbad},
            "env": {"host": "h", "model": "m", "when": "t",
                    "OLLAMA_NUM_PARALLEL": "unset"}}))

    # Warm-up: the discarded call must not appear in the results.
    calls = []
    def counting(payload):
        calls.append(payload["options"]["num_predict"])
        return fake()(payload)
    rep = run("m", [1], 32, "t", runner=counting, warmup=True)
    ck("warm-up fires and is EXCLUDED from the results",
       calls[0] == 8 and len(calls) == 2 and rep["levels"]["1"]["requests"] == 1)
    calls.clear()
    run("m", [1], 32, "t", runner=counting, warmup=False)
    ck("...and can be turned off", len(calls) == 1)

    # A real end-to-end sweep against the fake server.
    rep = run("m", [1, 4], 16, "e2e", runner=fake(), warmup=False)
    ck("a sweep records every level", set(rep["levels"]) == {"1", "4"})
    ck("...and captures the settings a comparison depends on",
       "OLLAMA_NUM_PARALLEL" in rep["env"] and rep["env"]["model"] == "m")

    ck("env_value takes the VALUE, not the whole KEY=VALUE pair",
       env_value("OLLAMA_NUM_PARALLEL=4") == "4")
    ck("...an empty reading is 'unset', never a blank field",
       env_value("") == "unset" and env_value(None) == "unset")
    ck("...quotes from systemd are stripped",
       env_value('"OLLAMA_NUM_PARALLEL=8"') == "8")
    ck("...and a bare value is passed through unharmed",
       env_value("4") == "4")

    # scaling_verdict — the question the whole tool exists to answer, and the
    # one the first live run got wrong: overlap 4.5x with throughput FLAT.
    flat = {"1": {"aggregate_gen_tok_s": 30.0}, "4": {"aggregate_gen_tok_s": 29.0},
            "8": {"aggregate_gen_tok_s": 30.0}}
    v = scaling_verdict(flat)
    ck("flat throughput scores ~0% gain, however much requests overlapped",
       v["1"] is None and abs(v["4"]) < 5 and abs(v["8"]) < 5)
    real = {"1": {"aggregate_gen_tok_s": 30.0}, "4": {"aggregate_gen_tok_s": 90.0}}
    ck("...while genuine scaling scores +200% — the inverse",
       scaling_verdict(real)["4"] == 200.0)
    ck("a missing baseline yields None, not a fabricated percentage",
       scaling_verdict({"4": {"aggregate_gen_tok_s": 90.0}})["4"] is None)
    ck("a level with no usable result yields None rather than 0%",
       scaling_verdict({"1": {"aggregate_gen_tok_s": 30.0},
                        "4": {}})["4"] is None)
    env = {"host": "h", "model": "m", "when": "t", "OLLAMA_NUM_PARALLEL": "4"}
    flat_full = {k: dict(v, per_request_gen_tok_s=30, prefill_tok_s=400,
                         median_latency_s=4, overlap_ratio=4.5, usable=1,
                         failed=0, requests=1, concurrency_real=True)
                 for k, v in flat.items()}
    ck("a flat result says CONCURRENCY BOUGHT NOTHING, loudly",
       "bought NOTHING" in render({"label": "x", "num_predict": 8,
                                   "levels": flat_full, "env": env}))
    real_full = {k: dict(v, per_request_gen_tok_s=30, prefill_tok_s=400,
                         median_latency_s=4, overlap_ratio=4.5, usable=1,
                         failed=0, requests=1, concurrency_real=True)
                 for k, v in real.items()}
    ck("...and a genuinely scaling result does NOT — or the warning is noise",
       "bought NOTHING" not in render({"label": "x", "num_predict": 8,
                                       "levels": real_full, "env": env}))

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        a, b = Path(td) / "a.json", Path(td) / "b.json"
        mk = lambda agg, m="m": {
            "label": "L", "num_predict": 8,
            "env": {"host": "h", "model": m, "when": "t",
                    "OLLAMA_NUM_PARALLEL": "unset"},
            "levels": {"4": {"aggregate_gen_tok_s": agg, "usable": 4}}}
        a.write_text(json.dumps(mk(10.0))); b.write_text(json.dumps(mk(25.0)))
        ck("compare reports the percentage change", "+150%" in compare(a, b))
        b.write_text(json.dumps(mk(25.0, m="other")))
        ck("...and REFUSES to compare different models silently",
           "DIFFERENT MODELS" in compare(a, b))

    print("\nALL PASS" if not bad else f"\n{bad} FAILED")
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None)
    ap.add_argument("--levels", default=",".join(map(str, DEFAULT_LEVELS)))
    ap.add_argument("--num-predict", type=int, default=128)
    ap.add_argument("--label", default="run")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.compare:
        print(compare(*a.compare)); return 0
    model = a.model
    if not model:
        try:
            creds = json.loads((HERE / "config/credentials.json").read_text())
            model = creds.get("ollama_model")
        except Exception:
            pass
    if not model:
        print("no model: pass --model or set ollama_model in credentials.json")
        return 1
    rep = run(model, [int(x) for x in a.levels.split(",")], a.num_predict, a.label)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    p = OUT_DIR / f"{stamp}-{a.label}.json"
    p.write_text(json.dumps(rep, indent=1))
    print(render(rep))
    print(f"\n   saved: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
