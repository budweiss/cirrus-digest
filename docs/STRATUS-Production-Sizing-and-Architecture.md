# STRATUS — Production Sizing & Architecture
*Planning doc, Session 43 (2026-07-20). Sizing the production tier so it has
enough memory + storage to serve live client workloads, and a growth path that
matches Buddy's preference to scale CUMULUS/DGX Spark out rather than buy one
large box. Verify all prices/specs with a reseller before purchase.*

Related: `COWORK-STRUCTURE.md` (server table), `CUMULUS-Beta-Buildout-and-Scaling-Plan.md`,
`docs/CIRRUS-Hardware-Sizing-2026-07.md`.

---

## 1. The goal, stated plainly

STRATUS serves **production inference** for the live client workloads —
pedagogy (Alyssa), real estate (Aggie), snow + property management (Bill) — plus
the daily/weekly digest generation. It is NOT primarily a training box. The
"own specialized LLM" ambition is achieved by **fine-tuning open models
(LoRA/QLoRA) + RAG**, not training a foundation model from scratch (that is a
millions-of-dollars, thousands-of-GPU effort and is off the table). See the
companion `Pedagogy-Specialized-FineTune-Sketch.md`.

Design principle: **separate serving from training.** Serving scales out cheaply
with more nodes; heavy training wants one big-memory box (or a cloud burst).

---

## 2. Interconnect reality (the growth constraint), corrected

Each DGX Spark has **two ConnectX-7 (200G QSFP56) ports**. Topologies:

| Setup | How | Notes |
|---|---|---|
| 2 Sparks | 1 DAC cable, port-to-port | Direct, simplest |
| 3 Sparks | Ring — each unit's 2 ports wire to the other two | **Switchless max** |
| 4+ Sparks | Via a managed QSFP switch | Supported; not capped at 3 |

**Correction to "3 is the max":** three is the ceiling only for the *switchless
ring*. NVIDIA supports clustering more than two Sparks through a QSFP switch, so
you can grow past three when needed — the trade is adding a switch.

**Bandwidth caveat that shapes everything:** the ConnectX-7 sits behind Gen5 x4
links; the box tops out at ~200Gb (~25 GB/s) of usable inter-node bandwidth no
matter how it's wired (the second port is for topology, not extra throughput).
A single Spark's own unified memory runs at **273 GB/s** — roughly **10× the
inter-node link.** So crossing nodes is expensive.

Consequence:
- **Serving / inference / single-node fine-tune → Spark scale-out is great.**
- **Tightly-coupled distributed training → poor** (the 200Gb link bottlenecks
  it). Do heavy training on one big-memory box or in the cloud, not across Sparks.

