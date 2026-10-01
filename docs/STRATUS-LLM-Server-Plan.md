# STRATUS — LLM Server Plan (frontier-class headroom)

*Planning doc, Session 361 (2026-10-01). Companion to
`STRATUS-Single-LLM-Strategy-Proposal.md`, `STRATUS-Production-Sizing-and-Architecture.md`,
and `STRATUS-Network-Switch-Spec.md`. This doc answers a separate question from
those: **what does a server need to host an "Opus-class" open model locally**
— larger than what one DGX Spark can hold — with concrete parts, prices,
power, and a staged path that does not break the $7.3K baseline plan.*

Status: plan, not a purchase. Nothing here is approved spend; the baseline
Spark plan and the CUMULUS A/B test remain the pre-purchase gates.

---

## 1. Market reality as of October 2026 (changes the earlier advice)

Verified October 1, 2026 via price searches (sources in §10):

1. **Global DRAM/GDDR7 shortage, expected to persist through 2027** (TrendForce
   via trade press). Memory prices up industry-wide; system RAM is affected too.
   Do not build a plan that assumes prices fall in 6 months.
2. **RTX 5090 is scalped, not priced.** MSRP $1,999; real direct-retail stock
   is scarce (Newegg direct ~$4,700, Micro Center in-store ~$4,300); marketplace
   listings run $6,400–$10,000. A 4×5090 build at honest prices means paying
   $17–19K for GPUs alone, at scalper risk for the other two cards.
3. **Apple discontinued the M3 Ultra 512GB option** (March 2026); the line now
   caps at 256 GB (+$2,000 upgrade → ~$6,000 class). Secondary-market 512 GB
   units trade at $12K–$18K. The 512 GB Tier-A path I described a week ago no
   longer exists at that price — it is now a $12K+ used-market path.
4. **NVIDIA's RTX PRO Blackwell line (96 GB cards, ~$7,000–9,000 per
   the trade press — verify exact quotes)** is where AI buyers were redirected,
   which is part of why gaming 5090s vanished. RTX 60-series reportedly slips
   to 2028, so there is no "wait for next gen" relief.

