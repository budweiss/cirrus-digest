#!/usr/bin/env python3
"""
CIRRUS RAG Knowledge Base
Indexes past digests using nomic-embed-text embeddings stored locally.
Retrieves relevant past knowledge before summarizing new content.
Gives CIRRUS running memory across weeks.
"""

import json
import re
import requests
import numpy as np
from datetime import datetime
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────

CONFIG_PATH = Path.home() / "projects/cirrus-digest/config/sources.json"

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

DIGEST_CFG  = CONFIG["digest"]
OUTPUT_DIR  = Path(DIGEST_CFG["output_dir"])
OLLAMA_HOST = DIGEST_CFG["ollama_host"]
EMBED_MODEL = "nomic-embed-text"

# Knowledge base stored here
KB_DIR      = Path.home() / "projects/cirrus-digest/knowledge"
KB_DIR.mkdir(parents=True, exist_ok=True)
KB_INDEX    = KB_DIR / "index.json"      # metadata for each chunk
KB_VECTORS  = KB_DIR / "vectors.npy"    # numpy array of embeddings

# ── Helpers ──────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")

def get_embedding(text: str) -> list:
    """Get embedding vector from Ollama nomic-embed-text."""
    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text[:2000]},
            timeout=30
        )
        resp.raise_for_status()
        return resp.json().get("embedding", [])
    except Exception as e:
        log(f"Embedding error: {e}")
        return []

def load_index() -> list:
    """Load the metadata index."""
    if KB_INDEX.exists():
        with open(KB_INDEX) as f:
            return json.load(f)
    return []

def save_index(index: list):
    """Save the metadata index."""
    with open(KB_INDEX, "w") as f:
        json.dump(index, f, indent=2)

def load_vectors() -> np.ndarray:
    """Load the embeddings array."""
    if KB_VECTORS.exists():
        return np.load(KB_VECTORS)
    return np.array([]).reshape(0, 768)  # nomic-embed-text = 768 dims

def save_vectors(vectors: np.ndarray):
    """Save the embeddings array."""
    np.save(KB_VECTORS, vectors)

def chunk_text(text: str, chunk_size: int = 500) -> list:
    """Split text into overlapping chunks for indexing."""
    words = text.split()
    chunks = []
    step = chunk_size // 2  # 50% overlap
    for i in range(0, len(words), step):
        chunk = " ".join(words[i:i+chunk_size])
        if len(chunk.strip()) > 50:  # skip tiny chunks
            chunks.append(chunk)
    return chunks

