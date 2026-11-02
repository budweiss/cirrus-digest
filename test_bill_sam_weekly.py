"""S554: Bill's weekly SAM.gov summary. Temp files only, no network, no send (T32)."""
import csv
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "samleads"))
import bill_sam_weekly as sam

NOW = datetime(2026, 11, 9, 5, 15)
FIELDS = sorted(sam.REQUIRED)


def row(sol="A1", title="Snow Removal Services", code="SDVOSBC", due="2026-11-20T10:00:00-05:00",
    naics="561730", psc="S218", when="2026-11-05 10:00:00", **kw):
    r = dict.fromkeys(FIELDS, "")
    r.update({"NoticeId": sol + when, "Sol#": sol, "Title": title, "SetASideCode": code,
              "SetASide": "SDVOSB Set-Aside" if code == "SDVOSBC" else "", "ResponseDeadLine": due,
              "NaicsCode": naics, "ClassificationCode": psc, "PostedDate": when,
              "BaseType": "Solicitation", "Office": "VA", "PopCity": "Wilmington", "PopState": "DE",
              "PopCountry": "USA", "Link": "https://sam.gov/opp/x/view",
              "PrimaryContactFullname": "A Person", "PrimaryContactEmail": "a@example.gov"})
    r.update(kw)
    return r


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.file = self.root / "x.csv"
        self.state = self.root / "seen.json"
        self.sent, self.notes = [], []
        self.guard = Mock()
        self.guard.already_sent_today.return_value = None
        self.guard.mark_sent.return_value = True

    def write(self, rows, filler=sam.MIN_ROWS, fields=FIELDS):
        with open(self.file, "w", encoding="cp1252", newline="") as f:
            w = csv.DictWriter(f, fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
            blank = row(sol="Z", title="Office chairs", naics="337214", psc="7110", code="NONE")
            for _ in range(filler):
                w.writerow(blank)

    def go(self, dry=False, sends=True):
        def send(subject, body):
            self.sent.append((subject, body))
            return sends
        return sam.run(self.file, dry=dry, now=NOW, state=self.state, send=send,
                       record=lambda ok, note: self.notes.append((ok, note)), guard=self.guard)


class Matching(unittest.TestCase):
    def test_bills_categories_match(self):
        self.assertEqual(sam.category(row()), "snow/landscaping")
        self.assertEqual(sam.category(row(title="Ballfield care", psc="S208")), "snow/landscaping")
        self.assertEqual(sam.category(row(title="Parking lot snow removal", naics="561210", psc="S216")),
                         "snow/landscaping")
        self.assertEqual(sam.category(row(title="LUMBER AGREEMENT", naics="321113", psc="5510")), "lumber/pallets")
        self.assertEqual(sam.category(row(title="Wooden pallets", naics="", psc="")), "lumber/pallets")

    def test_the_false_positives_from_the_11_02_data_do_not_match(self):
        for title, naics, psc in (("Forklifts, Pallet Jacks, and Pallet Stackers", "423830", "3930"),
                                  ("16--APS PALLET", "336413", "1680"),
                                  ("2420--SNOW BROOM VEHICLE", "333112", "2420"),
                                  ("Snow Survey Services for Idaho", "541990", "F999"),
                                  ("Animal Bedding", "321999", "5510")):
            self.assertIsNone(sam.category(row(title=title, naics=naics, psc=psc)), title)

    def test_eligibility(self):
        self.assertTrue(sam.eligible(row(), NOW))
        self.assertTrue(sam.eligible(row(due=""), NOW), "a sources-sought may have no deadline")
        self.assertFalse(sam.eligible(row(due="2026-11-01T10:00:00-05:00"), NOW))
        self.assertFalse(sam.eligible(row(code="8A"), NOW))
        self.assertFalse(sam.eligible(row(code='["WOSB"]'), NOW))
        self.assertFalse(sam.eligible(row(PopCountry="COL"), NOW), "Bogota is not nationwide")
        self.assertFalse(sam.eligible(row(BaseType="Award Notice"), NOW))
        self.assertFalse(sam.eligible(row(AwardNumber="123"), NOW))
        self.assertFalse(sam.eligible(row(title="Notice of Intent to Award a BPA Call Order"), NOW))


class Weekly(Base):
    def test_amendments_of_one_solicitation_are_one_item_with_the_newest_deadline(self):
        self.write([row(due="2026-11-02T10:00:00-05:00", when="2026-10-15 10:00:00"),
                    row(sol="A1_1", due="2026-11-20T10:00:00-05:00", when="2026-11-05 10:00:00"),
                    row(sol="RENUMBERED", when="2026-11-06 10:00:00", due="2026-11-21T10:00:00-05:00")])
        self.assertEqual(self.go(), 0)
        subject, body = self.sent[0]
        self.assertIn("1 new this week, 1 SDVOSB", subject)
        self.assertEqual(body.count("Snow Removal Services"), 1)
        self.assertIn("Nov 21", body)

    def test_a_seen_notice_never_returns_even_if_amended(self):
        self.write([row()])
        self.go()
        self.write([row(sol="A1_2", when="2026-11-06 10:00:00")])
        self.guard.reset_mock()
        self.guard.already_sent_today.return_value = None
        self.go()
        self.assertIn("nothing new this week", self.sent[1][0])
        self.assertEqual(self.notes[1][0], True)
        self.assertIn("quiet week sent", self.notes[1][1])

    def test_sdvosb_is_separated_and_the_mail_is_honest_about_its_limits(self):
        self.write([row(), row(sol="B2", title="Mowing Services", code="SBA", psc="S208")])
        self.go()
        body = self.sent[0][1]
        self.assertLess(body.index("SDVOSB and veteran-owned set-asides (1)"), body.index("Small business set-asides"))
        self.assertIn("confirm each one on the notice", body)
        self.assertIn("No bid has been submitted", body)
        self.assertIn("-- CUMULUS", body)

    def test_next_seasons_contract_alerts_while_last_years_row_never_returns(self):
        old = row(sol="OLD26", when="2026-10-01 10:00:00", due="2027-03-30T10:00:00-05:00")
        self.write([old])
        self.go()
        later = datetime(2027, 10, 11, 5, 15)     # a year on, the old row is still in the file
        fresh_file = row(sol="NEW27", when="2027-10-08 10:00:00", due="2027-10-30T10:00:00-05:00")
        self.write([old, fresh_file])
        sent = []
        sam.run(self.file, now=later, state=self.state, send=lambda s, b: sent.append((s, b)) or True,
                record=lambda ok, note: None, guard=self.guard)
        self.assertIn("1 new", sent[0][0])
        self.assertIn("Oct 30, 2027", sent[0][1])
        self.assertEqual(sent[0][1].count("Snow Removal Services"), 1)

    def test_links_are_public_and_a_numeric_city_is_dropped(self):
        self.write([row(PopCity="0", PopState="NE", Link="https://sam.gov/workspace/contract/opp/abc/view")])
        self.go()
        self.assertIn("https://sam.gov/opp/abc/view", self.sent[0][1])
        self.assertNotIn("workspace", self.sent[0][1])
        self.assertIn("  NE | Deadline", self.sent[0][1])

    def test_a_tiered_notice_is_flagged(self):
        self.write([row(Description="Tiered evaluation: SDVOSB first, then others")])
        self.go()
        self.assertIn("tiered competition", self.sent[0][1])

    def test_dry_run_sends_and_saves_nothing(self):
        self.write([row()])
        self.assertEqual(self.go(dry=True), 0)
        self.assertEqual((self.sent, self.notes), ([], []))
        self.assertFalse(self.state.exists())
        self.guard.mark_sent.assert_not_called()

    def test_a_failed_send_keeps_the_notice_new_and_records_failure(self):
        self.write([row()])
        self.assertEqual(self.go(sends=False), 1)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.notes[0][0])
        self.guard.mark_sent.assert_not_called()

    def test_the_guard_stops_a_second_send_the_same_day(self):
        self.write([row()])
        self.guard.already_sent_today.return_value = {"at": "x"}
        self.assertEqual(self.go(), 0)
        self.assertEqual(self.sent, [])

    def test_seed_marks_only_notices_through_the_date(self):
        self.write([row(when="2026-11-01 10:00:00"),
                    row(sol="B2", title="Mowing Services", when="2026-11-02 10:00:00")])
        self.assertEqual(sam.seed(self.file, "2026-11-01", now=NOW, state=self.state), 1)
        self.go()
        self.assertIn("1 new", self.sent[0][0])
        self.assertIn("Mowing", self.sent[0][1])
        self.assertNotIn("Snow Removal", self.sent[0][1])


