#!/usr/bin/env python3
"""hoa_county_refresh.py (S537, 2026-10-29) -- monthly re-pull of Kent and Sussex
County association owners into Bill's HOA CRM (entity_kb `hoa_leads_bill`).

New Castle's association directory is already re-pulled daily (nccde_directory
inside cirrus-hoaleads). Kent (178) and Sussex (592) were one-time pulls in
July/August, so an association recorded since then never reached Bill. Buddy,
10-29: "just do all of them. make this the best product you can."

Each run, per county:
  * pulls association-owned parcels from the county's OWN GIS and refuses a
    layer that is not in Delaware (the S67 Henrico, Virginia lesson);
  * keeps only names with an unambiguous association marker, and drops
    person-shaped names (S75: RHOADS, "DINH HOA VIET & LIEU THI") and
    non-residential bodies (churches, fire companies, cemeteries...);
  * GENUINELY NEW = an owner name absent from last month's pull. It is added to
    the CRM as a cold lead with a `new-association` signal, so Bill sees it in
    his Monday digest. The first run only seeds that memory (names it adds
    then were merely outside the July hand-built list, not newly recorded, so
    they get no "new" claim);
  * a changed county mailing address on a known association is recorded, and
    becomes a `mgmt-change` signal when the "C/O" management line changed
    (hired, switched, or dropped a manager -- each is a sales opening).

Refuses to write (and Telegrams Buddy) when the source looks broken: fewer than
half the county's known associations matched, or more than MAX_NEW new names.
Dry run by default; the timer passes --apply.
"""
import json
import math
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import entity_kb  # noqa: E402

PROJECT = "hoa_leads_bill"
SEEN_PATH = ROOT / "data/hoa_county_seen.json"
MAX_NEW = 40
SOURCES = {
    "Kent": {"url": "https://gis.kentcountyde.gov/server/rest/services/Parcels/Parcels/FeatureServer/0",
             "name": "OWNERNAME", "mail": ("MAILINGADDRESS", "MAILINGADDRESS2"),
             "city": "OWNERCITY", "state": "OWNERSTATE", "zip": "OWNERZIP",
             "source": "kent_county_gis"},
    "Sussex": {"url": ("https://map.sussexcountyde.gov/trdserver/rest/services/Geographic_Information_Office/"
                       "Parcels_PIN_With_Assessment_Unit/FeatureServer/1"),
               "name": "FULLNAME", "mail": ("MAILINGADDRESS",), "city": "CITY", "state": "STATE",
               "zip": "ZIPCODE", "source": "sussex_county_gis"},
}
# Query terms (broad) -- the strong-marker filter below decides what is kept.
TERMS = ["HOMEOWNER", "OWNERS ASSOC", "ASSOCIATION", "MAINTENANCE", "CONDOMINIUM",
         "PROPERTY OWNERS", "COUNCIL OF CO", " POA"]
# Unambiguous association markers (sussex_hoa.py _ASSOC_RX, minus the bare
# ASSOCIATION that also names churches, fire companies and camp meetings).
# "MAINTENANCE CO/COMPANY" is left out on purpose: it also names landscapers.
STRONG_RX = re.compile(r"(HOMEOWNER|OWNERS'? ASSOC|MAINTENANCE (CORP|ASSOC|ORG)|"
                       r"CONDOMINIUM|COUNCIL OF CO|\bPOA\b|PROPERTY OWNERS|"
                       r"COMMUNITY ASSOC|CIVIC ASSOC|ROAD MAINTENANCE)", re.I)
NOT_RESIDENTIAL_RX = re.compile(r"(CHURCH|CEMETERY|FIRE CO|FIRE DEP|VOLUNTEER|CAMP MEETING|"
                                r"LEGION|\bVFW\b|LODGE|GRANGE|SPORTSM|HUNTING|GUN CLUB|BANK|"
                                r"SAVINGS|BUILDING & LOAN|HOSPITAL|SCHOOL|MINISTR|CONFERENCE|"
                                r"AIRPORT|DRAINAGE|TAX DITCH|WATER CO|SEWER|CHAMBER OF|"
                                # commercial condos (Dover Medical Center, Creekstone Office Center)
                                r"MEDICAL|OFFICE|\bCENTER\b|PROFESSIONAL|BUSINESS|COMMERCIAL|PLAZA|"
                                r"EXECUTIVE|INDUSTRIAL|RETAIL|SHOPP)", re.I)