Sources: [Spark Stacking / clustering](https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html),
[QSFP deep-dive](https://github.com/vroomfondel/dgxarley/blob/main/docs/DGX%20Spark%20QSFP%20DeepDive%20EN.md),
[Spark hardware](https://docs.nvidia.com/dgx/dgx-spark/hardware.html).

---

## 3. Why the Spark scale-out fits production serving

DGX Spark is bandwidth-bound on single-stream generation, but its strength is
**concurrent throughput with batching** — exactly what a production request
queue looks like. Measured examples (public benchmarks): ~695 tok/s aggregate at
256 concurrent streams; Llama 3.1 8B FP4 ~924 tok/s at 128 concurrency;
Qwen3-Coder 30B ~483 tok/s at batch 64. So:

- A **specialized small model (8–30B) + high concurrency** is the sweet spot —
  each node is an independent inference server behind a load balancer.
- Adding a node ≈ adding serving capacity (near-linear for independent request
  streams) OR pooling memory to host a bigger model across 2–3 nodes.

This is why "double CUMULUS, expand to 3 if needed" is a sound production plan.

Sources: [Spark concurrency benchmark](https://dendro-logic.com/engineering/nvidia-dgx-spark-concurrency-benchmark/),
[LMSYS Spark review](https://www.lmsys.org/blog/2025-10-13-nvidia-dgx-spark/).

---

## 4. Recommended topology

```
CIRRUS (Mac Studio M4 Max, 64GB)         → DEV / staging (unchanged)
        │  promote validated models
        ▼
CUMULUS  = production SERVING cluster
   Spark #1 (128GB)  ← online now
   Spark #2 (128GB)  ← "double it" (near-term)         [direct DAC link]
   Spark #3 (128GB)  ← add if load needs it            [ring, switchless max]
   + managed QSFP switch  ← only when you outgrow 3
   + load balancer (route client requests across nodes)
        │
        ▼
STRATUS  = the promoted production endpoint
   Option A (preferred, matches your plan): the Spark cluster above, hardened
      for prod — load balancer, health checks, the CIRRUS runner/heartbeat
      pattern, backups. Grows 2 → 3 → switched.
   Option B (only if heavy TRAINING becomes routine): add ONE big-memory box —
      DGX Station GB300 (748GB unified) or a 2× H200 server — dedicated to
      fine-tuning, feeding models back to the serving cluster. Don't buy this
      for serving; buy it only when training load justifies it.
```

Keep **one node (or CIRRUS) as the "data-prep / training" role**: Whisper
transcription, RAG indexing, QLoRA runs. These are bursty and shouldn't compete
with the live serving nodes.

---

## 5. Memory sizing — "enough to handle production"

The binding resource. Budget per node = model weights + KV cache (grows with
concurrency × context) + headroom.

| Serving target | Weights (approx) | Practical per-node memory | Fits on 1 Spark (128GB)? |
|---|---|---|---|
| Specialist 8B (FP8/FP4) | 5–9 GB | 24–48 GB w/ big KV cache | Yes, with lots of concurrency headroom |
| Specialist 13–14B | 9–15 GB | 32–64 GB | Yes |
| Specialist 30–32B | 18–30 GB | 48–90 GB | Yes |
| General 70B (Q4) | ~40–48 GB | 64–110 GB | Yes (bandwidth-limited single-stream; batch to compensate) |
| 100–200B | 60–130 GB | pool across 2 Sparks | 2-node |

Guidance: for the client workloads, **specialist 8–32B models are the target** —
they fit one Spark with generous KV-cache room for concurrency, and (fine-tuned)
should beat a general 70B on your tasks. Reserve 2–3-node memory pooling for the
occasional large general model. **Rule of thumb: keep steady-state memory use
≤ ~70% per node** so KV cache under peak concurrency doesn't OOM.

Fine-tuning memory (single node): QLoRA of a 13–32B ≈ 24–48 GB, of a 70B ≈
50–65 GB — all fit one 128GB Spark. Full 70B fine-tune (~400–600 GB) does NOT —
that's the only case that needs a Station-class box or cloud.

---

## 6. Storage sizing

Each Spark ships with **4TB NVMe** — enough for hot models + indexes, not for a
production training/serving box's full footprint. Split hot vs. bulk:

| Tier | Put here | Size target |
|---|---|---|
| Hot NVMe (on-box) | Live model weights, RAG/vector index, current KV/cache | 4 TB (stock) — OK to start |
| Bulk NAS/RAID (network) | Datasets, fine-tune checkpoints (10s–100s GB each), Whisper audio + transcripts, digest history, corpora | **20–50 TB** |
| Backup | Off-box copies of config-excluded state, models, checkpoints | ≥ equal to bulk; offsite/rotated |

Why bulk matters: transcription audio + transcripts, per-run fine-tune
checkpoints, and growing client corpora accumulate fast. Keep credentials and
secrets OFF the NAS (same rule as today). A NAS also lets every Spark node mount
the same models/index — simplifying the cluster.

---

## 7. CPU

For inference the GPU/unified-memory dominates; Spark's **20-core Arm** is ample
for serving. CPU matters for **data prep** — transcription, tokenization, RAG
indexing, scraping. Options: run those on the dedicated data-prep node, or if you
add a big training box (Option B), its 72-core Grace (Station) / dual-socket
EPYC (48–128 cores) covers it. Don't over-spec CPU for the serving nodes.

---

## 8. The non-compute realities (learned on CUMULUS)

Budget for these before they bite (they already did on CUMULUS bring-up):
networking (the QSFP switch + a normal LAN/router + internet feed), power
(dedicated circuit + strip/UPS as node count grows), cooling/airflow, physical
space, and keyboard/monitor/KVM for setup. Mirror the CIRRUS operational stack
onto each prod node: runner + heartbeat/watchdog, nightly backups (credentials
excluded), Cloudflare tunnel, config snapshots.

---

## 9. Staged plan + decision triggers

1. **Now → CUMULUS #1 (online):** stand up Ollama + the serving stack; port the
   CIRRUS runner/heartbeat/backup pattern. Prove one specialist model end-to-end.
2. **Trigger: sustained concurrency or latency at peak → add Spark #2** ("double
   it"). Direct DAC link. Put a load balancer in front; decide per model whether
   to pool memory (bigger model) or run independent (2× throughput).
3. **Trigger: still saturating at 2 nodes → add Spark #3** (switchless ring).
4. **Trigger: need >3 nodes → add a managed QSFP switch** (removes the 3-node cap).
5. **Trigger: routine heavy/full fine-tuning → add ONE big-memory box** (Station
   GB300 or 2× H200) as a dedicated trainer; keep serving on the Spark cluster.
6. **Promote to STRATUS** when the CUMULUS cluster is hardened (LB, health checks,
   backups, monitoring) — production rules apply: everything tested on CUMULUS
   before it ships to STRATUS.

## 10. Cost notes (verify current)
DGX Spark ≈ **$4,699** each (public 2026 pricing) — so "double it" is roughly
one more Spark + a $100-ish 200G DAC cable; a third adds two more cables. A
managed QSFP switch is a modest add. A DGX Station GB300 or 2× H200 server is an
order of magnitude more and only justified by real training load — hence
"reserve it, don't lead with it." Confirm all pricing before budgeting.

Sources: [DGX Spark product page](https://www.nvidia.com/en-us/products/workstations/dgx-spark/),
[DGX Spark price/review](https://intuitionlabs.ai/articles/nvidia-dgx-spark-review),
[DGX Station GB300 specs](https://www.guru3d.com/story/nvidia-dgx-station-gb300-superchip-specifications-and-748gb-unified-memory/).

---

## 11. Addendum — Alex Ziskind's real-world multi-Spark testing (S47, 2026-07-28)

Buddy flagged Alex Ziskind (YouTube [@AZisk](https://www.youtube.com/@AZisk), X
[@digitalix](https://x.com/digitalix)) as a hardware source worth mining for the
STRATUS Spark cluster. Reviewed his cluster work + the official NVIDIA
`dgx-spark-playbooks`. Findings, and how they revise Section 2:

**The cable Buddy half-remembered = QSFP56.** It "ends in 56," not the 20s. The
"20s" part is **QSFP28** (100G, one generation down). There is no QSFP59. The
Spark's onboard **ConnectX-7** NIC runs **200GbE = QSFP56**; the in-box cable is
a 200G QSFP56 passive DAC (~0.5 m). Use QSFP56 (or a QSFP-DD→2×QSFP56 breakout
off a switch) — never a QSFP28.

**Official NVIDIA support stops at 2 nodes.** The playbook only documents the
direct 2-Spark link (one QSFP56 DAC, port-to-port, no switch → 256 GB pooled).
Setup: netplan link-local (169.254.x.x) or static IP, passwordless SSH via the
`discover-sparks.sh` mDNS/Avahi script (shared `id_ed25519`), then validate with
an NCCL `all_gather_perf` run (`NCCL_SOCKET_IFNAME`/`UCX_NET_DEVICES` =
`enp1s0f1np1`). This is the safe, supported path for **Spark #2**.

**Beyond 2 = switch + breakout + unofficial glue (Ziskind's build).** To pass
NVIDIA's 2-node ceiling he used a **MikroTik switch** (CRS804-4DDQ, 1.6 Tbps,
4× 400G QSFP-DD ports) with **QSFP-DD 400G → 2× QSFP56 200G breakout cables**
(one 400G port feeds two Sparks) — scalable to 8 nodes at full bandwidth. It
required deep manual config **plus community/unofficial software** to get
NCCL + RDMA working over the switched fabric. Powerful but fiddly and unsupported.

**Scaling payoff is non-linear — this changes our node math.** On *small* models
(Qwen 34B) he saw **little token/s gain past ~4 nodes** — throughput isn't the
reason to cluster. The real win is **models that won't load at all otherwise**:
the 8-node / **~1 TB pooled VRAM** cluster ran **Qwen 3.5 (397B, ~800 GB)** at a
usable **24 tok/s** — impossible on any single 128 GB box. Cost of his rig:
~€23,600 (8 Sparks) + ~€2,000 networking.

**Revision to Section 2 & the staged plan:** treat the **switchless 3-node ring**
as unofficial/community, not a documented NVIDIA topology. For STRATUS, the clean
break is: **2 nodes direct (supported)** → **switch the moment we need node #3**,
and size the cluster by *the largest model we must fit*, not by chasing tok/s
(which plateaus ~4 nodes for models that already fit). If STRATUS's job is
serving current-size specialist models, 2 nodes may be plenty; multi-node is
justified mainly to host a frontier-scale model locally.

Sources: [NVIDIA dgx-spark-playbooks — Connecting Two Sparks](https://deepwiki.com/NVIDIA/dgx-spark-playbooks/7.1-connecting-two-sparks),
[NVIDIA "Connect Two Sparks" playbook](https://build.nvidia.com/spark/connect-two-sparks/overview),
[Ziskind "NVIDIA didn't want me to do this" (8-Spark cluster)](https://www.youtube.com/watch?v=QJqKqxQR36Y),
[Notebookcheck writeup of the 8-node build](https://www.notebookcheck.com/Acht-Nvidia-DGX-Spark-im-Cluster-YouTuber-laesst-gigantische-LLMs-auf-den-kleinen-KI-Rechnern-laufen.1233035.0.html),
[Ziskind X — MikroTik switch for 4/8 Sparks](https://x.com/digitalix/status/2024518832090431957).

**Auto-tracking his uploads:** channel RSS (no API key needed) —
`https://www.youtube.com/feeds/videos.xml?channel_id=UCajiMK_CY9icRhLepS8_3ug`.
