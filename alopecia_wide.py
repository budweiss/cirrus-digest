#!/usr/bin/env python3
"""ALOPECIA wide collection (S685) -- research from every field, not only AA papers.

Buddy, 2026-11-24 (alopecia/RESEARCH-PLAN.md section 0b): the three standing
questions "were just observation in RCW case but shouldn't be limited to these
points ... use chemistry, biology, dietary, physics, and any thing else that
could help with a cure. daily collect ...". The original collector
(alopecia_collect.py) searches for "alopecia areata" and keeps titles only; it
still feeds the Friday brief unchanged. This module adds fixed lanes, one per
field, through Europe PMC (PubMed, PMC and the preprint servers, WITH
abstracts), plus newly funded NIH grants (RePORTER). Its store is
alopecia/wide/, which the Friday brief and the daily agent never read: their
inputs are exactly what they were. The weekly synthesis (alopecia_synthesis.py)
reads both stores.

It does NOT judge. Each item carries deterministic tags only: its lane and
field, a condition tag (is this AA, other hair loss, a related disease or
follicle biology?) and the brief's own A/B/C/D/T/E evidence grade.

PRIVACY (spec hard rule): every lane query is built only from
WIDE_QUERY_VOCAB -- condition and biology words. No person, initials, place,
age or identifier. run() fails CLOSED if the check trips; the selftest plants
a non-vocabulary word to prove it fires.

A QUIET DAY AND A DAY WE COULD NOT SEE MUST NOT LOOK THE SAME (collector S83).
Europe PMC is measured slow and flaky (2 time-outs in 9 searches, one 503, on
2026-11-24), so each request retries, a failed lane is listed in an INCOMPLETE
banner, and every lane answering zero for a three-day index window is treated
as a source failure, not a quiet day.

    python3 alopecia_wide.py                 # collect, write, record seen
    python3 alopecia_wide.py --dry-run       # collect, write NOTHING
    python3 alopecia_wide.py --days 14       # widen the index-date window (first run)
    python3 alopecia_wide.py report          # the last day's file
    python3 alopecia_wide.py --selftest
"""

import html
import json
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
WIDE_DIR = PROJECT_DIR / "alopecia/wide"
SEEN_PATH = WIDE_DIR / "seen.json"

EPMC_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
REPORTER_URL = "https://api.reporter.nih.gov/v2/projects/search"
USER_AGENT = "cirrus-alopecia-monitor/1.0"
TIMEOUT = 60
RETRY_WAITS = (5, 15)          # three attempts in all
DEADLINE_S = 15 * 60           # the whole wide pass; later lanes are reported, not lost silently
LOOKBACK_DAYS = 3              # index-date window; the seen ledger removes the overlap
PDATE_FLOOR_DAYS = 365         # 2026-11-24: newly indexed 1890s "Journal Matter" records
PAGE_SIZE = 100
LANE_CAP = 300                 # per lane per run; a truncated lane is an error, never silent
ABSTRACT_CHARS = 2500

