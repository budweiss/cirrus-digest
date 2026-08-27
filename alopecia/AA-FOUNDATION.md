# AA-FOUNDATION — alopecia areata, state of knowledge

*P0 deliverable for the ALOPECIA project (`alopecia/ALOPECIA-SPEC.md`).
Researched live 2026-08-27 (S82), Opus 5. Every claim below was checked against
current sources this session — this document does NOT rest on model knowledge.
Claims are graded; sources are listed at the bottom and inline by number.*

**Not medical advice.** This is a research grounding document. It describes what
the literature says about a population. It does not tell any individual what to
do, and the weekly brief built on it must not either.

---

## 0. Evidence grades used throughout

| Grade | Meaning |
|:--|:--|
| **A** | Randomized controlled trial / regulatory approval evidence |
| **B** | Cohort, registry, large retrospective, or systematic review of those |
| **C** | Association study, Mendelian randomization, small/uncontrolled series |
| **D** | Case report, mechanistic speculation, preclinical only |

A claim's grade is about *how well established it is*, not how interesting.
The weekly brief inherits this scale (spec priority 4) so a case report can
never be laundered into a finding.

---

## 1. What the disease is — mechanism

Alopecia areata (AA) is an autoimmune disease in which the immune system
attacks the hair follicle. The central mechanism is **collapse of hair follicle
immune privilege** [1][2][3].

- Healthy follicles are "immune privileged": they keep **MHC class I and II
  expression low** and maintain locally immunosuppressive signals, which hides
  their autoantigens from the immune system. **Grade A/B** (long-established) [1][3]
- In AA, MHC class I/II are **aberrantly upregulated**, follicular autoantigens
  become visible, and **autoreactive cytotoxic CD8+ T cells** recognize them
  and attack. **Grade B** [1][2]
- The effector population is characterized as **CD8+NKG2D+ T cells**. **IL-15**
  stimulates them; they destroy follicular epithelium via NKG2D/NKG2DL; the
  **IFN-γ** they produce activates follicular epithelial cells, which drives
  further IL-15 — a **pathogenic positive feedback loop**. **Grade B** [1][2]
- That loop is why JAK inhibition works: IL-15 and IFN-γ both signal through
  **JAK-STAT**, so blocking JAK breaks the cycle. The 2025–26 reviews describe
  JAK-STAT as the central regulatory node of immune-privilege collapse.
  **Grade A** (drugs work) / **B** (mechanistic account) [2][3]
- CD4+ T cells are not bystanders — recent work shows **TH1 effector CD4 T
  cells require IFN-γ production to induce AA**. **Grade C/D** (model systems) [4]

**Critically: the follicle is not destroyed.** AA is non-scarring. The follicle
persists in a suppressed state, which is the biological reason regrowth remains
possible even after many years. This is the single most important fact in this
document for a long-duration case.

## 2. What triggers it — THE OPEN QUESTION

**Buddy's question, and the honest answer: not known.** [1][2][3]

What is established: **genetic susceptibility** (multiple risk loci; family
history is a recognized prognostic factor [6]) combined with **some
environmental trigger** that tips a susceptible follicle into privilege
collapse.

Trigger candidates in the literature, none proven causal:

| Candidate | Grade | Note |
|:--|:--|:--|
| **Viral / infectious trigger** | **C** | Named explicitly in the immunology reviews as a triggering event leading to IP collapse [1] |
| Psychological / physiological stress | C/D | Widely reported, poorly controlled; association ≠ cause |
| Gut dysbiosis / gut–skin axis | C | See §5 |
| Vitamin D deficiency | C | Association-level [7][8] |
| Drug-induced | D→C | Actively accruing: a **first report of new-onset AA during CGRP-inhibitor therapy** appeared 2026-08-24; GLP-1 and nonscarring alopecia under study 2026-08-26 [PubMed, this session] |

**Why age ~10 (RCW and the second known case):** peak onset spans childhood to
young adulthood, and childhood onset is common — but *why the trigger window
falls where it does is not answered in the literature.* There is a signal that
onset age stratifies prognosis: onset **earlier than 4 years** may carry a
better short-term prognosis [9], implying the ~10-year window is a distinct
group, not a continuum. **This remains an open standing question — exactly what
the monitor exists to watch.**

Two boys with near-identical rapid prepubertal universalis presentations is the
kind of observation that motivates cluster/epidemiology work. The way to chase
it is **published epidemiology and case series** (spec boundary: never by
searching for individuals).

