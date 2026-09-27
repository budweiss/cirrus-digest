# Labs register: who is working on alopecia areata

*Research plan Phase A, lane L3 (S261, 2026-09-22). Built from three free
public sources by `build_labs_register.py`, which writes the raw data to
`labs_register_data.json`. The groupings and "what they work on" judgments
below were curated by hand from that data. Queries use the condition name
only (plan §3.7): nothing personal is searched for, and nobody is contacted
(plan §9).*

**Rebuild:** `python3 alopecia/research/build_labs_register.py` takes about a
minute. Phase C turns this into a monthly refresh.

## 0. Correction to the plan (§2b)

The plan said **"19 NIH-funded alopecia-areata projects"**, with Mount Sinai 4,
Columbia 4, UMass 2, Pittsburgh 2 and Penn 2. **That was a count of
award-year rows, not projects,** and it included projects that only mention
AA in passing.
- The 19 rows reduce to **14 distinct projects**: 15 core numbers, where
  R61/R33AR084210 is one phased award.
- Only **8 of the 14 study AA**.
- **3** are adjacent: skin T-cell or drug-delivery work, where AA appears only
  in RePORTER's keyword terms.
- **3** merely mention an AA drug approval as background: an SIV/HIV study,
  a STING project and a hidradenitis hydrogel.

Reproduced 2026-09-22 with the same query (title, abstract and terms; FY2025–26).

## 1. NIH-funded projects (RePORTER, FY2025–26)

US federal grants only. Most AA research is funded by industry or outside the
US; see §2 and §4.

### 1a. Projects that study AA (8)

| Institution | PI(s) | Project | Type | What it tests | Lane |
|:--|:--|:--|:--|:--|:--|
| Columbia University | Angela Christiano | Targeting the NKG2A pathway in AA (R01AR084005) | R01 | An NK/CD8 checkpoint receptor as a drug target | L2 mechanism |
| Columbia University | Angela Christiano | Influence of the microbiome on the natural history of AA (R01AR079744) | R01 | Gut microbiome and disease course | L2 / cause |
| Columbia University | Zhenpeng Dai | Gut microbes and gluten in AA (R21AR086384) | R21 | Diet and microbiome, mechanistic | L2 / diet |
| Icahn School of Medicine at Mount Sinai | Emma Guttman | Dupilumab in paediatric AA (U01AI179629) | U01 | Blocking the type 2 (atopic) pathway | L1 trials |
| Icahn School of Medicine at Mount Sinai | Dusan Bogunovic; Emma Guttman | JAK inhibition for skin and scalp disease in Down syndrome (R61/R33AR084210) | R61→R33 | JAK inhibitors where AA is one of several conditions. **Relevant to Q2:** Down syndrome shows reduced AIRE in the thymus (EP290 ledger §5). | L1 / Q2 |
| University of Iowa | Ali Jabbari | IL-27 and downstream mechanisms in AA (R01AR077194) | R01 | Cytokine signalling behind the T-cell attack | L2 mechanism |
| University of Pennsylvania | Leo Wang | Hydrogel therapies for AA (K08AR084610) | K08 | Local drug delivery in an AA model | L1 / L2 |
| Lybra Bio (small business) | Nuria Puigmal | Microneedle expansion of skin-resident regulatory T cells (R43AI194889) | SBIR | **Local Treg expansion: rebuilding tolerance.** AA is the example disease named in the abstract. | **L2 cure direction** |

### 1b. Adjacent: relevant T-cell biology, but AA only in RePORTER's keyword terms (3)

| Institution | PI(s) | Project | Why it is relevant |
|:--|:--|:--|:--|
| University of Pittsburgh | Daniel Kaplan; Harinder Singh | How TGF-β competition sustains skin **memory CD8 T cells** (R01AR083713) | Resident memory T cells drive relapse (T-cell review, link 5) |
| University of Pennsylvania | Christoph Ellebrecht | Protein translation and **T-cell adaptation to human skin** (R01AR086989) | How T cells come to live in skin |
| UMass Chan Medical School | Qi Tang | Programmable siRNA gene silencing in skin (R00AR082987) | A possible way to deliver local therapy |

