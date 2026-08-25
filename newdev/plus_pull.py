#!/usr/bin/env python3
"""plus_pull.py — pull Delaware PLUS major-residential projects for Bill's
new-construction lead pipeline (S47, 2026-07-28).

Queries the OSPC "DE Planning Development – PLUS Project Areas" feature layer
(FirstMap, updated monthly), keeps residential projects >= MIN_UNITS in the
target counties, and writes out/plus_<counties>.json for the xlsx builder.

Runs on the MacBook via the runner (stdlib only — urllib). NOT from the Cowork
sandbox (which shouldn't fetch GIS directly). Mirrors the kent_hoa.py pattern.
"""
import json, urllib.request, urllib.parse, time
from pathlib import Path
from datetime import datetime, timezone

SVC = ("https://enterprise.firstmap.delaware.gov/arcgis/rest/services/"
       "PlanningCadastre/DE_Planning_Development/FeatureServer")
MIN_UNITS = 50
COUNTIES = {"Kent County", "Sussex County"}   # Kent + Sussex per Buddy (S47)
OUT = Path(__file__).resolve().parent / "out"

# 2026-08-25 (S78) — THE SOURCE SPELLS COUNTY NAMES TWO WAYS. Layer 4 carries
# both "Sussex County" (373 rows) and "Sussex_County" (35), both "Kent County"
# (131) and "Kent_County" (6). An exact-string `in COUNTIES` test silently
# dropped every underscore row -- 41 in total, and 36 of them dated 2023 or
# later, i.e. the NEWEST records in the layer. Bill's feed has been quietly
# missing recent PLUS project areas for as long as the source has used that
# spelling. Compare normalised, so a punctuation change at the source cannot
# decide what a client sees.
COUNTIES_NORM = {c.lower().replace("_", " ").strip() for c in COUNTIES}

# A source-side ID reset must never reach a client as "new leads this week".
# On 2026-08-24 layer 4 republished with fresh GLOBALIDs; every one of its 504
# historical projects failed the "have I seen this id" test at once, and Bill
# was emailed 504 "new" leads dating back to 2004. The diff was working exactly
# as written -- it has no notion of a source that renumbers itself.
#
# So: an implausible number of new leads is treated as EVIDENCE OF A RESET, not
# as news. Nothing is discarded (the rows go to plus_reset_backlog.json for a
# human), and nothing is emailed. Same shape as every other guard here: the
# quiet wrong answer is the one worth catching.
MAX_PLAUSIBLE_NEW = 60

# 2026-08-25 (S78) — GLOBALID IS NOT AN IDENTITY ON EVERY LAYER. The guard above
# fired on its first real run and caught all 545 layer-4 rows, including the 504
# that had been seen the week before: the PLUS Project Areas layer is reissued
# with fresh GLOBALIDs on every republish. So layer 4 would have produced a
# full-layer false positive EVERY time the source updated, not just once. Bill's
# 504-lead email was the steady state, not an accident.
#
# PLUS_ID is the project's own identifier and survives a republish, so it is the
# key for that layer. Layers 2 and 3 did not churn (the 2026-08-24 run flagged
# exactly the 504 layer-4 rows and nothing else), so GLOBALID stands there.
# The guard remains as a backstop for the next layer that does this.
IDENTITY_FIELD = {
    "PLUS (built)": "PLUS_ID",
    "Dev Application": "GLOBALID",
    "Building Permit": "GLOBALID",
}


def identity(kind: str, attrs) -> str:
    """The stable id for one row, or "" if this row cannot be tracked.

    Falls back to GLOBALID rather than to nothing: an untrackable row would be
    reported as new on every single run, which is the loudest possible way to
    be wrong at a client.
    """
    key = IDENTITY_FIELD.get(kind, "GLOBALID")
    return str(attrs.get(key) or attrs.get("GLOBALID") or "")


def county_ok(attrs) -> bool:
    """Is this row in one of the target counties, however the source spells it?"""
    return (str(attrs.get("COUNTY") or "").lower().replace("_", " ").strip()
            in COUNTIES_NORM)


def fetch_layer(layer, where, order):
    rows, offset = [], 0
    while True:
        params = {"where": where, "outFields": "*", "returnGeometry": "false",
                  "orderByFields": order, "resultOffset": offset,
                  "resultRecordCount": 1000, "f": "json"}
        url = f"{SVC}/{layer}/query?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "cowork-plus-pull/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
        feats = data.get("features", [])
        rows += [f["attributes"] for f in feats]
        if data.get("exceededTransferLimit") and feats:
            offset += len(feats); time.sleep(0.3); continue
        break
    return rows


def ms_to_date(ms):
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return ""


