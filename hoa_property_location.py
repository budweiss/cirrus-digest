#!/usr/bin/env python3
"""hoa_property_location.py — S75. Backfill each Delaware HOA's PROPERTY
location into Bill's CRM from the county GIS it was sourced from.

WHY THIS EXISTS
---------------
Buddy, 2026-08-24: "include the address of these properties", and separately
"note the difference between a property location, manager, and HOA info."

The CRM had no property address at all -- property_address / street_address /
site_address / address / location / physical_address were populated on 0 of
2,444 entities. The only address data was `mailing_address` (26% coverage), and
94 of those are in ANOTHER STATE, because HOA mail goes to the management
company. Chimney Hill: county=Kent, and its only stored address is
1326 Fretz Drive, Edmond, OK -- the out-of-state manager.

So three DIFFERENT things were being conflated, and this module keeps them apart:

    property_*  WHERE THE HOMES ARE.        From county GIS. Always Delaware.
    mailing_*   Where the HOA gets post.    May be a PO box, may be the manager.
    current_mgmt_co / board_contact         Who runs it. May be anywhere.

An owner or a manager in Maryland is a LEAD, not a disqualifier (Buddy's rule).
The GIS models this correctly and we mirror it: New Castle's parcel layer has
PROPSTATE (the parcel) and OWNSTATE (the owner) as separate fields. Shannon Cove
is 422 parcels, PROPSTATE=DE on every one, while OWNSTATE shows 9 Maryland
owners and 1 New Jersey. Property Delaware; owners elsewhere. Both true.

SOURCES
-------
New Castle (source=nccde_parcel_subdiv, 1,623 entities)
    gis.nccde.org BaseMaps/Base_Layers/MapServer/0 'Parcels', keyed on SUBDIV.
    Gives ADDRESS/STNAME/PROPCITY/PROPSTATE/PROPZIP per parcel.
    NOTE: the layer id moved -- nccde_hoa.py's MapServer/9 is now a 404, and
    the FeatureServer copy rejects `where` queries entirely (400). Layer 0 over
    POST is the one that works; GET with a long where can 400 as well.

Sussex (source=sussex_county_gis, 637 entities)
    map.sussexcountyde.gov Parcels_PIN_With_Assessment_Unit/FeatureServer/1
    'OwnershipInformation', keyed on FULLNAME.
    The HOA's OWN parcels are common areas (open space, "billboard area") and
    carry no 911 street address -- verified: three Cape Shores PINs all return
    no CAMA_Addresses record. So Sussex yields town/state/zip, not a street.
    Recorded honestly as such rather than left blank or invented.

Usage:
  python3 hoa_property_location.py --dry-run [--limit N] [--only SLUG]
  python3 hoa_property_location.py --live [--limit N] [--refresh]
  python3 hoa_property_location.py selftest
"""
import argparse
import collections
import json
import sys
import time
import urllib.parse
import urllib.request

import entity_kb

PROJECT = "hoa_leads_bill"
UA = {"User-Agent": "CIRRUS-pm/1.0 (Knight Property Services lead research)"}

NCC_Q = ("https://gis.nccde.org/agsserver/rest/services/BaseMaps/"
         "Base_Layers/MapServer/0/query")
SUSSEX_OWN_Q = ("https://map.sussexcountyde.gov/trdserver/rest/services/"
                "Geographic_Information_Office/Parcels_PIN_With_Assessment_Unit/"
                "FeatureServer/1/query")

DE_COUNTIES = {"kent", "sussex", "new castle"}
THROTTLE_S = 0.35          # be a polite guest on a county server


def _post(url: str, params: dict) -> dict:
    """POST, not GET. The NCC endpoint 400s on GET for anything but the
    simplest where clause, and silently returns [] for others."""
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(params).encode(), headers=UA)
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode())


def _query(url: str, params: dict, tries: int = 3) -> list:
    for a in range(tries):
        try:
            d = _post(url, params)
            if "error" in d:
                raise RuntimeError(str(d["error"])[:160])
            return [f.get("attributes", {}) for f in d.get("features", [])]
        except Exception:
            if a == tries - 1:
                raise
            time.sleep(2 + 2 * a)
    return []