# sussex_hoa.py S75: an association marker always wins over the person shape,
# and bare "HOA" is never a marker here. Joint owners and personal trusts are
# still refused outright -- no association is named that way.
_PERSON_RX = re.compile(r"(&|\bTTEE\b|\bTRUSTEE\b|\bREV TR\b|\bIRR TR\b|\bLIV TR\b)", re.I)
_ACRONYMS = {"Hoa", "Poa", "Inc", "Llc", "Ii", "Iii", "Iv"}
DE_BOX = (-75.8, 38.4, -75.0, 39.9)        # lon/lat bounds of Delaware


def is_association(name):
    n = (name or "").strip()
    if not STRONG_RX.search(n) or NOT_RESIDENTIAL_RX.search(n):
        return False
    return not _PERSON_RX.search(n)


def norm(name):
    t = re.sub(r"[^A-Z0-9 ]", " ", re.sub(r"['’]", "", (name or "").upper()))
    return " ".join(w for w in t.split() if w not in {"THE", "INC", "LLC", "CO", "CORP", "CORPORATION"})


def pretty(name):
    """ingest_sussex.pretty: CRM title case, INC/HOA kept readable."""
    out = []
    for w in re.split(r"(\s+)", (name or "").strip().title()):
        out.append(w.upper() if w in _ACRONYMS else w)
    return re.sub(r"'([A-Z])(?![A-Za-z])", lambda m: "'" + m.group(1).lower(), "".join(out).strip())


_ABBR = {"STATION": "STA", "STREET": "ST", "DRIVE": "DR", "ROAD": "RD", "LANE": "LN", "COURT": "CT",
         "CIRCLE": "CIR", "AVENUE": "AVE", "BOULEVARD": "BLVD", "SUITE": "STE", "HIGHWAY": "HWY",
         "TRAIL": "TRL", "PLACE": "PL", "TERRACE": "TER", "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W"}


def addr_key(address):
    """Canonical address for comparison: '14 WOODLAND STATION' == '14 WOODLAND STA',
    '28657 WOODCREST DR' == '28657WOODCREST DR'."""
    words = re.sub(r"[^A-Z0-9 ]", " ", (address or "").upper()).split()
    return "".join(_ABBR.get(w, w) for w in words)


def co_line(address):
    """The management "C/O ..." (the county also writes "% ...") part of a
    mailing address, normalised, or ''."""
    m = re.search(r"(?:C/O|%)\s*(.+)", address or "", re.I)
    return norm(m.group(1)) if m else ""


def in_delaware(meta):
    """(ok, why) from layer metadata. Fails closed."""
    ext = meta.get("extent") or {}
    sr = ext.get("spatialReference") or meta.get("spatialReference") or {}
    wkid = sr.get("latestWkid") or sr.get("wkid")
    try:
        x, y = float(ext["xmin"]), float(ext["ymin"])
    except Exception:
        return False, "no extent"
    if wkid in (3857, 102100):
        x = x / 6378137.0 * 180 / math.pi
        y = (2 * math.atan(math.exp(y / 6378137.0)) - math.pi / 2) * 180 / math.pi
    elif wkid in (26957, 2776, 3509, 103500):
        return True, f"wkid {wkid} (Delaware State Plane)"
    elif wkid != 4326:
        return False, f"wkid {wkid} unrecognised"
    ok = DE_BOX[0] - 0.3 <= x <= DE_BOX[2] and DE_BOX[1] - 0.3 <= y <= DE_BOX[3]
    return ok, f"extent corner {x:.2f},{y:.2f}"


def _get(url, params):
    u = url + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "cowork-hoa-refresh/1.0"}),
                                timeout=120) as r:
        return json.load(r)