def main():
    from collections import Counter
    OUT.mkdir(parents=True, exist_ok=True)

    # Layer 2 — Development Applications (current pipeline; residential >= MIN_UNITS)
    da = [a for a in fetch_layer(2, f"R_NR='R' AND R_UNITS>={MIN_UNITS}", "P_YEAR DESC")
          if county_ok(a)]
    da.sort(key=lambda a: (a.get("P_YEAR") or ""), reverse=True)

    # Layer 3 — Building Permits (residential >= MIN_UNITS = large multifamily
    # buildings ACTIVELY under construction now; single-lot permits drop out)
    bp = [a for a in fetch_layer(3, f"R_NR='R' AND R_UNITS>={MIN_UNITS}", "P_YEAR DESC")
          if county_ok(a)]
    bp.sort(key=lambda a: (a.get("P_YEAR") or ""), reverse=True)

    # Layer 4 — PLUS Project Areas (historical major residential; built communities
    # now at/near developer→homeowner turnover = ready PM targets)
    plus = [a for a in fetch_layer(4, f"RESIDENTIAL_UNITS>={MIN_UNITS}", "PLUS_ID DESC")
            if county_ok(a)]
    for a in plus:
        a["PLUS_YEAR"] = (a.get("PLUS_ID") or "")[:4]
    plus.sort(key=lambda a: (a.get("PLUS_ID") or ""), reverse=True)

    (OUT / "plus_leads.json").write_text(json.dumps(
        {"dev_applications": da, "building_permits": bp, "plus_projects": plus}, indent=1))

    # ── Weekly diff: flag leads not seen on a previous run (for the Bill monitor).
    # First run baselines silently (won't flag all history as "new").
    seen_file = OUT / "plus_seen.json"
    tagged = ([("Dev Application", a) for a in da]
              + [("Building Permit", a) for a in bp]
              + [("PLUS (built)", a) for a in plus])
    cur_ids = {identity(k, a) for k, a in tagged if identity(k, a)}
    prev = set(json.loads(seen_file.read_text())) if seen_file.exists() else None
    if prev is None:
        new = []
    else:
        new = [dict(kind=k, **a) for k, a in tagged
               if identity(k, a) and identity(k, a) not in prev]

    # See MAX_PLAUSIBLE_NEW. A genuine week produces a handful of leads; 504 is
    # a source that renumbered itself. Absorb the new ids into the baseline so
    # the reset happens exactly once, park the rows where a human can read them,
    # and report ZERO new -- which is what stops the weekly job emailing.
    reset = prev is not None and len(new) > MAX_PLAUSIBLE_NEW
    if reset:
        (OUT / "plus_reset_backlog.json").write_text(json.dumps(new, indent=1))
        print(f"*** SUSPECTED SOURCE RESET: {len(new)} rows failed the seen-id "
              f"test in one run (ceiling {MAX_PLAUSIBLE_NEW}). These are almost "
              f"certainly renumbered records, not new leads.")
        print(f"*** Nothing emailed. The rows are parked in "
              f"out/plus_reset_backlog.json and the baseline has absorbed them.")
        print(f"*** If they ARE genuinely new, send them deliberately — do not "
              f"just re-run and hope.")
        new = []

    seen_file.write_text(json.dumps(sorted(str(i) for i in cur_ids if i)))
    (OUT / "plus_new.json").write_text(json.dumps(new, indent=1))
    print(f"NEW since last run: {len(new)}"
          + (" (baseline established)" if prev is None else "")
          + (" (SOURCE RESET SUPPRESSED — see above)" if reset else ""))

    CLABEL = " + ".join(sorted(c.replace(" County", "") for c in COUNTIES))
    print(f"Building Permits (residential >= {MIN_UNITS}u, {CLABEL}): {len(bp)}")
    print("  by year:", dict(sorted(Counter((a.get('P_YEAR') or '?') for a in bp).items(), reverse=True)))

    print(f"Development Applications (residential >= {MIN_UNITS}u, {CLABEL}): {len(da)}")
    print("  by county:", dict(Counter(a["COUNTY"] for a in da)))
    print("  by year  :", dict(sorted(Counter((a.get('P_YEAR') or '?') for a in da).items(), reverse=True)))
    for a in da[:12]:
        print(f"    {a.get('P_YEAR','?'):5} | {a.get('COUNTY',''):13} | {str(a.get('R_UNITS','?')):>6}u | "
              f"{a.get('RECTYPE',''):18} | {(a.get('NOTES') or '').replace(chr(10),' / ')[:45]}")
    print(f"\nPLUS Project Areas (historical >= {MIN_UNITS}u, {CLABEL}): {len(plus)}")
    print("  by year:", dict(sorted(Counter((a.get('PLUS_YEAR') or '?') for a in plus).items(), reverse=True)))


def selftest() -> int:
    """Offline: no network, no files. The two rules that decide what a CLIENT
    is told, both of which have already failed in production once.
    """
    bad = 0

    def check(label, ok):
        nonlocal bad
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            bad += 1

    check("a plain county name matches", county_ok({"COUNTY": "Kent County"}))
    check("the UNDERSCORE spelling matches too — 41 rows were dropped by an "
          "exact-string test", county_ok({"COUNTY": "Sussex_County"}))
    check("case and padding do not matter", county_ok({"COUNTY": " sussex county "}))
    check("a county we do not cover is still excluded",
          not county_ok({"COUNTY": "New Castle County"}))
    check("...including its underscore spelling",
          not county_ok({"COUNTY": "New_Castle_County"}))
    check("a missing county is excluded, not admitted", not county_ok({}))
    check("a None county does not crash", not county_ok({"COUNTY": None}))

    check("a PLUS row is tracked by PLUS_ID, which survives a republish",
          identity("PLUS (built)", {"PLUS_ID": "2024-01-01", "GLOBALID": "{churns}"})
          == "2024-01-01")
    check("a dev application is still tracked by GLOBALID",
          identity("Dev Application", {"GLOBALID": "{abc}"}) == "{abc}")
    check("a PLUS row with no PLUS_ID falls back rather than becoming untrackable",
          identity("PLUS (built)", {"GLOBALID": "{abc}"}) == "{abc}")
    check("a row with no id at all is reported as untrackable, not as new",
          identity("PLUS (built)", {}) == "")

    check("the reset ceiling is below the 504 that actually shipped",
          MAX_PLAUSIBLE_NEW < 504)
    check("...and above a plausible busy week", MAX_PLAUSIBLE_NEW > 20)
    return 1 if bad else 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    main()
