#!/usr/bin/env python3
"""ALOPECIA P1 — daily collector.

Pulls the day's alopecia-areata research, trials and news from four sources
that were each proven callable before this module was written (see
alopecia/AA-FOUNDATION.md §7), dedupes against everything ever seen, tags each
item with the project's relevance priorities, and writes one daily file.

It does NOT summarise or judge. A local-model triage was considered and left
out on purpose: P1's job is collection INTEGRITY (nothing missed, nothing
repeated, nothing identifying), and every judgment call belongs to the P2
council, where five models disagree in the open. A deterministic tagger can be
tested; a model's opinion about today's papers cannot.

PRIVACY (spec hard rule): every outbound query is built only from
APPROVED_QUERY_VOCAB -- condition-level vocabulary. No person, initials,
location, age or identifier is used in any request or written to any log. The
selftest enforces this exhaustively via outbound_queries(); see
check_queries_condition_level().

    python3 alopecia_collect.py                 # collect, write, record
    python3 alopecia_collect.py --dry-run       # collect, write NOTHING
    python3 alopecia_collect.py --days 7        # widen the lookback window
    python3 alopecia_collect.py report          # what the last run found
    python3 alopecia_collect.py --selftest      (also: selftest)
"""

import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
OUT_DIR = PROJECT_DIR / "alopecia/daily"
SEEN_PATH = PROJECT_DIR / "alopecia/seen.json"
TIMEOUT = 45

CONDITION = "alopecia areata"

# Only these words may appear in an outbound query. Anything else -- a name, a
# place, an age -- fails check_queries_condition_level() and the selftest. The
# list is deliberately vocabulary, not a blocklist: a blocklist of personal
# terms would have to CONTAIN those terms, which is the leak it prevents.
APPROVED_QUERY_VOCAB = {
    "alopecia", "areata", "totalis", "universalis", "hair", "loss", "follicle",
    "jak", "inhibitor", "baricitinib", "ritlecitinib", "deuruxolitinib",
    "immune", "privilege", "autoimmune", "microbiome", "diet", "trial",
    "recruiting", "and", "or", "not",
}

# Spec relevance scoring, highest first. First pattern to match wins.
PRIORITIES = [
    (1, "subgroup: long-duration / adult / universalis",
     r"universalis|totalis|long[- ]?(standing|duration)|severe alopecia|refractory|"
     r"inadequate response|treatment[- ]resistant"),
    (2, "regrowth & remission",
     r"regrow|remission|relapse|spontaneous recover|durability|withdrawal|discontinu"),
    # WHAT CAUSED THIS -- Buddy's standing question (S82), and the band most
    # easily under-built. The first draft of this pattern matched 2 of 11
    # realistic causation-discovery titles: "molecular mimicry between a viral
    # antigen and a follicle autoantigen", "EBV infection precedes alopecia
    # areata", "trichohyalin identified as the dominant autoantigen" and six
    # others all fell through to P7 "general AA news" -- the bottom of the
    # file. A discovery of the CAUSE would have been reported as miscellaneous.
    # Those eleven titles are now a regression test (see selftest): this band
    # can narrow again, but not silently.
    (3, "etiology / cause / trigger",
     r"pathogenes|etiolog|a?etiolog|immune privilege|trigger|onset|cd8|nkg2d|"
     r"interferon|il-15|genetic|genom|gwas|risk (factor|locus|loci)|"
     r"cause|causal|causation|caus(ing|es)|"
     r"autoantigen|antigen|epitope|mimicry|autoimmun|tolerance|"
     r"viral|virus|infection|epstein|ebv|covid|vaccin|microb(e|ial) trigger|"
     r"hla|haplotype|heritab|twin|famil(y|ial) (history|aggregation)|"
     r"susceptib|predispos|"
     r"tcr|t[- ]cell receptor|clonal|single[- ]cell|repertoire|"
     r"epidemiolog|incidence|prevalence|cohort stud|case[- ]control|"
     r"preced|antecedent|prodrom"),
    (4, "diet / microbiome / environment",
     r"diet|nutrition|microbiome|microbiota|gut|vitamin|zinc|iron|biotin|"
     r"probiotic|supplement|environmental|stress"),
    (5, "new mechanisms / pipeline",
     r"rezpegaldesleukin|bempikibart|ox40|il-7|dupilumab|treg|phase (1|2|3|i|ii|iii)|"
     r"novel|first[- ]in[- ]class|pipeline|topical jak"),
    (6, "trials",
     r"recruiting|enroll|clinical trial|nct\d+"),
]
DEFAULT_PRIORITY = (7, "general AA news")