def _clean(s) -> str:
    return " ".join(str(s or "").split()).strip()


def _zip5(z) -> str:
    z = _clean(z).split("-")[0]
    return z if z.isdigit() and len(z) == 5 else ""


def _sql_quote(s: str) -> str:
    return _clean(s).replace("'", "''")


# ── New Castle ────────────────────────────────────────────────────────────────
def lookup_ncc(name: str) -> dict:
    """SUBDIV -> the parcels that make up the community."""
    rows = _query(NCC_Q, {
        "where": f"SUBDIV='{_sql_quote(name.upper())}'",
        "outFields": "SUBDIV,ADDRESS,STNAME,PROPCITY,PROPSTATE,PROPZIP,OWNSTATE",
        "returnGeometry": "false", "f": "json"})
    if not rows:
        return {}
    cities = collections.Counter(_clean(r.get("PROPCITY")) for r in rows if _clean(r.get("PROPCITY")))
    states = collections.Counter(_clean(r.get("PROPSTATE")) for r in rows if _clean(r.get("PROPSTATE")))
    zips = collections.Counter(_zip5(r.get("PROPZIP")) for r in rows if _zip5(r.get("PROPZIP")))
    streets = collections.Counter(_clean(r.get("STNAME")) for r in rows if _clean(r.get("STNAME")))
    owner_states = collections.Counter(_clean(r.get("OWNSTATE")) for r in rows if _clean(r.get("OWNSTATE")))
    out_of_state_owners = sum(v for k, v in owner_states.items() if k and k != "DE")
    return {
        "property_city": cities.most_common(1)[0][0] if cities else "",
        "property_state": states.most_common(1)[0][0] if states else "",
        "property_zip": zips.most_common(1)[0][0] if zips else "",
        "property_streets": ", ".join(s for s, _ in streets.most_common(4)),
        "property_parcels": str(len(rows)),
        "property_basis": "New Castle County parcel layer (SUBDIV match)",
        # Kept because it is a PITCH, not a disqualifier: an owner elsewhere is
        # a reason to call, and conflating it with the property's own location
        # is the mistake this whole module exists to prevent.
        "owners_out_of_state": str(out_of_state_owners) if out_of_state_owners else "",
    }


# ── Sussex ────────────────────────────────────────────────────────────────────
def lookup_sussex(name: str) -> dict:
    """FULLNAME -> the association's parcel records. Yields town/state/zip; the
    association's own parcels are common areas with no 911 street address."""
    rows = _query(SUSSEX_OWN_Q, {
        "where": f"FULLNAME='{_sql_quote(name.upper())}'",
        "outFields": "FULLNAME,MAILINGADDRESS,CITY,STATE,ZIPCODE,DESCRIPTION",
        "returnGeometry": "false", "f": "json"})
    if not rows:
        return {}
    # The town the parcels sit in. Sussex mails to a DE town for these, and a
    # non-DE mailing state means the manager gets the post -- so a mailing row
    # is only usable as a LOCATION hint when it is in Delaware.
    de = [r for r in rows if _clean(r.get("STATE")).upper() == "DE"]
    cities = collections.Counter(_clean(r.get("CITY")) for r in de if _clean(r.get("CITY")))
    zips = collections.Counter(_zip5(r.get("ZIPCODE")) for r in de if _zip5(r.get("ZIPCODE")))
    # A street address that is NOT a PO box, in DE, is very likely inside the
    # community (e.g. Cape Shores -> 400 E CAPE SHORES DR).
    street = ""
    for r in de:
        a = _clean(r.get("MAILINGADDRESS")).upper()
        if a and not a.startswith(("PO BOX", "P O BOX", "P.O.")):
            street = a
            break
    return {
        "property_city": cities.most_common(1)[0][0] if cities else "",
        "property_state": "DE" if de else "",
        "property_zip": zips.most_common(1)[0][0] if zips else "",
        "property_streets": street,
        "property_parcels": str(len(rows)),
        "property_basis": ("Sussex County ownership records (town/zip; the "
                           "association's own parcels are common area and carry "
                           "no 911 street address)"),
    }