### 1c. AA mentioned only as background (3, not AA research)

- Seattle Children's: JAK inhibition in SIV infection.
- UT Southwestern: STING antagonists.
- Incuta Therapeutics: a hydrogel for hidradenitis suppurativa. Its earlier
  work used an AA mouse model, so this one is borderline.

## 2. Who publishes AA research (PubMed, 2023–2026)

Query: AA as a MeSH major topic or in the title, 2023–26, excluding reviews,
case reports, comments, editorials and letters. That gives **960 records, 920
parsed**. A group is identified by its **last author**, the usual lab-head
position, and its most frequent affiliation. This is a heuristic: authors who
share a name are merged, and some affiliations parse badly.

**Last-author affiliations by country:** USA 196 · China 164 · Italy 50 ·
Korea 49 · Egypt 40 · UK 33 · India 30 · Japan 27 · Spain 27 · France 21 ·
Turkey 21 · Iran 19 · Germany 17 · Australia 16 · Canada 15 · unparsed 79.
China publishes nearly as much as the US and appears nowhere in RePORTER.

### 2a. Most active groups

| Papers | Group (last author) | Institution | Focus, read from titles | Lane |
|--:|:--|:--|:--|:--|
| 18 | Mostaghimi A | Brigham and Women's Hospital, Boston | Treatment consensus (chaired the 2026 US Delphi), diagnosis delays, patient preferences | L1 clinical |
| 17 | Shi W | Central South University, China | **JAK response in long-standing AA (≥8 years)**; predictors of relapse after stopping ritlecitinib | **L1: most relevant to the subgroup** |
| 12 | Ungar B | Mount Sinai | Real-world ritlecitinib; dupilumab and AA risk | L1 |
| 11 | Piraccini BM | University of Bologna | Real-world JAK effectiveness, how long to treat | L1 |
| 10 | Zhou C | Peking University People's Hospital | **Totalis/universalis outcomes**, upadacitinib long-term, ophiasis prognosis | **L1: subgroup-relevant** |
| 9 | Lejeune A | Pfizer | Ritlecitinib safety up to about 5 years | L1 (industry) |
| 8 | King B | Dermatology Physicians of Connecticut, Fairfield (earlier papers: Yale) | Baricitinib maintenance; **switching JAKs after failure**; oral minoxidil | L1 |
| 8 | Arias-Santiago S | Hospital Virgen de las Nieves, Granada | Baricitinib cohort; cardiovascular risk | L1 |
| 7 | Atef LM | Suez Canal University, Egypt | microRNA and lncRNA biomarkers | L2 (exploratory) |
| 6 | Song X | Zhejiang University | **Soluble CD83 reverses AA via Treg activation** (mouse); Mendelian randomisation | **L2 tolerance** |
| 6 | Guttman-Yassky E | Mount Sinai | Single-cell scalp profiling, OX40, multiomics of ritlecitinib response | L2 mechanism |
| 6 | Mesinkovska NA | University of California, Irvine | JAK adverse events, mortality trends | L1 |
| 5 | Ohyama M | Kyorin University, Japan | **Effector memory T cells characterise treatment-resistant severe AA** | **L2: memory T cells** |
| 5 | Lipner SR | Weill Cornell | AA and fertility or pregnancy outcomes (TriNetX) | L5 / epidemiology |
| 5 | Sinclair R | Melbourne | Baricitinib long-term safety; patient burden | L1 |
| 5 | Tziotzios C | St John's Institute of Dermatology, London | Clinical signs; psychosocial burden | L1 |
| 5 | Wu W | Shanghai Institute of Dermatology | Upadacitinib in acute, refractory and paediatric AA | L1 |
| 5 | Bhoyrul B | Melbourne | Case-level JAK and TYK2 responses (deucravacitinib) | L1 |
| 5 | Starace M | University of Bologna | Baricitinib 48-week results; etrasimod | L1 |
| 4 | Christiano AM | Columbia | **Pathogenic CD8 T cells targeting a follicle layer (K71+ Henle's layer)** | **L2: the target itself** |
| 4 | Lew BL | Kyung Hee University, Seoul | NLRP3 inflammasome; **Th17/Treg in acute diffuse and total AA**; early-onset childhood AA | L2 / L5 |
| 4 | Donovan J | University of British Columbia | Systematic reviews: upadacitinib, diet, retinal findings | L1 / diet |

### 2b. Mechanism slice: tolerance, T cells, thymus (lane L2)

The primary query was narrowed to Tregs, immune privilege, tolerance,
thymus, memory and resident-memory T cells, CD8, IL-2/7/15, NKG2D and
autoantigens. That leaves **79 records**. The groups:

| Group | Institution | Line of work | Why it matters |
|:--|:--|:--|:--|
| Christiano AM | Columbia | CD8 T cells and the follicle target; NKG2A (R01) | Defines what the attack hits |
| Song X | Zhejiang / Hangzhou | CD83 → IDO → Treg activation reverses AA in mice | Tolerance-restoring, preclinical |
| Gilhar A | Technion (Rappaport Faculty), Israel | **Autologous γδ T-cell therapy for AA** (humanised mouse) | A cell-therapy route to tolerance, flagged in the plan |
| Bertolini M; Paus R | QIMA Monasterium (Germany); University of Miami | Collapse and **restoration of follicle immune privilege** ex vivo | The "rebuild the shield" link |
| Jabbari A | University of Iowa | Th1 CD4 cells and IFN-γ in AA induction; IL-27 (R01) | Mechanism |
| Park SH | KAIST, Korea | A CD8 subset originating from virtual memory T cells | Memory T cells |
| Ohyama M | Kyorin University | Memory T cells in resistant disease | Relapse and resistance |
| Honda T | Hamamatsu University | Skin-resident memory T cells in refractory chronic AA | Memory T cells: the long-duration problem |
| Shi W; Lee YT | Central South University; Chung Shan Medical University | Sequential tofacitinib then **low-dose IL-2** | Treg approach in patients |
| Guttman-Yassky E | Mount Sinai | Single-cell scalp atlas | Mechanism map |
| Li S (Du 2025) | Peking Union Medical College Hospital | **AA in thymoma patients**, CD4/CD8 inversion | Q2 thymus link |

### 2c. Adjacent to plan Q2: thymus and central-tolerance groups

These groups do **not** work on AA. They are listed because the podcast lead
and Q2 point at them. Every affiliation was checked against a paper fetched
this session.

| Group | Institution | Relevance |
|:--|:--|:--|
| Lionakis MS | NIAID, NIH | Runs the largest US APECED (AIRE-deficiency) cohort (Ferre 2016) |
| Castelo-Soccio L | NIAMS Dermatology Branch, NIH | AA timing in that APECED cohort (Englander 2023) |
| McCarthy EA; Markert ML | Duke | Cultured thymus implants for infants born without a thymus; alopecia among the later autoimmune events (Markert 2022) |
| Sykes M | Columbia (Center for Translational Immunology) | Thymus-induced transplant tolerance; the 2026 *Nature* thymokidney decedent study |
| Colobran R | Vall d'Hebron, Barcelona | Reduced thymic AIRE in Down syndrome |
| Blackburn CC | MRC Centre for Regenerative Medicine, Edinburgh | A working thymus built from FOXN1-reprogrammed cells (mouse, 2014) |
| Manley N | Arizona State University | Thymus organogenesis; a United Therapeutics partner, per the podcast |
| Zandstra P | University of British Columbia | Engineered thymic niche for growing T cells outside the body; a United Therapeutics partner, per the podcast |
| Thymmune Therapeutics (Stan Wang) | owned by United Therapeutics since 2026-07-02 | THY-100, thymic cells made from induced stem cells; preclinical; lists "autoimmune diseases" as a target |

## 3. Who sponsors the recruiting AA trials (ClinicalTrials.gov)

**34 studies are recruiting now.** The trials module's 46 also counts studies
not yet recruiting. These 34 are what remains after dropping the 49 non-AA
studies that ClinicalTrials.gov's search returns for "alopecia areata"
(§5). Sponsor class: industry 18, academic/other 10, government 3, research
network 2, NIH 1.

| Sponsor | Studies | What |
|:--|--:|:--|
| Pfizer | 4 | Ritlecitinib (dose studies, Litfulo registry) |
| AbbVie | 3 | Upadacitinib Phase 3 |
| Eli Lilly | 2 | Baricitinib Phase 3; **LY4005130**, code-named, Phase 2 |
| Sun Pharma (US and Japan) | 2 | Deuruxolitinib Phase 3 |
| **HCW Biologics** | 1 | **HCW9302, an IL-2 fusion protein** (Phase 1): the only Treg/tolerance trial |
| **Innovent Biologics** | 1 | **IBI3013** (Phase 1): an IL-7/IL-15/CD122 approach against memory T cells |
| Forte Biosciences | 1 | FB102 (Phase 1), code-named |
| Aldena Therapeutics | 1 | ALD-102 (Phase 1/2), code-named |
| Almirall | 1 | LAD603 (Phase 2), code-named |
| Jiangsu Vcare | 1 | VC005 (Phase 2), code-named |
| NEXTGEN Bioscience | 1 | NXC736 (Phase 2), code-named |
| Mount Sinai / E. Guttman | 3 | Dupilumab ×2; abrocitinib |
| NIAID (NIH) | 1 | Ruxolitinib Phase 2 |
| **University of Minnesota** | 1 | **Microbiota transplant therapy** (vancomycin + neomycin, then MTT capsules; Phase 2): the gut-microbiome route |
| Erasmus MC | 2 | Cyclosporin vs methotrexate (Phase 4); registry |
| Others | 9 | Registries (CorEvitas ×2, Rome, Kiel, Zhejiang), UVB laser (Szeged), triamcinolone and vitamin D (Istanbul, Zagazig) |

Six code-named drugs are recruiting: LY4005130, FB102, ALD-102, LAD603, VC005
and NXC736. They are plan **Q4**; their sponsors are the place to look for
mechanism disclosures (patents, conference abstracts, pipeline pages).

## 4. What this register cannot see

- **Non-US public funding.** RePORTER is NIH only. China, Italy, Korea, Egypt
  and Japan publish heavily with no RePORTER footprint. Possible later
  additions: NSFC (China), Europe PMC grant links, EU CORDIS. None was
  checked this session.
- **Company pipelines** beyond what trial registrations disclose.
- **Groups that publish under other disease names.** Thymus, Treg and
  memory-T-cell groups working on vitiligo, type 1 diabetes or lupus are
  largely invisible to an AA query. §2c is a hand-picked start, not a
  sweep.
- **Name collisions.** Grouping by last author merges different people with
  the same surname and initials (e.g. Shi W, Lee Y). Check before citing.

## 5. Defect found while building this (fixed in the collector, see the recap)

ClinicalTrials.gov's `query.cond=alopecia areata` also matches androgenetic,
chemotherapy-induced and scarring alopecia. The trials watch already filters
this out (`alopecia_trials.is_aa`); the **daily collector did not**. As a
result, **54 of the 91 "trials" items in the live collector ledger were not
AA**. The fix, and what it did to the baseline, are in `BASELINE-2026-09.md`.
