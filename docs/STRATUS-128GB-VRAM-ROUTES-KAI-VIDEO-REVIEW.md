# 128 GB VRAM routes — KAI video review + Strata offload note

*Written 2026-10-11 (S399) from Buddy's /todo prop-2026-10-11-559 ("every ways
to get 128 GBVRAM for a local IA at full content — YouTube video") and
prop-2026-10-11-560 ("review Strata — something to handle memory look ups").*
Both items turn out to be ONE subject: Kai's video covers every route to a
128-GB memory box for local AI, and its punchline — and the answer to 560 — is
the **Strata** inference engine, which handles the model's memory look-ups by
offloading them instead of buying a bigger box.

**Source video:** "Every Ways to Get 128GB VRAM for Local AI at Full Context"
<https://www.youtube.com/watch?v=NA9D49kQFuU> — reviewed from its auto-caption
transcript. ⚠️ Auto-caption garbling is real in this one: proper nouns are
transcriptions and several dollar figures lost digits ("$3,99", "$5,99").
Cross-checked where possible (Strix Halo prices vs the Oct-2026 mini-PC roundups;
Strata vs the engine's own paper). Treat every number below as the creator's
reported figure unless marked verified.

---

## TL;DR for our estate

1. **We already own the "pool" route twice over.** Both DGX Spark cards hold
   full 4-bit builds of 120B-class models — which is exactly what the video's
   $5,995-$6,950 machines exist to replicate. Nothing in this video argues
   against the 2026-10-10 STRATUS decision (Mac Studio M5 Ultra 512GB).
2. **The Stratus M5 Ultra is one better than the 128-GB Mac route in the
   video:** it escapes the ~96-GB GPU-addressability cap AND has ~4× the pool
   (512 GB), so it holds 200B+ models and does NOT need the offload tricks.
3. **But the video's core insight changes our sizing argument, not our
   destination:** with Strata-style offload, a $2,400 gaming PC (12-GB card +
   64 GB RAM) reaches 3-bit quality of the 125B class — so for SINGLE lanes
   that only run Flash-NeXT-family models, buying 128 GB of memory is optional.
   Where it breaks: multi-user/agent-swarm serving (our estate's real shape),
   and anything that leaves the Flash-NeXT-NeXT family.
4. **Strata answer (todo 560):** Strata is a CUDA+CPU engine purpose-built for
   Qwen3.8-Flash-Next (125B MoE, ~6B active/token) — it keeps a small hot set
   in VRAM, experts in pinned host RAM, and leaves the 28.8-GB n-gram LOOKUP
   TABLE on the SSD, reading ~16 rows (~23 KB) per draft token. It is the
   "something to handle memory look-ups," at the cost of being a one-model
   engine (review below).

## Why 128 GB is *the* number (video's premise)

The threshold comes from one model: **Qwen3.8-Flash-Next** ("Quinn 3.8 flash
next" in the captions) — ~125 B parameters, of which a **~51-B n-gram lookup
table** holds word-sequence stats, but only **~6 B active parameters per
token**. Its 4-bit files land at 94–124 GB depending on format — which fits in
128 GB and nothing smaller, hence the "everyone says 128 GB" folklore.

⚠️ **The cap nobody advertises:** not all "128 GB" boxes give the GPU 128 GB.
Windows-on-Strix-Halo caps GPU-assignable memory at ~96 GB by default (Linux
lets owners reach ~100–120); macOS caps the GPU lock around ~96 GB (the main
Mac engine's docs tell you to raise it manually — a DAY-ONE step for Stratus
too); even the DGX Spark exposes ~122 usable GB, and NVIDIA's own 4-bit file
at 124 GB means **the $6,950 Spark also offloads** to SSD in the popular
recipe. The 128 GB printed on the box is a ceiling, not a promise.

## The routes, priced (Oct 2026)

| Route | Cost | Bandwidth | Reported speed (Flash-NeXT 4-bit unless noted) | Watch-outs |
|:--|:--|:--|:--|:--|
| **Strix Halo mini PC** (Bosgame M5 / GMKtec EVO-X2 / Framework / Minisforum MS-S1 Max) | $2,999–$4,349 for 128 GB — Bosgame cheapest in stock, Framework $3,449 (out of stock) [verified vs Oct-2026 roundups] | 256 GB/s | 47–54 tok/s write; prompt-read 1,227 tok/s on the community fork vs 113 on the default engine | Windows default 96-GB GPU cap (Linux ~120); pick the ENGINE first, not the brand; same chip in every box |
| **Mac Studio M5 Max 128** | video: $5,999 (40-core GPU config is "the only way to get 128"); one roundup quotes $5,099 | 614 GB/s | MLX serve ~40 tok/s (pros) / 75 (code); prompt-read unpublished | Raise the ~96-GB GPU-locked cap day one; quiet + best polish |
| **DGX Spark** | $6,950 (raised ~74% since launch); 64-GB model $4,999 | ~273 GB/s | ~41 tok/s median, 27 on pros (17 without draft-ahead) | Offloads anyway (124-GB file vs ~122 usable); its old prompt-reading edge is now ~4 s on 16k-token reads, not 83 s; 2×64-GB = $10k |
| **"RTX Spark"** (different chip; Surface Laptop Ultra / Dev Box, Oct 7 launch) | $5,899 / $5,999 | ~273–307 GB/s | Microsoft claims 2.1×/6.2× vs M5 Pro — **measured on pre-production 64-GB units, with sparsity flags** | Wait for independent timing before $6k |
| **Used server cards** (the viral build: 4× Radeon Pro V620 + EPYC + 256-GB DDR4) | ~$3,000 at the time; **the deal is gone** — prices climbed after the video went viral. Today: 4× Intel Arc Pro B70 or R9700 ≈ $6,800; 4× used MI50s $2,300–2,600 (maintenance-mode GPU stack); RTX Pro 6000 Blackwell 96 GB $8,900–13,250 (our S67 re-price saw the card at ~$16,000 2026-08-18) | — | 60–70 tok/s — fastest writer of anything in the video | Draws 500–600 W (~$120/yr at 4 h/day, 15¢/kWh vs ~$38/yr Strix Halo); multi-card host needed; availability roulette |
| **Offload route — Strata on a modest PC** (RTX 5070 12 GB + 64 GB RAM) | ~$2,400 new; ~$880 if you already own a 12-GB card (RAM only) | card-limited | 94 tok/s (2-bit), 50–62 (3-bit), prompt-read 1,750 tok/s | ONE model family only (see Strata review); 130k context vs 256k on the 27B class reported by owners |

## The bit-economics (the video's real punchline)

The 3-bit (mostly-quantized) file keeps **99.4%** of the full-precision score
across the lab's three benchmarks — but one of those benchmarks is already
saturated (full precision gets everything right), so "99.4% of perfect is
still perfect." An owner calls the 2/3-bit builds "lobotomized"; another
claims the 4-bit build across 3×3090s "blows 27B out of the water." **Nobody
has published 3-bit vs 4-bit side by side on a long agent session** — which is
precisely our use case — so that gap is real.

Price of the fourth bit: **~$700** if you build a new 12-GB-card PC vs the
Strix Halo route, **~$2,200** if you only need to add 64-GB RAM to an existing
gaming PC. That is the whole argument for the "small desk + shelf" setup.

## Strata review (todo 560) — the engine that handles memory look-ups

What it is: a purpose-built **CUDA + CPU inference engine** for
Qwen3.8-Flash-Next (512 experts/layer, 10 active/token ≈ 6 B active params),
built on parts of llama.cpp/ggml (MIT), with a Windows one-click installer
(the garbled captions called it "Strata" — verified: that is the real name;
"Niko1221/Strata").

How it handles memory, per its own paper:

- **VRAM:** ~3.5 GB always-in-GPU dense weights + an adaptive expert cache
  (~4,500 of 24,576 experts hot).
- **Host RAM:** a **pinned 34–43 GB arena** holding all experts; misses are
  computed on the CPU *concurrently* with GPU work so decode doesn't stall.
- **The lookup table:** the ~28.8-GB n-gram embedding table **stays on the
  SSD** and is read through the OS page cache — only ~16 rows (≤23 KB) per
  draft token. This is the named solution to "memory look-ups."
- **Speed tricks:** uses the model's MTP layer for exact-greedy speculative
  decoding (1.6–1.8×); Windows pinning needs per-layer ranges because one big
  34–43-GB lock fails on Windows.

Limits, plainly:

1. **One model family.** Its gains come Flash-NeXT's own design (small active
   set, offload-friendly lookup table). Point it at a different large model
   and you're back to stock llama.cpp behavior.
2. **Context is tighter** than a full-memory box on big prompts (owners report
   130k usable vs 256k they get from Qwen3.8-27B).
3. **The next Flash model may not cooperate** — the family has grown every
   version, and the 125B is a preview of the Qwen-4 architecture.
4. **Multi-user/agent-swarm serving changes the math:** 4× R9700 cards serve 8
   clients at 165 tok/s total; the DGX Spark holds 26 tok/s per stream ×4; a
   single 12-GB-card PC is a one-person machine. Our estate serves multiple
   concurrent lanes, so the pool matters to us even if a single lane doesn't.

Rent-vs-buy guardrails from the video: RunPod rents an RTX 6000 (96 GB, real
VRAM) at $29/h → $3,999 buys ~1,483 hours (~1 year at 4 h/day); Alibaba's
hosted Qwen3.8-Flash costs $0.47/M output tokens → $3,000 = 6.6 B output
tokens, ~4 years of writing at 50 tok/s nonstop. If a box sits idle most of
the week, rent the bit.

## What this means for the STRATUS build (Nov)

- **Supports the standing decision.** The M5 Ultra 512 GB holds 4× the pool at
  ~2× the M5 Max's bandwidth, escapes the 96-GB cap problem entirely at our
  footprint, and does not depend on Strata's one-family bet.
- **Day-one checklist item:** raise the GPU memory-limit cap before first
  full-context runs (the video's Mac route warning applies to Ultra too, not
  just to fitting 128-GB models — headroom planning needs the real cap).
- **Where the video genuinely challenges us:** if a future STRATUS lane is a
  single Flash-NeXT-family worker, a $2,400 Strata box does that job — so the
  512-GB purchase should be justified by SERVING (concurrency, 200B+ targets,
  fine-tune residency), which is exactly how the purchase plan frames it.
- **Watch:** Kai promises a follow-up with hands-on benchmarks; that should
  settle the 3-bit-vs-4-bit-on-agent-sessions gap. Candidates for the
  discovery lane once the channel is identified.

## Sources

- Video (reviewed): https://www.youtube.com/watch?v=NA9D49kQFuU —
  "Every Ways to Get 128GB VRAM for Local AI at Full Context" (Kai)
- Forum transcript anchor: artofsm.art thread (same series, 32-GB installment):
  https://www.artofsm.art/t/every-ways-to-get-32gb-vram-for-local-ai-at-full-context/24938
- Strata paper: https://raw.githubusercontent.com/Niko1221/Strata/master/docs/paper/Strata-Paper.pdf
- Strata docs site: https://stratallm.org/
- Mini-PC price re-check (2026-10): https://computingforgeeks.com/ryzen-ai-max-395-mini-pc-comparison/ ,
  https://minipcdeals.net/bosgame-m5-review-the-cheapest-128gb-strix-halo-mini-pc-in-2026/ ,
  https://localaimaster.com/blog/best-strix-halo-mini-pc
- Estate cross-refs: `docs/STRATUS-Production-Sizing-and-Architecture.md`,
  `docs/STRATUS-Hardware-Watch.md` (S67 re-price), STRATUS purchase-plan memory
  (2026-10-10 Mac Studio M5 Ultra 512 GB decision).