#!/usr/bin/env python3
"""ALOPECIA project — grounded knowledge base over the foundation document.

WHY THIS IS NOT cirrus_rag.py (S82, measured before writing a line of it):

  1. cirrus_rag.retrieve() keeps only ONE chunk per source_file, deliberately,
     for diversity across many digests. This KB is built from a SMALL number of
     documents, so that cap would return exactly one chunk per question and the
     grounding criterion could never be met.
  2. cirrus_rag stores text as chunk[:300] -- a preview. A citation needs the
     passage, not the first 300 characters of it.
  3. It shares one index with the digest KB, and build_context() labels every
     hit "FROM PREVIOUS DIGESTS". Mixing medical reference text into the index
     the Telegram bot answers digest questions from would corrupt that feature.

Same embedding model and same host as cirrus_rag (nomic-embed-text via Ollama)
-- only the storage and retrieval policy differ. Python 3.9 compatible: CIRRUS
runs 3.9.

    python3 alopecia_kb.py index <file.md> [...]
    python3 alopecia_kb.py query "how does immune privilege collapse?"
    python3 alopecia_kb.py stats
    python3 alopecia_kb.py --selftest      (also: selftest)
"""

import json
import re
import sys
from pathlib import Path

CONFIG_PATH = Path.home() / "projects/cirrus-digest/config/sources.json"
KB_DIR = Path.home() / "projects/cirrus-digest/knowledge-alopecia"
EMBED_MODEL = "nomic-embed-text"
MIN_SIM = 0.45          # below this a hit is not evidence of anything
CHUNK_WORDS = 350


def _ollama_host():
    with open(CONFIG_PATH) as f:
        return json.load(f)["digest"]["ollama_host"]


def _embed_real(text):
    """Embedding vector from Ollama. Returns [] on any failure (never raises)."""
    import requests
    try:
        resp = requests.post(
            "%s/api/embeddings" % _ollama_host(),
            json={"model": EMBED_MODEL, "prompt": text[:2000]},
            timeout=30)
        resp.raise_for_status()
        return resp.json().get("embedding", [])
    except Exception as e:
        print("embedding error: %s" % e)
        return []


# Injectable so the selftest needs no network and no Ollama (T32: tests never
# touch live services or live files).
_embed = _embed_real


def chunk_doc(text, source):
    """Split a markdown doc into chunks that each carry their own citation.

    Chunks are cut at '## ' section boundaries so a retrieved passage can name
    the section it came from -- a hit that cannot cite its section is not
    usable as grounding. Long sections are further split on word count, and
    every piece inherits the section heading.
    """
    chunks = []
    parts = re.split(r'\n(?=## )', text)
    for part in parts:
        part = part.strip()
        if len(part) < 40:
            continue
        first = part.split("\n", 1)[0].strip()
        heading = first.lstrip("#").strip() if first.startswith("#") else "(preamble)"
        words = part.split()
        if len(words) <= CHUNK_WORDS:
            pieces = [part]
        else:
            pieces = [" ".join(words[i:i + CHUNK_WORDS])
                      for i in range(0, len(words), CHUNK_WORDS)]
        for n, piece in enumerate(pieces):
            chunks.append({
                "source": source,
                "section": heading,
                "part": n + 1,
                "parts": len(pieces),
                "text": piece,          # FULL text, not a preview
            })
    return chunks


def _paths(kb_dir):
    d = Path(kb_dir)
    return d / "index.json", d / "vectors.json"


def load_kb(kb_dir=KB_DIR):
    idx_p, vec_p = _paths(kb_dir)
    if not idx_p.exists() or not vec_p.exists():
        return [], []
    with open(idx_p) as f:
        index = json.load(f)
    with open(vec_p) as f:
        vectors = json.load(f)
    return index, vectors


def save_kb(index, vectors, kb_dir=KB_DIR):
    Path(kb_dir).mkdir(parents=True, exist_ok=True)
    idx_p, vec_p = _paths(kb_dir)
    with open(idx_p, "w") as f:
        json.dump(index, f, indent=1)
    with open(vec_p, "w") as f:
        json.dump(vectors, f)


def index_doc(path, kb_dir=KB_DIR):
    """Index one document. Re-indexing a document REPLACES its chunks.

    Replace-not-append matters: the foundation doc gets corrected as the field
    moves, and an append would leave superseded medical text in the KB
    competing with the correction. Stale grounding is worse than none.
    """
    path = Path(path)
    index, vectors = load_kb(kb_dir)
    source = path.name

    keep = [(i, v) for i, v in zip(index, vectors) if i["source"] != source]
    index = [i for i, _ in keep]
    vectors = [v for _, v in keep]

    added = 0
    for ch in chunk_doc(path.read_text(), source):
        vec = _embed(ch["text"])
        if not vec:
            continue
        ch["chunk_id"] = len(index)
        index.append(ch)
        vectors.append(vec)
        added += 1

    save_kb(index, vectors, kb_dir)
    return added


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def query(question, top_k=3, kb_dir=KB_DIR):
    """Top-k passages for a question. No one-per-file cap (see module docstring)."""
    index, vectors = load_kb(kb_dir)
    if not index:
        return []
    qv = _embed(question)
    if not qv:
        return []
    scored = []
    for item, vec in zip(index, vectors):
        scored.append((_cosine(qv, vec), item))
    scored.sort(key=lambda t: t[0], reverse=True)
    out = []
    for sim, item in scored[:top_k]:
        if sim < MIN_SIM:
            continue
        out.append({
            "similarity": round(sim, 3),
            "source": item["source"],
            "section": item["section"],
            "text": item["text"],
        })
    return out


