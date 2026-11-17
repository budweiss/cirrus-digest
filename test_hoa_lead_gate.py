"""S635 regression: state procurement documents never reach Bill's HOA report.

The 2026-11-16 report listed "State of Delaware (GSS)" as an HOA opportunity,
from an award notice and a 2014 RFP on bidcondocs.delaware.gov, one summary
reading "garbled binary". Fixture: fixtures/hoa_state_procurement_2026-11-16.json.
Scratch SQLite and temp files only; network and mail are forbidden.
"""
import contextlib
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import entity_kb as kb
import entity_kb_weekly_digest as weekly
import hoa_daily_research as research
import hoa_lead_gate as gate

FIXTURE = json.loads((Path(__file__).resolve().parent /
                      "fixtures/hoa_state_procurement_2026-11-16.json").read_text())
TODAY = FIXTURE["report_date"]
P = research.KB_PROJECT


class Monday(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 16, 5, 0, 0, tzinfo=tz)


class Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = str(self.root / "kb.db")
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for target in ("socket.create_connection", "urllib.request.urlopen",
                       "smtplib.SMTP", "smtplib.SMTP_SSL", "subprocess.run"):
            stack.enter_context(patch(target, side_effect=AssertionError("external call forbidden")))
        stack.enter_context(patch.object(weekly, "_send_mail",
                                         side_effect=AssertionError("send forbidden")))


class GateRules(unittest.TestCase):
    def test_every_live_gss_signal_is_withheld(self):
        name = FIXTURE["kb_entity"]["renamed_to"]
        for s in FIXTURE["report_signals"] + FIXTURE["earlier_signals"]:
            self.assertIsNotNone(gate.withhold_reason(s["source_url"], name, s["signal_kind"],
                                                      s["summary"], today=TODAY), s["source_url"])

    def test_rule1_agency_procurement_by_host_and_by_name(self):
        for s in FIXTURE["report_signals"]:
            self.assertIn("bidcondocs.delaware.gov", gate.agency_procurement_reason(s["source_url"]))
        self.assertIsNotNone(gate.agency_procurement_reason("https://mmp.delaware.gov/Bids/"))
        self.assertIsNotNone(gate.agency_procurement_reason("https://news.example/x", "State of Delaware (GSS)"))
        self.assertIsNotNone(gate.agency_procurement_reason("https://news.example/x", "General Services Section (GSS)"))
        self.assertIsNotNone(gate.agency_procurement_reason("https://news.example/x", "Delaware State Housing Authority"))
        # Not procurement: a court docket and the state's HOA registry are evidence, not bids.
        self.assertIsNone(gate.agency_procurement_reason(
            "https://courtconnect.courts.delaware.gov/cc/cconnect/ck_public_qry_doct.cp_dktrpt_docket_report?case_id=X",
            "Fixture Condominiums"))
        self.assertIsNone(gate.agency_procurement_reason(
            "https://demhra.delaware.gov/registered-communities/", "Fixture Meadows Homeowners Association"))

    def test_rule2_unreadable_summary_is_withheld_on_its_own(self):
        garbled = FIXTURE["report_signals"][1]["summary"]
        self.assertIsNotNone(gate.unreadable_source_reason(garbled))
        # Even from a neutral host about a real-sounding HOA.
        self.assertIsNotNone(gate.withhold_reason("https://news.example/a", "Fixture Meadows",
                                                  "complaint", garbled, today=TODAY))
        self.assertIsNone(gate.unreadable_source_reason("Board accused of corruption by residents."))
        self.assertEqual(gate.readable_snippet(FIXTURE["screen_candidates"][1]["text"]),
                         "(no readable text: binary document)")
        self.assertEqual(gate.readable_snippet("Plain article text."), "Plain article text.")

    def test_rule3_only_open_solicitations_pass(self):
        docs = FIXTURE["document_text"]
        award_url = FIXTURE["report_signals"][1]["source_url"]
        self.assertEqual(gate.solicitation_status(award_url, "", TODAY)[0], "award")
        self.assertEqual(gate.solicitation_status("https://x.example/doc.pdf", docs["award_notice"], TODAY)[0], "award")
        self.assertEqual(gate.solicitation_status("https://x.example/rfp.pdf", docs["rfp_2025"], TODAY),
                         ("closed", "deadline passed 2025-04-22"))
        self.assertEqual(gate.solicitation_status("https://x.example/rfp.pdf", docs["rfp_2014"], TODAY)[0], "closed")
        self.assertEqual(gate.solicitation_status("https://x.example/GSS_ADD1.pdf", "", TODAY)[0], "addendum")
        self.assertEqual(gate.solicitation_status("https://x.example/rfp.pdf", "", TODAY)[0], "unverified")
        c = FIXTURE["controls"]
        self.assertEqual(gate.solicitation_status(c["hoa_rfp_open"]["url"], c["hoa_rfp_open"]["doc_text"], TODAY),
                         ("open", "responses due 2026-12-01"))
        # Without the agency rule, the live RFP summary still fails rule 3 (no deadline).
        self.assertIn("not open", gate.withhold_reason("https://x.example/rfp.pdf", "Fixture Landing", "RFP",
                                                       FIXTURE["report_signals"][0]["summary"], today=TODAY))
        self.assertIsNone(gate.withhold_reason(c["hoa_complaint"]["url"], c["hoa_complaint"]["community"],
                                               "complaint", c["hoa_complaint"]["why"], today=TODAY))


