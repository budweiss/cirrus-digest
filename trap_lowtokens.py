#!/usr/bin/env python3
"""trap_lowtokens.py — S75. Mechanical half of T26 in docs/TOOLING-TRAPS.md.

A liveness/health probe that calls an LLM with a tiny `max_tokens` gets an
EMPTY string back from a reasoning-first model (DeepSeek V4 emits nothing at
max_tokens=5 and "OK" from 20 up, measured on CIRRUS 2026-08-24). The call
SUCCEEDS, so nothing raises; the caller sees empty text and calls it a failure.
That is how com.cirrus.modelhealth failed every night against a healthy
provider.

Uses the AST rather than grep, for two reasons:
  * the keyword is routinely on a continuation line (model_health.py had it
    three lines below the call), which a line-oriented grep misses entirely;
  * `def f(..., max_tokens=0)` stubs in ensemble.py are DEFAULTS, not calls,
    and flagging those would be crying wolf (T9).

Emits one line per hit:  T26:<path>:<line>:<message>
READ-ONLY. Exit status is always 0 — the caller counts the lines.
"""
import ast
import sys
from pathlib import Path

MIN_TOKENS = 20          # the measured boundary; below this is unsafe
CALL_NAMES = {"call", "escalate"}


def hits(path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        # match <something>.call(...) / <something>.escalate(...) — the shape
        # llm_providers is always reached through, whatever it is aliased to.
        if not (isinstance(f, ast.Attribute) and f.attr in CALL_NAMES):
            continue
        for kw in node.keywords:
            if kw.arg != "max_tokens":
                continue
            v = kw.value
            if isinstance(v, ast.Constant) and isinstance(v.value, int) \
                    and 0 < v.value < MIN_TOKENS:
                yield (node.lineno, f.attr, v.value)


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    for sub in ("cirrus-repo", "runner", "cirrus"):
        base = root / sub
        if not base.is_dir():
            continue
        for py in sorted(base.rglob("*.py")):
            if "__pycache__" in py.parts:
                continue
            for lineno, attr, val in hits(py):
                rel = py.relative_to(root)
                print(f"T26:{rel}:{lineno}:"
                      f"`{attr}(... max_tokens={val})` — a reasoning-first model "
                      f"(DeepSeek V4) returns an EMPTY string below ~{MIN_TOKENS} "
                      f"tokens, and the call SUCCEEDS, so nothing raises. Use >= 64 "
                      f"for a probe, and never turn an empty reply into an empty "
                      f"error string.")


if __name__ == "__main__":
    main()