# (id, field, what it catches, term groups). Every group must match the TITLE
# or ABSTRACT (Europe PMC TITLE_ABS): S685's first real run searched full text
# and 199 of 304 items were unrelated (a cattle-gestation paper matched
# "hair follicle" + "T cell" somewhere in its body). Reviewed with the design
# (docs/evaluations/s685-alopecia-widened/DESIGN.md). Volumes measured on
# title/abstract, 2026-11-10..24: about 90 hits in 14 days across the lanes.
FOLLICLE = '"hair follicle" OR "hair follicles"'
LANES = [
    ("aa", "all fields",
     "alopecia areata in any field and on every preprint server",
     ['"alopecia areata" OR "alopecia universalis" OR "alopecia totalis"']),
    ("follicle-immunology", "biology & immunology",
     "immune privilege, T cells and interferon in the hair follicle",
     [FOLLICLE, '"immune privilege" OR "T cell" OR "T cells" OR interferon OR autoimmune '
                'OR autoimmunity']),
    ("follicle-regeneration", "biology",
     "follicle stem cells, regrowing follicles, the dermal papilla",
     ['"hair follicle stem cell" OR "hair follicle stem cells" OR "hair follicle regeneration" '
      'OR "hair neogenesis" OR "dermal papilla"']),
    ("shared-mechanism", "immunology",
     "vitiligo and similar diseases, where treatments often carry over",
     ['vitiligo', 'JAK OR "resident memory" OR CXCL10 OR "regulatory T" OR interferon']),
    ("tolerance", "immunology & biotech",
     "CAR-T, regulatory T-cell therapy, low-dose IL-2: drug-free remission in autoimmunity",
     ['"CAR-T" OR "CAR T" OR "regulatory T cell therapy" OR "low-dose IL-2" '
      'OR "antigen-specific tolerance"', 'autoimmune OR autoimmunity']),
    ("chemistry", "chemistry",
     "new molecules and ways of getting drugs into the follicle",
     [FOLLICLE + ' OR alopecia', '"small molecule" OR "drug delivery" OR nanoparticle '
      'OR nanoparticles OR "topical formulation" OR microneedle OR microneedles OR "JAK inhibitor"']),
    ("physics", "physics",
     "light, UV, lasers, ultrasound, electrical stimulation, microneedling",
     [FOLLICLE + ' OR alopecia', 'photobiomodulation OR "low-level laser" OR "low level laser" '
      'OR ultraviolet OR phototherapy OR excimer OR "electrical stimulation" OR ultrasound '
      'OR microneedling OR cryotherapy']),
    ("diet", "diet & microbiome",
     "diet, nutrients, vitamin D, zinc and gut bacteria linked to hair or AA",
     [FOLLICLE + ' OR alopecia OR "hair loss"', 'diet OR dietary OR microbiome OR microbiota '
      'OR "vitamin D" OR zinc OR gluten OR probiotic OR probiotics']),
]


def lane_query(lane):
    """The free-text part of a lane: each group must hit the title or abstract."""
    return " AND ".join("TITLE_ABS:(%s)" % g for g in lane[3])


REPORTER_TEXT = '"alopecia areata"'
REPORTER_SOURCE = "NIH RePORTER"

# Only these words may appear in a lane query or the grant search. A vocabulary,
# not a blocklist (alopecia_collect.py explains why).
WIDE_QUERY_VOCAB = {
    "and", "or", "not",
    "alopecia", "areata", "universalis", "totalis", "hair", "follicle", "immune",
    "privilege", "t", "cell", "cells", "interferon", "autoimmune", "autoimmunity",
    "stem", "regeneration", "neogenesis", "dermal", "papilla", "vitiligo", "jak",
    "resident", "memory", "cxcl10", "regulatory", "car", "therapy", "low-dose", "il-2",
    "antigen-specific", "tolerance", "skin", "dermatology", "dermatologic", "cutaneous",
    "small", "molecule", "drug", "delivery", "nanoparticle", "nanoparticles", "topical",
    "formulation", "microneedle", "microneedles", "inhibitor", "photobiomodulation",
    "low-level", "low", "level", "laser", "ultraviolet", "phototherapy", "excimer",
    "electrical", "stimulation", "ultrasound", "microneedling", "cryotherapy", "diet",
    "dietary", "microbiome", "microbiota", "vitamin", "d", "zinc", "gluten", "probiotic",
    "probiotics", "car-t", "follicles", "loss",
}

AA_RX = re.compile(r"areata|alopecia universalis|alopecia totalis|\balopecia (?:universalis|totalis)", re.I)
HAIR_RX = re.compile(r"alopecia|hair loss|baldness|hair growth|effluvium", re.I)
FOLLICLE_RX = re.compile(r"hair follicle|dermal papilla|hair cycle|hair shaft", re.I)


