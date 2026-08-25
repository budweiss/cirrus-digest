#!/usr/bin/env python3
"""
nccde_association_directory.py — pull New Castle County's PUBLIC community
association directory (maintenance corporations + civic associations).

S77 (2026-08-25). This is the source we told Bill in July we would find and
then didn't: ~224 New Castle County associations WITH a named officer or
management company, and for most of them an email and/or phone. It is free,
needs no account, and is served as plain server-rendered HTML — no portal
login, no deed search, no per-page fee.

Why it matters more than the parcel data we had: `nccde_subdivisions_clean.json`
gives community NAMES and acreage. This gives a PERSON TO CONTACT. It also
separates self-managed associations (Bill's prospects) from ones already held
by a management company (his competitors) — Knight Property Services itself
appears here, on Enclave at Odessa.

Emails are obfuscated on the page by a tiny inline script that concatenates
user + domain at render time, so a plain HTML scrape misses them; we
reconstruct from the script's own variables.

Two modes:
  -o FILE            dump the parsed directory as JSON (the lead workbook)
  --enrich PROJECT   match it against an entity_kb project and write the
                     officer/management details onto the entities we already
                     track. DRY RUN by default; --apply writes.

Matching is deliberately strict: normalised EXACT name only. A near match is
reported, never applied. Attaching the wrong president to the wrong community
would put a stranger's name in front of a client, which is worse than a gap.
"""

import argparse
import html
import json
import re
import sys
import urllib.request

URL = ("https://www.newcastlede.gov/BusinessDirectoryII.aspx"
       "?lngBusinessCategoryID=31,32&ysnShowAll=1")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

# Terminate each item at its own closing </div>, NOT at the next item's name:
# the last row on the page has no following name, and a lookahead for one
# silently dropped it (caught by the count guard in main(), 2026-08-25).
ROW_RX = re.compile(
    r'<span style="font-weight: bold;">(?P<name>[^<]+)</span>(?P<body>.*?)</div>',
    re.S)
EMAIL_RX = re.compile(r"var w = '([^']*)'\s*var x = '([^']*)'", re.S)
PHONE_RX = re.compile(r'Phone:\s*<a href="tel:[^"]*">([^<]+)</a>')
LINK_RX = re.compile(r'Link:\s*<a href="([^"]+)"')


def fetch(url: str = URL) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace")


