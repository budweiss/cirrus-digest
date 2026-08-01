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
          if (a.get("COUNTY") or "") in COUNTIES]
    da.sort(key=lambda a: (a.get("P_YEAR") or ""), reverse=True)

    # Layer 3 — Building Permits (residential >= MIN_UNITS = large multifamily
    # buildings ACTIVELY under construction now; single-lot permits drop out)
    bp = [a for a in fetch_layer(3, f"R_NR='R' AND R_UNITS>={MIN_UNITS}", "P_YEAR DESC")
          if (a.get("COUNTY") or "") in COUNTIES]
    bp.sort(key=lambda a: (a.get("P_YEAR") or ""), reverse=True)

    # Layer 4 — PLUS Project Areas (historical major residential; built communities
    # now at/near developer→homeowner turnover = ready PM targets)
    plus = [a for a in fetch_layer(4, f"RESIDENTIAL_UNITS>={MIN_UNITS}", "PLUS_ID DESC")
            if (a.get("COUNTY") or "") in COUNTIES]
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
    cur_ids = {a.get("GLOBALID") for _, a in tagged if a.get("GLOBALID")}
    prev = set(json.loads(seen_file.read_text())) if seen_file.exists() else None
    if prev is None:
        new = []
    else:
        new = [dict(kind=k, **a) for k, a in tagged
               if a.get("GLOBALID") and a["GLOBALID"] not in prev]
    seen_file.write_text(json.dumps(sorted(i for i in cur_ids if i)))
    (OUT / "plus_new.json").write_text(json.dumps(new, indent=1))
    print(f"NEW since last run: {len(new)}" + (" (baseline established)" if prev is None else ""))

    print(f"Building Permits (residential >= {MIN_UNITS}u, Kent+Sussex): {len(bp)}")
    print("  by year:", dict(sorted(Counter((a.get('P_YEAR') or '?') for a in bp).items(), reverse=True)))

    print(f"Development Applications (residential >= {MIN_UNITS}u, Kent+Sussex): {len(da)}")
    print("  by county:", dict(Counter(a["COUNTY"] for a in da)))
    print("  by year  :", dict(sorted(Counter((a.get('P_YEAR') or '?') for a in da).items(), reverse=True)))
    for a in da[:12]:
        print(f"    {a.get('P_YEAR','?'):5} | {a.get('COUNTY',''):13} | {str(a.get('R_UNITS','?')):>6}u | "
              f"{a.get('RECTYPE',''):18} | {(a.get('NOTES') or '').replace(chr(10),' / ')[:45]}")
    print(f"\nPLUS Project Areas (historical >= {MIN_UNITS}u, Kent+Sussex): {len(plus)}")
    print("  by year:", dict(sorted(Counter((a.get('PLUS_YEAR') or '?') for a in plus).items(), reverse=True)))


if __name__ == "__main__":
    main()