def log(msg):
    print("[%s] alopecia_collect: %s" % (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))


# ── outbound queries — the only strings that leave this machine ──────────────

def outbound_queries():
    """EVERY query this module can issue. The privacy check is only as
    exhaustive as this list, so a new source must be added here too."""
    return {
        "pubmed": CONDITION,
        "trials": CONDITION,
        "medrxiv": CONDITION,          # client-side title filter, not a server query
        "naaf": "",                    # plain feed fetch, no query at all
    }


def check_queries_condition_level():
    """True when every outbound query uses only approved condition vocabulary."""
    bad = []
    for name, q in outbound_queries().items():
        for tok in re.findall(r"[A-Za-z][A-Za-z\-']*", q or ""):
            if tok.lower() not in APPROVED_QUERY_VOCAB:
                bad.append((name, tok))
    return (not bad), bad


# ── fetching ─────────────────────────────────────────────────────────────────

def _get(url, as_json=True):
    import requests
    r = requests.get(url, timeout=TIMEOUT,
                     headers={"User-Agent": "cirrus-alopecia-monitor/1.0"})
    r.raise_for_status()
    return r.json() if as_json else r.text


def fetch_pubmed(days=1):
    """New PubMed records for the condition within the lookback window."""
    q = outbound_queries()["pubmed"].replace(" ", "+")
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    ids = _get("%s/esearch.fcgi?db=pubmed&term=%s&retmode=json&retmax=60"
               "&sort=date&datetype=edat&reldate=%d" % (base, q, max(1, days)))
    idlist = ids.get("esearchresult", {}).get("idlist", [])
    if not idlist:
        return []
    summ = _get("%s/esummary.fcgi?db=pubmed&id=%s&retmode=json"
                % (base, ",".join(idlist)))
    res = summ.get("result", {})
    out = []
    for uid in res.get("uids", []):
        r = res[uid]
        out.append({
            "key": "pmid:%s" % uid,
            "title": (r.get("title") or "").strip().rstrip("."),
            "date": r.get("pubdate", ""),
            "source": "PubMed",
            "url": "https://pubmed.ncbi.nlm.nih.gov/%s/" % uid,
            "extra": r.get("fulljournalname", ""),
        })
    return out


def fetch_trials():
    """Recruiting interventional studies for the condition."""
    url = ("https://clinicaltrials.gov/api/v2/studies?query.cond=%s"
           "&filter.overallStatus=RECRUITING&pageSize=100"
           "&fields=NCTId,BriefTitle,Phase,LastUpdatePostDate,OverallStatus"
           % outbound_queries()["trials"].replace(" ", "+"))
    data = _get(url)
    out = []
    for s in data.get("studies", []):
        ps = s.get("protocolSection", {})
        ident = ps.get("identificationModule", {})
        nct = ident.get("nctId", "")
        if not nct:
            continue
        phases = ps.get("designModule", {}).get("phases", []) or []
        out.append({
            "key": "nct:%s" % nct,
            "title": ident.get("briefTitle", ""),
            "date": ps.get("statusModule", {}).get("lastUpdatePostDateStruct", {}).get("date", ""),
            "source": "ClinicalTrials.gov",
            "url": "https://clinicaltrials.gov/study/%s" % nct,
            "extra": "/".join(phases),
        })
    return out


def fetch_naaf(limit=20):
    """National Alopecia Areata Foundation news feed (no query issued)."""
    xml = _get("https://www.naaf.org/feed/", as_json=False)
    root = ET.fromstring(xml)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not link:
            continue
        out.append({
            "key": "url:%s" % hashlib.sha1(link.encode()).hexdigest()[:16],
            "title": title,
            "date": (item.findtext("pubDate") or "")[:16],
            "source": "NAAF",
            "url": link,
            "extra": "",
        })
        if len(out) >= limit:
            break
    return out


MEDRXIV_MAX_PAGES = 60          # 1800 preprints ~= 30 days of the firehose