class Discovery(Isolated):
    def fake_monitor(self, cands, rows):
        self.screened_texts = []
        def council(batch, creds):
            self.screened_texts.extend(c["text"] for c in batch)
            return [{**batch[r["idx"]], "type": r["type"], "community": r["community"], "why": r["why"]}
                    for r in rows if r["lead"]]
        return types.SimpleNamespace(
            MAX_CANDIDATES=60, gather_x=lambda: [], gather_web=lambda: [dict(c) for c in cands],
            load_seen=set, save_seen=lambda s: None, _resolve_url=lambda u: u,
            council_filter=council)

    def run_discovery(self, cands, rows, screened=None):
        saved = sys.modules.get("hoa_monitor")
        sys.modules["hoa_monitor"] = self.fake_monitor(cands, rows)
        try:
            return research.run_discovery({}, db_path=self.db, today=TODAY,
                                          screened_path=screened or self.root / "screened.json")
        finally:
            if saved is not None:
                sys.modules["hoa_monitor"] = saved
            else:
                sys.modules.pop("hoa_monitor", None)

    def test_gss_items_never_become_hoa_findings(self):
        ent = FIXTURE["kb_entity"]
        kb.upsert_entity(P, ent["slug"], ent["original_name"], db_path=self.db)
        res = self.run_discovery(FIXTURE["screen_candidates"], FIXTURE["screen_rows"])
        # The screen never sees raw PDF bytes as a snippet.
        self.assertEqual(self.screened_texts[1], "(no readable text: binary document)")
        self.assertEqual(res["council_kept"], 2)
        self.assertEqual(len(res["withheld"]), 2)
        self.assertEqual(res["new_entities"] + res["updated_entities"], [])
        self.assertEqual(kb.get_entity(P, ent["slug"], db_path=self.db)["name"], ent["original_name"])
        self.assertEqual(kb.get_events(P, db_path=self.db), [])
        self.assertEqual(len(kb.list_entities(P, db_path=self.db)), 1)
        # Withheld counts as a rejected screening, so the article rests.
        screened = json.loads((self.root / "screened.json").read_text())
        self.assertEqual(sorted(screened), sorted(c["url"] for c in FIXTURE["screen_candidates"]))

    def test_matched_entity_keeps_its_name(self):
        kb.upsert_entity(P, "fixture-meadows", "Fixture Meadows Homeowners Association", db_path=self.db)
        c = FIXTURE["controls"]["hoa_complaint"]
        res = self.run_discovery([{"source": "web", "url": c["url"], "text": c["why"], "doc_text": ""}],
                                 [{"idx": 0, "lead": True, "type": "complaint",
                                   "community": "Fixture Meadows HOA", "why": c["why"]}])
        self.assertEqual(res["updated_entities"], ["Fixture Meadows HOA"])
        self.assertEqual(kb.get_entity(P, "fixture-meadows", db_path=self.db)["name"],
                         "Fixture Meadows Homeowners Association")
        self.assertEqual(len(kb.get_events(P, "fixture-meadows", db_path=self.db)), 1)

    def discover_name(self, name):
        return self.run_discovery(
            [{"source": "web", "url": "https://fixture.example/complaint", "text": "Board complaints"}],
            [{"idx": 0, "lead": True, "type": "complaint", "community": name,
              "why": "Residents report unresolved grounds maintenance complaints."}])

    def test_shared_words_do_not_attach_signal_to_another_hoa(self):
        for name in ("Fixture Meadows East HOA", "Fixture Meadows Phase 2 HOA",
                     "Fixture Meadows North HOA"):
            with self.subTest(name=name):
                slug = kb.slugify(name)
                kb.upsert_entity(P, slug, name, db_path=self.db)
        res = self.discover_name("Fixture Meadows West HOA")
        self.assertEqual(res["updated_entities"], [])
        self.assertEqual(res["new_entities"], ["Fixture Meadows West HOA"])
        for name in ("Fixture Meadows East HOA", "Fixture Meadows Phase 2 HOA",
                     "Fixture Meadows North HOA"):
            self.assertEqual(kb.get_events(P, kb.slugify(name), db_path=self.db), [])

    def test_duplicate_normalized_names_are_held(self):
        for slug, name in (("first", "Fixture Meadows HOA"),
                           ("second", "Fixture Meadows Homeowners Association, Inc.")):
            kb.upsert_entity(P, slug, name, db_path=self.db)
        res = self.discover_name("Fixture Meadows HOA")
        self.assertEqual(len(res["withheld"]), 1)
        self.assertEqual(kb.get_events(P, db_path=self.db), [])

    def test_occupied_slug_cannot_bypass_identity_check(self):
        kb.upsert_entity(P, "fixture-meadows-hoa", "Different Community HOA", db_path=self.db)
        res = self.discover_name("Fixture Meadows HOA")
        self.assertEqual(len(res["withheld"]), 1)
        self.assertEqual(kb.get_events(P, db_path=self.db), [])
        self.assertEqual(kb.get_entity(P, "fixture-meadows-hoa", db_path=self.db)["name"],
                         "Different Community HOA")

    def test_state_and_delaware_do_not_establish_identity(self):
        kb.upsert_entity(P, "dsha", "Delaware State Housing Authority", db_path=self.db)
        self.assertEqual(research._hoa_write_matches("State of Delaware (GSS)", self.db), [])

    def test_hoa_rfps_pass_only_while_open_and_say_until_when(self):
        c = FIXTURE["controls"]
        items = [c["hoa_rfp_open"], c["hoa_rfp_lapsed"], c["hoa_rfp_no_date"]]
        cands = [{"source": "web", "url": i["url"], "text": i["why"], "doc_text": i["doc_text"]} for i in items]
        rows = [{"idx": n, "lead": True, "type": "RFP", "community": i["community"], "why": i["why"]}
                for n, i in enumerate(items)]
        res = self.run_discovery(cands, rows)
        self.assertEqual([w["url"] for w in res["withheld"]], [items[1]["url"], items[2]["url"]])
        sigs = kb.get_events(P, event_type="signal", db_path=self.db)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["source_url"], items[0]["url"])
        self.assertIn("Open solicitation, responses due 2026-12-01.", sigs[0]["summary"])

    def test_live_candidates_fetch_the_document_for_its_deadline(self):
        # gather_web candidates carry no doc_text; discovery reads the page itself.
        c = FIXTURE["controls"]["hoa_rfp_open"]
        with patch.object(research, "_document_text", return_value=c["doc_text"]) as fetch:
            self.run_discovery([{"source": "web", "url": c["url"], "text": c["why"]}],
                               [{"idx": 0, "lead": True, "type": "RFP", "community": c["community"],
                                 "why": c["why"]}])
        fetch.assert_called_once_with(c["url"])
        self.assertEqual(len(kb.get_events(P, event_type="signal", db_path=self.db)), 1)
        # Agency documents are never fetched at all.
        with patch.object(research, "_document_text", return_value="") as fetch:
            gss = {k: v for k, v in FIXTURE["screen_candidates"][0].items() if k != "doc_text"}
            self.run_discovery([gss], FIXTURE["screen_rows"][:1], screened=self.root / "s2.json")
        fetch.assert_not_called()


