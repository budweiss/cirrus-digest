# STRATUS — Single LLM + Agents Strategy Proposal

*Planning doc, Session 359 (2026-10-01). Captures Buddy's proposal to shift
STRATUS from multiple fine-tuned specialist models to ONE general LLM trained
incrementally over time, with specialist agents (prompts/tools/RAG) per client.
This is a proposal, not a decision — it needs A/B testing on CUMULUS before it
replaces the current plan.*

Related: `STRATUS-Production-Sizing-and-Architecture.md` (current plan),
`Pedagogy-Specialized-FineTune-Sketch.md` (specialist fine-tune approach),
`STRATUS-Network-Switch-Spec.md` (network for this topology).

---

## 1. Buddy's vision (in his words)

> "What do you think if we change the approach for STRATUS to be a LLM that we
> can develop? Maybe start off with an open source LLM and work on teaching it
> and building agents to support it? We can reference this LLM with our Cirrus
> and Cumulus instead of running locals on these servers. This is just a
> thought for now so we don't need to run so many LLM but one complete LLM that
> we can train over time."

> "This will give both best LLM locally and only needs to reach out to
> foundationals when stuck or when teaching the LLM on Stratus base on what it
> finds in its research."

**Summary:** One LLM on STRATUS, trained incrementally, serving all client
workloads. CIRRUS and CUMULUS call it instead of running their own local
models. The LLM reaches out to Claude/Gemini ("foundationals") only when it
is stuck or when learning new things from research.

---

## 2. Current plan (for contrast)

The architecture doc's current plan: **multiple fine-tuned specialist models**
(8–32B, QLoRA + RAG per client task) running independently on each Spark.
Each specialist fits one Spark with concurrency headroom. Claude/Gemini as
fallback for hard problems. See `Pedagogy-Specialized-FineTune-Sketch.md`.

| Aspect | Current (specialists) | Proposed (single LLM) |
|--------|----------------------|----------------------|
| Models | One per client task (8–32B fine-tuned) | One general model for all tasks |
| Per-client customization | Fine-tune weights (QLoRA) | Agent layer (system prompt + tools + RAG) |
| Serving | Each Spark runs its own specialist(s) | STRATUS hosts the one model; others call it |
| Training | QLoRA per specialist, as needed | Incremental training of the general model |
| Fallback | Claude/Gemini for hard problems | Same — Claude/Gemini when stuck |
| Hardware per Spark | Fits 128 GB with headroom | Larger model may need pooling |

---

## 3. Honest assessment

### What is strong about this approach

**Agent layer is scalable.** Adding a new client (e.g., a 4th workload) means
writing a new system prompt + tool definitions + RAG index — no fine-tune run,
no checkpoint to manage. This is faster, cheaper, and more maintainable than
QLoRA per client. The agent pattern is also how the industry is moving
(Claude/GPT + tools + RAG beats fine-tuned small models for many tasks).

**One model to serve, one model to improve.** Instead of tracking N
specialists with different versions, checkpoints, and eval suites, there is
one model. Improvements (new training data, better reasoning) benefit all
clients at once. Operational simplicity is real.

**Research loop is compelling.** STRATUS doing monthly research, learning from
what it finds, and incorporating that into the shared model — that is a
coherent vision for self-improvement. The monthly STRATUS research job (which
we just fixed) already gathers the raw material.

**Network efficiency.** CIRRUS and CUMULUS no longer need to run their own
local models — they make API calls to STRATUS. This frees their memory and
compute for other work (data prep, transcription, indexing, digest
generation).

### What to be honest about

**Latency tradeoff for larger models.** A 70B model on a single Spark is
bandwidth-bound for single-stream generation (273 GB/s on-box, but the model
weights are ~40 GB, so each generated token requires reading all weights →
~0.15 s/token theoretical, ~5–7 tokens/second realistic). A fine-tuned 14B
specialist reads ~10 GB per token → ~20–30 tokens/second. For interactive
use (chat, real-time drafting), the specialist is 3–5× faster. For batch
work (digests, research synthesis), the 70B's quality may win despite slower
generation.

**Single point of failure.** If STRATUS is down, all three boxes lose LLM
serving. With specialists, each Spark is independent — CUMULUS can serve
even if CIRRUS is down. Mitigation: run a standby model on CUMULUS as
fallback, or keep Claude/Gemini as the always-available cloud fallback.