def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Compute cosine similarity between two vectors."""
    if np.linalg.norm(a) == 0 or np.linalg.norm(b) == 0:
        return 0.0
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

# ── Indexing ──────────────────────────────────────────────────────────────────

def is_already_indexed(source_file: str, index: list) -> bool:
    """Check if a digest file has already been indexed."""
    return any(item["source_file"] == source_file for item in index)

def index_digest(digest_file: Path) -> int:
    """Index a digest file into the knowledge base. Returns chunks added."""
    index   = load_index()
    vectors = load_vectors()

    if is_already_indexed(digest_file.name, index):
        log(f"  Already indexed: {digest_file.name}")
        return 0

    log(f"  Indexing: {digest_file.name}")
    content = digest_file.read_text()

    # Split into sections by ### headers
    sections = re.split(r'\n#{1,3} ', content)
    new_chunks = 0

    for section in sections:
        if len(section.strip()) < 50:
            continue

        # Further chunk long sections
        for chunk in chunk_text(section, chunk_size=400):
            embedding = get_embedding(chunk)
            if not embedding:
                continue

            vec = np.array(embedding).reshape(1, -1)
            vectors = np.vstack([vectors, vec]) if vectors.shape[0] > 0 else vec

            index.append({
                "source_file": digest_file.name,
                "date": digest_file.stem.replace("digest-", "").replace("daily-", ""),
                "type": "weekly" if digest_file.name.startswith("digest") else "daily",
                "chunk_id": len(index),
                "text": chunk[:300]  # store preview for context
            })
            new_chunks += 1

    save_index(index)
    save_vectors(vectors)
    log(f"  Added {new_chunks} chunks from {digest_file.name}")
    return new_chunks

def index_all_digests():
    """Index all existing digest files."""
    log("=== Indexing all digests ===")
    total = 0
    for pattern in ["digest-*.md", "daily-*.md"]:
        for f in sorted(OUTPUT_DIR.glob(pattern)):
            total += index_digest(f)
    log(f"=== Indexed {total} total chunks ===")
    return total

# ── Retrieval ─────────────────────────────────────────────────────────────────

def retrieve(query: str, top_k: int = 3, exclude_file: str = None) -> list:
    """
    Retrieve the most relevant past knowledge for a query.
    Returns list of dicts with text and source info.
    """
    index   = load_index()
    vectors = load_vectors()

    if len(index) == 0 or vectors.shape[0] == 0:
        return []

    query_vec = np.array(get_embedding(query))
    if query_vec.size == 0:
        return []

    # Compute similarities
    similarities = []
    for i, item in enumerate(index):
        if exclude_file and item["source_file"] == exclude_file:
            continue  # don't retrieve from the file being processed
        if i >= vectors.shape[0]:
            continue
        sim = cosine_similarity(query_vec, vectors[i])
        similarities.append((sim, item))

    # Return top_k most similar
    similarities.sort(key=lambda x: x[0], reverse=True)
    results = []
    seen_files = set()
    for sim, item in similarities[:top_k * 2]:  # over-fetch to deduplicate
        if len(results) >= top_k:
            break
        if sim < 0.5:  # minimum relevance threshold
            continue
        # Limit to one chunk per source file for diversity
        if item["source_file"] in seen_files:
            continue
        seen_files.add(item["source_file"])
        results.append({
            "text": item["text"],
            "source_file": item["source_file"],
            "date": item["date"],
            "similarity": round(sim, 3)
        })

    return results

def build_context(query: str, exclude_file: str = None) -> str:
    """Build a context string from retrieved past knowledge."""
    results = retrieve(query, top_k=3, exclude_file=exclude_file)
    if not results:
        return ""

    context = "RELEVANT PAST KNOWLEDGE FROM PREVIOUS DIGESTS:\n"
    for r in results:
        context += f"\n[From {r['date']}]: {r['text']}\n"
    return context

# ── Stats ─────────────────────────────────────────────────────────────────────

def kb_stats() -> dict:
    """Return knowledge base statistics."""
    index   = load_index()
    vectors = load_vectors()

    files = list(set(item["source_file"] for item in index))
    weekly = [f for f in files if f.startswith("digest")]
    daily  = [f for f in files if f.startswith("daily")]

    return {
        "total_chunks": len(index),
        "total_files": len(files),
        "weekly_digests": len(weekly),
        "daily_digests": len(daily),
        "vector_shape": list(vectors.shape),
        "kb_size_mb": round((KB_VECTORS.stat().st_size / 1024 / 1024) if KB_VECTORS.exists() else 0, 2)
    }

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "index":
        index_all_digests()
    elif len(sys.argv) > 1 and sys.argv[1] == "stats":
        stats = kb_stats()
        print(json.dumps(stats, indent=2))
    elif len(sys.argv) > 2 and sys.argv[1] == "query":
        query = " ".join(sys.argv[2:])
        results = retrieve(query)
        for r in results:
            print(f"\n[{r['date']}] (similarity: {r['similarity']})")
            print(r['text'])
    else:
        print("Usage:")
        print("  python3 cirrus_rag.py index          — index all digests")
        print("  python3 cirrus_rag.py stats          — show KB stats")
        print('  python3 cirrus_rag.py query <text>   — test a query')