class MondayDigest(Isolated):
    def setUp(self):
        super().setUp()
        patcher = patch.object(weekly, "datetime", Monday)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_2026_11_16_report_has_no_gss_item(self):
        ent = FIXTURE["kb_entity"]
        kb.upsert_entity(P, ent["slug"], ent["renamed_to"], entity_type="hoa", db_path=self.db)
        for s in FIXTURE["report_signals"]:
            kb.add_signal(P, ent["slug"], s["signal_kind"], s["summary"], source_url=s["source_url"],
                          confidence="medium", occurred_at=s["occurred_at"], db_path=self.db)
        c = FIXTURE["controls"]
        kb.upsert_entity(P, "fixture-meadows", "Fixture Meadows", fields={"county": "Kent"}, db_path=self.db)
        kb.add_signal(P, "fixture-meadows", "complaint", c["hoa_complaint"]["why"],
                      source_url=c["hoa_complaint"]["url"], occurred_at="2026-11-14 08:00:00", db_path=self.db)
        kb.upsert_entity(P, "fixture-landing", "Fixture Landing", fields={"county": "Sussex"}, db_path=self.db)
        kb.add_signal(P, "fixture-landing", "RFP",
                      "Delaware HOA is seeking bids for snow removal. Open solicitation, responses due 2026-12-01.",
                      source_url=c["hoa_rfp_open"]["url"], occurred_at="2026-11-12 08:00:00", db_path=self.db)
        kb.add_signal(P, "fixture-landing", "RFP",
                      "Delaware HOA is seeking bids for snow removal. Open solicitation, responses due 2026-11-14.",
                      source_url=c["hoa_rfp_lapsed"]["url"], occurred_at="2026-11-10 08:00:00", db_path=self.db)

        body = weekly.compose_digest([P], "2026-11-09 05:00:00", db_path=self.db, opportunities_only=True)
        for banned in ("GSS", "State of Delaware", "garbled", "binary", "RFP document", "bidcondocs"):
            self.assertNotIn(banned, body)
        self.assertIn("Fixture Meadows", body)
        self.assertIn("responses due 2026-12-01", body)
        self.assertNotIn("responses due 2026-11-14", body)
        self.assertIn("3 public-agency bid(s), unreadable document(s) or solicitation(s) not confirmed open", body)
        self.assertIn("(2 communities, 2 findings)", body)


if __name__ == "__main__":
    unittest.main()
