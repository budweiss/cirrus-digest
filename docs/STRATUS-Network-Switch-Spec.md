# STRATUS — Network Switch Spec & Topology

*Planning doc, Session 359 (2026-10-01). Specs the switch, cabling, topology,
and power for connecting CIRRUS, CUMULUS, and a future STRATUS server so that
STRATUS can host a shared LLM serving the other two boxes. Verify all prices
and port types with the vendor before purchase.*

Related: `STRATUS-Production-Sizing-and-Architecture.md`,
`CUMULUS-Beta-Buildout-and-Scaling-Plan.md`.

---

## 1. The key constraint: CIRRUS is Ethernet-only

CIRRUS is a Mac Studio M4 Max. Its network port is a single **10 Gigabit
Ethernet (10GBASE-T, RJ-45 copper)** jack — no QSFP, no SFP+, no fiber.
No switch changes that; the Mac Studio physically cannot do 100G or 200G.

**This is fine for LLM inference serving.** The traffic between CIRRUS and
STRATUS is text: a prompt goes in (1–8 KB), a response comes back (2–16 KB).
At 10GbE (~1.25 GB/s) those payloads transfer in microseconds — the bottleneck
is always the model's generation speed (tokens/second), never the network. Even
streaming 200 tokens/second over a 10GbE link uses <0.1% of the bandwidth.

The 200G QSFP56 path matters only for **memory pooling** (loading model weights
across nodes at ~25 GB/s) and **distributed inference** (NCCL/RDMA across
Sparks). Those are CUMULUS↔STRATUS concerns, not CIRRUS.

---

## 2. Two network tiers

| Tier | Purpose | Who uses it | Speed |
|------|---------|-------------|-------|
| **QSFP56 fabric** | Memory pooling, model-weight transfer, NCCL/RDMA | CUMULUS Sparks ↔ STRATUS (if STRATUS has ConnectX-7) | 200G per link |
| **Ethernet (10GBASE-T)** | Inference serving requests, management, internet | CIRRUS + CUMULUS + STRATUS + uplink | 10G |

STRATUS needs **both**: a QSFP56 port for the fabric (to pool memory with
CUMULUS when hosting large models), and an Ethernet port for serving and
management. A DGX Spark, DGX Station, or any server with a ConnectX-7 NIC
satisfies both — the ConnectX-7 is QSFP56 200G, and the motherboard has
onboard Ethernet.

---

## 3. Recommended switch: MikroTik CRS804-4DDQ

One box handles both tiers.

| Spec | Value |
|------|-------|
| QSFP-DD ports | 4 × 400G (each breaks out to 2 × 200G QSFP56) |
| Ethernet ports | 2 × RJ-45 copper 10GBASE-T (1G/2.5G/5G/10G) |
| Throughput | 1.6 Tbps non-blocking |
| Form factor | 1U rackmount (387 × 218 × 44 mm) |
| Power | 92 W (no optics) / 123 W (with optics) |
| Power supply | Dual-redundant hot-swap AC (100–240V) |
| OS | RouterOS v7 (L6) |
| Price | **~$1,295** (suggested retail; ~$1,146–1,195 from resellers) |