LOOKUPS = {
    "nccde_parcel_subdiv": lookup_ncc,
    "sussex_county_gis": lookup_sussex,
}

COUNTY_BY_SOURCE = {
    "nccde_parcel_subdiv": "New Castle",
    "sussex_county_gis": "Sussex",
}


def run(dry_run=True, limit=None, only=None, refresh=False, db_path=None) -> dict:
    ents = entity_kb.list_entities(PROJECT, db_path=db_path)
    stats = collections.Counter()
    done = 0
    for ent in ents:
        slug = ent.get("slug")
        if only and slug != only:
            continue
        st = ent.get("state") or {}
        source = _clean(st.get("source"))
        fn = LOOKUPS.get(source)
        if not fn:
            stats["skipped: no GIS source"] += 1
            continue
        if st.get("property_state") and not refresh:
            stats["skipped: already has property_state"] += 1
            continue
        if limit is not None and done >= limit:
            break
        name = _clean(st.get("legal_name")) or _clean(ent.get("name"))
        try:
            got = fn(name)
        except Exception as e:
            stats["lookup error"] += 1
            print(f"  ERROR {ent.get('name')!r}: {str(e)[:120]}")
            time.sleep(THROTTLE_S)
            continue
        done += 1
        time.sleep(THROTTLE_S)
        if not got or not got.get("property_state"):
            stats["no GIS match"] += 1
            print(f"  no match  {ent.get('name')!r}  (source={source})")
            continue
        # A property that does not come back Delaware is a data problem, not a
        # lead. Never write it silently -- Bill's report is Delaware-only.
        if got.get("property_state") != "DE":
            stats[f"NON-DE property_state: {got['property_state']}"] += 1
            print(f"  *** NON-DE  {ent.get('name')!r} -> {got.get('property_state')}")
            continue
        fields = dict(got)
        if not _clean(st.get("county")):
            fields["county"] = COUNTY_BY_SOURCE.get(source, "")
            stats["county backfilled"] += 1
        stats["matched"] += 1
        loc = f"{got.get('property_city')}, DE {got.get('property_zip')}".strip()
        print(f"  ok  {ent.get('name')[:38]:38s} -> {loc}"
              f"  [{got.get('property_parcels')} parcels]")
        if not dry_run:
            entity_kb.upsert_entity(PROJECT, slug, ent.get("name"),
                                    fields=fields, db_path=db_path)
    print("\n== summary ==")
    for k, v in stats.most_common():
        print(f"  {v:6d}  {k}")
    if dry_run:
        print("\n  DRY RUN — nothing written. Re-run with --live to apply.")
    return dict(stats)


def selftest() -> bool:
    checks = []
    checks.append(("zip is normalised to 5 digits", _zip5("19709-    ") == "19709"))
    checks.append(("a bad zip yields empty, not garbage", _zip5("-") == ""))
    checks.append(("whitespace in GIS values is collapsed",
                   _clean("1405     SMITH BRIDGE RD  ") == "1405 SMITH BRIDGE RD"))
    checks.append(("an apostrophe cannot break the SQL where clause",
                   _sql_quote("O'HARA FARM") == "O''HARA FARM"))
    checks.append(("both GIS sources are wired", set(LOOKUPS) ==
                   {"nccde_parcel_subdiv", "sussex_county_gis"}))
    checks.append(("every wired source maps to a Delaware county",
                   all(v.lower() in DE_COUNTIES for v in COUNTY_BY_SOURCE.values())))
    all_ok = all(ok for _, ok in checks)
    for desc, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
    return all_ok


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(0 if selftest() else 1)
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--live", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only", default=None)
    ap.add_argument("--refresh", action="store_true",
                    help="re-look-up entities that already have property_state")
    a = ap.parse_args()
    run(dry_run=not a.live, limit=a.limit, only=a.only, refresh=a.refresh)
