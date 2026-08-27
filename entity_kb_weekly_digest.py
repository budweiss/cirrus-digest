"""entity_kb_weekly_digest.py — Monday weekly digest of a week's entity_kb
changes for a client (S65). Buddy's ask: on Mondays, Bill should get
everything the CRM's daily research found that week, regardless of lead
status — separate from any event-triggered hot-lead alert. Generic across
projects, not HOA-specific: extend WEEKLY_DIGEST_RECIPIENTS below for a new
client using entity_kb.py on their own project(s), no logic change needed.

Usage:
  python3 entity_kb_weekly_digest.py --client bill [--dry-run]
  python3 entity_kb_weekly_digest.py selftest
"""
import argparse
import json
import re
import mailer
from datetime import datetime, timedelta
from pathlib import Path

import entity_kb

PROJECT_DIR = Path.home() / "projects/cirrus-digest"
CREDS_PATH = PROJECT_DIR / "config/credentials.json"
CC_ADDR = "Buddy.Weiss@outlook.com"

# Per-client digest config: which entity_kb project(s) to summarize + who
# gets the email. Extend this dict, not the digest logic, for a new client.
WEEKLY_DIGEST_RECIPIENTS = {
    "bill": {
        "to": "whutchins@knightpropertysvs.com",
        "kb_projects": ["hoa_leads_bill"],
        "label": "Delaware HOA research",
        # S75 (Buddy, 2026-08-24): "only properties that are in Delaware, only
        # list properties where we have opportunities." The 2026-08-24 send was
        # 1,328 "updates" of which 1,296 were field_change bookkeeping (637
        # confidence_basis + 637 lead_confidence from one bulk re-scoring pass)
        # -- a 75 KB email in which 96% was internal noise and the 32 real
        # findings were unfindable. Two of those 32 were about a New York
        # village and a Maryland town that share a name with a Delaware HOA.
        "opportunities_only": True,
        # S77 (Buddy, 2026-08-25): attach the New Castle County association
        # contact workbook to Bill's Monday email. Rebuilt from the county
        # directory at send time, so he never gets a stale copy.
        "attach_workbook": "nccde",
    },
    # S66: Buddy's own new-project shortlist (business_idea_scan.py), not a
    # client -- "to" is Buddy himself, so CC_ADDR below just double-lists him
    # (harmless, same address in To and Cc).
    "buddy-business": {
        "to": "Buddy.Weiss@outlook.com",
        "kb_projects": ["business_ideas"],
        "label": "AI-run business ideas",
        "days": 1,  # daily, not weekly -- Buddy tunes the scoring from what it surfaces
    },
}


# ── Opportunity / locality filtering (S75) ────────────────────────────────────
# Only applied to clients whose config sets "opportunities_only". The
# business-ideas digest is deliberately unaffected: it wants everything.
#
# signal_kind is CALLER-DEFINED free text and the research model invents it, so
# both spellings of the same idea occur ("management-change" and
# "management_company_change"). Normalise before matching.

def _norm_kind(kind: str) -> str:
    return re.sub(r"[\s_]+", "-", (kind or "").strip().lower())


# A finding Bill can act on: distress, a management shake-up, or an HOA whose
# manager is out of state (an explicit local-PM pitch).
OPPORTUNITY_KINDS = {
    "complaint", "distress", "litigation", "lawsuit",
    "special-assessment", "rfp", "bid-request",
    "mgmt-change", "management-change", "management-company-change",
    "out-of-state-mgmt",
}

# Real findings that are NOT opportunities -- identity confirmations, dues
# amounts, board email addresses. Useful data for the CRM; not a reason to
# call anyone. Listed explicitly so that a kind which is in NEITHER set is
# reported as unclassified rather than silently dropped (T8/T23: every value
# the filter accepts must have a decided path, and a dropped finding nobody
# hears about is indistinguishable from no finding).
INFORMATIONAL_KINDS = {
    "identification", "operational-detail", "governance-contact",
    "governance-info", "policy-change", "leadership-change", "other",
}

_NON_DE_STATE_RX = re.compile(
    r"\b(Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|"
    r"Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|"
    r"Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|"
    r"Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|"
    r"New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|"
    r"Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|"
    r"Virginia|Washington|West Virginia|Wisconsin|Wyoming)\b", re.IGNORECASE)
_DELAWARE_RX = re.compile(r"\bDelaware\b|\bDE\b")