def parse(page: str) -> list:
    out = []
    for m in ROW_RX.finditer(page):
        body = m.group("body")
        # Strip the inline scripts before reading the address lines, else the
        # email-obfuscation JS lands in the address.
        clean = re.sub(r"<script.*?</script>", "", body, flags=re.S)
        parts = [html.unescape(p).replace("\xa0", " ").strip()
                 for p in re.split(r"<br\s*/?>", clean)]
        parts = [p for p in (re.sub(r"<[^>]+>", "", p).strip() for p in parts) if p]
        parts = [p for p in parts if not p.startswith(("Phone:", "Email:", "Link:"))]

        em = EMAIL_RX.search(body)
        ph = PHONE_RX.search(body)
        ln = LINK_RX.search(body)
        contact = parts[0] if parts else ""
        rec = {
            "name": html.unescape(m.group("name")).strip(),
            "contact": contact,
            # "<Company>, MC" is the directory's own marker for a community
            # being professionally managed -- i.e. NOT a prospect for Bill.
            "managed": bool(re.search(r",\s*MC$", contact)),
            "address": ", ".join(parts[1:]),
            "phone": ph.group(1).strip() if ph else "",
            "email": f"{em.group(1)}@{em.group(2)}" if em else "",
            "website": ln.group(1) if ln else "",
        }
        out.append(rec)
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def enrich(project: str, recs: list, apply: bool) -> int:
    """Write directory officer/management details onto entities we already
    track. Only ever UPDATES an existing entity -- the directory is a contact
    source, not a source of new leads, and inventing entities from it would mix
    two provenances in one project."""
    import entity_kb

    by_norm = {}
    for r in recs:
        by_norm.setdefault(_norm(r["name"]), []).append(r)

    entities = entity_kb.list_entities(project)
    matched, changed, ambiguous, unmatched = 0, 0, [], 0
    for e in entities:
        hits = by_norm.get(_norm(e["name"]), [])
        if not hits:
            unmatched += 1
            continue
        if len(hits) > 1:
            ambiguous.append(e["name"])
            continue
        r = hits[0]
        matched += 1

        fields = {}
        if r["managed"]:
            fields["current_mgmt_co"] = r["contact"]
            fields["mgmt_status"] = f"professionally managed ({r['contact']})"
        else:
            fields["mgmt_status"] = "self-managed (no management company listed)"
            if r["contact"]:
                fields["board_contact"] = r["contact"]
        # Never blank out something we already hold with something we don't.
        if r["email"]:
            fields["board_email"] = r["email"]
        if r["phone"]:
            fields["board_phone"] = r["phone"]
        if r["website"]:
            fields["website"] = r["website"]
        fields["directory_source"] = ("New Castle County Community Association "
                                      "Directory (public, county-maintained)")

        if not apply:
            # DIFF against what is stored, don't just count the match. A dry run
            # that reports "109 would change" every time, including straight
            # after a successful apply, can never tell you there is nothing to
            # do -- and a report that is never quiet gets ignored (T9).
            cur = (entity_kb.get_entity(project, e["slug"]) or {}).get("state", {})
            diff = sorted(k for k, v in fields.items() if cur.get(k) != v)
            if diff:
                changed += 1
                print(f"  WOULD UPDATE {e['name']}: {diff}")
            continue
        res = entity_kb.upsert_entity(project, e["slug"], e["name"], fields=fields)
        if res.get("changed_fields"):
            changed += 1
            print(f"  {e['name']}: {res['changed_fields']}")

    print(f"\n{'APPLIED' if apply else 'DRY RUN'} — project '{project}'")
    print(f"  entities in project : {len(entities)}")
    print(f"  matched to directory: {matched}")
    print(f"  with field changes  : {changed}"
          + ("  (already up to date)" if matched and not changed else ""))
    print(f"  ambiguous (skipped) : {len(ambiguous)}"
          + (f" -> {ambiguous[:5]}" if ambiguous else ""))
    print(f"  no directory entry  : {unmatched}")
    # Say what was NOT covered. A directory row we could not place is a lead
    # this project simply does not know about yet.
    placed = {_norm(e["name"]) for e in entities}
    orphan = [r["name"] for r in recs if _norm(r["name"]) not in placed]
    print(f"  directory rows with no entity here: {len(orphan)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out")
    ap.add_argument("--enrich", metavar="PROJECT")
    ap.add_argument("--apply", action="store_true",
                    help="with --enrich: actually write (default is a dry run)")
    ap.add_argument("--url", default=URL)
    a = ap.parse_args()
    if not a.out and not a.enrich:
        a.out = "out/nccde_associations.json"

    page = fetch(a.url)
    expected = re.search(r"of (\d+) Listing", page)
    recs = parse(page)

    # Do not let a silent parser regression look like a small county. If the
    # page says 224 and we parsed 12, that is a broken scrape, not a shrunken
    # directory -- exactly the "absence treated as valid" shape.
    if expected and len(recs) != int(expected.group(1)):
        print(f"REFUSING to write: page reports {expected.group(1)} listings, "
              f"parsed {len(recs)}. The page layout probably changed.",
              file=sys.stderr)
        return 2
    if not recs:
        print("REFUSING to write: parsed 0 listings.", file=sys.stderr)
        return 2

    if a.out:
        import os
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        with open(a.out, "w") as f:
            json.dump(recs, f, indent=2)
        print(f"wrote {a.out}")

    managed = sum(r["managed"] for r in recs)
    print(f"{len(recs)} associations parsed")
    print(f"  self-managed (PROSPECTS): {len(recs) - managed}")
    print(f"  already professionally managed (competitors): {managed}")
    print(f"  with email: {sum(bool(r['email']) for r in recs)}"
          f" | with phone: {sum(bool(r['phone']) for r in recs)}")

    if a.enrich:
        print()
        return enrich(a.enrich, recs, a.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
