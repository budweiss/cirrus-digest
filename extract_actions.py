#!/usr/bin/env python3
"""
CIRRUS Action Extractor
Reads a digest file, uses Ollama to pull out action items, recommendations,
and CIRRUS improvement notes into a clean actions file.
"""

import json
import re
import requests
from datetime import datetime
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────

CONFIG_PATH = Path.home() / "projects/cirrus-digest/config/sources.json"

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

DIGEST_CFG  = CONFIG["digest"]
OUTPUT_DIR  = Path(DIGEST_CFG["output_dir"])
LOG_DIR     = Path(DIGEST_CFG["log_dir"])
MODEL       = DIGEST_CFG["ollama_model"]
OLLAMA_HOST = DIGEST_CFG["ollama_host"]

ACTIONS_DIR = OUTPUT_DIR / "actions"
ACTIONS_DIR.mkdir(parents=True, exist_ok=True)

# ── Helpers ──────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")

def ollama_extract(prompt):
    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": MODEL, "prompt": prompt, "stream": False,
                  "options": {"num_ctx": 8192}},
            timeout=120
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception as e:
        return f"[Extraction error: {e}]"

# ── Extractor ────────────────────────────────────────────────────────────────
#
# Digests grew far beyond the old single-prompt window (a 44-item digest is
# ~54KB; the old code read only the first 6,000 chars, so most items were
# never scanned for actions). The digest is now split into item-boundary
# chunks, each chunk is extracted separately, and the section bullets are
# merged and deduplicated into one actions file. Output format is unchanged,
# so cirrus_bot.py's extract_recommendations() keeps working as-is.

SECTIONS = [
    "ACTION ITEMS",
    "RECOMMENDATIONS",
    "CIRRUS IMPROVEMENT NOTES",
    "INTERESTING TOOLS/MODELS",
    "FOLLOW-UP READING",
]

MAX_CHUNK_CHARS = 5500
MAX_CHUNKS = 10  # safety cap: 10 x ~30s qwen calls ≈ 5 min worst case

_NONE_BULLET = re.compile(
    r'^-\s*\**\s*(none|n/?a|nothing)\b', re.IGNORECASE
)

def split_digest_chunks(content: str, max_chunk: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split digest content into chunks along item separators (\\n---\\n),
    packing as many whole items per chunk as fit under max_chunk."""
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

def parse_sections(text: str) -> dict:
    """Parse an LLM extraction response into {section: [bullet, ...]}."""
    result = {s: [] for s in SECTIONS}
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r'^#{1,4}\s+', stripped) or re.match(r'^\*\*[A-Z]', stripped):
            heading = re.sub(r'^#{1,4}\s+|\*\*', '', stripped).upper().strip().rstrip(':')
            current = None
            for s in SECTIONS:
                if s in heading or heading in s:
                    current = s
                    break
            continue
        if current and stripped.startswith(("-", "*", "•")):
            bullet = "- " + stripped.lstrip("-*• ").strip()
            if len(bullet) > 5:
                result[current].append(bullet)
    return result

def extract_actions(digest_file: Path) -> Path:
    """Extract action items from a digest file (chunked) and save to actions folder."""
    log(f"Extracting actions from: {digest_file.name}")

    content = digest_file.read_text()
    date_str = datetime.now().strftime("%Y-%m-%d")

    # The Links Visited section is operational metadata, not content —
    # exclude it so URLs don't get extracted as "follow-up reading".
    content = content.split("## 🔗 Links Visited This Run")[0]

    chunks = split_digest_chunks(content)[:MAX_CHUNKS]
    log(f"  Digest is {len(content):,} chars → {len(chunks)} chunk(s)")

    merged = {s: [] for s in SECTIONS}
    seen = set()

    for i, chunk in enumerate(chunks, 1):
        log(f"  Extracting chunk {i}/{len(chunks)} ({len(chunk):,} chars)")
        prompt = f"""You are CIRRUS, reviewing a PORTION of your own AI digest to extract actionable items.

Read the following digest section and extract ALL of the following:

1. ACTION ITEMS — concrete tasks or next steps mentioned (e.g. install a tool, test a model, update a script)
2. RECOMMENDATIONS — suggestions for improving AI workflows, tools, or setups
3. CIRRUS IMPROVEMENT NOTES — anything tagged with "→ CIRRUS NOTE:" or suggestions for improving this digest system
4. INTERESTING TOOLS/MODELS — any new AI tools, models, or services worth investigating
5. FOLLOW-UP READING — articles, papers, or resources mentioned that are worth reading in full

Format your response with EXACTLY these 5 markdown headers:
## ACTION ITEMS
## RECOMMENDATIONS
## CIRRUS IMPROVEMENT NOTES
## INTERESTING TOOLS/MODELS
## FOLLOW-UP READING

Under each header, write bullet items ("- ..."), each with a one-line source reference (which newsletter/article it came from).
Be specific and actionable — skip vague observations, stock-market items, and anything not related to AI tools, models, or workflows.
If a section has nothing, leave it EMPTY — do not write "None" or "N/A" bullets.

DIGEST CONTENT:
{chunk}

Output the action items now:"""

        extracted = ollama_extract(prompt)
        if extracted.startswith("[Extraction error"):
            log(f"    {extracted}")
            continue
        for section, bullets in parse_sections(extracted).items():
            for bullet in bullets:
                if _NONE_BULLET.match(bullet):
                    continue
                key = re.sub(r'\W+', '', bullet.lower())[:80]
                if key and key not in seen:
                    seen.add(key)
                    merged[section].append(bullet)

    # Determine prefix (daily vs weekly)
    prefix = "daily-actions" if digest_file.name.startswith("daily") else "weekly-actions"
    actions_file = ACTIONS_DIR / f"{prefix}-{date_str}.md"

    with open(actions_file, "w") as f:
        f.write(f"# CIRRUS Action Items — {date_str}\n")
        f.write(f"*Extracted from: {digest_file.name} "
                f"({len(chunks)} chunk(s), full digest scanned)*\n\n")
        f.write("---\n\n")
        for section in SECTIONS:
            f.write(f"## {section}\n")
            if merged[section]:
                f.write("\n".join(merged[section]) + "\n")
            f.write("\n")
        f.write(f"---\n*Generated by CIRRUS using `{MODEL}`*\n")

    total = sum(len(v) for v in merged.values())
    log(f"Actions saved: {actions_file} ({total} item(s) across {len(chunks)} chunk(s))")
    return actions_file


def extract_from_latest(prefix="digest"):
    """Find the most recent digest file and extract actions from it."""
    files = sorted(OUTPUT_DIR.glob(f"{prefix}-*.md"), reverse=True)
    if not files:
        log(f"No {prefix} files found in {OUTPUT_DIR}")
        return None
    return extract_actions(files[0])


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        extract_actions(Path(sys.argv[1]))
    else:
        # Default: extract from latest weekly digest
        extract_from_latest("digest")