Sources: [MikroTik product page](https://mikrotik.com/product/crs804_ddq),
[Baltic Networks listing](https://www.balticnetworks.com/products/mikrotik-crs804-ddq-cloud-router-switch-crs804-4ddq-hrm),
[MikroTik user manual](https://help.mikrotik.com/docs/pages/viewpage.action?pageId=363987013).

**Why this switch:** It is the same model Alex Ziskind used in his 8-Spark
cluster build (documented in the architecture doc addendum). Proven to work
with DGX Spark ConnectX-7 ports via QSFP-DD→2×QSFP56 breakout cables. The two
RJ-45 10G ports mean CIRRUS plugs straight in — no second switch needed.

**Caveat (from Ziskind's experience):** getting NCCL/RDMA working over the
switched fabric required community/unofficial configuration beyond NVIDIA's
2-node direct-link playbook. The switch itself works; the software stack for
distributed training across a switch is the fiddly part. For inference serving
(our primary use case), this is not a concern — the QSFP path is for model
loading, and the Ethernet path handles requests.

---

## 4. Cables

### QSFP-DD → 2× QSFP56 breakout DAC (CUMULUS/STRATUS fabric)

| Spec | Value |
|------|-------|
| Type | Passive twinax copper (DAC) |
| Speed | 400G → 2 × 200G |
| Max length | 3 m (passive); longer runs need active optical (AOC) |
| Price | **~$71–124** per cable (varies by vendor/length) |

Vendors: SFPcables.com (~$71), LSOLINK (~$85), QSFPTEK (~$122), FS.com (~$124).
Sources: [SFPcables.com](https://www.sfpcables.com/dac/400g-qsfp-dd-to-2x200g-qsfp56-breakout-dac-passive-0-5-2-meters),
[FS.com](https://www.fs.com/products/101806.html).

One breakout cable serves two QSFP56 endpoints. For 2 CUMULUS Sparks + 1
STRATUS (3 endpoints), you need **2 breakout cables** (one 400G port → 2
Sparks; another 400G port → 1 Spark + spare, or 1 Spark + STRATUS).

Alternatively, for the existing 2-Spark direct link, a simple **QSFP56 DAC
(200G, passive, ~0.5–3 m)** costs ~$50–100 and can stay in place — the
switch is only needed when adding the 3rd node.

### Ethernet (CIRRUS + uplink)

Standard **Cat 6a or Cat 7** cables for 10GBASE-T. Cables are commodity; use
Cat 6a minimum for rated 10G performance at distances up to 100 m.

---

## 5. Topology

```
                          ┌─────────────────────────────────┐
                          │   MikroTik CRS804-4DDQ          │
                          │   (1U, ~$1,295)                 │
                          │                                 │
   CIRRUS (Mac Studio     │  RJ-45 10G ◄──── Cat 6a ───────┼── CIRRUS
   M4 Max, 64GB)          │  RJ-45 10G ◄──── Cat 6a ───────┼── Internet/LAN uplink
          │               │                                 │
          │  10GbE         │  QSFP-DD #1 ── breakout ──┐    │
          │  (inference    │  QSFP-DD #2 ── breakout ──┤    │
          │   requests     │  QSFP-DD #3 ── (spare)    │    │
          │   only)        │  QSFP-DD #4 ── (spare)    │    │
          └────────────────┤                            │    │
                           │              ┌─────────────┼────┼── Spark #1 (200G QSFP56)
                           │              │  ┌──────────┼────┼── Spark #2 (200G QSFP56)
                           │              │  │  ┌───────┼────┼── STRATUS  (200G QSFP56)
                           │              │  │  │      │    │
                           └──────────────┘──┘──┘──────┘────┘
                              breakout cables (each 400G → 2× 200G QSFP56)
```

**Traffic flows:**
- CIRRUS → STRATUS (inference request): CIRRUS → 10G RJ-45 → switch → STRATUS Ethernet. ~10 Gb/s, text payloads, sub-ms.
- CUMULUS → STRATUS (inference request): CUMULUS Ethernet → switch → STRATUS Ethernet. Same path, same speed.
- CUMULUS ↔ STRATUS (model loading / memory pooling): QSFP56 200G via breakout cable. ~25 GB/s.
- All boxes → Internet: via the switch's second 10G RJ-45 port to the office LAN/router.

**If STRATUS does not have ConnectX-7** (e.g., a custom server without an
NVIDIA NIC): it connects via Ethernet only. It cannot pool memory with the
CUMULUS Sparks, but it can still serve inference to both CIRRUS and CUMULUS
over 10GbE. The QSFP fabric stays between the two CUMULUS Sparks (direct DAC,
no switch needed). This is the simplest and cheapest path if STRATUS hosts a
model that fits in its own memory.

---

## 6. What STRATUS hardware needs

The high-speed QSFP56 path requires STRATUS to have a **ConnectX-7 (200G
QSFP56)** port. Hardware options:

| STRATUS hardware | Memory | ConnectX-7? | Can pool with CUMULUS? | Est. price |
|-------------------|--------|-------------|------------------------|------------|
| DGX Spark (GB10) | 128 GB | Yes (2 ports) | Yes | ~$4,699 |
| DGX Station GB300 | 748 GB | Yes | Yes | ~$90–100K |
| Custom server + ConnectX-7 NIC | Varies | Yes (add ~$1,500 NIC) | Yes | Varies |
| Custom server (no NVIDIA NIC) | Varies | No (Ethernet only) | No | Varies |

For Buddy's "one LLM we train over time" vision, the starting point is a
**70B-class model** (Llama 3.3 70B, Qwen 2.5 72B) at Q4 — ~40–48 GB of
weights. That **fits on a single DGX Spark (128 GB)** with room for KV cache.
So STRATUS could start as another Spark (~$4,699) and join the fabric via the
switch. If the model grows beyond 128 GB, pool memory across STRATUS + one
CUMULUS Spark (256 GB) or all three (384 GB).

A DGX Station GB300 (748 GB) would host a frontier-scale model (400B+) alone
but costs ~20× more. Lead with a Spark; reserve the Station for when the model
outgrows 3-node pooling (384 GB).

---

## 7. Power budget and UPS

### Per-device power draw

| Device | Idle | Typical load | Peak | PSU |
|--------|------|-------------|------|-----|
| DGX Spark | 38 W | ~120–180 W | 233 W | 240 W external |
| Mac Studio M4 Max | ~10 W | ~60–200 W | 480 W | internal |
| MikroTik CRS804-4DDQ | — | 92 W | 123 W | dual-redundant AC |
| 10GbE switch (if separate) | — | 15–30 W | 30 W | — |

Sources: [NVIDIA DGX Spark hardware guide](https://docs.nvidia.com/dgx/dgx-spark/hardware.html),
[NVIDIA forum power clarification](https://forums.developer.nvidia.com/t/dgx-spark-power-clarification/349668/1),
[Apple Mac Studio specs](https://support.apple.com/en-us/122211).

### Total power budget (3 Sparks + CIRRUS + switch)

| Scenario | Typical | Peak |
|----------|---------|------|
| 2 CUMULUS Sparks + 1 STRATUS Spark + switch | ~510 W | ~830 W |
| + CIRRUS (Mac Studio, always on) | ~670 W | ~1,310 W |
| + monitors/peripherals (setup only) | ~750 W | ~1,500 W |

### Electrical circuit

A standard **20A circuit at 120V** provides 2,400 W. The full stack above
peaks at ~1,300 W, leaving ~1,100 W of headroom — enough for a 4th Spark
and future expansion without re-wiring. A dedicated 20A circuit is
recommended; a 15A circuit (1,800 W) would be tight at peak load.

### UPS sizing

| Goal | Recommended UPS | Runtime | Price |
|------|-----------------|---------|-------|
| Graceful shutdown (5 min) | APC Smart-UPS 1500VA (SMT1500RM2UC) | ~8–10 min at 670 W load | ~$1,100–1,500 |
| 15+ min ride-through | APC Smart-UPS 2200VA (SMT2200RM2U) | ~15–20 min at 670 W load | ~$1,900–2,000 |
| 30+ min (wait out brief outages) | 2200VA + external battery pack | ~30+ min | ~$2,500+ |

Sources: [APC SMT1500RM2UC (Markertek)](https://www.markertek.com/product/apc-smt1500rm2uc/),
[CDW APC Smart-UPS listings](https://www.cdw.com/product/apc-smart-ups-x-1500va-smartconnect-port-rackmount-network-card-lcd-120v/6386179).

**Recommendation:** the APC SMT1500RM2UC (1500VA, 1000W) covers the typical
~670 W load with enough runtime to trigger graceful shutdowns on all three
Sparks + CIRRUS. If you want to ride through short outages without shutting
down, step up to the 2200VA model. Either way, configure the UPS's network
management card (or USB connection) to trigger shutdown scripts on each Spark
when battery drops below 30%.

### Cooling

Each Spark dissipates up to 233 W into the room; the switch adds ~100 W;
CIRRUS adds up to 480 W. Total heat output at typical load: ~670 W ≈
~2,300 BTU/hour. A small room with normal HVAC handles this easily. If the
Sparks are in a closet or enclosed rack, ensure at least 200 CFM of airflow
(a single 120mm fan at ~50 CFM per Spark plus the switch is ample).

---

## 8. Cost summary (verify before purchase)

| Item | Price (USD) | Notes |
|------|-------------|-------|
| MikroTik CRS804-4DDQ switch | ~$1,295 | Or ~$1,146–1,195 from resellers |
| QSFP-DD→2×QSFP56 breakout DAC (×2) | ~$142–248 | 2 cables at ~$71–124 each |
| Cat 6a Ethernet cables (×2, 3m) | ~$20 | CIRRUS + uplink |
| APC Smart-UPS 1500VA (SMT1500RM2UC) | ~$1,100–1,500 | Graceful shutdown |
| **Network + power total** | **~$2,557–3,063** | Excludes the STRATUS server itself |
| DGX Spark (as STRATUS) | ~$4,699 | If starting with a 3rd Spark |
| **Total with STRATUS Spark** | **~$7,256–7,762** | Full 3-node + switch + UPS |

For comparison: without a switch (keeping CUMULUS's 2 Sparks on their direct
DAC and connecting STRATUS via Ethernet only), the network cost is just
~$20 for Cat 6a cables plus the UPS. The switch is only needed if you want
the 200G QSSP56 path between STRATUS and CUMULUS for memory pooling.

---

## 9. Phased approach

**Phase 1 — STRATUS as a 3rd Spark, switchless (cheapest start):**
- Keep CUMULUS's 2 Sparks on their direct QSFP56 DAC (200G, supported, free)
- Add STRATUS Spark, connect via 10GbE Ethernet to the existing LAN
- STRATUS hosts the model alone (128 GB fits a 70B at Q4)
- CIRRUS and CUMULUS send inference requests over Ethernet
- Cost: ~$4,699 (Spark) + ~$20 (cables) = ~$4,720
- Limitation: no memory pooling between STRATUS and CUMULUS

**Phase 2 — Add the switch (when you need pooling):**
- Buy the MikroTik CRS804-4DDQ + breakout cables
- All 3 Sparks join the 200G fabric via the switch
- Can now pool memory across all 3 (384 GB) for models too big for one Spark
- Cost: + ~$1,437–1,543 (switch + cables)
- Total cumulative: ~$6,157–6,263 + UPS

**Phase 3 — Beyond 3 Sparks (future):**
- The MikroTik switch supports up to 8 endpoints via breakout
- Add Sparks as serving load grows
- Each Spark adds ~$4,699 + one breakout cable port
- If model size exceeds 384 GB pooled (3 Sparks), add a 4th Spark or
  consider a DGX Station GB300 (748 GB standalone)

---

## 10. What to buy first

If Buddy wants to move now: **one DGX Spark (~$4,699) as STRATUS**, connected
via Ethernet to the existing LAN. No switch needed yet — the 10GbE path is
sufficient for inference serving. Add the MikroTik switch when memory pooling
between STRATUS and CUMULUS becomes necessary (i.e., when the model outgrows
128 GB on a single Spark).

If Buddy wants to wire the fabric proactively: add the **MikroTik CRS804-4DDQ
(~$1,295) + 2 breakout cables (~$200)** at the same time. The switch is
1U, low-power (~92 W), and has spare QSFP-DD ports for future Sparks.