# STRATUS Ecosystem — Rolling Research Log
*Living log. Revisited **at least monthly** (scheduled) plus whenever the
daily/weekly digest surfaces something relevant. Goal: don't commit to a STRATUS
ecosystem until the options are proven — keep tracking hardware AND local-LLM
technique changes, so the design keeps up with the field.*

**Guiding intent (Buddy, 2026-07-20):** STRATUS should run a **local LLM that
solves most project work itself**, calling out to foundational/frontier models
(Claude, Gemini, etc.) only when it needs help. The stack should be
**self-improving** — continuously folding in new hardware options and new
fine-tuning/serving techniques rather than freezing on today's answer.

Companion docs: `STRATUS-Production-Sizing-and-Architecture.md` (current design),
`Pedagogy-Specialized-FineTune-Sketch.md` (first specialized model),
`CIRRUS-Autonomous-Dev-Loop.md` (the self-improvement engine).

---

## Current recommendation snapshot (as of 2026-07-20)
- **Serving:** scale out DGX Spark (128GB each) — double CUMULUS, expand to 3
  (switchless ring), add a QSFP switch to grow past 3. Great for concurrent
  serving + single-node QLoRA; weak for tightly-coupled distributed training
  (~200Gb interconnect vs 273GB/s on-box memory).
- **Training:** keep single-node (QLoRA small specialist models fit one Spark);
  reserve a big-memory box (DGX Station GB300, 748GB) or cloud burst ONLY if
  routine heavy/full fine-tuning appears.
- **Memory:** specialist 8–32B models fit one Spark with concurrency headroom;
  keep steady-state ≤ ~70% per node.
- **Storage:** 4TB hot NVMe on-box + 20–50TB bulk NAS (datasets, checkpoints,
  transcripts, backups; credentials never on the NAS).
- **Model strategy:** specialize open bases (fine-tune + RAG), NOT train from
  scratch. Small, focused models beat a general 70B on our narrow tasks.

## Watch list — what to re-check each month
1. **New/updated hardware:** DGX Spark successors, DGX Station variants, RTX Pro
   (Blackwell) cards, H200/B200/next-gen datacenter GPUs, Apple Mac Studio (M-series
   unified memory), AMD (Strix Halo / Instinct), and any new high-unified-memory
   desktop class. Track $/GB-memory and memory bandwidth, not just TOPS.
2. **Interconnect:** any change to Spark clustering limits, switch options,
   inter-node bandwidth.
3. **Local-LLM technique changes:** better small models (8–32B) closing the gap
   on big ones; quantization advances (lower-bit, quality-preserving); fine-tune
   methods beyond QLoRA; distillation; long-context + KV-cache efficiency;
   serving engines (vLLM/TensorRT-LLM/etc.) throughput gains.
4. **"Ask-for-help" boundary:** when does the local model still need a frontier
   model, and is that gap shrinking?
5. **Cost:** current street prices (verify before any purchase).

## How this stays current
- **Monthly:** scheduled task `stratus-monthly-review` researches the watch list,
  compares to the snapshot above, and appends a dated entry below — flagging if
  the recommendation should change.
- **Daily/weekly:** the digest already surfaces AI-infrastructure + model notes
  (it routinely flags new models/hardware). Relevant hits get harvested into the
  "Digest hits" subsections of each monthly entry.
- **Self-improvement:** proven technique/hardware changes flow through the normal
  Dev-Loop (proposal → test → promote) once validated on CUMULUS.

---

## Log entries (newest first)

### 2026-07-20 — Baseline established
- Wrote the two companion docs; snapshot above is the starting position.
- Current CIRRUS models: qwen2.5:72b/14b, qwen2.5-coder:14b (Alibaba), llama3.2:3b,
  nomic-embed-text. CUMULUS = 1× DGX Spark (128GB), just received.
- Open questions to resolve as research accrues: (a) which small base specializes
  best for our domains (Qwen vs Llama), (b) real peak concurrency the client
  workloads generate (sizes node count), (c) whether any single-box option beats
  a 2–3 Spark cluster on $/throughput by the time we commit.
- Next monthly review: 2026-08-01.

<!-- New monthly entries appended above this line by stratus-monthly-review -->