DE_COUNTIES = {"kent", "sussex", "new castle"}


def _is_delaware_entity(project: str, slug: str, summaries: list,
                        db_path: str = None) -> bool:
    """Delaware is decided per ENTITY, not per finding. These communities come
    from the Kent / Sussex / New Castle county HOA registries, so a stored
    county IS the authority. Falls back to the findings' own text when the
    county was never captured (5 of 14 entities in the 2026-08-24 window had
    no county but were plainly Delaware in their text)."""
    try:
        ent = entity_kb.get_entity(project, slug, db_path=db_path) or {}
    except Exception:
        ent = {}
    county = ((ent.get("state") or {}).get("county") or "").strip().lower()
    if county in DE_COUNTIES:
        return True
    return any(_DELAWARE_RX.search(t or "") for t in summaries)


# Buddy, 2026-08-24: "it should only be properties located in Delaware. They
# can be managed or owned out of state." So another state's name disqualifies a
# finding only when it describes WHERE THE PROPERTY IS -- never when it
# describes who manages, owns, developed or banks it. An HOA in Milford run
# from Maryland is a lead; that is the entire out-of-state-mgmt pitch.
_MGMT_CONTEXT_RX = re.compile(
    r"(manage(d|ment|r|s)?|property manager|PM\b|owner|owned|ownership|"
    r"developer|developed|builder|landlord|contractor|vendor|law firm|"
    r"attorney|counsel|headquarter|based in|located out of|area code|"
    r"corporate|parent company|billing)", re.IGNORECASE)


def _signal_is_out_of_state(kind: str, summary: str) -> bool:
    """True only when the FINDING'S SUBJECT looks like it sits outside Delaware
    -- the failure that put a New York village mayor and a Maryland town
    commission into a Delaware HOA report.

    Management and ownership are explicitly NOT disqualifying (Buddy's rule).
    Two independent reasons a state name is allowed to appear:
      * the kind is out-of-state-mgmt -- naming another state IS its meaning;
      * the sentence is talking about management/ownership/developers, so the
        other state describes the company, not the property.
    """
    if _norm_kind(kind) == "out-of-state-mgmt":
        return False
    text = summary or ""
    if not _NON_DE_STATE_RX.search(text):
        return False          # no other state named at all
    if _DELAWARE_RX.search(text):
        return False          # affirms Delaware itself
    if _MGMT_CONTEXT_RX.search(text):
        return False          # the other state describes WHO RUNS it, not WHERE it is
    return True


# Buddy, 2026-08-24: "include the address of these properties." The CRM has NO
# property street address -- property_address / street_address / site_address /
# address / location / physical_address are populated on 0 of 2,444 entities.
# What it has is `mailing_address` on 26%, and 94 of those are in ANOTHER STATE
# (MD 45, PA 17, TX 10, VA 10, NY 4, ...) because an HOA's mail often goes to
# its management company. Chimney Hill is the worked example: county=Kent, and
# the only address on file is 1326 Fretz Drive, Edmond, OK -- the out-of-state
# manager. Printing that as "the address" would put an Oklahoma address under a
# Delaware property, which is the exact confusion this report is meant to end.
#
# So: LOCATION is stated from the county, which is authoritative (99.8%
# coverage, sourced from county GIS / corporate records). Any mailing address
# is shown separately and LABELLED as mail, with its state, so an out-of-state
# one reads as the manager's office and never as where the property sits.

