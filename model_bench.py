#!/usr/bin/env python3
"""
CUMULUS Model Bench — run a fixed prompt suite across one or more local Ollama
models and record output + tokens/sec side by side, so a new LLM can be
validated before we adopt it. Read-only: no digest side effects.

Usage:
  .venv/bin/python model_bench.py --models qwen2.5:14b,qwen3-coder:30b
  .venv/bin/python model_bench.py --models qwen3-coder:30b --suite config/bench_suite.json --num_ctx 8192

Writes bench/bench-<ts>.md (human report) and bench/bench-<ts>.json (raw).
Metrics come straight from Ollama's /api/generate response:
  tok/s = eval_count / (eval_duration seconds)   (pure generation speed)
"""
import argparse
import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
OLLAMA_URL = "http://localhost:11434/api/generate"


def generate(model, prompt, num_ctx=8192, timeout=900):
    """Single non-streaming generation; return text + timing metrics."""
    body = json.dumps({
        "model": model, "prompt": prompt, "stream": False,
        "options": {"num_ctx": num_ctx},
    }).encode()
    req = urllib.request.Request(
        OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        d = json.load(resp)
    wall = time.time() - t0
    eval_count = d.get("eval_count", 0) or 0
    eval_ns = d.get("eval_duration", 0) or 0
    prompt_count = d.get("prompt_eval_count", 0) or 0
    tok_s = round(eval_count / (eval_ns / 1e9), 1) if eval_ns else 0.0
    return {
        "response": (d.get("response") or "").strip(),
        "eval_count": eval_count,
        "prompt_tokens": prompt_count,
        "tok_s": tok_s,
        "wall_s": round(wall, 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True,
                    help="comma-separated Ollama model tags")
    ap.add_argument("--suite", default=str(APP_DIR / "config/bench_suite.json"))
    ap.add_argument("--num_ctx", type=int, default=8192)
    # S92 (Buddy: "run the repeat trials on reason_plan"). The suite ran every
    # task exactly once, so a finding rested on ONE sample of ONE prompt — which
    # is precisely the objection that made these flags necessary before anyone
    # deletes a 47 GB model on the strength of it.
    ap.add_argument("--only", default="",
                    help="comma-separated task names to run (default: all)")
    ap.add_argument("--repeats", type=int, default=1,
                    help="run each task N times; reports every trial plus "
                         "mean and spread, so variance is visible")
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    suite = json.load(open(args.suite))
    tasks = suite["tasks"]
    if args.only:
        want = {t.strip() for t in args.only.split(",") if t.strip()}
        tasks = [t for t in tasks if t["name"] in want]
        missing = want - {t["name"] for t in tasks}
        if missing:
            # Refuse rather than silently benching fewer tasks than asked for.
            # "I ran what I could find" reads identically to "I ran everything".
            raise SystemExit(f"no such task(s) in the suite: {sorted(missing)}")
    if not tasks:
        raise SystemExit("no tasks selected")
    reps = max(1, args.repeats)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = APP_DIR / "bench"
    out_dir.mkdir(exist_ok=True)

    results = {"ts": ts, "num_ctx": args.num_ctx, "models": models, "tasks": []}
    tok_by_model = {m: [] for m in models}
    body_md = []

    for task in tasks:
        name, prompt = task["name"], task["prompt"]
        body_md += [f"## {name}", "", f"_{task.get('desc','')}_", ""]
        tr = {"name": name, "runs": {}, "trials": {}}
        for m in models:
            trials = []
            for i in range(reps):
                print(f"[{name}] {m} trial {i+1}/{reps} ...", flush=True)
                try:
                    r = generate(m, prompt, num_ctx=args.num_ctx)
                except Exception as e:
                    r = {"response": f"[ERROR: {e}]", "eval_count": 0,
                         "prompt_tokens": 0, "tok_s": 0.0, "wall_s": 0.0}
                trials.append(r)
            tr["runs"][m] = trials[0]          # back-compat with existing readers
            tr["trials"][m] = trials
            # ONE number per task per model, or the summary row grows longer
            # than its header and the table silently misaligns.
            _sps = [t["tok_s"] for t in trials]
            tok_by_model[m].append(round(sum(_sps) / len(_sps), 1) if _sps else 0.0)
            if reps == 1:
                r = trials[0]
                body_md += [
                    f"### `{m}` — {r['tok_s']} tok/s · {r['eval_count']} tok · {r['wall_s']}s",
                    "", r["response"], ""]
            else:
                toks = [t["eval_count"] for t in trials]
                walls = [t["wall_s"] for t in trials]
                sps = [t["tok_s"] for t in trials]
                def _mean(xs):
                    return round(sum(xs) / len(xs), 1) if xs else 0.0
                body_md += [
                    f"### `{m}` — {reps} trials · "
                    f"{_mean(sps)} tok/s mean · "
                    f"{_mean(toks)} tok mean (min {min(toks)}, max {max(toks)}) · "
                    f"{_mean(walls)}s mean (min {min(walls)}, max {max(walls)})",
                    ""]
                for i, t in enumerate(trials, 1):
                    body_md += [
                        f"<details><summary>trial {i} — {t['tok_s']} tok/s · "
                        f"{t['eval_count']} tok · {t['wall_s']}s</summary>", "",
                        t["response"], "", "</details>", ""]
        results["tasks"].append(tr)

    # Throughput summary table (goes near the top)
    header = "| model | " + " | ".join(t["name"] for t in tasks) + " | avg |"
    sep = "|" + "---|" * (len(tasks) + 2)
    summary = ["## Throughput summary (tok/s)", "", header, sep]
    for m in models:
        row = tok_by_model[m]
        avg = round(sum(row) / len(row), 1) if row else 0.0
        summary.append(f"| `{m}` | " + " | ".join(str(x) for x in row) + f" | {avg} |")

    md = ([f"# Model Bench — {ts}", "",
           f"Models: {', '.join('`'+m+'`' for m in models)} · num_ctx={args.num_ctx} · "
           f"suite={Path(args.suite).name} · repeats={reps}"
           + (f" · only={args.only}" if args.only else ""),
           ""] + summary + ["", "---", ""] + body_md)

    md_path = out_dir / f"bench-{ts}.md"
    md_path.write_text("\n".join(md))
    (out_dir / f"bench-{ts}.json").write_text(json.dumps(results, indent=2))
    print(f"WROTE {md_path}")
    print("\n".join(summary))


if __name__ == "__main__":
    main()
