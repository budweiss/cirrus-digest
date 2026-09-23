#!/usr/bin/env python3
"""ALOPECIA P3 — trials watch: nearest site + an honest eligibility read.

    python3 alopecia_trials.py              # print the brief section (network)
    python3 alopecia_trials.py --selftest

S258. The spec's P3 (alopecia/ALOPECIA-SPEC.md). Deterministic, no LLM.

PRIVACY. The outbound query is condition vocabulary only, checked against
alopecia_collect.APPROVED_QUERY_VOCAB. Location never goes into a query
string: we fetch every recruiting AA study and compute distances HERE, from a
reference point in config/alopecia_profile.json -- an on-box file (chmod 600,
never committed). Without it the section says so and omits distances.

THE TRAP (T48, foundation doc 7a): a study's `locations` are NOT distance-
ordered and a geo filter matches if ANY site is in range, so every site is
scanned and the nearest computed here.

THE CONSTRAINT THAT MATTERS MOST: eligibility, not geography. AA trials often
cap disease duration or exclude universalis, and the subject is ~16 years in.
A trial 20 miles away that excludes him is reported as LIKELY EXCLUDED with
the criterion quoted -- never as "you may fit". This reads posted criteria by
pattern; a site coordinator and a dermatologist decide. Registration = grade T.
"""
import json
import math
import re
import sys
from pathlib import Path
from urllib.parse import urlencode

PROJECT_DIR = Path(__file__).resolve().parent
PROFILE_PATH = PROJECT_DIR / "config/alopecia_profile.json"
CT_URL = "https://clinicaltrials.gov/api/v2/studies"
QUERY = {"query.cond": "alopecia areata",
         "filter.overallStatus": "RECRUITING|NOT_YET_RECRUITING",
         "pageSize": "100"}
MAX_PAGES = 5

# Condition-level, matching alopecia_brief.SUBGROUP (adult, ~16-year
# universalis, onset ~age 10). No name, no location.
SUBJECT = {"age": 26, "duration_years": 16, "sex": "MALE", "pattern": "universalis"}

LIVE_SITE = {"RECRUITING", "NOT_YET_RECRUITING", "ENROLLING_BY_INVITATION", ""}
LONG_DURATION_MIN = 5          # a trial that REQUIRES >= this many years is flagged


# ── fetching ────────────────────────────────────────────────────────────────
def query_is_condition_level(q=QUERY):
    from alopecia_collect import APPROVED_QUERY_VOCAB
    words = re.findall(r"[A-Za-z][A-Za-z\-']*", q["query.cond"])
    return all(w.lower() in APPROVED_QUERY_VOCAB for w in words)


def fetch_studies():
    if not query_is_condition_level():
        raise RuntimeError("REFUSED: trials query is not condition-level vocabulary")
    from alopecia_collect import _get
    out, token = [], None
    for _ in range(MAX_PAGES):
        q = dict(QUERY, **({"pageToken": token} if token else {}))
        d = _get(CT_URL + "?" + urlencode(q))
        out += d.get("studies", [])
        token = d.get("nextPageToken")
        if not token:
            break
    return out


def is_aa(study):
    p = study.get("protocolSection", {})
    text = " ".join(p.get("conditionsModule", {}).get("conditions", []) +
                    [p.get("identificationModule", {}).get("briefTitle", "")]).lower()
    return bool(re.search(r"areata|universalis|totalis", text))