**Training interference.** Fine-tuning a general model on pedagogy data
might degrade its real-estate or snow-management performance (catastrophic
forgetting). Specialists don't have this problem — each is isolated. A
hybrid (general model + light LoRA adapter per client) avoids interference
while keeping the "one model" operational model. This is the "LoRA stacking"
pattern: one base model + N small adapter files, swapped at inference time.

**"Teaching" is harder than it sounds.** Incremental training of a general
LLM requires curated datasets, eval suites, and careful learning-rate
schedules. QLoRA on a narrow domain (pedagogy) is well-understood; continuous
improvement of a general model across multiple domains is a research project.
The realistic path: periodic QLoRA fine-tunes of the base model on curated
data from all domains, validated against per-client eval suites before
deployment.

**Memory for a 70B+.** A 70B at Q4 (~40–48 GB) fits one Spark (128 GB) with
room for KV cache. A 100–200B model needs 2 Sparks pooled. A frontier-scale
model (400B+) needs 3+ Sparks or a Station. Starting with 70B is fine;
growing beyond that means buying more Sparks or pooling.

---

## 4. Recommended hybrid: one base model + per-client LoRA adapters + agents

This captures the operational simplicity Buddy wants (one model to serve,
one model to improve) while avoiding the tradeoffs:

1. **One base model** on STRATUS (start with Qwen 2.5 72B or Llama 3.3 70B,
   Q4 quantized). Fits on a single Spark.

2. **Per-client LoRA adapters** (small, ~100–500 MB each) instead of separate
   fine-tuned models. Load the base model once, swap adapters per request.
   No catastrophic forgetting — each adapter is trained on one domain.
   vLLM and llama.cpp both support multi-LoRA serving.

3. **Agent layer** (system prompt + tools + RAG index) per client. This is
   where the per-client logic lives: pedagogy agent knows about lesson plans
   and curriculum standards; real-estate agent knows about MLS and
   comparable sales; snow agent knows about weather APIs and property
   management. The agent layer is cheap to build and iterate — no training
   needed, just prompt engineering.

4. **Claude/Gemini fallback** when the local model is stuck (hard reasoning,
   long context, multi-step planning). The agent layer detects "stuck" and
   escalates to the cloud API. This is the "only reaches out to
   foundationals when stuck" pattern Buddy described.

5. **Monthly research → training data pipeline.** The STRATUS monthly
   research job gathers new hardware/model/technique findings. Use these
   to inform: (a) agent prompt updates, (b) RAG index updates, (c) curated
   QLoRA training data for periodic base-model improvements. The research
   itself does not go into the model raw — it is curated into training
   examples by the agent layer.

```
                    STRATUS (Spark, 128 GB)
                    ┌──────────────────────────┐
                    │  Base model: Qwen 2.5 72B │
                    │  (Q4, ~45 GB weights)     │
                    │                           │
                    │  LoRA adapters (swap):    │
                    │   ├── pedagogy (500 MB)   │
                    │   ├── real-estate (300 MB)│
                    │   ├── snow-mgmt (200 MB)  │
                    │   └── general (none)      │
                    │                           │
                    │  Agent layer (per request):│
                    │   ├── system prompt       │
                    │   ├── tool definitions    │
                    │   ├── RAG index (shared)  │
                    │   └── fallback detector   │
                    └──────────┬───────────────┘
                               │
                    ┌──────────┴───────────────┐
                    │  Inference API (vLLM)    │
                    │  10GbE Ethernet          │
                    └──────┬──────────┬────────┘
                           │          │
              ┌────────────┘          └────────────┐
              ▼                                     ▼
         CIRRUS (API client)              CUMULUS (API client)
         (dev + digest +                   (beta serving +
          data prep)                        load balancer)
              │                                     │
              ▼                                     ▼
         Claude/Gemini fallback            Claude/Gemini fallback
         (when STRATUS is stuck)           (when STRATUS is stuck)
```

---

## 5. Hardware implications

| Component | Requirement | Notes |
|-----------|-------------|-------|
| STRATUS server | 1× DGX Spark (128 GB) | Fits 70B at Q4 + KV cache + LoRA adapters |
| STRATUS network | 10GbE Ethernet minimum | For serving requests from CIRRUS/CUMULUS |
| STRATUS network (optional) | QSFP56 200G via ConnectX-7 | For memory pooling with CUMULUS (models >128 GB) |
| CUMULUS | Existing 2 Sparks | No local model needed — becomes an API client |
| CIRRUS | Existing Mac Studio | No local model needed — becomes an API client |
| Switch | See STRATUS-Network-Switch-Spec.md | MikroTik CRS804-4DDQ if pooling needed |