def log(msg):
    print("[%s] alopecia_wide: %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))


# ── privacy ──────────────────────────────────────────────────────────────────

def outbound_queries():
    """EVERY free-text query this module can issue (dates are added in code)."""
    q = {"epmc:" + lane[0]: " AND ".join(lane[3]) for lane in LANES}
    q["reporter"] = REPORTER_TEXT
    return q


def check_queries_condition_level():
    bad = []
    for name, q in outbound_queries().items():
        for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]*", q or ""):
            if tok.lower() not in WIDE_QUERY_VOCAB:
                bad.append((name, tok))
    return (not bad), bad


# ── fetching ─────────────────────────────────────────────────────────────────

def _request(method, url, params=None, body=None):
    import requests
    last = None
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            if method == "POST":
                r = requests.post(url, json=body, timeout=TIMEOUT,
                                  headers={"User-Agent": USER_AGENT})
            else:
                r = requests.get(url, params=params, timeout=TIMEOUT,
                                 headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            return r.json()
        except Exception as e:      # time-outs and 503s were both measured
            last = e
            if attempt < len(RETRY_WAITS):
                time.sleep(RETRY_WAITS[attempt])
    raise RuntimeError("%s after %d attempts" % (type(last).__name__, len(RETRY_WAITS) + 1))


def _clean(text, limit):
    # Europe PMC titles arrive escaped once or twice ("&lt;i&gt;in vivo&lt;/i&gt;").
    text = html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(text or "")))
    text = re.sub(r"\s+([.,;:)])", r"\1", re.sub(r"\s+", " ", text)).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " ..."


def window(today, days):
    start = today - timedelta(days=max(1, days))
    floor = today - timedelta(days=PDATE_FLOOR_DAYS)
    return ("FIRST_IDATE:[%s TO %s] AND FIRST_PDATE:[%s TO 2100-12-31]"
            % (start.isoformat(), today.isoformat(), floor.isoformat()))


def epmc_item(r):
    """One Europe PMC record -> one item. Returns None for an admin record."""
    types = (r.get("pubTypeList") or {}).get("pubType") or []
    if isinstance(types, str):
        types = [types]
    if any(t.lower() == "admin" for t in types):
        return None
    pmid, doi, src = r.get("pmid"), r.get("doi"), r.get("source") or ""
    key = ("pmid:%s" % pmid) if pmid else ("doi:%s" % doi.lower()) if doi else \
        "epmc:%s:%s" % (src, r.get("id"))
    if pmid:
        url = "https://pubmed.ncbi.nlm.nih.gov/%s/" % pmid
    elif doi:
        url = "https://doi.org/%s" % doi
    else:
        url = "https://europepmc.org/article/%s/%s" % (src, r.get("id"))
    preprint = src == "PPR"
    venue = ((r.get("journalInfo") or {}).get("journal") or {}).get("title") or \
        (r.get("bookOrReportDetails") or {}).get("publisher") or ""
    authors = (r.get("authorList") or {}).get("author") or []
    who = []
    for a in (authors[:1] + authors[-1:]) if len(authors) > 1 else authors[:1]:
        affs = ((a.get("authorAffiliationDetailsList") or {}).get("authorAffiliation") or [])
        aff = affs[0].get("affiliation", "") if affs else ""
        who.append(_clean("%s%s" % (a.get("fullName", ""), (" — " + aff) if aff else ""), 200))
    return {
        "key": key,
        "title": _clean(r.get("title"), 400).rstrip("."),
        "abstract": _clean(r.get("abstractText"), ABSTRACT_CHARS),
        "date": r.get("firstPublicationDate", ""),
        "indexed": r.get("firstIndexDate", ""),
        "source": "Europe PMC (%s)" % ("preprint" if preprint else (venue[:80] or src)),
        "url": url,
        "extra": ", ".join(types)[:160] + (", preprint" if preprint else ""),
        "preprint": preprint,
        "who": [w for w in who if w],
    }


