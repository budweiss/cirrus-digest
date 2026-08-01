#!/usr/bin/env python3
"""plus_enrich.py — add owner-of-record ("who's the builder") + mailing contact to
the DE new-development leads, per Bill's request (S47, 2026-07-29).

Reads out/plus_leads.json, looks up each lead's PARCEL_ID in the county parcel
system, and writes out/plus_owners.json = { PARCEL_ID: {owner, owner2, mailing,
link, subdivision} }. Runs on the MacBook via the runner (urllib only), same as
plus_pull.py — NOT from the Cowork sandbox.

Sources (public, authoritative):
- Kent:   gis.kentcountyde.gov Parcels FeatureServer/0  (match field 'Name';
          OWNERNAME/SECONDARYOWNER/MAILINGADDRESS.., 'Pride' = property-info URL)
- Sussex: map.sussexcountyde.gov Parcels_PIN_With_Assessment_Unit MapServer/1
          (OwnershipInformation table; match 'PIN'; FULLNAME/Second_Owner_Name/..)
"""
import json, urllib.request, urllib.parse, time
from pathlib import Path

OUT = Path(__file__).resolve().parent / "out"
KENT = "https://gis.kentcountyde.gov/server/rest/services/Parcels/Parcels/FeatureServer/0/query"
SUSSEX = ("https://map.sussexcountyde.gov/trdserver/rest/services/"
          "Geographic_Information_Office/Parcels_PIN_With_Assessment_Unit/MapServer/1/query")


def _q(url, where, fields):
    params = {"where": where, "outFields": ",".join(fields),
              "returnGeometry": "false", "f": "json"}
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": "cowork-plus-enrich/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8")).get("features", [])


def _chunks(xs, n):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _inlist(ids):
    return "(" + ",".join("'" + i.replace("'", "") + "'" for i in ids) + ")"


def enrich_kent(pids):
    out = {}
    for ch in _chunks(sorted(pids), 80):
        try:
            feats = _q(KENT, f"Name IN {_inlist(ch)}",
                       ["Name", "OWNERNAME", "SECONDARYOWNER", "MAILINGADDRESS",
                        "MAILINGADDRESS2", "OWNERCITY", "OWNERSTATE", "OWNERZIP",
                        "Pride", "PropertyUse"])
        except Exception as e:
            print("kent chunk error:", e); feats = []
        for f in feats:
            a = f["attributes"]
            mail = " ".join(x for x in [a.get("MAILINGADDRESS"), a.get("MAILINGADDRESS2"),
                    ", ".join(y for y in [a.get("OWNERCITY"), a.get("OWNERSTATE"), a.get("OWNERZIP")] if y)] if x)
            out[a.get("Name")] = {"owner": a.get("OWNERNAME") or "", "owner2": a.get("SECONDARYOWNER") or "",
                                  "mailing": mail.strip(), "link": a.get("Pride") or "",
                                  "use": a.get("PropertyUse") or ""}
        time.sleep(0.2)
    return out


def enrich_sussex(pids):
    out = {}
    for ch in _chunks(sorted(pids), 80):
        try:
            feats = _q(SUSSEX, f"PIN IN {_inlist(ch)}",
                       ["PIN", "FULLNAME", "Second_Owner_Name", "MAILINGADDRESS",
                        "CITY", "STATE", "ZIPCODE", "DESCRIPTION"])
        except Exception as e:
            print("sussex chunk error:", e); feats = []
        for f in feats:
            a = f["attributes"]
            mail = " ".join(x for x in [a.get("MAILINGADDRESS"),
                    ", ".join(y for y in [a.get("CITY"), a.get("STATE"), a.get("ZIPCODE")] if y)] if x)
            out[a.get("PIN")] = {"owner": a.get("FULLNAME") or "", "owner2": a.get("Second_Owner_Name") or "",
                                 "mailing": mail.strip(),
                                 "link": f"https://map.sussexcountyde.gov/ (search PIN {a.get('PIN')})",
                                 "use": a.get("DESCRIPTION") or ""}
        time.sleep(0.2)
    return out


def main():
    d = json.load(open(OUT / "plus_leads.json"))
    leads = d["dev_applications"] + d["building_permits"]
    kent = {(a.get("PARCEL_ID") or "").strip() for a in leads
            if (a.get("COUNTY") == "Kent County") and (a.get("PARCEL_ID") or "").strip()}
    sussex = {(a.get("PARCEL_ID") or "").strip() for a in leads
              if (a.get("COUNTY") == "Sussex County") and (a.get("PARCEL_ID") or "").strip()}
    print(f"looking up owners — Kent {len(kent)}, Sussex {len(sussex)} parcels...")
    owners = {}
    owners.update(enrich_kent(kent))
    owners.update(enrich_sussex(sussex))
    (OUT / "plus_owners.json").write_text(json.dumps(owners, indent=1))
    matched = sum(1 for p in (kent | sussex) if p in owners and owners[p]["owner"])
    print(f"owner match: {matched}/{len(kent)+len(sussex)} parcels -> {OUT/'plus_owners.json'}")
    for p in list(owners)[:8]:
        print(f"  {p:26} | {owners[p]['owner'][:40]}")


if __name__ == "__main__":
    main()