# ── distance ────────────────────────────────────────────────────────────────
def miles(a_lat, a_lon, b_lat, b_lon):
    r = 3958.8
    dlat, dlon = math.radians(b_lat - a_lat), math.radians(b_lon - a_lon)
    h = (math.sin(dlat / 2) ** 2 + math.cos(math.radians(a_lat)) *
         math.cos(math.radians(b_lat)) * math.sin(dlon / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(h))


def nearest_site(locations, ref):
    """(miles, location) for the nearest LIVE site with coordinates, or None.
    Scans EVERY location -- the list is not distance-ordered."""
    best = None
    for loc in locations or []:
        g = loc.get("geoPoint") or {}
        if "lat" not in g or (loc.get("status") or "") not in LIVE_SITE:
            continue
        d = miles(ref["ref_lat"], ref["ref_lon"], g["lat"], g["lon"])
        if best is None or d < best[0]:
            best = (d, loc)
    return best


# ── eligibility ─────────────────────────────────────────────────────────────
_NUM = r"(\d+(?:\.\d+)?)\s*(years?|months?)"
_UPPER = (r"(?:≤|<=|<|not exceed(?:ing)?|does not exceed|do not exceed|no (?:more|longer) than|"
          r"less than|up to|maximum(?: of)?)\s*" + _NUM)
_LOWER = r"(?:≥|>=|>|at least|more than|greater than|longer than)\s*" + _NUM
_BETWEEN = r"between\s*\d+(?:\.\d+)?\s*(?:years?|months?)\s*and\s*" + _NUM
_DURATION_WORDS = r"episode|duration|hair loss|regrowth"
_AGE_WORDS = r"\bage\b|years old|years of age|\baged\b"


def _years(n, unit):
    return float(n) / (12.0 if unit.startswith("month") else 1.0)


def split_criteria(text):
    t = (text or "").replace("\\", "")
    m = re.search(r"exclusion criteria", t, re.I)
    return (t[:m.start()], t[m.end():]) if m else (t, "")


def _clauses(text):
    return [" ".join(c.split()) for c in re.split(r"[\n;]|\*", text) if c.strip()]


def _around(c, m):
    """The words around the matched number -- a long clause cut at its start
    hid the '7 years' that made it a cap (NCT07242638, first live run)."""
    a, b = max(0, m.start() - 90), min(len(c), m.end() + 40)
    return ("…" if a else "") + c[a:b] + ("…" if b < len(c) else "")


def duration_limits(inclusion, exclusion):
    """{'max': (years, quote) | None, 'min': (years, quote) | None}."""
    out = {"max": None, "min": None}
    for c in _clauses(inclusion):
        if not re.search(_DURATION_WORDS, c, re.I) or re.search(_AGE_WORDS, c, re.I):
            continue
        for rx in (_UPPER, _BETWEEN):
            m = re.search(rx, c, re.I)
            if m:
                y = _years(*m.groups())
                if out["max"] is None or y < out["max"][0]:
                    out["max"] = (y, _around(c, m))
        for m in re.finditer(_LOWER, c, re.I):
            y = _years(*m.groups())
            if out["min"] is None or y > out["min"][0]:
                out["min"] = (y, _around(c, m))
    for c in _clauses(exclusion):
        if not re.search(_DURATION_WORDS, c, re.I) or re.search(_AGE_WORDS, c, re.I):
            continue
        m = re.search(_LOWER, c, re.I)     # "excluded if episode > N years" = a cap
        if m:
            y = _years(*m.groups())
            if out["max"] is None or y < out["max"][0]:
                out["max"] = (y, _around(c, m))
    return out


def _age_years(s):
    m = re.match(r"(\d+)\s*(year|month|week|day)", (s or "").lower())
    if not m:
        return None
    n, u = int(m.group(1)), m.group(2)
    return n if u == "year" else n / {"month": 12, "week": 52, "day": 365}[u]


def fit(study, subject=SUBJECT):
    """{'excluded': [reasons], 'check': [notes], 'min_years': float|None}."""
    e = study.get("protocolSection", {}).get("eligibilityModule", {})
    inc, exc = split_criteria(e.get("eligibilityCriteria", ""))
    excluded, check = [], []

    lo, hi = _age_years(e.get("minimumAge")), _age_years(e.get("maximumAge"))
    if (lo is not None and subject["age"] < lo) or (hi is not None and subject["age"] > hi):
        excluded.append("age %s–%s" % (e.get("minimumAge") or "any",
                                        e.get("maximumAge") or "any"))
    sex = (e.get("sex") or "ALL").upper()
    if sex not in ("ALL", subject["sex"]):
        excluded.append("%s only" % sex.lower())

    lim = duration_limits(inc, exc)
    if lim["max"] and subject["duration_years"] > lim["max"][0]:
        excluded.append('disease-duration cap: "%s"' % lim["max"][1])
    if lim["min"] and subject["duration_years"] < lim["min"][0]:
        excluded.append('needs longer disease: "%s"' % lim["min"][1])

    for c in _clauses(exc):
        if re.search(r"universalis|totalis", c, re.I) and \
                not re.search(r"not (be )?excluded|are eligible|allowed", c, re.I):
            excluded.append('pattern: "%s"' % c[:140])
            break
    for c in _clauses(exc):
        if re.search(r"\bJAK\b|janus kinase|baricitinib|ritlecitinib|tofacitinib|"
                     r"ruxolitinib|deuruxolitinib|upadacitinib", c, re.I):
            check.append('depends on treatment history: "%s"' % c[:120])
            break
    return {"excluded": excluded, "check": check,
            "min_years": lim["min"][0] if lim["min"] else None}


def verdict(f):
    if f["excluded"]:
        return "LIKELY EXCLUDED — " + "; ".join(f["excluded"])
    v = "No exclusion found on age, sex, duration or pattern"
    if f["check"]:
        v += " — " + f["check"][0]
    return v + " — every other criterion still applies"


# ── how the field is approaching it ─────────────────────────────────────────
# Grouped by the intervention's NAME and REGISTERED DESCRIPTION only -- a
# code-named drug is classified when its registration says what it does, and
# otherwise listed as undisclosed rather than guessed. Order follows the
# attack chain in alopecia/TCELL-ANSWER-2026-09-11.md: suppress the signal,
# rebuild tolerance, starve memory T cells, then everything else.
APPROACHES = [
    ("Block the attack signal — JAK inhibitors (suppress while taken)",
     r"baricitinib|upadacitinib|ruxolitinib|tofacitinib|deuruxolitinib|abrocitinib|"
     r"ritlecitinib|brepocitinib|ivarmacitinib|litfulo|olumiant|leqselvi|\bjak\d?\b|"
     r"janus kinase"),
    ("Rebuild tolerance — IL-2 / regulatory T cells (aims at lasting remission)",
     r"\bil-?2\b|interleukin-?2\b|regulatory t|\btregs?\b|rezpegaldesleukin|aldesleukin"),
    ("Starve the memory T cells — IL-7 / IL-15 / CD122",
     r"\bil-?7|\bil-?15|interleukin-?(?:7|15)\b|cd122|bempikibart"),
    ("Other targeted immune drugs",
     r"dupilumab|\bil-?4|\bil-?13|ox40|\bil-?17|\bil-?23|pde-?4|apremilast|abatacept"),
    ("Broad immunosuppressants & steroids",
     r"methotrexate|cyclospor|azathioprine|mycophenol|triamcinolone|betamethasone|"
     r"mometasone|clobetasol|prednis|dexamethasone|corticoster|conventional systemic"),
    ("Gut microbiome", r"fecal|faecal|microbio|vancomycin|neomycin|probiotic"),
    ("Repurposed / metabolic & vitamin D",
     r"simvastatin|statin|metformin|vitamin d|\bvit d\b|calcipotriol"),
    ("Physical & devices", r"laser|uvb|phototherap|microneedl|platelet[- ]rich|\bprp\b"),
    ("Hair-growth stimulants (not immune)", r"minoxidil"),
]
UNDISCLOSED = "Code-named — mechanism not stated in the registration"


def approaches(study):
    """{approach: [intervention names]} for one study."""
    out = {}
    for iv in study.get("protocolSection", {}).get("armsInterventionsModule", {}) \
                  .get("interventions", []) or []:
        name = iv.get("name", "")
        if re.search(r"placebo|vehicle|saline|observation", name, re.I):
            continue
        text = "%s %s" % (name, iv.get("description", ""))
        hits = [label for label, rx in APPROACHES if re.search(rx, text, re.I)]
        if not hits and (iv.get("type") or "").upper() in ("DRUG", "BIOLOGICAL"):
            hits = [UNDISCLOSED]
        for h in hits:
            out.setdefault(h, []).append(name)
    return out


def approach_map(studies):
    """[(approach, n_studies, sorted distinct intervention names)], most first."""
    agg = {}
    for s in studies:
        for label, names in approaches(s).items():
            n, seen = agg.get(label, (0, set()))
            agg[label] = (n + 1, seen | set(names))
    order = [a for a, _ in APPROACHES] + [UNDISCLOSED]
    return sorted(((a, n, sorted(v)) for a, (n, v) in agg.items()),
                  key=lambda r: (-r[1], order.index(r[0])))


# ── the brief section ───────────────────────────────────────────────────────
def load_profile(path=None):
    try:
        return json.loads(Path(path or PROFILE_PATH).read_text())
    except Exception:
        return None


def _line(study, near, f):
    p = study["protocolSection"]
    ident = p["identificationModule"]
    nct = ident["nctId"]
    phase = "/".join(ph.replace("PHASE", "Ph") for ph in
                     p.get("designModule", {}).get("phases", []) or []) or "phase n/a"
    where = ""
    if near:
        d, loc = near
        where = " · nearest: %s, %s (%d mi)" % (loc.get("facility", "?"),
                                                loc.get("city", "?"), round(d))
    return ("- **%s** — %s (%s)%s  \n  %s · https://clinicaltrials.gov/study/%s"
            % (nct, ident.get("briefTitle", ""), phase, where, verdict(f), nct))


def build_section(studies, profile, subject=SUBJECT):
    aa = [s for s in studies if is_aa(s)]
    head = ["## Trials watch — nearest sites and fit", ""]
    rows = []
    for s in aa:
        locs = s["protocolSection"].get("contactsLocationsModule", {}).get("locations", [])
        rows.append((s, nearest_site(locs, profile) if profile else None, fit(s, subject)))

    if profile:
        tight, wide = profile.get("tight_mi", 50), profile.get("wide_mi", 120)
        near = sorted([r for r in rows if r[1] and r[1][0] <= wide], key=lambda r: r[1][0])
        n_tight = sum(1 for r in near if r[1][0] <= tight)
        open_near = [r for r in near if not r[2]["excluded"]]
        head.append("%d recruiting alopecia-areata studies; %d with a site within %d mi, "
                    "%d within %d mi (from %s). **%d of those show no exclusion on "
                    "the checked criteria.**" % (len(aa), n_tight, tight, len(near), wide,
                                                 profile.get("ref_label", "the reference point"),
                                                 len(open_near)))
        head.append("")
        head += [_line(*r) for r in near] or ["_None within %d mi this week._" % wide]
        shown = {id(r[0]) for r in near}
    else:
        head.append("_No location profile on this box — distances not computed._ "
                    "%d recruiting alopecia-areata studies." % len(aa))
        shown = set()

    # Only trials that do NOT exclude the subject: a ">= N years" match can be
    # an allowance ("more than 10 years could be included") beside a cap, and
    # a "long-standing" label on a trial that caps duration is the false hope
    # this section exists to prevent (NCT06562894, first live run).
    longrun = [r for r in rows if (r[2]["min_years"] or 0) >= LONG_DURATION_MIN
               and not r[2]["excluded"] and id(r[0]) not in shown]
    if longrun:
        head += ["", "**Open to long-standing disease (any distance):**"]
        head += [_line(*r) for r in longrun]

    amap = approach_map(aa)
    if amap:
        head += ["", "**How the %d recruiting trials are approaching it** (any distance; "
                 "a trial can count under more than one):" % len(aa)]
        head += ["- %s — %d trial%s: %s" % (a, n, "" if n == 1 else "s",
                                            ", ".join(names[:6]) + (" …" if len(names) > 6 else ""))
                 for a, n, names in amap]

    head += ["", "_Registrations only (grade T): a plan, not a result. Fit is read "
             "from the posted criteria by pattern and checks only age, sex, disease "
             "duration and universalis/totalis — a site coordinator decides the rest. "
             "Worth asking a dermatologist about; not a recommendation._", ""]
    return "\n".join(head)


def brief_section():
    """Network + profile. Raises on failure -- the caller makes that visible."""
    return build_section(fetch_studies(), load_profile())


# ── selftest ────────────────────────────────────────────────────────────────
def selftest():
    import tempfile
    ok = [True]

    def ck(name, cond):
        print("%s %s" % ("PASS" if cond else "FAIL", name))
        ok[0] = ok[0] and bool(cond)

    ref = {"ref_lat": 40.0, "ref_lon": -75.0, "tight_mi": 50, "wide_mi": 120,
           "ref_label": "ref"}
    far = {"facility": "Far", "city": "Irvine", "status": "RECRUITING",
           "geoPoint": {"lat": 33.67, "lon": -117.82}}
    close = {"facility": "Close", "city": "Near", "status": "RECRUITING",
             "geoPoint": {"lat": 40.1, "lon": -75.1}}
    shut = {"facility": "Shut", "city": "X", "status": "COMPLETED",
            "geoPoint": {"lat": 40.0, "lon": -75.0}}
    n = nearest_site([far, {"facility": "NoGeo"}, shut, close], ref)
    ck("nearest scans EVERY site, not the first listed (the T48 trap)",
       n and n[1]["facility"] == "Close")
    ck("a closed site is not 'nearest' even at distance 0", n[1]["facility"] != "Shut")
    ck("distance is right: 0.1 deg lat/lon ~ 8.6 mi", 8 < n[0] < 9.5)
    ck("no usable site -> None", nearest_site([{"facility": "NoGeo"}], ref) is None)

    def st(crit, lo="18 Years", hi=None, sex="ALL", cond="Alopecia Areata"):
        return {"protocolSection": {
            "identificationModule": {"nctId": "NCT0", "briefTitle": "t"},
            "conditionsModule": {"conditions": [cond]},
            "eligibilityModule": {"eligibilityCriteria": crit, "minimumAge": lo,
                                  "maximumAge": hi, "sex": sex}}}

    f = fit(st("Inclusion Criteria:\n* Current episode of hair loss lasting at least "
               "6 months and not exceeding 7 years\nExclusion Criteria:\n* none"))
    ck("'not exceeding 7 years' excludes a 16-year case, quoting the criterion",
       f["excluded"] and "not exceeding 7 years" in f["excluded"][0])
    f = fit(st("Inclusion Criteria:\n* Current duration of severe alopecia areata "
               "≥6 months and \\<4 years;"))
    ck("escaped '\\<4 years' is read as a 4-year cap", f["excluded"]
       and "duration" in f["excluded"][0])
    f = fit(st("Inclusion Criteria:\n* no evidence of hair regrowth for ≥ 7 years "
               "since their last episode"))
    ck("a >=7-year REQUIREMENT does not exclude a 16-year case, and is flagged",
       not f["excluded"] and f["min_years"] == 7)
    f = fit(st("Inclusion Criteria:\n* adults\nExclusion Criteria:\n* Duration of "
               "current episode greater than 10 years"))
    ck("an EXCLUSION 'duration > 10 years' is a cap", f["excluded"])
    f = fit(st("Inclusion Criteria:\n* adults\nExclusion Criteria:\n* Participant has a "
               "history of AA with no evidence of hair regrowth for ≥ 7 years since "
               "their last episode of hair loss."))
    ck("'no regrowth for >= 7 years' under EXCLUSION excludes a 16-year case "
       "(NCT05866562 -- it does NOT recruit long-standing disease)", f["excluded"])
    long_clause = ("Participants with AA: A. cause of hair loss is indeterminable and/or "
                   "they have concomitant causes of alopecia, such as traction, "
                   "cicatricial, pregnancy-related or drug-induced; B. no evidence of "
                   "hair regrowth for ≥7 years")
    f = fit(st("Inclusion Criteria:\n* adults\nExclusion Criteria:\n* " + long_clause))
    ck("the quote shows the number even deep in a long clause", "≥7 years" in f["excluded"][0])
    f = fit(st("Inclusion Criteria:\n* Active skin disease for more than 6 months"))
    ck("'disease' alone is not a duration trigger", not f["excluded"])
    f = fit(st("Inclusion Criteria:\n* Men or women between ≥19 and ≤65 years of age"))
    ck("an AGE clause is not mistaken for a duration cap", not f["excluded"])
    f = fit(st("Inclusion Criteria:\n* SALT >= 50\nExclusion Criteria:\n"
               "* Alopecia universalis or totalis"))
    ck("universalis in EXCLUSION -> likely excluded", f["excluded"]
       and "pattern" in f["excluded"][0])
    f = fit(st("Inclusion Criteria:\n* AA including alopecia universalis\n"
               "Exclusion Criteria:\n* pregnancy"))
    ck("universalis only in INCLUSION -> not excluded", not f["excluded"])
    ck("pediatric trial (6–17) -> excluded by age",
       fit(st("Inclusion Criteria:\n* x", lo="6 Years", hi="17 Years"))["excluded"])
    ck("female-only trial -> excluded by sex",
       fit(st("Inclusion Criteria:\n* x", sex="FEMALE"))["excluded"])
    f = fit(st("Inclusion Criteria:\n* x\nExclusion Criteria:\n* Prior treatment "
               "with a JAK inhibitor"))
    ck("prior-JAK exclusion is a CHECK, not a verdict", not f["excluded"] and f["check"])
    ck("an excluded verdict never reads as eligible",
       verdict({"excluded": ["age"], "check": [], "min_years": None}).startswith("LIKELY EXCLUDED"))
    ck("a clean verdict still says other criteria apply",
       "every other criterion still applies" in verdict({"excluded": [], "check": [],
                                                          "min_years": None}))
    ck("androgenetic alopecia is not an AA study",
       not is_aa(st("x", cond="Androgenetic Alopecia")))

    s_near = st("Inclusion Criteria:\n* x")
    s_near["protocolSection"]["contactsLocationsModule"] = {"locations": [far, close]}
    s_allow = st("Inclusion Criteria:\n* The current episode of hair loss was less than "
                 "10 years ago (overall duration more than 10 years could be included)")
    ck("an allowance beside a cap is NOT listed as open to long-standing disease",
       "long-standing" not in build_section([s_allow], None))
    s_open = st("Inclusion Criteria:\n* Duration of current episode at least 8 years")
    ck("a real >= 8-year requirement with no cap IS listed",
       "Open to long-standing disease" in build_section([s_open], None))
    sec = build_section([s_near], ref)
    ck("section lists the near trial with its nearest site and miles",
       "Close, Near (9 mi)" in sec)
    ck("no profile -> says distances were not computed",
       "distances not computed" in build_section([s_near], None))
    def iv(*pairs):
        return {"protocolSection": {"armsInterventionsModule": {"interventions": [
            {"type": t_, "name": n_, "description": d_} for t_, n_, d_ in pairs]}}}
    a = approaches(iv(("DRUG", "Ritlecitinib 50 mg", ""), ("DRUG", "Placebo", "")))
    ck("a JAK inhibitor is grouped as one; placebo is ignored",
       list(a) == [APPROACHES[0][0]])
    a = approaches(iv(("DRUG", "HCW9302, an IL-2 fusion protein", "")))
    ck("an IL-2 fusion protein is the tolerance approach", APPROACHES[1][0] in a)
    a = approaches(iv(("DRUG", "FB102", "a monoclonal antibody against CD122")))
    ck("a code-named drug is classified from its REGISTERED description",
       APPROACHES[2][0] in a)
    a = approaches(iv(("DRUG", "NXC736", "")))
    ck("a code-named drug with no stated mechanism is UNDISCLOSED, not guessed",
       list(a) == [UNDISCLOSED])
    a = approaches(iv(("OTHER", "Fecal Microbial Transplant Enema", "")))
    ck("a microbiome transplant is grouped as microbiome", "Gut microbiome" in a)
    m = approach_map([iv(("DRUG", "Baricitinib", "")), iv(("DRUG", "Upadacitinib", "")),
                      iv(("DRUG", "Dupilumab", ""))])
    ck("the map counts trials per approach, most first",
       m[0][0] == APPROACHES[0][0] and m[0][1] == 2)
    ck("the outbound query is condition vocabulary only", query_is_condition_level())
    ck("a query carrying a place name is REFUSED",
       not query_is_condition_level({"query.cond": "alopecia areata Philadelphia"}))
    with tempfile.TemporaryDirectory() as td:
        ck("a missing profile file reads as None, not an error",
           load_profile(Path(td) / "nope.json") is None)

    print("\n%s" % ("ALL PASS" if ok[0] else "FAILURES ABOVE"))
    return ok[0]


if __name__ == "__main__":
    if "--selftest" in sys.argv or "selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    print(brief_section())