**Memory math for 70B + multi-LoRA serving on one Spark:**
- Base weights (Q4): ~45 GB
- KV cache (256 concurrent, 4K context): ~20–30 GB
- LoRA adapters (in memory, swappable): ~2 GB (4 adapters × 500 MB)
- System/overhead: ~5 GB
- Total: ~72–82 GB of 128 GB → comfortable headroom

**If the model grows to 100–200B:** pool memory across STRATUS + 1 CUMULUS
Spark (256 GB) via the QSFP56 fabric. Requires the switch. CUMULUS's second
Spark continues as an API client.

---

## 6. Migration path from current plan

1. **Now:** STRATUS does not exist yet. CUMULUS runs local models per the
   current plan. No change.

2. **Step 1 — Stand up STRATUS with the base model:**
   - Buy 1× DGX Spark (~$4,699), connect via 10GbE
   - Install vLLM with Qwen 2.5 72B (Q4) + multi-LoRA support
   - Port the pedagogy RAG index + system prompt as the first agent
   - Serve inference to CIRRUS over the LAN

3. **Step 2 — A/B test on CUMULUS:**
   - Run the same client workloads two ways: (a) CUMULUS's existing
     specialist model, (b) STRATUS's general model + LoRA + agent
   - Measure: quality (human review of outputs), latency (tokens/second),
     cost (electricity, no cloud spend), reliability
   - This is the decision point. If the general model + agent matches or
     beats the specialist on quality at acceptable latency, migrate.
   - If not: keep specialists on CUMULUS, use STRATUS as a research/training
     box that produces improved LoRA adapters for the specialists.

4. **Step 3 — Migrate CUMULUS from local models to STRATUS API client:**
   - If the A/B test favors the general model: point CUMULUS at STRATUS's
     inference API instead of running local models
   - Frees CUMULUS's memory for: load-balanced serving (run a second
     STRATUS replica on CUMULUS for HA), data prep, or training
   - Keep one specialist on CUMULUS as emergency fallback

5. **Step 4 — Add the switch + pooling (when model outgrows 128 GB):**
   - Buy the MikroTik CRS804-4DDQ + breakout cables (~$1,500)
   - Pool STRATUS + CUMULUS memory (256 GB) for larger models
   - See STRATUS-Network-Switch-Spec.md for details

6. **Step 5 — Continuous improvement loop:**
   - Monthly STRATUS research job gathers findings
   - Curate findings into training data + agent prompt updates
   - Periodic QLoRA run on STRATUS: improve base model or refresh adapters
   - Eval against per-client test suites before deploying

---

## 7. What to try first (concrete next step)

**On CUMULUS, before buying anything:**

1. Pull Qwen 2.5 72B (Q4) onto one CUMULUS Spark using the existing 128 GB
   memory. vLLM supports multi-LoRA serving.
2. Create a pedagogy LoRA adapter from the existing fine-tune data (or use
   the existing pedagogy specialist's training set).
3. Build the pedagogy agent: system prompt + tools + RAG index (already
   exists from the specialist).
4. Serve both ways on the same Spark:
   - Old: pedagogy specialist (fine-tuned 14B) directly
   - New: 72B base + pedagogy LoRA + agent layer
5. Run 10–20 representative pedagogy tasks through both. Compare:
   - Quality (does the output meet the rubric?)
   - Latency (tokens/second at the same concurrency)
   - Memory headroom (how much of 128 GB is used?)

This costs nothing but time and tells us whether the single-LLM approach
works before buying a 3rd Spark. If the 72B + LoRA + agent matches the 14B
specialist on quality, the approach is validated. If the 14B specialist wins
on quality (fine-tuned models often do on narrow tasks), we have data to
decide whether the operational simplicity is worth the quality tradeoff.

---

## 8. Decision needed from Buddy

This is a proposal, not a done deal. The decision points are:

1. **Approve the direction?** (single LLM + agents vs. multiple specialists)
2. **Start with A/B test on CUMULUS** (no purchase needed), or **buy a 3rd
   Spark as STRATUS** and start building there?
3. **Base model choice:** Qwen 2.5 72B (strong general, Chinese bilingual),
   Llama 3.3 70B (strong English, permissive license), or something else?
4. **When to add the switch:** proactively (wire the fabric now) or
   reactively (only when memory pooling is needed)?

The A/B test on CUMULUS (step 7) can start immediately with no purchases.
That is the recommendation: try before you buy.