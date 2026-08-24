#!/usr/bin/env python3
"""hoa_prune_individuals.py — S75. Remove private individuals from Bill's CRM.

WHY THEY ARE THERE
------------------
sussex_hoa.py selected owner records with FULLNAME LIKE '%HOA%'. "HOA" is a
substring of ordinary surnames -- RHOADS, RHOADES, HOADLEY, HOAG -- and of the
Vietnamese given name Hoa. 135 private individuals landed in a commercial lead
CRM, with their home mailing addresses, and the daily research job has been
spending Brave and LLM budget researching private citizens.

The ingest bug is fixed (sussex_hoa.py, S75). This removes what it already let
in. Buddy asked for the removal explicitly, 2026-08-24.

SAFETY -- this is the only script here that DESTROYS client data
----------------------------------------------------------------
* --dry-run is the default. --live is required to delete anything.
* A RESTORE FILE is written BEFORE the first delete, containing every entity
  and every one of its events in full. Deleting without a readable restore file
  is treated as a failure, not a warning: the script aborts.
* Selection reuses the same person/trust shape guard as the fixed ingest, so
  what gets deleted is exactly what would no longer be admitted.
* An explicit association marker ALWAYS wins over the person shape, so a real
  "Smith Farm HOA" cannot be caught.
* Anything carrying real research history (a signal or a recorded outcome) is
  REPORTED AND SKIPPED rather than deleted -- if we researched it and the client
  engaged with it, a name-shape heuristic is not enough to throw it away.
  Nothing is silently dropped from either direction.

Usage:
  python3 hoa_prune_individuals.py --dry-run
  python3 hoa_prune_individuals.py --live
  python3 hoa_prune_individuals.py selftest
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import entity_kb

PROJECT = "hoa_leads_bill"
RESTORE_DIR = Path.home() / "cirrus-digest" / "logs" / "crm-restore"

# Same shapes as the fixed sussex_hoa.py guard. Kept in sync deliberately: what
# we delete must equal what the ingest would now refuse.
_PERSON_RX = re.compile(
    r"(\b[A-Z][a-zA-Z]* [A-Z][a-zA-Z]* [A-Z]\b"     # Rhoads David M
    r"|\b[A-Z][a-zA-Z]+ [A-Z]\b"                     # Rhoads J
    r"|&"                                            # Joseph I & Darlene M
    r"|\bTTEE\b|\bTRUSTEE\b|\bREV TR\b|\bIRR TR\b|\bLIV TR\b)",
    re.IGNORECASE)

# \bHOA\b is deliberately absent: it is the ambiguous token (also a given name)
# and letting it rescue a person-shaped name is what kept DINH HOA VIET in.
_ASSOC_RX = re.compile(
    r"(HOMEOWNER|OWNERS ASSOC|ASSOCIATION|MAINTENANCE|CONDOMINIUM|"
    r"PROPERTY OWNERS|COUNCIL OF CO|\bPOA\b)", re.IGNORECASE)

# A SUBDIVISION SECTION DESIGNATOR looks exactly like a middle initial.
# The first dry-run against the real CRM selected "Westover Hills Sec C",
# "Cooper Farm Sec A", "North Hills Sec A" and "Darley Green Condo I" for
# deletion -- "Hills Sec C" and "Green Condo I" match the <word> <word>
# <single-capital> person shape perfectly. These are real leads, and deleting
# them would have been the most expensive kind of false positive: silent, and
# in the direction of losing client data.
#
# No person's name contains SEC / PHASE / BLDG / LOT, so this is a safe rescue
# in a way a generic word list would not be. Found only because the destructive
# path is dry-run-by-default and the output was read before running it live.
# Reviewing the full 89-name dry-run by hand found five more of these:
# "Concord Manor  Blk A" / "Blk E" (BLK, the abbreviation BLOCK missed),
# "Bunker Hill Centre I", "G & C Lane Road", "Limestone Hills H-2".
# The asymmetry decides how aggressive this list should be: leaving an
# individual in the CRM is mild (the digest filter keeps them out of Bill's
# mail anyway), while deleting a real community is destroying client data.
# So this errs toward rescuing, and any name it spares is reported, not hidden.
#
# ESTATES is PLURAL-ONLY on purpose: "Somebody Estate" is a deceased person's
# estate and SHOULD go, while "Foxhill Estates" is a subdivision.
_PLACE_RX = re.compile(
    r"(\b(SEC|SECT|SECTION|PHASE|PH|CONDO|CONDOS|UNIT|UNITS|BLDG|BUILDING|"
    r"BLOCK|BLK|LOT|LOTS|TRACT|PLAT|POD|PARCEL|VILLAGE|APTS|APARTMENTS|"
    r"CENTRE|CENTER|ROAD|LANE|STREET|AVENUE|BOULEVARD|HIGHWAY|TERRACE|"
    r"SQUARE|PLAZA|ESTATES|ACRES|HEIGHTS|COMMONS|CROSSING|LANDING|"
    r"SUBDIVISION|DEVELOPMENT|COURT|CIRCLE)\b"
    r"|\b[A-Z]-\d+\b)",                    # a section designator like H-2
    re.IGNORECASE)


def looks_like_a_person(name: str) -> bool:
    n = (name or "").strip()
    if _ASSOC_RX.search(n) or _PLACE_RX.search(n):
        return False
    return bool(_PERSON_RX.search(n))


def select(db_path=None):
    """-> (to_delete, kept_because_researched). Never returns a name twice."""
    to_delete, researched = [], []
    for ent in entity_kb.list_entities(PROJECT, db_path=db_path):
        if not looks_like_a_person(ent.get("name")):
            continue
        evs = entity_kb.get_events(PROJECT, slug=ent["slug"], db_path=db_path)
        meaningful = [e for e in evs
                      if e.get("event_type") in ("signal", "outcome")]
        if meaningful:
            researched.append((ent, len(meaningful)))
        else:
            to_delete.append(ent)
    return to_delete, researched


def run(dry_run=True, db_path=None) -> dict:
    to_delete, researched = select(db_path=db_path)

    print(f"selected for removal : {len(to_delete)}")
    print(f"skipped (has research history): {len(researched)}")
    print()
    for ent in to_delete:
        st = ent.get("state") or {}
        print(f"  DELETE  {str(ent.get('name'))[:44]:44s} "
              f"source={str(st.get('source'))[:20]:20s} county={st.get('county')}")
    if researched:
        print("\n  These match the person shape but carry real findings, so they "
              "are KEPT for a human to look at:")
        for ent, n in researched:
            print(f"  KEEP    {str(ent.get('name'))[:44]:44s} ({n} signal/outcome event(s))")

    if dry_run:
        print(f"\n  DRY RUN — nothing deleted. Re-run with --live to remove "
              f"{len(to_delete)} record(s).")
        return {"deleted": 0, "selected": len(to_delete),
                "skipped": len(researched)}

    if not to_delete:
        print("\n  nothing to delete.")
        return {"deleted": 0, "selected": 0, "skipped": len(researched)}

    # ── restore file FIRST. No restore file, no deletion. ────────────────────
    RESTORE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = RESTORE_DIR / f"pruned-individuals-{stamp}.json"
    payload = []
    for ent in to_delete:
        payload.append({
            "entity": ent,
            "events": entity_kb.get_events(PROJECT, slug=ent["slug"],
                                           db_path=db_path),
        })
    path.write_text(json.dumps({"project": PROJECT, "pruned_at": stamp,
                                "records": payload}, indent=2, default=str))
    # Prove it is readable and complete before touching anything.
    check = json.loads(path.read_text())
    if len(check.get("records", [])) != len(to_delete):
        print(f"\n  *** ABORT: restore file has {len(check.get('records', []))} "
              f"records but {len(to_delete)} are queued. Nothing deleted.")
        return {"deleted": 0, "aborted": "restore file incomplete"}
    print(f"\n  restore file written: {path}  ({len(check['records'])} records)")

    deleted = 0
    for ent in to_delete:
        got = entity_kb.delete_entity(PROJECT, ent["slug"], db_path=db_path)
        if got:
            deleted += 1
    print(f"  deleted: {deleted}")
    print(f"  entities remaining: {len(entity_kb.list_entities(PROJECT, db_path=db_path))}")
    print(f"\n  to restore: python3 hoa_prune_individuals.py is NOT the tool for "
          f"that — the file above holds every field and event; re-import with "
          f"entity_kb.upsert_entity / add_signal.")
    return {"deleted": deleted, "restore_file": str(path),
            "skipped": len(researched)}


def selftest() -> bool:
    import os
    import tempfile
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db)
    checks = []
    try:
        for n in ("RHOADS KATHLEEN A", "RHOADES WILLIAM W", "HOADLEY RONALD L",
                  "HOAG PAULA Z", "DINH HOA VIET & LIEU THI",
                  "RHOADS BETTY LOUISE TTEE REV TR", "HOAG DAVID W TTEE",
                  # a deceased person's estate is still a person, and the
                  # singular ESTATE must not be rescued by plural ESTATES
                  "Aikman Fairfax H Estate", "The Estate Of Isabel S Ryan",
                  "Peel Estate Of Mary T", "Ochoa Patricia A Portillo",
                  "Quan Tuong T & Mai & Hoang"):
            checks.append((f"individual rejected :: {n[:30]}", looks_like_a_person(n)))
        for n in ("CAPE SHORES HOMEOWNERS ASSOCIATION", "BAYSIDE HOA",
                  "HOA OF CHIMNEY HILL", "APPLE ARBOR PROPERTY OWNERS",
                  "SMITH FARM HOA", "LE PARC CONDOMINIUM COUNCIL OF CO OWNERS",
                  "ATLANTIS II CONDOMINIUM ASSOCIATION",
                  # the four the first live-CRM dry-run nearly deleted
                  "Westover Hills Sec C", "Cooper Farm Sec A",
                  "North Hills Sec A", "Darley Green Condo I",
                  "Willow Grove Mill Sec 2", "Brandywine Hundred Ph 3",
                  # found by reading the full 89-name dry-run by hand
                  "Concord Manor  Blk A", "Concord Manor  Blk E",
                  "Bunker Hill Centre I", "G & C Lane Road",
                  "Limestone Hills H-2"):
            checks.append((f"association kept :: {n[:30]}", not looks_like_a_person(n)))

        entity_kb.upsert_entity(PROJECT, "rhoads-a", "Rhoads Kathleen A",
                                fields={"county": "Sussex"}, db_path=db)
        entity_kb.upsert_entity(PROJECT, "cape-shores", "Cape Shores Homeowners Association",
                                fields={"county": "Sussex"}, db_path=db)
        # a person-shaped record that HAS been researched -> must be kept
        entity_kb.upsert_entity(PROJECT, "hoag-x", "Hoag David W Ttee",
                                fields={"county": "Kent"}, db_path=db)
        entity_kb.add_signal(PROJECT, "hoag-x", "complaint", "a real finding",
                             db_path=db)

        dele, kept = select(db_path=db)
        checks.append(("plain individual is selected",
                       any(e["slug"] == "rhoads-a" for e in dele)))
        checks.append(("association is NOT selected",
                       all(e["slug"] != "cape-shores" for e in dele)))
        checks.append(("a researched person-shaped record is SKIPPED, not deleted",
                       all(e["slug"] != "hoag-x" for e in dele)
                       and any(e["slug"] == "hoag-x" for e, _ in kept)))

        r = run(dry_run=True, db_path=db)
        checks.append(("dry run deletes nothing",
                       r["deleted"] == 0
                       and len(entity_kb.list_entities(PROJECT, db_path=db)) == 3))
    finally:
        if os.path.exists(db):
            os.unlink(db)
    ok = all(o for _, o in checks)
    for d, o in checks:
        print(f"  {'PASS' if o else 'FAIL'}  {d}")
    return ok


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(0 if selftest() else 1)
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--live", action="store_true")
    a = ap.parse_args()
    run(dry_run=not a.live)