def _detail_block(ent: dict) -> str:
    """Buddy, 2026-08-24: "note the difference between a property location,
    manager, and HOA info." They are three different things and were being run
    together, which is how an Oklahoma management address ended up reading as a
    Delaware property's address. Each gets its own labelled line:

      Property location  WHERE THE HOMES ARE.      County GIS. Always Delaware.
      HOA contact        Where the association     May be a PO box.
                         gets its post.
      Managed by         Who runs it.              May be in any state; that is
                                                   a PITCH, not a problem.
    """
    st = (ent or {}).get("state") or {}
    out = []

    # 1. PROPERTY LOCATION -- the authoritative one.
    city = (st.get("property_city") or "").strip()
    pstate = (st.get("property_state") or "").strip()
    pzip = (st.get("property_zip") or "").strip()
    county = (st.get("county") or "").strip()
    if city and pstate:
        loc = f"{city.title()}, {pstate} {pzip}".strip()
        if county:
            loc += f" ({county} County)"
    elif county:
        loc = f"{county} County, DE"
    else:
        loc = "not yet confirmed"
    out.append(f"  Property location: {loc}")
    streets = (st.get("property_streets") or "").strip()
    if streets:
        out.append(f"    streets: {streets}")

    # 2. HOA CONTACT -- the association's own mail. Not the property.
    addr = (st.get("mailing_address") or "").strip()
    mcity = (st.get("mailing_city") or "").strip()
    mstate = (st.get("mailing_state") or "").strip().upper()
    if addr or mcity:
        parts = ", ".join(x for x in (addr, mcity, mstate) if x)
        tag = "" if mstate in ("DE", "") else "   [out of state — association mail, NOT the property]"
        out.append(f"  HOA contact:       {parts}{tag}")

    # 3. MANAGER -- may be anywhere. An out-of-state manager is the lead.
    # "Managed by: Unknown" is worse than no line: it looks like a researched
    # finding and says nothing. Placeholders are treated as absent.
    _NOTHING = {"unknown", "none", "n/a", "na", "tbd", "pending", "-", "?"}
    mgmt = (st.get("current_mgmt_co") or "").strip()
    mgmt_status = (st.get("mgmt_status") or "").strip()
    if mgmt.lower() in _NOTHING:
        mgmt = ""
    if mgmt_status.lower() in _NOTHING:
        mgmt_status = ""
    if mgmt:
        out.append(f"  Managed by:        {mgmt}")
    elif mgmt_status:
        out.append(f"  Managed by:        {mgmt_status}")

    owners_oos = (st.get("owners_out_of_state") or "").strip()
    if owners_oos:
        out.append(f"    note: {owners_oos} unit owner(s) live out of state "
                   f"(the property is in Delaware)")
    return "\n".join(out)


def compose_digest(kb_projects: list, since: str, db_path: str = None,
                   opportunities_only: bool = False) -> str:
    """Builds the plain-text digest body from entity_kb events across one
    or more projects since a given timestamp. Returns '' if there's
    genuinely nothing to report -- caller decides whether to send at all
    (a client should never get an empty "nothing happened" email).

    opportunities_only (S75, Buddy's ask for Bill): keep only Delaware
    communities, and only findings he can act on. Drops every field_change --
    "lead_confidence updated" tells a client nothing -- keeps opportunity
    signals, and reports what it withheld instead of withholding it silently.
    """
    lines = []
    total_events = 0
    n_field, n_info, n_offstate = 0, 0, 0
    unclassified = {}

    for kb_project in kb_projects:
        events = entity_kb.get_events(kb_project, since=since, db_path=db_path)
        if not events:
            continue

        by_entity = {}
        for ev in events:
            by_entity.setdefault(ev["slug"], {"name": ev["name"], "events": []})
            by_entity[ev["slug"]]["events"].append(ev)

        for slug, group in sorted(by_entity.items(), key=lambda kv: kv[1]["name"]):
            evs = group["events"]

            if opportunities_only:
                sigs = [e for e in evs if e.get("event_type") == "signal"]
                n_field += len(evs) - len(sigs)

                summaries = [e.get("summary") or "" for e in sigs]
                if not sigs or not _is_delaware_entity(kb_project, slug, summaries,
                                                       db_path=db_path):
                    n_offstate += len(sigs)
                    continue

                keep = []
                for e in sigs:
                    kind = _norm_kind(e.get("signal_kind"))
                    if kind not in OPPORTUNITY_KINDS:
                        if kind in INFORMATIONAL_KINDS:
                            n_info += 1
                        else:
                            # Neither list knows this kind. Count it by name so a
                            # new one surfaces instead of vanishing.
                            unclassified[kind or "(none)"] = \
                                unclassified.get(kind or "(none)", 0) + 1
                        continue
                    if _signal_is_out_of_state(e.get("signal_kind"),
                                               e.get("summary")):
                        n_offstate += 1
                        continue
                    keep.append(e)
                evs = keep

            if not evs:
                continue

            total_events += len(evs)
            lines.append(group["name"])
            if opportunities_only:
                try:
                    ent = entity_kb.get_entity(kb_project, slug, db_path=db_path)
                except Exception:
                    ent = None
                lines.append(_detail_block(ent))
            for ev in sorted(evs, key=lambda e: e["occurred_at"]):
                date = (ev.get("occurred_at") or "")[:10]
                if ev["event_type"] == "signal":
                    lines.append(f"  - {date}: {ev.get('summary', '')}")
                else:
                    lines.append(f"  - {date}: {ev['field']} updated")
            lines.append("")

    if total_events == 0:
        return ""
    plural = "s" if total_events != 1 else ""

    if opportunities_only:
        # community headers are the only unindented lines; location lines and
        # findings are both indented, so this still counts communities.
        n_comm = sum(1 for l in lines if l and not l.startswith("  "))
        header = (f"Delaware HOAs with an opportunity this week "
                  f"({n_comm} communit{'ies' if n_comm != 1 else 'y'}, "
                  f"{total_events} finding{plural}):\n\n")
        body = header + "\n".join(lines).strip()
        held = []
        if n_field:
            held.append(f"{n_field} internal record update(s)")
        if n_info:
            held.append(f"{n_info} informational finding(s)")
        if n_offstate:
            held.append(f"{n_offstate} finding(s) not confirmed as Delaware")
        if unclassified:
            held.append(f"{sum(unclassified.values())} of an unrecognised type "
                        f"({', '.join(sorted(unclassified))})")
        if held:
            body += ("\n\n---\nNot shown: " + "; ".join(held) +
                     ". These are kept in the CRM.")
        return body

    header = f"This week's research findings ({total_events} update{plural}):\n\n"
    return header + "\n".join(lines).strip()


