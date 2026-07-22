#!/usr/bin/env python3
"""CIRRUS LLM-Routing Watch (S44).

Scans each finished digest for INTELLIGENCE ON HOW PEOPLE/TEAMS DECIDE WHICH LLM to
use for which task — model selection & routing strategy — and accumulates what it
finds into an ever-growing "LLM Routing Playbook". Mirrors extract_actions.py
(read digest -> local Ollama extract -> write file).

Output:
  * <output_dir>/llm-routing-playbook.md   — the growing, deduped playbook (dated,
    sourced).  Readable via /read/digest/llm-routing-playbook.md.
  * a short "🔀 LLM Routing Watch" note appended to the digest file (the digest note).

SAFE BY DESIGN: watch_latest() never raises — the digest/email pipeline can call it
without any risk. Additive only; changes nothing about how the digest is built.
"""

import json
import re
import requests
from datetime import datetime
from pathlib import Path

CONFIG_PATH = Path.home() / "projects/cirrus-digest/config/sources.json"
with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

DIGEST_CFG  = CONFIG["digest"]
OUTPUT_DIR  = Path(DIGEST_CFG["output_dir"])
MODEL       = DIGEST_CFG["ollama_model"]
OLLAMA_HOST = DIGEST_CFG["ollama_host"]
PLAYBOOK    = OUTPUT_DIR / "llm-routing-playbook.md"

MAX_CHUNK_CHARS = 5500
MAX_CHUNKS = 10


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}")


def ollama_extract(prompt):
    try:
        r = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": MODEL, "prompt": prompt, "stream": False,
                  "options": {"num_ctx": 8192}},
            timeout=120,
        )
        r.raise_for_status()
        return r.json().get("response", "").strip()
    except Exception as e:
        return f"[Extraction error: {e}]"


def split_digest_chunks(content, max_chunk=MAX_CHUNK_CHARS):
    blocks = content.split("\n---\n")
    chunks, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) + 5 > max_chunk:
            chunks.append(cur)
            cur = b
        else:
            cur = f"{cur}\n---\n{b}" if cur else b
    if cur.strip():
        chunks.append(cur)
    return chunks


_PROMPT = """You are CIRRUS, scanning a PORTION of your AI digest for ONE specific thing:
INTELLIGENCE ON HOW PEOPLE OR TEAMS DECIDE WHICH LLM / AI MODEL TO USE for different
tasks or questions — i.e. model SELECTION and ROUTING strategy.

Capture ONLY concrete, reusable approaches, such as:
- a heuristic ("use <model> for coding, <model> for long-context research")
- a routing rule or decision framework for picking a model per task
- a team's or person's stated method for choosing between models
- a specific benchmark or test they use to decide which model
- a model-router tool or technique

For EACH distinct approach, output ONE bullet, on its own line:
- <the approach/heuristic, one specific line> — SOURCE: <who or what said it>

IGNORE: generic model-release news, hype, raw benchmarks with no selection guidance,
and anything NOT about CHOOSING/ROUTING between models. Write ONLY in English.
If there is NOTHING relevant in this portion, output the single word: NONE

DIGEST CONTENT:
{chunk}

Output the routing-intelligence bullets now:"""


def _norm(b):
    return re.sub(r'\W+', '', b.lower())[:80]


def _existing_keys():
    if not PLAYBOOK.exists():
        return set()
    try:
        return {_norm(l) for l in PLAYBOOK.read_text().splitlines()
                if l.strip().startswith("-")}
    except Exception:
        return set()


def _append_playbook(digest_file, new_bullets):
    if not PLAYBOOK.exists():
        PLAYBOOK.write_text(
            "# CIRRUS — LLM Routing Playbook\n"
            "*How other people/teams choose & route between LLMs. Auto-collected from\n"
            "daily/weekly digests by routing_watch.py; deduped, grows over time.*\n\n")
    date_str = datetime.now().strftime("%Y-%m-%d")
    with open(PLAYBOOK, "a") as f:
        f.write(f"\n## {date_str} — from {digest_file.name}\n")
        f.write("\n".join(new_bullets) + "\n")


def _append_digest_note(digest_file, new_bullets):
    """Append a short 'routing watch' note to the digest file (the digest note)."""
    try:
        with open(digest_file, "a") as f:
            f.write("\n\n## 🔀 LLM Routing Watch\n")
            f.write(f"*{len(new_bullets)} new model-selection approach(es) captured to "
                    f"`llm-routing-playbook.md`:*\n")
            f.write("\n".join(new_bullets) + "\n")
    except Exception as e:
        log(f"could not append digest note (non-fatal): {e}")


def watch(digest_file: Path):
    """Scan one digest file for routing intelligence; update playbook + digest note.
    Returns the list of NEW bullets added (deduped against the playbook)."""
    try:
        raw = digest_file.read_text()
    except Exception as e:
        log(f"cannot read {digest_file}: {e}")
        return []
    content = raw.split("## 🔗 Links Visited This Run")[0]
    chunks = split_digest_chunks(content)[:MAX_CHUNKS]
    seen = _existing_keys()
    new = []
    for i, chunk in enumerate(chunks, 1):
        out = ollama_extract(_PROMPT.format(chunk=chunk))
        if out.startswith("[Extraction error"):
            log(f"  chunk {i}: {out}")
            continue
        for line in out.splitlines():
            s = line.strip()
            if not s.startswith(("-", "*", "•")):
                continue
            bullet = "- " + s.lstrip("-*• ").strip()
            if len(bullet) < 12 or re.match(r'^-\s*none\b', bullet, re.IGNORECASE):
                continue
            k = _norm(bullet)
            if k and k not in seen:
                seen.add(k)
                new.append(bullet)
    if new:
        _append_playbook(digest_file, new)
        _append_digest_note(digest_file, new)
    log(f"routing-watch: {len(new)} new approach(es) from {digest_file.name}")
    return new


def watch_latest(prefix="daily"):
    """Scan the most recent digest. NEVER raises (safe for the digest pipeline)."""
    try:
        files = sorted(OUTPUT_DIR.glob(f"{prefix}-*.md"), reverse=True)
        if not files:
            log(f"routing-watch: no {prefix}-*.md digests found")
            return []
        return watch(files[0])
    except Exception as e:
        log(f"routing-watch failed (non-fatal): {e}")
        return []


if __name__ == "__main__":
    import sys
    try:
        if len(sys.argv) > 1:
            watch(Path(sys.argv[1]))
        else:
            watch_latest("daily")
    except Exception as e:
        log(f"routing-watch fatal (ignored): {e}")