Conclusion for the plan: the cheap-gaming-GPU route (my earlier "Tier B:
4×5090") is currently the worst value. The honest options are Spark-family
(unified memory, unpooled price ~$4,699 each), pro-Blackwell cards, or used
datacenter GPUs.

## 2. The gate: memory is the spec that decides everything

Decoding speed is memory-bandwidth-bound (every generated token must read all
weights); capacity decides which models run at all. Sizing includes weights +
10–20% overhead + KV cache (scales with context; budget 3–10 GB per 32k-token
session at these scales).

| Model (late-2025/26 class) | Params | 4-bit weights | 8-bit | Fits 1 Spark (128 GB)? | Needs |
|---|---|---|---|---|---|
| Llama 3.3 / Qwen 2.5 72B (dense) | 70B | ~42 GB | ~75 GB | Yes, comfortable | — |
| gpt-oss-120b (MoE, 5.1B active) | 120B | ~60 GB | ~120 GB | Yes (running today on CUMULUS class HW) | — |
| GLM-4.5-Air (MoE) | 110B | ~60 GB | ~115 GB | Yes | — |
| MiniMax-M2 (MoE, 10B active) | 230B | ~120 GB | — | Tight, Q4 only | pooling at 8-bit |
| Qwen3-235B-A22B (MoE) | 235B | ~130 GB | ~250 GB | No | 2 Sparks (Q4) |
| GLM-4.6 (MoE, 32B active) | 355B | ~185 GB | ~360 GB | No | 2–3 Sparks / 2×96 GB |
| DeepSeek-V3.2 / R1 (MoE, 37B active) | 671B | ~380–400 GB | ~700 GB | No | 4 Sparks / 8×80 GB |
| Kimi K2 (MoE, 32B active) | 1T | ~600 GB | — | No | 8×80 GB class |

"Opus-level feel" among open models in practice = GLM-4.6 / DeepSeek-R1 /
Kimi K2 / Qwen3-235B territory (frontier MoE), reached by quality per token.
No open model equals Opus 1:1; treat this as headroom to close the gap.

## 3. Options, costed

### Option 1 — baseline (already approved-in-principle, §5 of the strategy doc)

Spark-based STRATUS, 70B-Q4 class base model + LoRA + agents.

| Item | Price |
|---|---|
| MikroTik CRS804-4DDQ switch | ~$1,295 |
| Breakout DACs + Cat 6a | ~$160 |
| APC Smart-UPS 1500VA | ~$1,100–1,500 |
| DGX Spark (3rd, as STRATUS) | ~$4,699 |
| **Total (matches purchase-plan memory)** | **~$7.3–7.8K** |

Runs: 70B dense Q4 (~5–7 tok/s single stream), gpt-oss-120b, GLM-4.5-Air,
MiniMax-M2 Q4. The A/B test on CUMULUS (no purchase) comes first per the
strategy doc.

### Option 2 — memory growth by Spark pooling (the path already planned)

The strategy doc's step 4: pool Sparks over the CRS804's 200 Gb/s QSFP56
fabric (each Spark has one ConnectX-7 port).

| Config | Pool | Candidate models | Ballpark cost (marginal) |
|---|---|---|---|
| 2 Sparks pooled | 256 GB | Qwen3-235B-A22B Q4; GLM-4.6 at low quant | +$4,699 (4th Spark) |
| 3 Sparks pooled | 384 GB | GLM-4.6 Q4 comfortable | +$4,699 |
| 4 Sparks | 512 GB | DeepSeek-class Q4 | +$4,699 |

Honest cautions: pooled-Spark serving (EXO-style or llama.cpp RPC over QSFP56)
runs big MoEs but is bandwidth-limited — expect single-digit tok/s for
DeepSeek-class; fine for the batch/nightly workloads this house runs
(digests, research synthesis, agent fleet), frustrating for interactive chat.
Needs benchmarking on real arrivals before relying on paper numbers (rule 1:
the 1–5 tok/s figures here are estimates, not measurements).

### Option 3 — pro-Blackwell workstation node (interactive frontier serving)

2× RTX PRO 6000 Blackwell (96 GB each = 192 GB pool, ~1.6–2 TB/s aggregate):

| Part | Approx. | Notes |
|---|---|---|
| 2× RTX PRO 6000 Blackwell 96GB | ~$8,500–9,000 ea (verify quote) | ECC, pro drivers, blower |
| Threadripper/EPYC board + 24–32c CPU | ~$2,500 | lanes for 2 PCIe 5 x16 |
| 256 GB DDR5 RDIMM | ~$2,000–4,000 in shortage | **verify — RAM prices volatile** |
| NVMe storage (2×4TB PCIe 4/5) | ~$400–600 | models + KV-offload |
| PSU (dual 1600W or 2000W unit) + chassis | ~$1,000–1,500 | 2× blower cards |
| 10 GbE NIC (tie into CRS804/LAN) | ~$150–300 | |
| **Total** | **~$23–26K** | plus UPS share |

Runs GLM-4.6 Q4 (~185 GB) with real vLLM multi-tenant concurrency — the
strong single-node answer for serving the whole agent fleet interactively.
Does NOT fit DeepSeek/Kimi-scale weights (need 380–600 GB); those stay on the
pooled Sparks (Option 2) or cloud.

Flagged, not recommended: 4×5090 (~$22–25K at honest retail, scalper market at
375% MSRP elsewhere) and 8× used H100 80GB (~$120K+, only if STRATUS serving
becomes revenue-bearing).

## 4. Recommendation (staged, matching Buddy's "solid environment going forward")

1. **Now:** run the CUMULUS A/B test (no purchase) — validates the whole
   single-LLM strategy for free.
2. **Buy the baseline (Option 1, ~$7.3K):** 3rd Spark + switch + UPS. This is
   already wired through the existing STRATUS docs; approve when ready.
3. **Growth path A (memory):** buy Sparks one at a time and pool over the
   already-specified switch — each step is +$4,699, reversible, and the
   hardware keeps serving whatever size the software supports. Best
   shortlist: gpt-oss-120b → Qwen3-235B-A22B → GLM-4.6.
4. **Growth path B (only if interactive frontier serving is genuinely
   needed):** the 2×RTX PRO 6000 node (~$23–26K). Gate it on measured
   evidence: if pooled-Spark tok/s is fine for the fleet's real workloads,
   path B stays unbought.
5. **Do not** buy scalped 5090s, do not chase the used 512 GB Mac Studio
   market, and re-check the market before any GPU purchase in 2027 (RTX 60
   ~2028; shortage easing "late 2027" is a forecast, not a promise).

## 5. Environment requirements for whatever hosts the big model

- **Power:** Sparks + switch + CIRRUS ≈ 670 W typical / 1,300 W peak (from
  the switch spec). A 2-GPU workstation node adds ~1,500 W sustained —
  dedicated 240 V / 20 A circuit before it lands. UPS sizing for the whole
  kit in the switch spec; the big node wants its own ≥2 kVA unit.
- **Cooling:** pooled Sparks sit in the room like the existing ones; the GPU
  node is a sustained ~1.5 kW space heater — needs a ventilated spot, not a
  closed closet (same discipline as CIRRUS).
- **Network:** all paths converge on the CRS804/10 GbE LAN; inference
  traffic is text. CIRRUS (Mac Studio, 10 GbE) stays an API client.
- **Storage:** ≥2 TB NVMe per model host; keep model GGUF/safetensors on a
  NAS or replicated NVMe — a corrupted 400 GB download should be replaceable
  by re-pull without taking the fleet down.
- **Software:** vLLM (multi-LoRA, paged KV) as the serving target for NVIDIA
  and single-Spark; llama.cpp/EXO for pooled Spark; OpenAI-compatible
  endpoints so CIRRUS/CUMULUS switch clients via config only.
- **Ops:** same runner/API discipline as CIRRUS/CUMULUS — health check, log
  capture, no secrets in URLs; register it in PROJECT-RUNTIME-REGISTRY when
  it is real, not before.

## 6. Decision points for Buddy

1. Approve Option 1 purchase (existing ~$7.3K plan) — or wait for the A/B
   result first (recommended order, but the switch/UPS are useful either way).
2. Pre-approve Option 2's per-Spark increments (+$4,699 each) as the memory
   growth path rather than deciding per purchase.
3. Option 3 (~$23–26K) stays gated on measured interactive-latency evidence
   from the pooled path.
4. Base-model shortlist stays as in the strategy doc (start 70B/gpt-oss-120b;
   grow through the pooled path as quality demands).

## 7. Sources for the October 2026 price/market claims

Price checks performed 2026-10-01: RTX 5090 retail scarcity and scalping
(Newegg direct $4,699.99 Gigabyte; Micro Center ~$4,299; marketplace
$6,395–$10,000); Apple's March 2026 removal of the M3 Ultra 512GB option,
256 GB upgrade repriced from $1,600 to $2,000, used-market 512 GB units at
$11,979–$17,999; RTX PRO Blackwell line ~$7,000–9,000 with NVIDIA steering
silicon to pro cards; GDDR7/DRAM shortage through 2027 (TrendForce); RTX
60-series reportedly at 2028. Full link list recorded in the session log and
chat; individual price points should be re-verified at purchase time — this
market moves weekly and this doc is a snapshot.