def stats(kb_dir=KB_DIR):
    index, _ = load_kb(kb_dir)
    per = {}
    for i in index:
        per[i["source"]] = per.get(i["source"], 0) + 1
    return {"chunks": len(index), "documents": len(per), "per_document": per}


def selftest():
    """Offline: no Ollama, no network, no live KB directory (T32)."""
    import tempfile
    ok = fail = 0

    def check(name, cond):
        nonlocal ok, fail
        ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
        print("  [%s] %s" % ("OK " if cond else "FAIL", name))

    # every section mentions at least one vocabulary word, so the stub's
    # "matched nothing" axis belongs to the unrelated QUERY alone
    doc = ("# Title\n\nintro paragraph about the follicle, long enough to "
           "survive the minimum-length filter applied to every chunk.\n\n"
           "## Mechanism\n\nImmune privilege collapse exposes follicle "
           "autoantigens to CD8 T cells and this sentence pads the section.\n\n"
           "## Diet\n\n" + ("microbiome evidence word " * 500))
    chunks = chunk_doc(doc, "doc.md")
    secs = [c["section"] for c in chunks]
    check("chunk_doc: splits on ## headings", "Mechanism" in secs and "Diet" in secs)
    check("chunk_doc: every chunk carries a citable section",
          all(c["section"] for c in chunks))
    check("chunk_doc: a long section is split into multiple parts",
          len([c for c in chunks if c["section"] == "Diet"]) > 1)
    check("chunk_doc: stores FULL text, not a 300-char preview",
          max(len(c["text"]) for c in chunks) > 300)

    # deterministic fake embeddings: bag-of-words over a fixed vocabulary
    # Last dimension is an "matched nothing" axis, so a genuinely unrelated
    # query comes out ORTHOGONAL to real content. An earlier version fell back
    # to a uniform vector, which correlates with everything and made the
    # threshold check fail against the stub rather than against the code.
    vocab = ["immune", "privilege", "collapse", "cd8", "microbiome", "diet",
             "follicle", "evidence"]

    def fake(text):
        t = text.lower()
        v = [float(t.count(w)) for w in vocab]
        return v + [0.0] if any(v) else [0.0] * len(vocab) + [1.0]

    global _embed
    real = _embed
    _embed = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "doc.md"
            p.write_text(doc)
            n = index_doc(p, kb_dir=td)
            check("index_doc: indexes chunks", n == len(chunks))
            check("stats: counts one document", stats(kb_dir=td)["documents"] == 1)

            hits = query("immune privilege collapse cd8", top_k=3, kb_dir=td)
            check("query: returns hits", len(hits) > 0)
            check("query: top hit is the Mechanism section",
                  hits and hits[0]["section"] == "Mechanism")
            check("query: hit carries a citation", hits and hits[0]["source"] == "doc.md")

            # THE reason this module exists rather than cirrus_rag.retrieve()
            many = query("microbiome diet evidence", top_k=3, kb_dir=td)
            check("query: returns MORE THAN ONE chunk from a single document "
                  "(cirrus_rag caps at one)", len(many) > 1)

            # re-index replaces, never appends
            before = stats(kb_dir=td)["chunks"]
            index_doc(p, kb_dir=td)
            check("index_doc: re-indexing REPLACES, so no stale duplicates",
                  stats(kb_dir=td)["chunks"] == before)

            check("query: irrelevant question returns nothing above threshold",
                  query("zzzz unrelated", top_k=3, kb_dir=td) == [])

            _embed = lambda t: []
            check("query: embedding failure returns [] rather than raising",
                  query("immune", kb_dir=td) == [])
            _embed = fake
    finally:
        _embed = real

    print("\n%d passed, %d failed" % (ok, fail))
    return fail == 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd in ("--selftest", "selftest"):
        sys.exit(0 if selftest() else 1)
    elif cmd == "index":
        for f in sys.argv[2:]:
            print("indexed %d chunks from %s" % (index_doc(f), f))
    elif cmd == "query":
        for h in query(" ".join(sys.argv[2:])):
            print("\n[%.3f] %s -- %s\n%s" % (h["similarity"], h["source"],
                                             h["section"], h["text"][:600]))
    else:
        print(json.dumps(stats(), indent=2))