def fetch_owners(county, get=_get):
    s = SOURCES[county]
    meta = get(s["url"], {"f": "json"})
    if not meta.get("extent"):
        # A table (Sussex OwnershipInformation) has no extent: prove the
        # service it belongs to instead.
        svc = get(s["url"].rsplit("/", 1)[0], {"f": "json"})
        meta = {"extent": svc.get("fullExtent") or svc.get("initialExtent")}
    ok, why = in_delaware(meta)
    if not ok:
        raise RuntimeError(f"{county} layer not proven Delaware ({why})")
    where = " OR ".join(f"{s['name']} LIKE '%{t.strip()}%'" if not t.startswith(" ")
                        else f"{s['name']} LIKE '%{t}'" for t in TERMS)
    fields = [s["name"], *s["mail"], s["city"], s["state"], s["zip"]]
    by, off = {}, 0
    while True:
        d = get(s["url"] + "/query", {"where": where, "outFields": ",".join(fields),
                                      "returnGeometry": "false", "resultOffset": off,
                                      "resultRecordCount": 1000, "orderByFields": "OBJECTID", "f": "json"})
        if "error" in d:
            raise RuntimeError(f"{county} query rejected: {str(d['error'])[:120]}")
        feats = d.get("features", [])
        for f in feats:
            a = f.get("attributes", {})
            name = (a.get(s["name"]) or "").strip()
            if name:
                by.setdefault(name.upper(), {
                    "owner": name, "city": (a.get(s["city"]) or "").strip(),
                    "state": (a.get(s["state"]) or "").strip(), "zip": str(a.get(s["zip"]) or "").strip(),
                    "mailing": ", ".join(str(a.get(m) or "").strip() for m in s["mail"] if a.get(m))})
        if len(feats) < 1000 or off > 100000:
            break
        off += len(feats)
    return list(by.values())


def plan(county, owners, entities, seen):
    """Pure diff. entities: CRM rows ({slug, name, state}); seen: owner names
    (upper) from last month's pull, or None on the first run."""
    known = {}
    for e in entities:
        st = e.get("state") or {}
        for key in (st.get("legal_owner_name"), st.get("legal_name"), e.get("name")):
            if key:
                known.setdefault(norm(key), e)
    keep, names = [], set()
    for o in owners:
        if is_association(o["owner"]) and norm(o["owner"]) not in names:
            names.add(norm(o["owner"]))
            keep.append(o)
    out = {"county": county, "pulled": len(owners), "associations": len(keep),
           "matched": 0, "new": [], "added_untagged": [], "address_changes": []}
    # An address shared by 3+ associations is a management company's office.
    share = {}
    pairs = [(o["mailing"], norm(o["owner"])) for o in keep] + [
        ((e.get("state") or {}).get("mailing_address") or "",
         norm((e.get("state") or {}).get("legal_name") or (e.get("state") or {}).get("legal_owner_name")
              or e.get("name"))) for e in entities]
    for addr, who in pairs:
        if addr_key(addr):
            share.setdefault(addr_key(addr), set()).add(who)
    managed = lambda a: bool(co_line(a)) or len(share.get(addr_key(a), ())) >= 3
    for o in keep:
        e = known.get(norm(o["owner"])) or known.get(norm(pretty(o["owner"])))
        if e is None:
            fresh = seen is not None and o["owner"].upper() not in seen
            out["new" if fresh else "added_untagged"].append(o)
            continue
        out["matched"] += 1
        old = (e.get("state") or {}).get("mailing_address")
        if old is not None and addr_key(old) != addr_key(o["mailing"]):
            out["address_changes"].append({"slug": e["slug"], "name": e["name"], "old": old,
                                           "new": o["mailing"], "city": o["city"], "state": o["state"],
                                           "mgmt": managed(old) or managed(o["mailing"])})
    return out


def refuse_reason(p, county_known):
    if county_known and p["matched"] < 0.5 * county_known:
        return (f"{p['county']}: only {p['matched']} of {county_known} known associations matched "
                "-- the county source or its field names probably changed")
    if len(p["new"]) > MAX_NEW:
        return f"{p['county']}: {len(p['new'])} 'new' associations in one month -- likely a source change"
    return ""