def _send_mail(from_email: str, password: str, to_addr: str, cc_addr: str,
               subject: str, body: str, attachments=None) -> bool:
    """Thin shim over mailer.send, kept so the call sites below read unchanged.
    The old body ended in a bare `except: return False` -- a client email that
    never arrived was indistinguishable from one that did. mailer logs it."""
    # watch_promises=False (S78): this is a RECURRING generated digest, not a
    # conversation. Its template carries standing phrases ("we'll keep watching
    # these", the working-rates note) that trip the promise prefilter, so
    # watching it would open a fresh phantom promise every single week and
    # train everyone to ignore the overdue list. A promise made in a digest is
    # a template change, and template changes get reviewed by a human.
    return mailer.send(from_email, password, to_addr, subject, body,
                       cc=cc_addr, attachments=attachments, from_name=False,
                       on_error="false", log=print, watch_promises=False)


def run(client: str, dry_run: bool = False, db_path: str = None,
        days: int = None) -> dict:
    """`days` overrides the lookback window (default: the recipient's own
    `days` if set, else 7). S66: Buddy wants the business-ideas digest DAILY
    so he can tune the scoring criteria from what it surfaces, while Bill's
    client digest stays weekly -- so the window is per-recipient config, not
    a global constant. An empty window still sends nothing, unchanged."""
    cfg = WEEKLY_DIGEST_RECIPIENTS.get(client)
    if not cfg:
        return {"sent": False, "reason": f"no digest config for client '{client}'"}

    window = days or cfg.get("days", 7)
    since = (datetime.now() - timedelta(days=window)).strftime("%Y-%m-%d 00:00:00")
    body = compose_digest(cfg["kb_projects"], since, db_path=db_path,
                          opportunities_only=cfg.get("opportunities_only", False))
    if not body:
        return {"sent": False, "reason": f"nothing to report in the last {window}d"}

    cadence = "Daily" if window == 1 else ("Weekly" if window == 7 else f"{window}-day")
    subject = f"{cadence} {cfg['label']} update — {datetime.now():%Y-%m-%d}"
    if dry_run:
        print(f"SUBJECT: {subject}\n\n{body}")
        if cfg.get("attach_workbook") == "nccde":
            try:
                import nccde_directory
                import os
                path = str(PROJECT_DIR / "out" / "DRYRUN-NCC-HOA-Contacts.xlsx")
                nccde_directory.build_workbook(path)
                print(f"\n[dry-run] WOULD ATTACH: {os.path.basename(path)} "
                      f"({os.path.getsize(path)} bytes)")
            except Exception as e:
                print(f"\n[dry-run] workbook build FAILED: {e}")
        return {"sent": False, "reason": "dry-run"}

    try:
        creds = json.loads(CREDS_PATH.read_text())
    except Exception as e:
        return {"sent": False, "reason": f"no creds: {e}"}

    # Attachment is built FRESH at send time, not read from a cached file, so a
    # stale workbook can never go to a client. If the county site is down we
    # send the digest WITHOUT it and say so in the result -- a missing
    # attachment must not cost Bill his whole Monday email.
    attachments, attach_note = None, ""
    if cfg.get("attach_workbook") == "nccde":
        try:
            import nccde_directory
            path = str(PROJECT_DIR / "out" /
                       f"NCC-HOA-Contacts-{datetime.now():%Y-%m-%d}.xlsx")
            attachments = [nccde_directory.build_workbook(path)]
        except Exception as e:
            attach_note = f"workbook build FAILED: {type(e).__name__}: {e}"
            print(f"[warn] {attach_note} — sending digest without it")

    ok = _send_mail(creds.get("outlook_email", ""), creds.get("outlook_password", ""),
                    cfg["to"], CC_ADDR, subject, body, attachments=attachments)
    return {"sent": ok, "reason": ("" if ok else "send failed"),
            "attached": bool(attachments), "attach_note": attach_note}