def fetch_medrxiv(days=1):
    """medRxiv preprints in the window, filtered client-side on the title.

    The details endpoint has NO keyword search: it returns every preprint in a
    date range and the filter has to happen here.

    S82, measured before trusting it: an 88-day range holds **5,088** preprints
    (~58/day), and the first draft of this function capped scanning at 6 pages
    = 180 records. For any window past ~3 days it therefore examined a slice of
    the corpus and returned "0 new preprints" -- indistinguishable from a
    genuinely quiet day. Alopecia preprints are RARE (0 in a 600-record sample),
    so a truncated scan is the one thing that could never be noticed by reading
    the output.

    Now: it reads the API's own `total`, scans every page up to a generous cap,
    and if the cap ever truncates it says so loudly. A bounded scan is fine; a
    SILENT bounded scan is not.
    """
    end = datetime.now().date()
    start = end - timedelta(days=max(1, days))
    out, cursor, total = [], 0, None
    for _ in range(MEDRXIV_MAX_PAGES):
        data = _get("https://api.biorxiv.org/details/medrxiv/%s/%s/%d"
                    % (start.isoformat(), end.isoformat(), cursor))
        if total is None:
            try:
                total = int(data.get("messages", [{}])[0].get("total", 0))
            except Exception:
                total = 0
        coll = data.get("collection", []) or []
        if not coll:
            break
        for p in coll:
            title = (p.get("title") or "")
            if "alopecia" not in title.lower():
                continue
            doi = p.get("doi", "")
            out.append({
                "key": "doi:%s" % doi,
                "title": title.strip(),
                "date": p.get("date", ""),
                "source": "medRxiv",
                "url": "https://doi.org/%s" % doi,
                "extra": "preprint",
            })
        cursor += len(coll)
        if total and cursor >= total:
            break
        if len(coll) < 30:
            break
    if total and cursor < total:
        log("medRxiv TRUNCATED: scanned %d of %d preprints in %s..%s "
            "(page cap %d) -- coverage is PARTIAL, not empty"
            % (cursor, total, start, end, MEDRXIV_MAX_PAGES))
    return out


COLLECTORS = {
    "PubMed": fetch_pubmed,
    "ClinicalTrials.gov": fetch_trials,
    "NAAF": fetch_naaf,
    "medRxiv": fetch_medrxiv,
}


# ── classify / dedupe ────────────────────────────────────────────────────────

def classify(item):
    """(rank, label) for an item, from its title and any extra text."""
    text = "%s %s" % (item.get("title", ""), item.get("extra", ""))
    low = text.lower()
    for rank, label, pattern in PRIORITIES:
        if re.search(pattern, low):
            return rank, label
    return DEFAULT_PRIORITY


def load_seen(path=SEEN_PATH):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}


def save_seen(seen, path=SEEN_PATH):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n")


def new_items(items, seen):
    """Items never seen before, de-duplicated within the batch too."""
    out, batch = [], set()
    for it in items:
        k = it.get("key")
        if not k or k in seen or k in batch:
            continue
        batch.add(k)
        out.append(it)
    return out


def render(items, day):
    """The daily file. Grouped by priority so the eye lands on rank 1 first."""
    lines = ["# Alopecia areata — daily collection %s" % day, ""]
    if not items:
        lines += ["_Nothing new today._", "",
                  "A quiet day is a result, not a failure: the sources were "
                  "queried and every item had been seen before.", ""]
        return "\n".join(lines)
    lines.append("**%d new item(s).**" % len(items))
    lines.append("")
    by_rank = {}
    for it in items:
        by_rank.setdefault((it["rank"], it["label"]), []).append(it)
    for (rank, label) in sorted(by_rank):
        lines.append("## P%d — %s" % (rank, label))
        lines.append("")
        for it in by_rank[(rank, label)]:
            extra = " · %s" % it["extra"] if it.get("extra") else ""
            lines.append("- **%s**  \n  %s · %s%s  \n  %s"
                         % (it["title"] or "(untitled)", it["source"],
                            it.get("date", "") or "undated", extra, it["url"]))
        lines.append("")
    return "\n".join(lines)