## 3. Course and prognosis — including the honest part

| Fact | Grade | Source |
|:--|:--|:--|
| AA follows a prolonged relapsing–remitting course | B | [6] |
| Spontaneous remission with **<50% scalp loss**: 30–50% within 6–12 months | B | [6] |
| ~66% show complete regrowth within 5 years (milder presentations) | B | [6] |
| ~**5%** of AA cases progress to **totalis/universalis** | B | [6] |
| Spontaneous complete regrowth once **AT/AU** is established: **<10%** | B | [6] |
| Childhood onset → more severe course, poorer treatment outcome | B | [6] |
| Poor-regrowth predictors: extensive loss, atopic dermatitis history, other autoimmune disease, family history, childhood onset | B | [6] |

**On the second boy.** If his presentation was also universalis, the <10% figure
is the relevant base rate for spontaneous full regrowth — but that number
predates the JAK era entirely, and says nothing about treated outcomes. The
monitor should treat "what happens to prepubertal universalis over 15+ years"
as a standing question, because the published follow-up is thin.

## 4. Treatment — the era that changed, and the finding that matters most

**Three FDA-approved systemic drugs, all JAK inhibitors** (confirmed current as
of this session) [5][10]:

| Drug | Brand | Approved | Population |
|:--|:--|:--|:--|
| Baricitinib | Olumiant | **2022** | Adults, severe AA |
| Ritlecitinib | Litfulo | **2023** | **Ages 12+** |
| Deuruxolitinib | Leqselvi | **2024** | Adults |

- Phase 3 data show significantly higher scalp regrowth (SALT score) vs
  placebo; reported regrowth response rates in the **23–40%** range. **Grade A** [5][10]
- Head-to-head comparison work suggests **deuruxolitinib 8 mg produced the
  greatest regrowth** of the three. **Grade B** (indirect comparison) [10]
- **All three carry an FDA boxed warning** — class-wide risks: serious
  infections, mortality, malignancy, major adverse cardiovascular events,
  thrombosis. Monitoring required. **Grade A** [5]
- **Relapse follows discontinuation**; long-term treatment is generally needed
  to maintain regrowth. **Grade A/B** [5]

### 4a. The duration finding — decision-relevant, and it must be stated plainly

Multiple lines of evidence converge on a **"therapeutic window of
opportunity"**: response to JAK inhibitors is **worse with longer disease
duration and with totalis/universalis** than with shorter, patchier disease.

- Patients with **duration >10 years**, and those with **AT/AU vs. localized**
  disease, have **lower response rates** to JAK inhibitors. **Grade B** [11]
- For baricitinib, early response was independently associated with **lower
  baseline SALT, shorter duration, and elevated ESR**; ROC analysis put the
  optimal duration threshold for predicting early response at **≤7 years**.
  **Grade B** [12]

**What this means for the case this project serves (RCW: universalis, 16 years,
adult) — stated honestly, and it is not a verdict:**

1. He sits on the unfavourable side of every one of those predictors. Pretending
   otherwise would make this whole system useless.
2. **"Lower response rate" is a population probability, not an individual
   prediction.** These are response *rates*, not response *impossibility*; the
   follicles are non-scarring and preserved (§1).
3. Published response data are dominated by **shorter-duration cohorts**, so the
   long-duration universalis subgroup is genuinely **under-measured** — an
   evidence gap, not established futility.
4. It defines what is worth watching: **long-duration-specific outcome data,
   rescue/combination strategies after inadequate response, and non-JAK
   mechanisms** (§5). This is why the spec's priority 1 is RCW's triple.
5. There is at least one documented pattern of **loss of JAK efficacy followed
   by successful rescue** in pediatric universalis [13] — the "inadequate
   responder" literature is an active area with its own management reviews [11].

**This is information for a conversation with a dermatologist. It is not a
recommendation, and the brief must never phrase it as one.**

## 5. The pipeline beyond JAK — why monitoring is worth doing now

Actively moving as of 2026 [14][15]:

| Candidate | Mechanism | Status | Grade |
|:--|:--|:--|:--|
| **Rezpegaldesleukin** (Nektar) | Treg-stimulating IL-2 conjugate | REZOLVE-AA Ph2b proof-of-concept met; **Phase 3 planned 2026**; Fast Track. 16-week extension data due early Q2 2026 | B (Ph2b) |
| **Bempikibart / ADX-914** | **anti-IL-7Rα** — upstream of JAK | FDA **Fast Track Apr 2025** | C/B |
| **Anti-OX40** (InmaGene) | T-cell costimulation blockade | Filings 2025–26 | C |
| **ILT7 binding protein** (Viela) | plasmacytoid dendritic cells | earlier filings | C |
| **Dupilumab** | IL-4Rα | 48–97% improvement reported in *atopic* AA subgroup | C (uncontrolled/selected) |
| **LAD603** (Almirall) | — | **Phase 2 started Jan 2026** | C |
| **OLX72021** (Olix) | — | **Ph1b/2a announced Jan 2026** | D/C |
| Topical JAK inhibitors | localized JAK | ongoing | C |

The strategically important point: **anti-IL-7Rα, anti-OX40 and Treg-stimulating
approaches act orthogonally to JAK inhibition**, and are explicitly aimed at
patients who fail or cannot tolerate small-molecule therapy [14]. That is the
population RCW's profile falls into, which makes this table the part of the
document most worth keeping current.

## 6. Diet, microbiome, micronutrients — tracked, and graded honestly

Buddy asked to include dietary factors. Here is the real state of it:

- **Gut–skin axis hypothesis:** gut dysbiosis may disrupt intestinal barrier
  integrity and immune tolerance via effects on **regulatory T cells**,
  plausibly contributing to onset/progression. Mechanistically coherent, and
  Tregs are the same lever rezpegaldesleukin pulls. **Grade C** [7][8]
- **A Mendelian-randomization study reports causal relationships between 16 gut
  microbial taxa and AA.** **Grade C.** MR is stronger than plain association
  but is routinely over-read in microbiome work; treat as hypothesis-generating,
  not settled. [7]
- **Proposed mechanisms:** bacterial **biotin** production, **short-chain fatty
  acids**, and **vitamin D** status affecting hair growth. **Grade C/D** [7][8]
- **Micronutrients:** case reports document lower intake of **vitamin D, iron,
  and folic acid** vs. recommended levels in AA patients. **Grade D** [8]
- **Case reports of regrowth after fecal microbiota transplant** performed for
  unrelated indications exist and are frequently cited. **Grade D** — striking,
  uncontrolled, not a basis for anything.

> **Bottom line, and the reviews say this themselves: a causal link between
> microbial dysbiosis and AA pathogenesis remains to be established** [7]. The
> honest summary is "biologically plausible, actively researched, not proven."

**Boundary restated:** this project tracks dietary *research* and grades it. It
does not produce dietary advice for RCW.

## 7. Where the information lives — validated source list for P1

**Every source below was called successfully from this environment this
session.** These are not proposed sources; they are proven ones.

| Source | Endpoint | Proven this session |
|:--|:--|:--|
| **ClinicalTrials.gov v2** | `https://clinicaltrials.gov/api/v2/studies` | ✅ **88 recruiting AA studies** worldwide |
| **PubMed E-utilities** | `esearch.fcgi` → `esummary.fcgi` | ✅ **7,834** AA records; recent-6 pulled by date, chain works |
| **NAAF** | naaf.org — news, FDA-approved-JAK page, **Registry/Biobank/CTN**, research grants | ✅ (registry is the world's largest AA data + DNA collection) |
| medRxiv / bioRxiv | preprints | planned, not yet called |
| Brave Search | catch-all news | key already in CIRRUS creds |

**NAAF Registry, Biobank & Clinical Trials Network** is the aggregation point
the spec means by "where people with AA are gathered and want to be found" —
an organized network of centers registering patients with samples in a central
repository [16]. Watch also **NAAF 2026 research grants** (up to $75k;
**applications close 2026-09-17**; awards by 2026-12-31) as an early indicator
of which research directions get funded [17].

### 7a. P3 geo matching — validated, with a trap found

Distance filtering works: `filter.geo=distance(LAT,LONG,50mi)`.

Measured this session against RCW's region (region itself stays out of this
repo — see spec):

- **7 recruiting AA studies** with a site within **50 miles**
- **19 recruiting AA studies** within **120 miles**

Real hits included pediatric/adolescent programs for **ritlecitinib**,
**baricitinib** and **deuruxolitinib**, plus device and cell-therapy studies.