def fetch_lane(lane, today, days, request=None):
    """All records for one lane in the window, following cursorMark pages."""
    request = request or _request
    query = "%s AND %s" % (lane_query(lane), window(today, days))
    out, cursor, total = [], "*", None
    while True:
        data = request("GET", EPMC_URL, params={
            "query": query, "format": "json", "resultType": "core",
            "pageSize": PAGE_SIZE, "cursorMark": cursor})
        if total is None:
            total = int(data.get("hitCount") or 0)
            if total > LANE_CAP:
                raise RuntimeError("lane %s has %d hits, over the %d cap -- coverage would be "
                                   "PARTIAL; raise LANE_CAP" % (lane[0], total, LANE_CAP))
        rows = (data.get("resultList") or {}).get("result") or []
        for r in rows:
            it = epmc_item(r)
            if it:
                out.append(it)
        nxt = data.get("nextCursorMark")
        if not rows or not nxt or nxt == cursor or len(out) >= total:
            break
        cursor = nxt
    return out, total


def fiscal_years(today):
    fy = today.year + (1 if today.month >= 10 else 0)
    return [fy - 1, fy]


def fetch_grants(today, request=None):
    request = request or _request
    data = request("POST", REPORTER_URL, body={
        "criteria": {"advanced_text_search": {"operator": "and",
                                              "search_field": "projecttitle,terms,abstracttext",
                                              "search_text": REPORTER_TEXT},
                     "fiscal_years": fiscal_years(today)},
        "include_fields": ["ProjectTitle", "Organization", "ProjectNum", "CoreProjectNum",
                           "AwardNoticeDate", "ContactPiName", "AbstractText", "FiscalYear"],
        "limit": 200, "offset": 0, "sort_field": "award_notice_date", "sort_order": "desc"})
    total = int((data.get("meta") or {}).get("total") or 0)
    rows = data.get("results") or []
    if total > len(rows):
        raise RuntimeError("RePORTER has %d projects, more than one page of %d -- "
                           "coverage would be PARTIAL" % (total, len(rows)))
    out = {}
    for r in rows:
        core = r.get("core_project_num") or r.get("project_num")
        if not core or core in out:
            continue                       # newest award year first: keep that one
        org = (r.get("organization") or {}).get("org_name", "")
        pi = r.get("contact_pi_name", "")
        out[core] = {
            "key": "nih:%s" % core,
            "title": _clean(r.get("project_title"), 400),
            "abstract": _clean(r.get("abstract_text"), ABSTRACT_CHARS),
            "date": (r.get("award_notice_date") or "")[:10],
            "source": REPORTER_SOURCE,
            "url": "https://reporter.nih.gov/project-details/%s" % (r.get("project_num") or core),
            "extra": "NIH grant FY%s" % r.get("fiscal_year", "?"),
            "preprint": False,
            "who": [w for w in [_clean("%s — %s" % (pi, org), 200) if (pi or org) else ""] if w],
        }
    return list(out.values()), total


# ── tagging ──────────────────────────────────────────────────────────────────

def condition_tag(it):
    """What the item is ABOUT. Deterministic, so pattern baldness is never read as AA."""
    title, text = it.get("title", ""), "%s %s" % (it.get("title", ""), it.get("abstract", ""))
    if AA_RX.search(title):
        return "AA"
    if AA_RX.search(text):
        return "mentions AA (abstract only)"
    if HAIR_RX.search(text):
        return "other hair loss (not AA)"
    if FOLLICLE_RX.search(text):
        return "hair-follicle biology"
    return "related disease or method (not hair)"


def evidence_grade(it):
    """The Friday brief's own grade (alopecia_brief.grade), with grants as plans."""
    if it.get("source") == REPORTER_SOURCE:
        return "T", "funded research plan (NIH grant) — a plan, not a result"
    from alopecia_brief import grade
    return grade({"title": it.get("title", ""), "extra": it.get("extra", ""),
                  "source": it.get("source", "")})


def tag(it, lanes):
    it = dict(it)
    it["lanes"] = sorted(set(it.get("lanes", [])) | set(lanes))
    by_id = {lane[0]: lane[1] for lane in LANES}
    it["fields"] = sorted({by_id.get(l, "who is funded") for l in it["lanes"]})
    it["condition"] = condition_tag(it)
    it["grade"], it["grade_label"] = evidence_grade(it)
    return it