def apply(p, today, db_path=None):
    src = SOURCES[p["county"]]["source"]
    made = 0
    for o, tagged in [(o, True) for o in p["new"]] + [(o, False) for o in p["added_untagged"]]:
        name = pretty(o["owner"])
        slug = entity_kb.slugify(name)
        res = entity_kb.upsert_entity(PROJECT, slug, name, entity_type="HOA", fields={
            "county": p["county"], "type": "HOA", "legal_name": o["owner"], "mailing_address": o["mailing"],
            "mailing_city": o["city"], "mailing_state": o["state"], "source": src}, lead_state="cold",
            db_path=db_path)
        made += bool(res.get("created"))
        if tagged and res.get("created"):
            entity_kb.add_signal(PROJECT, slug, "new-association",
                                 f"Newly recorded in {p['county']} County parcel owner records this month "
                                 f"(first seen {today}); mail goes to {o['city'] or '?'}, {o['state'] or '?'}. "
                                 "A new association or a community just turned over from its developer.",
                                 confidence="medium", db_path=db_path)
    for c in p["address_changes"]:
        entity_kb.upsert_entity(PROJECT, c["slug"], c["name"], fields={
            "mailing_address": c["new"], "mailing_city": c["city"], "mailing_state": c["state"]},
            db_path=db_path)
        if c["mgmt"]:
            entity_kb.add_signal(PROJECT, c["slug"], "mgmt-change",
                                 f"County owner records now send this association's mail to \"{c['new']}\" "
                                 f"(we had \"{c['old']}\"), and one of those is a management company's "
                                 "address -- it may have hired, changed or dropped its manager.",
                                 confidence="medium", db_path=db_path)
    return made


def run(do_apply, notify=print, get=_get, seen_path=SEEN_PATH, db_path=None, today=None):
    today = today or datetime.now().strftime("%Y-%m-%d")
    try:
        seen_all = json.loads(Path(seen_path).read_text())
    except Exception:
        seen_all = {}
    entities = entity_kb.list_entities(PROJECT, db_path=db_path)
    summary, problems = [], []
    for county in SOURCES:
        try:
            owners = fetch_owners(county, get)
        except Exception as exc:
            problems.append(f"{county}: pull failed ({str(exc)[:120]})")
            continue
        mine = [e for e in entities if (e.get("state") or {}).get("county") == county]
        seen = set(seen_all[county]) if county in seen_all else None
        p = plan(county, owners, mine, seen)
        why = refuse_reason(p, len(mine))
        line = (f"{county}: pulled {p['pulled']} owners, {p['associations']} associations, matched "
                f"{p['matched']}/{len(mine)}, new {len(p['new'])}, first-run additions "
                f"{len(p['added_untagged'])}, address changes {len(p['address_changes'])} "
                f"({sum(c['mgmt'] for c in p['address_changes'])} management)")
        summary.append(line)
        print(line)
        for o in (p["new"] + p["added_untagged"])[:60]:
            print("   +", o["owner"], "|", o["city"], o["state"])
        for c in p["address_changes"][:20]:
            print("   ~", c["name"], "|", c["old"], "->", c["new"], "| mgmt" if c["mgmt"] else "")
        if why:
            problems.append(why)
            continue
        if do_apply:
            apply(p, today, db_path)
            seen_all[county] = sorted(o["owner"].upper() for o in owners if is_association(o["owner"]))
    if do_apply:
        Path(seen_path).parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(seen_path) + ".tmp")
        tmp.write_text(json.dumps(seen_all, indent=1))
        tmp.replace(seen_path)
    if problems:
        notify("Bill HOA county refresh did not apply: " + "; ".join(problems))
    return summary, problems