def run(dry_run=False, days=1, collectors=None, seen_path=SEEN_PATH,
        out_dir=OUT_DIR):
    ok, bad = check_queries_condition_level()
    if not ok:
        # fail CLOSED: never issue a query that failed the privacy check
        raise RuntimeError("outbound query failed the condition-level check: %s" % bad)

    collectors = collectors or COLLECTORS
    day = datetime.now().strftime("%Y-%m-%d")
    seen = load_seen(seen_path)
    found, errors = [], []

    for name, fn in collectors.items():
        try:
            got = fn(days) if fn in (fetch_pubmed, fetch_medrxiv) else fn()
            found.extend(got)
            log("%s: %d item(s)" % (name, len(got)))
        except Exception as e:
            # One dead source must not lose the other three. It IS reported.
            errors.append("%s: %s" % (name, e))
            log("%s FAILED: %s" % (name, e))

    fresh = new_items(found, seen)
    for it in fresh:
        it["rank"], it["label"] = classify(it)
    fresh.sort(key=lambda i: (i["rank"], i["source"]))

    # The day's file ACCUMULATES across runs rather than being replaced.
    # S82, caught by reading the output instead of the exit code: run() wrote
    # alopecia-<day>.md unconditionally, so a second run on the same day -- a
    # retry, a manual invocation, or systemd Persistent=true firing a missed
    # timer (T39) -- found 0 new items and overwrote 103 real ones with
    # "Nothing new today." The run that destroyed the day's work reported
    # success, because by its own definition it had done nothing wrong.
    merged = fresh
    day_json = Path(out_dir) / ("alopecia-%s.json" % day)
    if not dry_run:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        prior = []
        try:
            prior = json.loads(day_json.read_text())
        except Exception:
            prior = []
        by_key = {}
        for it in prior + fresh:
            by_key[it.get("key")] = it
        merged = sorted(by_key.values(),
                        key=lambda i: (i.get("rank", 9), i.get("source", "")))
        day_json.write_text(json.dumps(merged, indent=1) + "\n")
        (Path(out_dir) / ("alopecia-%s.md" % day)).write_text(
            render(merged, day) + "\n")
        for it in fresh:
            seen[it["key"]] = day
        save_seen(seen, seen_path)

    return {"found": len(found), "new": len(fresh), "day_total": len(merged),
            "seen_total": len(seen), "errors": errors, "day": day,
            "body": render(merged, day)}


def report(out_dir=OUT_DIR):
    files = sorted(Path(out_dir).glob("alopecia-*.md")) if Path(out_dir).exists() else []
    if not files:
        print("no daily files yet")
        return 1
    print(files[-1].read_text())
    return 0


# ── selftest ─────────────────────────────────────────────────────────────────