# ── store ────────────────────────────────────────────────────────────────────

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def incomplete_banner(errors):
    if not errors:
        return []
    return (["> **INCOMPLETE — %d search(es) did not answer.** This day is not the full "
             "picture:" % len(errors), ">"] + ["> - %s" % e[:220] for e in errors] + [""])


def render(items, day, errors=()):
    lines = ["# Alopecia research, wider fields — daily collection %s" % day, "",
             "_Lanes: %s; plus new NIH grants. Tags are deterministic; the evidence grade is "
             "the Friday brief's._" % ", ".join(l[0] for l in LANES), ""]
    lines += incomplete_banner(errors)
    if not items:
        lines += (["_No new items from the searches that did answer._", ""] if errors else
                  ["_Nothing new today._", "", "A quiet day is a result: every search "
                   "answered and every item had been seen before.", ""])
        return "\n".join(lines)
    lines += ["**%d new item(s).**" % len(items), ""]
    by_field = {}
    for it in items:
        by_field.setdefault(it["fields"][0] if it.get("fields") else "?", []).append(it)
    for field in sorted(by_field):
        lines += ["## %s" % field, ""]
        for it in by_field[field]:
            lines.append("- **%s**  \n  grade %s · %s · %s · %s  \n  %s"
                         % (it.get("title") or "(untitled)", it.get("grade", "?"),
                            it.get("condition", "?"), it.get("source", ""),
                            it.get("date") or "undated", it.get("url", "")))
        lines.append("")
    return "\n".join(lines)


