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

### 2026-09-01

- **DGX Spark price confirmed still elevated:** street price now $4,699 (was $3,999 at Oct-2025 launch, raised 18% in Feb-2026 for "memory supply constraints"), listed at $4,679 on Amazon. That's ~$36.71/GB unified memory. Verify before purchase. ([intuitionlabs](https://intuitionlabs.ai/articles/nvidia-dgx-spark-review), [gpusmith](https://gpusmith.com/articles/en/nvidia-dgx-spark-review-specs-performance))
- **New competitor: NVIDIA RTX Spark (N1X)** — separate consumer/Windows-AI-PC line, not a DGX successor. Same GB10-class Grace-Blackwell silicon, up to 128GB LPDDR5X, ~1 PFLOP FP4, but estimated starting price ~$2,899 → ~$22.65/GB, ~38% cheaper per GB than DGX Spark. Worth tracking as it matures — could be a cheaper serving node if DGX-class software/clustering support is confirmed. Verify before purchase. ([aitooldiscovery](https://www.aitooldiscovery.com/ai-infra/nvidia-rtx-spark-explained))
- **DGX Station (GB300) pricing surfaced:** no official NVIDIA price; partner quotes (MSI, Dell, etc.) run ~$80K–$123K (one MSI SKU at $96,995.99), up sharply from earlier estimates due to memory shortage. Spec: up to 784GB coherent memory (252GB HBM3e @ 7.1 TB/s + Grace-side), ConnectX-8 @ 800Gb/s, MIG up to 7 instances. Confirms it stays a "reserve only if heavy full fine-tune needed" box — cost keeps rising, not falling. Verify before purchase. ([betterclaw](https://www.betterclaw.io/blog/dgx-station-alternative))
- **AMD Strix Halo reaffirmed as budget alternative:** ~$2,348, comparable inference performance to DGX Spark at FP8/FP16 — cheaper $/GB and $/perf, but lacks CUDA-native stack and DGX clustering. Still worth a bench pass if software gap narrows. Verify before purchase. ([intuitionlabs](https://intuitionlabs.ai/articles/nvidia-dgx-spark-review))
- **Serving-throughput correction, important for our design:** independent benchmarks now say DGX Spark is 6–7× slower than RTX PRO 6000 Blackwell for production inference serving, and memory bandwidth (273 GB/s) is flagged explicitly as the bottleneck for high-concurrency batch workloads — contradicts our snapshot's framing that Spark scale-out is "great" for concurrent serving. Needs re-examination: RTX PRO 6000 (96GB, ~$16K per recent quote, itself up from $8,565 less than a year ago) may be the better $/throughput serving card even though $/GB is worse. Verify before purchase. ([ifactoryapp](https://ifactoryapp.com/sap-integration/on-prem-ai/nvidia-dgx-spark-review-enterprise), [betterclaw](https://www.betterclaw.io/blog/dgx-station-alternative))
- **B200 datacenter tier, for context only:** 192GB HBM3e @ up to 8 TB/s, native FP4, ~4× H100 inference claimed — still cloud/rack-scale, not a desk box, but the FP4 native support plus large memory reinforces that our "small specialist model + quantization" strategy is directionally aligned with where the industry is optimizing. ([jarvislabs](https://jarvislabs.ai/ai-faqs/nvidia-b200-specs))
- **No hard evidence yet on local-vs-frontier gap shrinking this cycle** — no new benchmark comparing an 8–32B fine-tuned specialist against frontier models surfaced in this pass; software-side gains this month (TensorRT-LLM speculative decoding, up to 2.5x per NVIDIA's CES claims) are throughput/latency improvements, not capability-gap closers. Flag for next month's pass specifically.

Recommendation change suggested: re-verify the "Spark scale-out is great for concurrent serving" claim against the newly-surfaced 6–7× throughput gap vs RTX PRO 6000 Blackwell before committing further Spark purchases — get an apples-to-apples concurrent-batch benchmark (not just single-stream tok/s) before buying a 3rd/4th Spark node.

<!-- New monthly entries appended above this line by stratus-monthly-review -->