class BadData(Base):
    def assertFailsWithoutMail(self, phrase):
        self.assertEqual(self.go(), 1)
        self.assertEqual(self.sent, [], "a bad file must never become 'nothing new'")
        self.assertFalse(self.notes[0][0])
        self.assertIn(phrase, self.notes[0][1])

    def test_a_truncated_file(self):
        self.write([row()], filler=10)
        self.assertFailsWithoutMail("only")

    def test_a_stale_file(self):
        old = row(sol="Z", title="Office chairs", naics="337214", psc="7110", when="2026-10-01 10:00:00")
        with open(self.file, "w", encoding="cp1252", newline="") as f:
            w = csv.DictWriter(f, FIELDS)
            w.writeheader()
            for _ in range(sam.MIN_ROWS):
                w.writerow(old)
        self.assertFailsWithoutMail("stale")

    def test_a_changed_layout(self):
        self.write([row()], fields=[f for f in FIELDS if f != "SetASideCode"])
        self.assertFailsWithoutMail("changed shape")


class State(Base):
    def test_old_entries_expire_so_next_seasons_contract_alerts_again(self):
        sam.save_seen({"old": "2026-01-01", "new": "2026-11-01"}, NOW, self.state)
        self.assertEqual(json.loads(self.state.read_text()), {"new": "2026-11-01"})

    def test_every_note_the_job_writes_is_listed(self):
        for _, note, expect in sam.note_samples():
            self.assertTrue(note.startswith(("sent ", "quiet week sent", "failed:")), note)
            self.assertIn(expect, ("productive", "zero", "blind"))


if __name__ == "__main__":
    unittest.main()