def run(dry_run=False, days=LOOKBACK_DAYS, today=None, request=None,
        seen_path=None, out_dir=None, clock=time.monotonic):
    ok, bad = check_queries_condition_level()
    if not ok:
        raise RuntimeError("wide query failed the condition-level check: %s" % bad)
    today = today or date.today()
    out_dir = Path(out_dir or WIDE_DIR)
    seen_path = Path(seen_path or (out_dir / "seen.json"))
    seen = load_json(seen_path, {})
    started = clock()
    found, errors, totals = {}, [], {}

    for lane in LANES:
        if clock() - started > DEADLINE_S:
            errors.append("Europe PMC lane %s: skipped, the wide pass passed its %d-minute "
                          "deadline" % (lane[0], DEADLINE_S // 60))
            continue
        try:
            got, total = fetch_lane(lane, today, days, request=request)
            totals[lane[0]] = total
            for it in got:
                prior = found.get(it["key"])
                found[it["key"]] = tag(it, (prior or {}).get("lanes", []) + [lane[0]])
            log("lane %s: %d hit(s)" % (lane[0], total))
        except Exception as e:
            errors.append("Europe PMC lane %s: %s" % (lane[0], e))
            log("lane %s FAILED: %s" % (lane[0], e))
    answered = [l for l in totals]
    if answered and len(answered) == len(LANES) and not any(totals.values()):
        errors.append("Europe PMC: every lane answered ZERO hits for a %d-day index window "
                      "-- treated as a source failure, not a quiet day" % max(1, days))
    try:
        grants, total = fetch_grants(today, request=request)
        for it in grants:
            found.setdefault(it["key"], tag(it, ["grants"]))
        log("RePORTER: %d project(s)" % total)
    except Exception as e:
        errors.append("NIH RePORTER: %s" % e)
        log("RePORTER FAILED: %s" % e)

    fresh = [it for k, it in found.items() if k not in seen]
    fresh.sort(key=lambda i: (i.get("fields", ["~"])[0], i.get("grade", "E"), i.get("title", "")))
    day = today.isoformat()
    merged = fresh
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        day_json = out_dir / ("wide-%s.json" % day)
        by_key = {it.get("key"): it for it in load_json(day_json, []) + fresh}
        merged = sorted(by_key.values(), key=lambda i: (i.get("fields", ["~"])[0],
                                                       i.get("grade", "E"), i.get("title", "")))
        day_json.write_text(json.dumps(merged, indent=1) + "\n")
        (out_dir / ("wide-%s.md" % day)).write_text(render(merged, day, errors) + "\n")
        prior_errors = load_json(out_dir / "errors.json", {})
        if errors:
            prior_errors[day] = sorted(set(prior_errors.get(day, [])) | set(errors))
            (out_dir / "errors.json").write_text(json.dumps(prior_errors, indent=1) + "\n")
        for it in fresh:
            seen[it["key"]] = day
        seen_path.write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n")
    return {"found": len(found), "new": len(fresh), "day_total": len(merged),
            "errors": errors, "day": day, "sources": len(LANES) + 1,
            "body": render(merged, day, errors)}


def healthy(stats):
    """False when the pass crashed or every search failed: then we are blind."""
    if not stats or stats.get("crashed"):
        return False
    return len(stats.get("errors") or []) < stats.get("sources", len(LANES) + 1)


def report(out_dir=None):
    files = sorted(Path(out_dir or WIDE_DIR).glob("wide-*.md"))
    if not files:
        print("no wide daily files yet")
        return 1
    print(files[-1].read_text())
    return 0


# ── selftest ─────────────────────────────────────────────────────────────────

def _fake_request(pages, grants=None, fail=()):
    """A request() double: pages[lane_word] is the list of Europe PMC pages."""
    calls = []

    def request(method, url, params=None, body=None):
        calls.append((method, url, params, body))
        if method == "POST":
            if "grants" in fail:
                raise RuntimeError("ReadTimeout after 3 attempts")
            g = grants or []
            return {"meta": {"total": len(g)}, "results": g}
        lane = next(l for l in LANES if params["query"].startswith(lane_query(l) + " AND FIRST_IDATE"))
        if lane[0] in fail:
            raise RuntimeError("HTTPError after 3 attempts")
        seq = pages.get(lane[0], [{"hitCount": 0, "resultList": {"result": []}}])
        n = sum(1 for c in calls if c[2] and c[2]["query"] == params["query"]) - 1
        return seq[min(n, len(seq) - 1)]
    request.calls = calls
    return request


def _rec(pmid=None, doi=None, title="t", abstract="", types=("Journal Article",), src="MED",
         pdate="2026-11-20"):
    return {"id": pmid or doi, "source": src, "pmid": pmid, "doi": doi, "title": title,
            "abstractText": abstract, "firstPublicationDate": pdate,
            "firstIndexDate": "2026-11-21", "pubTypeList": {"pubType": list(types)},
            "journalInfo": {"journal": {"title": "J Test"}},
            "authorList": {"author": [
                {"fullName": "First A", "authorAffiliationDetailsList": {"authorAffiliation": [
                    {"affiliation": "Dept of Dermatology, Example University"}]}},
                {"fullName": "Last Z", "authorAffiliationDetailsList": {"authorAffiliation": [
                    {"affiliation": "Institute of Immunology, Sample College"}]}}]}}


def selftest():
    """Offline: no network, no live ledger, no live output dir (T32)."""
    global LANES
    import tempfile
    ok = fail = 0

    def ck(name, cond):
        nonlocal ok, fail
        ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
        print("  [%s] %s" % ("OK " if cond else "FAIL", name))

    good, bad = check_queries_condition_level()
    ck("privacy: every lane and grant query uses approved vocabulary only (%s)" % (bad or "clean"),
       good)
    ck("privacy: the check covers every lane and the grant search",
       set(outbound_queries()) == {"epmc:" + l[0] for l in LANES} | {"reporter"})
    saved = LANES
    LANES = LANES + [("x", "x", "x", ['"alopecia areata"', 'Reykjavik'])]   # irrelevant place, on purpose
    try:
        planted_ok, planted_bad = check_queries_condition_level()
        ck("privacy: the check FIRES on a planted place-name", not planted_ok and planted_bad)
        try:
            run(dry_run=True, request=_fake_request({}))
            ck("privacy: run() fails CLOSED on a planted word", False)
        except RuntimeError:
            ck("privacy: run() fails CLOSED on a planted word", True)
    finally:
        LANES = saved

    t = date(2026, 11, 24)
    w = window(t, 3)
    ck("window: three-day index window with a one-year publication floor",
       "FIRST_IDATE:[2026-11-21 TO 2026-11-24]" in w and "FIRST_PDATE:[2025-11-24" in w)
    ck("fiscal years: November is the new US fiscal year", fiscal_years(t) == [2026, 2027])

    ck("tag: AA in the title", condition_tag({"title": "Baricitinib in alopecia areata"}) == "AA")
    ck("tag: universalis counts as AA", condition_tag({"title": "Alopecia universalis cohort"}) == "AA")
    ck("tag: AA only in the abstract is labelled so",
       condition_tag({"title": "IFN in skin", "abstract": "as in alopecia areata"})
       == "mentions AA (abstract only)")
    ck("tag: pattern baldness is NOT AA",
       condition_tag({"title": "Microneedling for androgenetic alopecia"}) == "other hair loss (not AA)")
    ck("tag: follicle biology", condition_tag({"title": "Dermal papilla niche signals"})
       == "hair-follicle biology")
    ck("tag: vitiligo is a related disease",
       condition_tag({"title": "CXCL10 in vitiligo"}) == "related disease or method (not hair)")
    ck("grade: an RCT in the publication type is A",
       evidence_grade({"title": "x", "extra": "Randomized Controlled Trial", "source": "Europe PMC (J)"})[0] == "A")
    ck("grade: a case report is C",
       evidence_grade({"title": "x", "extra": "Journal Article, Case Reports", "source": "Europe PMC (J)"})[0] == "C")
    ck("grade: a grant is T, a plan not a result",
       evidence_grade({"title": "x", "source": REPORTER_SOURCE})[0] == "T")

    ck("item: an Admin record is dropped", epmc_item(_rec(pmid="1", types=("Admin",))) is None)
    it = epmc_item(_rec(pmid="42", doi="10.1/X", title="A <i>study</i> &lt;i&gt;in vivo&lt;/i&gt;.", abstract="a &amp; b"))
    ck("item: PubMed ID is the key, matching the main collector's", it["key"] == "pmid:42")
    ck("item: markup stripped from title and abstract",
       it["title"] == "A study in vivo" and it["abstract"] == "a & b")
    ck("query: every group is confined to title and abstract",
       all(lane_query(l).count("TITLE_ABS:(") == len(l[3]) for l in LANES))
    ck("item: first and last author with affiliation kept for 'who'",
       len(it["who"]) == 2 and "Sample College" in it["who"][1])
    pp = epmc_item(_rec(doi="10.1101/ABC", src="PPR", title="p"))
    ck("item: a preprint is keyed by lower-case DOI and marked",
       pp["key"] == "doi:10.1101/abc" and pp["preprint"] and "preprint" in pp["extra"])
    long = epmc_item(_rec(pmid="7", abstract="word " * 2000))
    ck("item: long abstracts are trimmed and say so",
       len(long["abstract"]) <= ABSTRACT_CHARS + 4 and long["abstract"].endswith("..."))

    with tempfile.TemporaryDirectory() as td:
        page = {"hitCount": 2, "nextCursorMark": "*", "resultList": {"result": [
            _rec(pmid="1", title="Alopecia areata T cells"), _rec(pmid="2", title="Dermal papilla")]}}
        req = _fake_request({"aa": [page], "follicle-regeneration": [
            {"hitCount": 1, "resultList": {"result": [_rec(pmid="2", title="Dermal papilla")]}}]},
            grants=[{"core_project_num": "R01AR1", "project_num": "5R01AR1-02",
                     "project_title": "AA T-cell study", "organization": {"org_name": "Example U"},
                     "contact_pi_name": "PI, A", "fiscal_year": 2027},
                    {"core_project_num": "R01AR1", "project_num": "5R01AR1-01",
                     "project_title": "older year", "fiscal_year": 2026}])
        s = run(today=t, request=req, out_dir=td, clock=lambda: 0)
        keys = {json.loads(Path(td, "wide-2026-11-24.json").read_text())[i]["key"]
                for i in range(s["day_total"])}
        ck("run: items from every lane plus the grant, deduped (3 new)",
           s["new"] == 3 and keys == {"pmid:1", "pmid:2", "nih:R01AR1"})
        two = [x for x in json.loads(Path(td, "wide-2026-11-24.json").read_text())
               if x["key"] == "pmid:2"][0]
        ck("run: an item found by two lanes carries both",
           set(two["lanes"]) == {"aa", "follicle-regeneration"})
        grant = [x for x in json.loads(Path(td, "wide-2026-11-24.json").read_text())
                 if x["key"] == "nih:R01AR1"][0]
        ck("run: a grant keeps its newest award year only", "FY2027" in grant["extra"])
        ck("run: a clean day renders no INCOMPLETE banner",
           "INCOMPLETE" not in Path(td, "wide-2026-11-24.md").read_text())
        s2 = run(today=t, request=req, out_dir=td, clock=lambda: 0)
        ck("run: a re-run finds nothing new and does NOT clobber the day's file",
           s2["new"] == 0 and s2["day_total"] == 3)
        s3 = run(today=t, request=_fake_request({}, fail=("physics", "grants")), out_dir=td,
                 clock=lambda: 0)
        md = Path(td, "wide-2026-11-24.md").read_text()
        ck("run: a failed lane and a failed grant search are banner-listed",
           "INCOMPLETE — 2" in md and "lane physics" in md and "RePORTER" in md)
        ck("run: the errors are kept for the weekly synthesis",
           len(json.loads(Path(td, "errors.json").read_text())["2026-11-24"]) == 2)
        ck("health: a partial day is still healthy", healthy(s3))
        s4 = run(dry_run=True, today=t, request=_fake_request({}), out_dir=td, clock=lambda: 0)
        ck("run: every lane answering ZERO is a source failure, not a quiet day",
           any("ZERO" in e for e in s4["errors"]))
        allfail = run(dry_run=True, today=t, out_dir=td, clock=lambda: 0,
                      request=_fake_request({}, fail=tuple(l[0] for l in LANES) + ("grants",)))
        ck("health: every search failing is unhealthy", not healthy(allfail))
        ck("health: a crash is unhealthy", not healthy({"crashed": True}))
        ticks = iter([0, 0] + [DEADLINE_S + 1] * 40)
        late = run(dry_run=True, today=t, request=_fake_request({}), out_dir=td,
                   clock=lambda: next(ticks))
        ck("run: lanes past the deadline are reported, not dropped silently",
           sum("deadline" in e for e in late["errors"]) == len(LANES) - 1)
        capped = {"hitCount": LANE_CAP + 50, "nextCursorMark": "next",
                  "resultList": {"result": [_rec(pmid=str(i)) for i in range(PAGE_SIZE)]}}
        s5 = run(dry_run=True, today=t, request=_fake_request({"aa": [capped]}), out_dir=td,
                 clock=lambda: 0)
        ck("run: a lane over the cap is an error, never a silent truncation",
           any("over the %d cap" % LANE_CAP in e for e in s5["errors"]))
        s6 = run(dry_run=True, today=date(2026, 11, 25), request=req, out_dir=td, clock=lambda: 0)
        ck("run: --dry-run writes nothing",
           not Path(td, "wide-2026-11-25.json").exists() and s6["new"] == 0)

    print("alopecia_wide selftest: %d passed, %d failed" % (ok, fail))
    return fail == 0


def main(argv):
    args = argv[1:]
    if "--selftest" in args or "selftest" in args:
        return 0 if selftest() else 1
    if "report" in args:
        return report()
    days = LOOKBACK_DAYS
    if "--days" in args:
        days = int(args[args.index("--days") + 1])
    stats = run(dry_run="--dry-run" in args, days=days)
    log("found %d, new %d%s" % (stats["found"], stats["new"],
        (", %d search error(s)" % len(stats["errors"])) if stats["errors"] else ""))
    if "--dry-run" in args:
        print(stats["body"])
    return 0 if healthy(stats) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
