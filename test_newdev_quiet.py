"""S537: the quiet-week leads note says why it is quiet and what is newest."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "newdev"))
import bill_newdev_weekly as N  # noqa: E402


class QuietNote(unittest.TestCase):
    counts = {"plus": 547, "dev": 249, "permit": 33, "newest_year": "2024",
              "latest_plus": ("2026-10-02", 227, "Sussex County")}

    def test_says_why_quiet_and_names_the_newest_project(self):
        body = N.compose_quiet("swept", self.counts)
        self.assertIn("newest data is for 2024", body)
        self.assertIn("monthly PLUS planning reviews", body)
        self.assertIn("PLUS 2026-10-02, 227 units in Sussex County", body)
        self.assertIn("Nothing new this week.", body)

    def test_unreadable_counts_still_send_a_plain_note(self):
        body = N.compose_quiet("swept", {})
        self.assertIn("roughly once a month", body)
        self.assertNotIn("PLUS 20", body)

    def test_no_internal_jargon_reaches_bill(self):
        body = N.compose_quiet("swept 547 plus, 249 dev-app, 33 permit", self.counts)
        self.assertNotIn("dev-app", body)
        self.assertNotIn("swept", body)


if __name__ == "__main__":
    unittest.main()