def selftest():
    """Offline: no network, no live seen-ledger, no live output dir (T32)."""
    import tempfile
    ok = fail = 0

    def ck(name, cond):
        nonlocal ok, fail
        ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
        print("  [%s] %s" % ("OK " if cond else "FAIL", name))

    # privacy — the rule the whole project rests on
    good, bad = check_queries_condition_level()
    ck("privacy: every outbound query is condition-level vocabulary only", good)
    ck("privacy: the check is exhaustive over outbound_queries()",
       set(outbound_queries()) >= {"pubmed", "trials", "medrxiv", "naaf"})
    # The planted place-name is a DELIBERATELY IRRELEVANT one. S82 first used
    # RCW's actual town and county here -- putting the exact strings this
    # project promises to keep out of the repo into the repo, inside the test
    # that proves they stay out. Any non-vocabulary word exercises the check
    # identically. Do not "improve" this by making it realistic.
    _orig = globals()["outbound_queries"]
    globals()["outbound_queries"] = lambda: {"x": "alopecia areata Reykjavik"}
    try:
        planted_ok, planted_bad = check_queries_condition_level()
        ck("privacy: the check FIRES on a planted place-name",
           (not planted_ok) and planted_bad)
    finally:
        globals()["outbound_queries"] = _orig

    # classification
    ck("classify: universalis is priority 1",
       classify({"title": "Ritlecitinib in alopecia universalis"})[0] == 1)
    ck("classify: regrowth is priority 2",
       classify({"title": "Spontaneous regrowth after withdrawal"})[0] == 2)
    ck("classify: etiology is priority 3",
       classify({"title": "CD8 T cells and immune privilege collapse"})[0] == 3)

    # THE CAUSATION REGRESSION TEST (S82, Buddy's standing question). Every one
    # of these landed in P7 "general AA news" before the etiology band was
    # widened. None may fall below P3 again.
    CAUSE_TITLES = [
        "Molecular mimicry between a viral antigen and a hair follicle autoantigen",
        "Epstein-Barr virus infection precedes alopecia areata in a national cohort",
        "Trichohyalin identified as the dominant autoantigen in alopecia areata",
        "HLA-DRB1 haplotypes and susceptibility to childhood alopecia areata",
        "Single-cell TCR sequencing reveals clonal expansion in lesional scalp",
        "Twin concordance and heritability of alopecia areata",
        "Incidence of alopecia areata after COVID-19 vaccination: a cohort study",
        "What causes alopecia areata? A mechanistic synthesis",
        "Childhood infection and later autoimmunity: a population-based analysis",
        "Environmental exposures preceding disease onset",
        "Genome-wide association study identifies new susceptibility loci",
    ]
    missed = [t for t in CAUSE_TITLES if classify({"title": t})[0] > 3]
    ck("classify: EVERY causation-discovery title ranks P3 or better "
       "(%d/%d)" % (len(CAUSE_TITLES) - len(missed), len(CAUSE_TITLES)),
       not missed)
    if missed:
        for t in missed:
            print("        MISSED: %s" % t[:70])
    ck("classify: diet is priority 4",
       classify({"title": "Gut microbiome and vitamin D in AA"})[0] == 4)
    ck("classify: pipeline is priority 5",
       classify({"title": "Rezpegaldesleukin phase 3 programme"})[0] == 5)
    ck("classify: anything else still lands somewhere",
       classify({"title": "A survey of barbers"})[0] == DEFAULT_PRIORITY[0])
    ck("classify: priority 1 beats a lower rank in the same title",
       classify({"title": "Diet in alopecia universalis"})[0] == 1)

    # dedupe
    a = [{"key": "pmid:1"}, {"key": "pmid:2"}]
    ck("dedupe: everything is new against an empty ledger",
       len(new_items(a, {})) == 2)
    ck("dedupe: a seen key is dropped",
       len(new_items(a, {"pmid:1": "2026-01-01"})) == 1)
    ck("dedupe: duplicates WITHIN one batch collapse",
       len(new_items([{"key": "pmid:9"}, {"key": "pmid:9"}], {})) == 1)
    ck("dedupe: an item with no key is never emitted",
       new_items([{"title": "no key"}], {}) == [])

    with tempfile.TemporaryDirectory() as td:
        sp = Path(td) / "seen.json"
        od = Path(td) / "daily"
        fake = [{"key": "pmid:100", "title": "Alopecia universalis case",
                 "date": "2026", "source": "PubMed", "url": "u", "extra": ""},
                {"key": "nct:NCT1", "title": "Recruiting study",
                 "date": "2026", "source": "ClinicalTrials.gov", "url": "u2", "extra": ""}]
        cols = {"fake": lambda: list(fake)}

        r1 = run(collectors=cols, seen_path=sp, out_dir=od)
        ck("run: day 1 finds both items", r1["new"] == 2)
        ck("run: day 1 wrote a daily file", any(od.glob("alopecia-*.md")))

        r2 = run(collectors=cols, seen_path=sp, out_dir=od)
        ck("run: day 2 over the SAME source finds NOTHING new (the P1 criterion)",
           r2["new"] == 0)
        ck("run: the ledger persisted across runs", r2["seen_total"] == 2)
        # the clobber bug: a 0-new re-run must not erase the day's real findings
        ck("run: a re-run finding nothing KEEPS the day's earlier items",
           r2["day_total"] == 2 and "Nothing new today" not in r2["body"])
        md = (od / ("alopecia-%s.md" % r2["day"])).read_text()
        ck("run: and the file on disk still holds them",
           "Alopecia universalis case" in md)

        extra = [{"key": "pmid:200", "title": "Gut microbiome study",
                  "date": "2026", "source": "PubMed", "url": "u3", "extra": ""}]
        r2b = run(collectors={"f": lambda: list(fake) + extra},
                  seen_path=sp, out_dir=od)
        ck("run: a later run ADDS to the day rather than replacing it",
           r2b["new"] == 1 and r2b["day_total"] == 3)

        # a genuinely quiet FIRST run of a day still says so
        od2 = Path(td) / "daily2"
        rq = run(collectors={"f": lambda: []}, seen_path=Path(td) / "s2.json",
                 out_dir=od2)
        ck("run: a truly empty day still renders 'nothing new'",
           "Nothing new today" in rq["body"])

        # assert the INVARIANT (nothing changed), not a count that any test
        # added above this line would silently invalidate
        before_ledger = json.loads(sp.read_text())
        before_md = (od / ("alopecia-%s.md" % r2["day"])).read_text()
        r3 = run(collectors={"f": lambda: list(fake) + [
            {"key": "pmid:999", "title": "brand new", "date": "d",
             "source": "PubMed", "url": "u", "extra": ""}]},
            seen_path=sp, out_dir=od, dry_run=True)
        ck("run: --dry-run finds new items but writes NOTHING",
           r3["new"] == 1
           and json.loads(sp.read_text()) == before_ledger
           and (od / ("alopecia-%s.md" % r2["day"])).read_text() == before_md)

        def boom():
            raise RuntimeError("source down")
        r4 = run(collectors={"fake": lambda: list(fake), "dead": boom},
                 seen_path=sp, out_dir=od, dry_run=True)
        ck("run: one dead source does not lose the others", r4["found"] == 2)
        ck("run: and the failure is REPORTED, not swallowed",
           any("dead" in e for e in r4["errors"]))

        # fail closed on a privacy violation
        globals()["outbound_queries"] = lambda: {"x": "alopecia Reykjavik"}
        try:
            threw = False
            try:
                run(collectors=cols, seen_path=sp, out_dir=od, dry_run=True)
            except RuntimeError:
                threw = True
            ck("run: FAILS CLOSED if a query would leak a place-name", threw)
        finally:
            globals()["outbound_queries"] = _orig

        # medRxiv truncation must ANNOUNCE itself (S82). Fake the API: claim a
        # huge total, hand back full pages forever, and check the warning fires.
        import io, contextlib
        _real_get = globals()["_get"]
        globals()["_get"] = lambda url, as_json=True: {
            "messages": [{"total": "99999"}],
            "collection": [{"title": "unrelated preprint", "doi": "d/%d" % i,
                            "date": "2026-08-27"} for i in range(30)]}
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                fetch_medrxiv(days=30)
            ck("medRxiv: a truncated scan says so, loudly",
               "TRUNCATED" in buf.getvalue() and "PARTIAL" in buf.getvalue())

            globals()["_get"] = lambda url, as_json=True: {
                "messages": [{"total": "2"}],
                "collection": [{"title": "Alopecia areata preprint",
                                "doi": "10.1/x", "date": "2026-08-27"},
                               {"title": "unrelated", "doi": "10.1/y",
                                "date": "2026-08-27"}]}
            got = buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                got = fetch_medrxiv(days=1)
            ck("medRxiv: keeps only alopecia titles", len(got) == 1)
            ck("medRxiv: a COMPLETE scan stays quiet",
               "TRUNCATED" not in buf2.getvalue())
        finally:
            globals()["_get"] = _real_get

        body = render([{"key": "k", "title": "T", "date": "d", "source": "s",
                        "url": "u", "extra": "", "rank": 1, "label": "L"}], "2026-01-01")
        ck("render: groups under its priority heading", "## P1 — L" in body)

    print("\n%d passed, %d failed" % (ok, fail))
    return fail == 0


def main():
    args = sys.argv[1:]
    if "--selftest" in args or "selftest" in args:
        return 0 if selftest() else 1
    if "report" in args:
        return report()
    dry = "--dry-run" in args
    days = 1
    if "--days" in args:
        try:
            days = int(args[args.index("--days") + 1])
        except Exception:
            pass

    stats = run(dry_run=dry, days=days)
    log("found %d, new %d, ledger %d%s"
        % (stats["found"], stats["new"], stats["seen_total"],
           (", %d source error(s)" % len(stats["errors"])) if stats["errors"] else ""))

    if not dry:
        try:
            import job_status
            note = "%d found, %d new" % (stats["found"], stats["new"])
            if stats["errors"]:
                note += ", %d source error(s)" % len(stats["errors"])
            # A run where EVERY source failed is not a healthy run.
            healthy = len(stats["errors"]) < len(COLLECTORS)
            job_status.record("alopeciacollect", healthy, note)
        except Exception as e:
            print("job_status.record failed: %s" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