def selftest():
    import os
    import tempfile
    ok = True

    def ck(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(("  PASS  " if cond else "  FAIL  ") + name)

    ck("a homeowners association is kept", is_association("BRENFORD WOODS HOMEOWNERS ASSOCIATION, INC."))
    ck("a road maintenance association is kept", is_association("PAYTON LANE ROAD MAINTENANCE ASSOCIATION INC"))
    ck("a church association is dropped", not is_association("ST PAULS CHURCH CEMETERY ASSOCIATION"))
    ck("a fire company association is dropped", not is_association("CAMDEN WYOMING FIRE CO ASSOCIATION"))
    ck("a bare ASSOCIATION is not enough", not is_association("DELAWARE AGRICULTURAL ASSOCIATION"))
    ck("RHOADS is not an HOA", not is_association("RHOADS DAVID M"))
    ck("a person-shaped HOA name is dropped", not is_association("DINH HOA VIET & LIEU THI"))
    ck("a landscaping 'maintenance company' is not an HOA", not is_association("JONES LAWN MAINTENANCE CO LLC"))
    ck("a personal trust is dropped even with a marker", not is_association("SMITH JOHN TTEE HOMEOWNERS TRUST"))
    ck("a commercial office condo is not an HOA", not is_association("CREEKSTONE OFFICE CENTER CONDOMINIUM"))
    ck("apostrophe spellings are one association",
       norm("CHAMPIONS' CLUB AT JONATHAN'S LANDING") == norm("CHAMPIONS CLUB AT JONATHANS LANDING"))
    tbl = lambda url, params: ({} if url.endswith("/1") else
                               {"fullExtent": {"xmin": -8414000, "ymin": 4695000, "spatialReference": {"wkid": 3857}}})
    try:
        fetch_owners("Sussex", lambda url, params: tbl(url, params) if "where" not in params else {"features": []})
        tbl_ok = True
    except RuntimeError:
        tbl_ok = False
    ck("a table with no extent is proven through its service", tbl_ok)
    ck("Kent's Web Mercator extent is proven Delaware",
       in_delaware({"extent": {"xmin": -8414000, "ymin": 4695000, "spatialReference": {"wkid": 102100}}})[0])
    ck("Henrico Virginia is refused",
       not in_delaware({"extent": {"xmin": -8630000, "ymin": 4520000, "spatialReference": {"wkid": 3857}}})[0])
    ck("an unknown projection fails closed",
       not in_delaware({"extent": {"xmin": 1, "ymin": 1, "spatialReference": {"wkid": 2284}}})[0])

    ents = [{"slug": "brenford-woods", "name": "Brenford Woods",
             "state": {"county": "Kent", "legal_owner_name": "BRENFORD WOODS HOMEOWNERS ASSOCIATION, INC."}},
            {"slug": "cape-shores", "name": "Cape Shores Homeowners Association",
             "state": {"county": "Sussex", "legal_name": "CAPE SHORES HOMEOWNERS ASSOCIATION",
                       "mailing_address": "PO BOX 208"}}]
    own = [{"owner": "BRENFORD WOODS HOMEOWNERS ASSOCIATION INC", "mailing": "PO BOX 1", "city": "DOVER", "state": "DE", "zip": ""},
           {"owner": "CAPE SHORES HOMEOWNERS ASSOCIATION", "mailing": "C/O SEASCAPE PROPERTY MANAGEMENT LLC, PO BOX 9", "city": "LEWES", "state": "DE", "zip": ""},
           {"owner": "NEW MEADOW HOMEOWNERS ASSOCIATION", "mailing": "PO BOX 5", "city": "SMYRNA", "state": "DE", "zip": ""},
           {"owner": "RHOADS DAVID M", "mailing": "1 MAIN", "city": "X", "state": "DE", "zip": ""}]
    p0 = plan("Kent", own, ents, None)
    ck("legal names match despite punctuation and INC", p0["matched"] == 2)
    ck("first run seeds: unknown association added WITHOUT a 'new' claim",
       not p0["new"] and [o["owner"] for o in p0["added_untagged"]] == ["NEW MEADOW HOMEOWNERS ASSOCIATION"])
    p1 = plan("Kent", own, ents, {"BRENFORD WOODS HOMEOWNERS ASSOCIATION INC", "CAPE SHORES HOMEOWNERS ASSOCIATION"})
    ck("an owner absent last month is NEW", [o["owner"] for o in p1["new"]] == ["NEW MEADOW HOMEOWNERS ASSOCIATION"])
    ck("an owner present last month is not re-announced",
       not plan("Kent", own, ents, {o["owner"] for o in own})["new"])
    ck("formatting is not an address change", addr_key("14 WOODLAND STATION") == addr_key("14 WOODLAND STA")
       and addr_key("28657 WOODCREST DR") == addr_key("28657WOODCREST DR"))
    ck("the county's '%' is a C/O line", co_line("% FIRST SERVICE RESIDENTIAL-EAST") == "FIRST SERVICE RESIDENTIAL EAST")
    office = "424 N UNION AVE"
    crm = [{"slug": f"a{i}", "name": f"A{i}", "state": {"legal_name": f"A{i} HOMEOWNERS ASSOCIATION",
            "mailing_address": office}} for i in range(4)]
    pulled = [{"owner": f"A{i} HOMEOWNERS ASSOCIATION", "mailing": office if i else "PO BOX 7",
               "city": "", "state": "DE", "zip": ""} for i in range(4)]
    moves = plan("Sussex", pulled, crm, None)["address_changes"]
    ck("leaving a shared management office is a management change", len(moves) == 1 and moves[0]["mgmt"])
    joiners = [{"owner": f"J{i} HOMEOWNERS ASSOCIATION", "mailing": "9 MANAGER WAY", "city": "", "state": "DE",
                "zip": ""} for i in range(3)]
    jcrm = [{"slug": f"j{i}", "name": f"J{i}", "state": {"legal_name": f"J{i} HOMEOWNERS ASSOCIATION",
             "mailing_address": f"PO BOX {i}"}} for i in range(3)]
    jm = plan("Sussex", joiners, jcrm, None)["address_changes"]
    ck("three associations moving to one new office are management changes", len(jm) == 3 and all(c["mgmt"] for c in jm))
    quiet = plan("Sussex", [dict(pulled[1], mailing="PO BOX 9")],
                 [{"slug": "b", "name": "B", "state": {"legal_name": "A1 HOMEOWNERS ASSOCIATION",
                                                         "mailing_address": "PO BOX 8"}}], None)["address_changes"]
    ck("a PO box to PO box move is recorded but not called a management change", len(quiet) == 1 and not quiet[0]["mgmt"])
    ch = p1["address_changes"]
    ck("a new C/O management company is a management change", len(ch) == 1 and ch[0]["mgmt"])
    ck("an address with no stored baseline is not a change", all(c["slug"] != "brenford-woods" for c in ch))
    ck("a source matching under half the known list is refused",
       refuse_reason({"county": "Kent", "matched": 80, "new": []}, 178))
    ck("too many 'new' in one month is refused",
       refuse_reason({"county": "Kent", "matched": 178, "new": [1] * (MAX_NEW + 1)}, 178))

    fd, db = tempfile.mkstemp(suffix=".db"); os.close(fd); os.unlink(db)
    seen_file = Path(tempfile.mkdtemp()) / "seen.json"
    try:
        entity_kb.upsert_entity(PROJECT, "brenford-woods", "Brenford Woods", fields=ents[0]["state"], db_path=db)
        entity_kb.upsert_entity(PROJECT, "cape-shores", "Cape Shores Homeowners Association",
                                fields=ents[1]["state"], db_path=db)
        meta = {"extent": {"xmin": -8414000, "ymin": 4695000, "spatialReference": {"wkid": 3857}}}

        def fake_get(url, params):
            if params.get("f") == "json" and "where" not in params:
                return meta
            county_rows = own if "kent" in url else own[1:2]
            n = SOURCES["Kent" if "kent" in url else "Sussex"]["name"]
            return {"features": [{"attributes": {n: o["owner"], "MAILINGADDRESS": o["mailing"]}} for o in county_rows]}
        msgs = []
        run(True, msgs.append, fake_get, seen_file, db, "2026-11-02")
        made = entity_kb.get_entity(PROJECT, "new-meadow-homeowners-association", db_path=db)
        sig = [e for e in entity_kb.get_events(PROJECT, "new-meadow-homeowners-association", db_path=db)
               if e["event_type"] == "signal"]
        ck("first live run adds the missing association as a cold lead, no 'new' signal",
           made and made["lead_state"] == "cold" and not sig)
        ck("first live run records last month's names", "NEW MEADOW HOMEOWNERS ASSOCIATION" in
           json.loads(seen_file.read_text())["Kent"])
        cape = [e for e in entity_kb.get_events(PROJECT, "cape-shores", db_path=db) if e["event_type"] == "signal"]
        ck("a management change is signalled for Bill", cape and cape[0]["signal_kind"] == "mgmt-change")
        ck("nothing refused on a healthy source", not msgs)
        ck("live CRM file untouched by the selftest", str(SEEN_PATH) != str(seen_file))
    finally:
        if os.path.exists(db):
            os.unlink(db)
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        return selftest()
    do_apply = "--apply" in sys.argv
    notify = print
    if do_apply:
        from immaculate_agent import send_telegram
        notify = send_telegram
    summary, problems = run(do_apply, notify)
    if do_apply:
        import job_status
        job_status.record("hoacountyrefresh", not problems,
                          ("; ".join(problems) if problems else "; ".join(s.split(",")[0] + "," +
                           s.split(",")[3] for s in summary))[:200])
    if not do_apply:
        print("DRY RUN -- nothing written. The timer passes --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