> **⚠️ TRAP FOR P3 — found by looking at the output, not the docs.**
> `filter.geo` returns a study if **ANY** of its sites falls in the radius, and
> **the returned `locations` array is NOT ordered by distance.** Printing the
> first three sites of a matched study showed *Encinitas CA, Miami FL, Skokie
> IL* for a study that matched a 50-mile Pennsylvania filter. A naive brief
> would tell Buddy the nearby trial is in California.
> **P3 must scan the full `locations` array and compute the nearest site
> itself**, then report that site and its distance. Pin this with a test.

## 8. What changed against the model's prior knowledge

Per the spec, P0 must state this explicitly rather than quietly agreeing with
itself.

**Confirmed unchanged:** the three approvals and their years/populations; the
immune-privilege-collapse mechanism; non-scarring follicle preservation;
approximate ~5% progression to AT/AU.

**Corrected / newly added by live research:**
1. **The duration/severity response finding (§4a) was not in the plan's
   grounding at all** — including the concrete **≤7-year** ROC threshold for
   baricitinib early response. It is the most decision-relevant fact found, and
   it directly justifies the spec's priority-1 filter.
2. **The 2026 pipeline is more advanced than assumed**: rezpegaldesleukin
   heading to **Phase 3 in 2026**; **Almirall LAD603 Phase 2 (Jan 2026)** and
   **Olix OLX72021 (Jan 2026)** both started after the model's cutoff.
3. **Prognosis numbers were absent** from the plan and are now quantified (§3).
4. **Drug-induced AA is an accruing signal** — a *first* CGRP-inhibitor report
   dated **2026-08-24**, three days ago, and GLP-1 work dated **2026-08-26**.
   Found in a routine recency pull, which is itself evidence the daily
   collector will catch real things.
5. **The microbiome literature has an MR-causality claim** (16 taxa) that is
   stronger than "association" and needed its own grade line.
6. **P3's geo trap (§7a)** — would have silently produced wrong briefs.

**Deliberately not asserted:** any claim about what caused onset at age 10 in
either boy. The literature does not support one, and inventing a narrative
would be the worst possible failure mode for a document meant to ground
everything downstream.

---

## Sources

1. Inhibition of T-cell activity in alopecia areata — Frontiers in Immunology / PMC10657858
2. From mechanisms to therapies: advances in alopecia areata immunopathology — PMC12433866
3. Hair follicle immune privilege in autoimmune alopecias: paths toward reestablishing immune tolerance — Frontiers in Medicine 2026 (10.3389/fmed.2026.1805170)
4. TH1 effector CD4 T cells rely on IFN-γ production to induce alopecia areata — Science Advances (10.1126/sciadv.adz2257)
5. FDA-Approved JAK Inhibitors — National Alopecia Areata Foundation (naaf.org)
6. Revisiting Pediatric Alopecia Areata: Newer Insights — Indian J Paediatric Dermatology 2021; Clinical Presentations and Comorbidities of Pediatric AA — Pediatric Dermatology 2025
7. Cutaneous and Gut Dysbiosis in Alopecia Areata: A Review — PMC12173129; The Multi-Faceted Role of Gut Microbiota in AA — Biomedicines 13(6):1379
8. Diet and Microbiome Influence on Alopecia Areata: Experience from Case Reports — J Nutr Med Diet Care
9. Early-onset AA in children: onset earlier than 4 years may have better short-term prognosis — Experimental Dermatology 2024
10. Comparing/choosing among baricitinib, ritlecitinib, deuruxolitinib — AJMC; HCPLive; Dermatology Advisor; Evaluating Current and Emergent JAK Inhibitors (PubMed 40794245)
11. Predictors and Management of Inadequate Response to JAK Inhibitors in Alopecia Areata — Am J Clin Dermatol (10.1007/s40257-024-00884-x)
12. Defining a Therapeutic Window of Opportunity in AA: Predictors of Early Response to Baricitinib — PMC12565575
13. Loss of JAK inhibitor efficacy and subsequent rescue in a pediatric case of alopecia universalis — PMC13089043
14. Alopecia areata drug pipeline — PatSnap; DelveInsight pipeline report (18 companies)
15. REZOLVE-AA Phase 2b — Nektar Therapeutics investor release
16. Alopecia Areata Registry, Biobank & Clinical Trials Network — naaf.org/advance-research/join-the-registry
17. 2026 Research Grants — naaf.org/research-grants/2026-research-grants/