def selftest() -> bool:
    import os
    import tempfile

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(db_path)
    checks = []
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        old = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

        # baseline create (no event), then a field change dated 30 days ago,
        # then a signal dated today
        entity_kb.upsert_entity("hoa_leads_bill", "test-hoa", "Test HOA",
                                fields={"county": "Sussex"}, db_path=db_path)
        entity_kb.upsert_entity("hoa_leads_bill", "test-hoa", "Test HOA",
                                fields={"county": "Kent"}, occurred_at=old, db_path=db_path)
        entity_kb.add_signal("hoa_leads_bill", "test-hoa", "distress",
                             "Pool closed this week", occurred_at=now, db_path=db_path)

        since_week = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00")
        body_week = compose_digest(["hoa_leads_bill"], since_week, db_path=db_path)
        checks.append(("this week's digest includes the recent signal",
                       "Pool closed" in body_week))
        checks.append(("this week's digest excludes the 30-day-old field change",
                       "county updated" not in body_week))

        since_far_past = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d 00:00:00")
        body_wide = compose_digest(["hoa_leads_bill"], since_far_past, db_path=db_path)
        checks.append(("a wider window includes the older field change too",
                       "county updated" in body_wide and "Pool closed" in body_wide))

        empty_body = compose_digest(["nonexistent_project"], since_week, db_path=db_path)
        checks.append(("no events in window -> empty digest, not a garbage send",
                       empty_body == ""))

        result = run("nobody-configured", dry_run=True, db_path=db_path)
        checks.append(("unconfigured client is refused, not silently sent",
                       result["sent"] is False and "no digest config" in result["reason"]))

        # S66: per-recipient lookback window (daily for buddy-business,
        # weekly for Bill) -- the 30-day-old change must be outside a 1-day
        # window but inside a 60-day one, from the same stored data.
        body_1d = compose_digest(["hoa_leads_bill"],
                                 (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d 00:00:00"),
                                 db_path=db_path)
        checks.append(("a 1-day window sees today's signal", "Pool closed" in body_1d))
        checks.append(("a 1-day window excludes the 30-day-old change",
                       "county updated" not in body_1d))
        checks.append(("buddy-business is configured for a DAILY window",
                       WEEKLY_DIGEST_RECIPIENTS["buddy-business"].get("days") == 1))
        checks.append(("bill keeps the weekly default (no days override)",
                       "days" not in WEEKLY_DIGEST_RECIPIENTS["bill"]))

        # ── S75: opportunities_only, tested against the REAL 2026-08-24 cases ──
        # Every scenario below is one that actually reached Bill's inbox.
        entity_kb.upsert_entity("hoa_leads_bill", "greens-wyoming",
                                "The Greens at Wyoming",
                                fields={"county": "Kent"}, db_path=db_path)
        entity_kb.add_signal("hoa_leads_bill", "greens-wyoming", "complaint",
                             "Delaware HOA in Kent County with residents "
                             "fighting for control and court action.",
                             occurred_at=now, db_path=db_path)
        # the bulk re-scoring pass that made up 96% of the 2026-08-24 email
        entity_kb.upsert_entity("hoa_leads_bill", "greens-wyoming",
                                "The Greens at Wyoming",
                                fields={"lead_confidence": "0.8"},
                                occurred_at=now, db_path=db_path)
        # a New York village mayor, filed under a Delaware HOA's name
        entity_kb.upsert_entity("hoa_leads_bill", "chestnut-ridge",
                                "Chestnut Ridge",
                                fields={"county": "Kent"}, db_path=db_path)
        entity_kb.add_signal("hoa_leads_bill", "chestnut-ridge",
                             "leadership_change",
                             "Mayor Rosario Presti appointed Chaim Rose to the "
                             "village board of trustees.",
                             occurred_at=now, db_path=db_path)
        # a Maryland town commission, likewise
        entity_kb.upsert_entity("hoa_leads_bill", "church-creek", "Church Creek",
                                fields={"county": "Kent"}, db_path=db_path)
        entity_kb.add_signal("hoa_leads_bill", "church-creek", "governance_info",
                             "Church Creek, Maryland is governed by a town "
                             "commission with Mayor Robert L. Herbert",
                             occurred_at=now, db_path=db_path)
        # Willowwood: names MARYLAND and is a REAL lead -- the manager is the
        # thing that is out of state. Must survive the locality filter.
        entity_kb.upsert_entity("hoa_leads_bill", "willowwood", "Willowwood",
                                fields={"county": "Kent"}, db_path=db_path)
        entity_kb.add_signal("hoa_leads_bill", "willowwood", "out-of-state-mgmt",
                             "Currently managed by an out-of-state company "
                             "(management contact references MARYLAND) - "
                             "potential local-PM pitch opportunity.",
                             occurred_at=now, db_path=db_path)
        # a kind neither list knows -- must be REPORTED, never silently dropped
        entity_kb.upsert_entity("hoa_leads_bill", "novel-kind", "Novel Kind HOA",
                                fields={"county": "Sussex"}, db_path=db_path)
        entity_kb.add_signal("hoa_leads_bill", "novel-kind", "brand-new-kind",
                             "Delaware HOA with something we have not seen.",
                             occurred_at=now, db_path=db_path)

        opp = compose_digest(["hoa_leads_bill"], since_week, db_path=db_path,
                             opportunities_only=True)
        checks.append(("opportunity signal is kept",
                       "fighting for control" in opp))
        checks.append(("field_change bookkeeping is dropped",
                       "lead_confidence updated" not in opp
                       and "county updated" not in opp))
        checks.append(("a New York village mayor is NOT in a Delaware report",
                       "Rosario Presti" not in opp and "Chestnut Ridge" not in opp))
        checks.append(("a Maryland town commission is NOT in a Delaware report",
                       "Robert L. Herbert" not in opp and "Church Creek" not in opp))
        checks.append(("out-of-state-mgmt survives despite naming MARYLAND",
                       "local-PM pitch" in opp and "Willowwood" in opp))
        checks.append(("an unrecognised kind is REPORTED, not silently dropped",
                       "unrecognised type" in opp and "brand-new-kind" in opp))
        checks.append(("the withheld-counts footer is present",
                       "Not shown:" in opp and "internal record update" in opp))
        checks.append(("header counts communities, not raw events",
                       "Delaware HOAs with an opportunity" in opp))

        # the other client must be untouched -- it wants everything
        unfiltered = compose_digest(["hoa_leads_bill"], since_week,
                                    db_path=db_path, opportunities_only=False)
        checks.append(("opportunities_only=False still returns everything",
                       "Rosario Presti" in unfiltered
                       and "lead_confidence updated" in unfiltered))
        checks.append(("buddy-business is NOT opportunity-filtered",
                       WEEKLY_DIGEST_RECIPIENTS["buddy-business"]
                       .get("opportunities_only", False) is False))

        # ── Buddy's rule, 2026-08-24: located in Delaware; managed or owned
        # out of state is FINE. Unit-test the predicate directly, both ways.
        located_elsewhere = [
            ("governance_info",
             "Church Creek, Maryland is governed by a town commission."),
            ("complaint",
             "The community sits in Ocean City, Maryland and has flooding."),
        ]
        for kind, txt in located_elsewhere:
            checks.append((f"property located out of state is rejected :: {txt[:38]}",
                           _signal_is_out_of_state(kind, txt) is True))

        run_from_elsewhere = [
            ("out-of-state-mgmt",
             "Managed by an out-of-state company (references MARYLAND) - "
             "potential local-PM pitch opportunity."),
            ("complaint",
             "Residents complain the property manager is based in "
             "Pennsylvania and never visits the Delaware site."),
            ("complaint",
             "The developer, a New Jersey builder, left drainage unfinished."),
            ("management-change",
             "Association fired its Virginia-based management company."),
            ("complaint",
             "Owner is a Texas investment group; dues have doubled."),
        ]
        for kind, txt in run_from_elsewhere:
            checks.append((f"managed/owned out of state is KEPT :: {txt[:38]}",
                           _signal_is_out_of_state(kind, txt) is False))

        # ── location line (S75): county is the authority; a mailing address
        # is labelled as mail and flagged when it is out of state.
        de_loc = _detail_block({"state": {"county": "Sussex",
                                           "mailing_address": "PO BOX 208",
                                           "mailing_city": "LEWES",
                                           "mailing_state": "DE"}})
        checks.append(("location states the DE county",
                       "Sussex County, DE" in de_loc))
        checks.append(("property location and HOA contact are LABELLED apart",
                       "Property location:" in de_loc and "HOA contact:" in de_loc))
        checks.append(("a DE mailing address is shown unflagged",
                       "PO BOX 208, LEWES, DE" in de_loc
                       and "out of state" not in de_loc))

        ok_loc = _detail_block({"state": {"county": "Kent",
                                           "mailing_address": "1326 Fretz Drive",
                                           "mailing_city": "Edmond",
                                           "mailing_state": "OK"}})
        checks.append(("Chimney Hill case: location is DELAWARE, not Oklahoma",
                       "Kent County, DE" in ok_loc))
        checks.append(("an out-of-state mailing address is LABELLED, not passed "
                       "off as the property location",
                       "Edmond, OK" in ok_loc and "out of state" in ok_loc
                       and "NOT the property" in ok_loc))

        bare = _detail_block({"state": {}})
        checks.append(("no location data says so rather than implying Delaware",
                       "not yet confirmed" in bare))
        checks.append(("no address stored -> no HOA contact line invented",
                       "HOA contact:" not in bare))
        mgr = _detail_block({"state": {"county": "Kent",
                                       "current_mgmt_co": "FirstService Residential",
                                       "property_city": "MAGNOLIA",
                                       "property_state": "DE",
                                       "property_zip": "19962"}})
        checks.append(("manager is labelled separately from location",
                       "Property location: Magnolia, DE 19962 (Kent County)" in mgr
                       and "Managed by:        FirstService Residential" in mgr))
        unk = _detail_block({"state": {"county": "Kent",
                                       "current_mgmt_co": "Unknown"}})
        checks.append(("a placeholder manager prints NO line, not 'Unknown'",
                       "Managed by:" not in unk))
        oos = _detail_block({"state": {"county": "New Castle",
                                       "property_city": "MIDDLETOWN",
                                       "property_state": "DE",
                                       "owners_out_of_state": "10"}})
        checks.append(("out-of-state OWNERS are noted without moving the property",
                       "10 unit owner(s) live out of state" in oos
                       and "the property is in Delaware" in oos))

        checks.append(("a plain Delaware finding is never rejected",
                       _signal_is_out_of_state(
                           "complaint",
                           "Delaware HOA in Kent County with resident litigation.")
                       is False))
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)

    all_ok = all(ok for _, ok in checks)
    for desc, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
    return all_ok


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(0 if selftest() else 1)

    parser = argparse.ArgumentParser()
    parser.add_argument("--client", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--days", type=int, default=None,
                        help="lookback window; overrides the recipient's own setting")
    args = parser.parse_args()
    outcome = run(args.client, dry_run=args.dry_run, days=args.days)
    print(outcome)
    # "nothing to report" is a legitimate no-op, not a failure -- on a DAILY
    # cadence quiet days are normal and common, and exiting non-zero would
    # log a false failure (and alarm the watchdog) most days. Only a real
    # send failure or a bad config is an error.
    quiet = outcome.get("reason", "").startswith("nothing to report")
    good = bool(outcome.get("sent") or args.dry_run or quiet)
    # S81: ledger write, so an overdue/failed run is SEEN. Best-effort; never
    # allowed to change the outcome -- monitoring must not break what it watches.
    # A quiet week is ok=True on purpose: the job RAN. Whether a quiet run is
    # meaningful is completeness.py's question, not this one, and conflating
    # the two is how "ran fine, produced nothing" hid for a month in S67.
    if not args.dry_run:
        try:
            import job_status
            job_status.record(
                "entitykbdigest", good,
                "sent" if outcome.get("sent")
                else (outcome.get("reason") or "no send")[:120])
        except Exception as e:
            print(f"job_status.record failed: {e}")
    sys.exit(0 if good else 1)
