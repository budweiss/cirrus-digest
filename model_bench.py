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
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    suite = json.load(open(args.suite))
    tasks = suite["tasks"]
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = APP_DIR / "bench"
    out_dir.mkdir(exist_ok=True)

    results = {"ts": ts, "num_ctx": args.num_ctx, "models": models, "tasks": []}
    tok_by_model = {m: [] for m in models}
    body_md = []

    for task in tasks:
        name, prompt = task["name"], task["prompt"]
        body_md += [f"## {name}", "", f"_{task.get('desc','')}_", ""]
        tr = {"name": name, "runs": {}}
        for m in models:
            print(f"[{name}] {m} ...", flush=True)
            try:
                r = generate(m, prompt, num_ctx=args.num_ctx)
            except Exception as e:
                r = {"response": f"[ERROR: {e}]", "eval_count": 0,
                     "prompt_tokens": 0, "tok_s": 0.0, "wall_s": 0.0}
            tr["runs"][m] = r
            tok_by_model[m].append(r["tok_s"])
            body_md += [
                f"### `{m}` — {r['tok_s']} tok/s · {r['eval_count']} tok · {r['wall_s']}s",
                "", r["response"], ""]
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
           f"Models: {', '.join('`'+m+'`' for m in models)} · num_ctx={args.num_ctx} · suite={Path(args.suite).name}",
           ""] + summary + ["", "---", ""] + body_md)

    md_path = out_dir / f"bench-{ts}.md"
    md_path.write_text("\n".join(md))
    (out_dir / f"bench-{ts}.json").write_text(json.dumps(results, indent=2))
    print(f"WROTE {md_path}")
    print("\n".join(summary))


if __name__ == "__main__":
    main()
