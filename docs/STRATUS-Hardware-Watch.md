# STRATUS Hardware Watch — living decision tracker
*Created S61 (2026-08-11). Fed by the daily digest's hardware watch; consolidated in the monthly
STRATUS review. This is where hardware news becomes a BUY decision. Companion to
`docs/CUMULUS-Autonomy-and-Learning-Plan.md` (§4) and `docs/STRATUS-Production-Sizing-and-Architecture.md`.*

## Latest purchase decision — Buddy, S406

**Wait for the new Mac Studio.** Buddy declined the refurbished Mac Studio at
the price under discussion: "I not spending this much money on a refrb."
This supersedes earlier purchase-timing assumptions, including the November
order mentioned in the research log. Revisit the purchase when the new model
is available, using verified specifications and pricing. No order date is set.

## Official Apple baseline and alert — S406, October 11, 2026

Apple has already announced the **M5 Max / M5 Ultra Mac Studio**. Its
[August 25 announcement](https://www.apple.com/newsroom/2026/08/apple-introduces-new-mac-studio-with-m5-max-and-m5-ultra/)
lists U.S. starting prices of **$2,499** and **$5,499**, respectively. These
are base prices, not a quote for the STRATUS configuration.

The [official specifications](https://www.apple.com/mac-studio/specs/) list
M5 Ultra configurations up to a 36-core CPU, 80-core GPU and 512GB unified
memory, with 1.2TB/s memory bandwidth. The
[live U.S. order page](https://www.apple.com/shop/buy-mac/mac-studio), checked
in the browser, says **512GB is coming late October** and offers no selectable
512GB memory option yet. Its exact configured price and order-opening date
remain unconfirmed. The older rumor/availability statements below are historical.

Buddy requested an alert for official specifications, pricing and when orders
open, then explicitly asked to run it on CIRRUS and chose the existing CIRRUS
Telegram bot. **Migrated October 12, S406:** system LaunchDaemon
**`com.cirrus.macstudiowatch`**, daily **16:45 America/New_York**, sends to Buddy
through **`@CIRRUSdigest_bot`**. CIRRUS owns the fetch, saved state and delivery;
the MacBook, desktop app and a CIRRUS login session are unnecessary. The original
Codex heartbeat **`watch-new-mac-studio-availability`** is **PAUSED** as rollback.
No purchase is authorized.

The lightweight, deterministic check reads Apple's specifications and U.S.
store pages, watches chip/specification, base-price, memory-selector and
availability-notice changes, and stays quiet when unchanged. It distinguishes
512GB storage from memory and labels starting prices accurately. A newly listed
512GB memory option prompts an alert with the Apple configuration link; it is
not automatically called a confirmed checkout or an exact configured quote.
The watch continues until explicitly retired, so subsequent pricing and order
notice changes can still be reported. It uses no paid model or search API.

Operational source: `cirrus-repo/mac_studio_watch.py` and its matching plist.
On CIRRUS these live in `/Users/buddy/projects/cirrus-digest/`; the installed
plist is `/Library/LaunchDaemons/com.cirrus.macstudiowatch.plist` (root:wheel,
0644, runs as buddy). `python3 mac_studio_watch.py` previews without sending or
saving; `--status` reports recent checks. `--live` is restricted to the deployed
CIRRUS checkout. State is `logs/mac-studio-watch.json`, with an exclusive lock
and atomic writes; only a successful Telegram response advances the baseline.
One alert reports a failed Apple check, with a recovery alert when it succeeds.
The existing run ledger (26-hour tolerance) and watchdog monitor this job.

Initial live verification: CIRRUS launchd completed at **09:54 ET October 12**,
exit 0; Telegram accepted the activation notice and the run ledger recorded
success. The Apple baseline remained unchanged: 512GB memory coming late October.
A second launchd run at **09:54:45 ET** exited 0 and recorded unchanged; the
alert count stayed at one. The post-install live window audit saw all 47 jobs
including the new 16:45 entry and found no policy violations. Eight focused
offline tests passed on both the MacBook and CIRRUS; existing run-ledger,
watchdog and runtime-window tests passed, and scrub verification found zero
cleartext tokens. Server code is commit `5bc7ecb`, deployed to both repositories;
the watch is installed only on CIRRUS.

## Why this exists (Buddy, S61)
The daily pulls should actively hunt for STRATUS serving hardware and flag when good options become
available. STRATUS must serve our self-learning model **fast AND smart** — enough GPU/CPU + memory to
respond quickly *and* run a large, capable model. We start small on CUMULUS; when we move to STRATUS we
must not lose speed or capability. **No rush to buy — target Fall/Winter 2026+** (DRAM shortage eases,
M5 Ultra ships). This doc tracks the field so we buy the right thing at the right moment.

## The STRATUS requirement — "fast AND smart" made concrete
Two independent dials; STRATUS needs BOTH:
- **Smart (capacity):** hold our best model at good precision — target a fine-tuned **~70–120B** open model
  now, with headroom toward **200B+** later. Rule of thumb: 4-bit ≈ 0.5 GB/1B params, FP16 ≈ 2 GB/1B,
  **plus** KV-cache headroom for concurrent project requests.
- **Fast (bandwidth):** decode speed tracks memory **bandwidth**, not capacity. Interactive target:
  **≥ ~30–50 tok/s** single-stream + a few concurrent requests → wants **≥ ~800 GB/s**, ideally
  **1.5 TB/s+**. This is the dial CUMULUS's DGX Sparks are weak on (273 GB/s), so STRATUS must do better.
- **Net target:** a box that holds a ~100B-class model AND generates fast. Bigger model → more memory;
  more speed/concurrency → more bandwidth. Budget sets which corner we land in (table below).

## ⚠️ S67 RE-PRICE (2026-08-18) — the market moved hard against buying

**Every anchor price in the table below is stale and low.** Re-verified live this session:

| Item | S61 tracker said | Actual, 2026-08-18 | Delta |
|:--|:--|:--|:--|
| RTX PRO 6000 Blackwell 96GB | ~$10–12K system | **$16,000 for the CARD** (launch MSRP was $8,565 Mar'25; $13,250 Jun'26) | ~2× |
| Mac Studio M3 Ultra | 256 GB (512 pulled Mar'26) | **Capped at 96 GB.** The 256 GB tier is gone too | Unbuyable at target size |
| DGX Spark | $4,699 | $4,699 (raised from $3,999 on memory supply) | Holding |
| M5 Ultra Mac Studio | "rumored ~Oct'26" | Still unannounced; Gurman: memory shortage is gating it. Apple has *tested* 768 GB but supply may prevent shipping it | Slipping |

**Root cause: the DRAM/GDDR7 shortage is worse and longer than this doc assumed.**
DRAM is forecast up **>400% from start-2024 to end-2026**; AI datacenters take ~70% of
high-end DRAM. Relief is now forecast for **late 2027–2028**, and SK Hynix's CEO expects
**2027 to be the worst supply year in the industry's history**. The old buy-trigger
"DRAM shortage eases (2027–2028) → revisit" should be read as **"do not buy the big box
until it does."**

**The capacity ceiling that changes the recommendation.** The table below ranks
RTX PRO 6000 as the front-runner. For *this* goal — running a model good enough to retire
the paid council — **it is disqualified on capacity, not price.** A 235B-class MoE at
4-bit needs ~118 GB of weights; the card holds 96 GB. It is capacity-capped *just below*
the tier that would actually replace Claude/Gemini, so $16K buys a very fast 70B box and
not independence.

**MoE changed the math this doc was written around.** The "smart vs fast" framing below
assumes dense models, where capacity and bandwidth scale together. Mixture-of-Experts
decouples them: Qwen3 235B-A22B needs capacity for 235B but bandwidth for only ~22B
active. That means **capacity is the binding constraint and bandwidth is far less
critical than the ★ ratings below imply** — which is what makes the already-owned Spark
path viable.

## Candidate options (specs current 2026-08; VERIFY live via the watch before buying)
| Option | Memory | Bandwidth | CUDA? | ~Price | Fast? | Smart (max model)? | Fit for STRATUS |
|:--|:--|:--|:--|:--|:--|:--|:--|
| **RTX PRO 6000 Blackwell** (workstation) | 96 GB GDDR7 | **1.8 TB/s** | Yes | ~$10–12K system | ★★★ (240 tok/s @120B; 80+ @70B FP16) | ≤ ~100–120B (capacity-capped) | **Strong** — fast + CUDA + affordable, if model ≤~100B |
| **Mac Studio M3 Ultra** | 256 GB (512 pulled Mar’26) | ~820 GB/s | No (MLX) | ~$5–7K | ★★ | ~100–120B good precision | Good simple serving box; slower; weak training |
| **Mac Studio M5 Ultra** (rumored ~Oct’26) | TBD (DRAM-constrained) | "significantly higher" | No (MLX) | TBD | ★★–★★★ | TBD | **Watch** — could be the sweet spot if RAM returns |
| **DGX Station (GB300)** | up to 748 GB unified | **~7.1 TB/s** | Yes | **$80–100K** (not retail) | ★★★ | Frontier (DeepSeek V3.2 FP16) | "Everything" box — fast+huge, but $$$ / team-scale |
| **Datacenter B200 / B300 / H200** | 141–288 GB HBM | 3.35–8 TB/s | Yes | $$$$ (server) | ★★★ | Frontier | Real production scale; facility/power/capex tier |
| **2× DGX Spark** (= CUMULUS today) | 256 GB pooled | 273 GB/s | Yes | ~$9.4K | ★ (70B @2.7 tok/s) | ~405B (but slow) | **Training box, not serving** — keep on CUMULUS |
| **AMD Ryzen AI Max / "Strix Halo"** | up to 128 GB unified | ~256 GB/s | No (ROCm) | ~$2–3K | ★ | ≤ ~70B | Cheap, but bandwidth-poor like Spark — low priority |

*Leading serving candidates today: **RTX PRO 6000 Blackwell** (fast + affordable, ~100B ceiling) for a
right-sized model, or **Mac Studio** (more capacity, simpler ops) for a bigger-but-slower model. The
**DGX Station GB300** is the "large + fast" answer if budget allows; **datacenter B200/B300** if real
production concurrency demands it.*

## Current recommendation (revisit as the watch updates)
**Wait for the new Mac Studio, per Buddy's S406 decision above.** Continue with
the existing machines while waiting. The older hardware comparisons in this
document are research history; they do not override this purchase decision.
Verify the new model's actual memory options, performance and price before
recommending a configuration or purchase timing.

## Buy-triggers — what should prompt action (the watch flags these)
- **M5 Ultra Mac Studio ships** with a usable memory ceiling (≥256 GB) at a sane price → re-rank vs RTX PRO 6000.
- **RTX PRO 6000** (or a successor) drops in price / a 128–192 GB variant appears → capacity ceiling lifts.
- **DGX Station GB300** becomes retail-available or a smaller/cheaper sibling launches.
- **DRAM/HBM shortage eases** (forecast 2027–2028) → high-capacity configs get affordable; revisit sizing.
- **A new Blackwell/next-gen workstation card** or Apple Mn Ultra with materially higher bandwidth/memory.
- Our own model grows past a box's capacity/bandwidth ceiling → forces the next tier.

## What the daily/weekly watch monitors (in `config/sources.json`)
- **Keywords:** DGX Spark, DGX Station, GB10/GB200/GB300, Grace Blackwell, RTX PRO 6000, M5 Ultra,
  Mac Studio memory, unified memory, memory bandwidth, HBM, AI workstation, inference server,
  AI infrastructure, LLM training. *(Tightened S61.)*
- **Feeds:** ServeTheHome, Tom's Hardware, a dedicated "STRATUS hardware watch" Google News query
  (DGX Spark / DGX Station / M5 Ultra / Mac Studio memory / RTX PRO 6000 / local-LLM-hardware price +
  availability). *(Broadened S61.)*
- **Consolidation:** the monthly STRATUS review should append notable offerings + availability here and
  re-rank the table. *(Wiring the monthly job to auto-append here = a follow-up.)*

## Sources (fetched 2026-08-11)
- RTX PRO 6000 Blackwell (96 GB, 1.8 TB/s, 240 tok/s @120B) + DGX Station GB300 (748 GB, ~7.1 TB/s,
  $80–100K) + M5 Max 546 GB/s + DGX Spark 273 GB/s: Most Wanted Gamers, "Local LLM Hardware 2026:
  M5 Max vs DGX Spark vs RTX PRO 6000 vs DGX Station."
- DGX Spark ($4,699, 273 GB/s, 70B ≈ 2.7 tok/s): GPUSmith, "NVIDIA DGX Spark Review (2026)."
- Mac Studio 512 GB pulled / 256 GB max: Tom's Hardware (2026-03-06). M5 Ultra ~Oct 2026: Macworld.
