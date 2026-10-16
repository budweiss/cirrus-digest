"""Scratch tests for unattended foundation renewal selection and refusal."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import foundation_renewal_auto as auto


class RenewalAutoTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.record = {"id": "anthropic", "approved": True,
                       "expires_at": time.time() + 3600}
        self.registry = {"projects": {"foundation:test": {"evaluations": [self.record]}}}
        (self.path / "decisions.json").write_text(json.dumps({
            "host": "cumulus", "records": [{"route": "foundation:test",
                "field": "evaluations", "index": 0, "provider": "anthropic",
                "approved": None, "rationale": "", "reviewed_fixtures": ["test-fixture"],
                "evidence_file": "run-anthropic.json"}], "hoa": []}))
        (self.path / "manifest-cumulus.json").write_text(json.dumps([{
            "route": "foundation:test", "field": "evaluations", "index": 0,
            "fixture_id": "test-fixture", "old_record_sha256": auto.record_sha(self.record)}]))
        (self.path / "PACKET.txt").write_text(
            "# foundation:test  evaluations[0]  anthropic / model\n"
            "--- case synthetic | shape=True identity=True finish=end_turn err=None\n"
            "INPUT: evidence\nNEW: answer\nPRIOR: answer\n")
        (self.path / "run-anthropic.json").write_text(json.dumps([{
            "route": "foundation:test", "field": "evaluations", "index": 0,
            "fixture_id": "test-fixture", "user": "complete evidence"}]))

    def tearDown(self):
        self.temp.cleanup()

    def test_clear_pass_is_selected_with_two_reviews(self):
        calls = []
        def reviewer(rule, case):
            calls.append((rule, case))
            return {"verdict": "pass", "issue_type": "none", "reason": "supported"}
        approved, hoa, held, details = auto.review_packet(
            self.path, self.registry, time.time() + 72 * 3600, reviewer)
        self.assertEqual((len(approved), len(hoa), held, len(details), len(calls)),
                         (1, 0, 0, 1, 2))
        self.assertTrue(approved[0]["approved"])
        self.assertTrue(approved[0]["rationale"])
        self.assertIn("INPUT (complete): complete evidence", calls[0][1])

    def test_held_on_any_review_failure(self):
        verdicts = iter(("pass", "fail"))
        def reviewer(_rule, _case):
            return {"verdict": next(verdicts), "issue_type": "other", "reason": "regression"}
        approved, _, held, _ = auto.review_packet(
            self.path, self.registry, time.time() + 72 * 3600, reviewer)
        self.assertEqual((len(approved), held), (0, 1))

    def test_reviewer_timeout_holds_record(self):
        def reviewer(_rule, _case):
            raise TimeoutError("local model unavailable")
        approved, _, held, details = auto.review_packet(
            self.path, self.registry, time.time() + 72 * 3600, reviewer)
        self.assertEqual((len(approved), held), (0, 1))
        self.assertEqual(details[0]["verdicts"], ["unsure"])

    def test_changed_record_is_not_reapproved_from_old_packet(self):
        self.record["expires_at"] += 100
        approved, _, held, _ = auto.review_packet(
            self.path, self.registry, time.time() + 72 * 3600,
            lambda *_: self.fail("reviewer should not run"))
        self.assertEqual((len(approved), held), (0, 0))

    def test_distant_expiry_is_not_reviewed(self):
        approved, _, held, _ = auto.review_packet(
            self.path, self.registry, time.time() + 10,
            lambda *_: self.fail("reviewer should not run"))
        self.assertEqual((len(approved), held), (0, 0))

    def test_missing_packet_uses_builder_live_expiry(self):
        with patch.object(auto, "command", return_value=json.dumps({
            "earliest_expiry": "2026-10-20T10:00"})):
            cutoff = auto.datetime(2026, 10, 20, 11, 0, tzinfo=auto.ZoneInfo(
                "America/New_York")).timestamp()
            self.assertTrue(auto.has_due_records("cumulus", cutoff))
            self.assertFalse(auto.has_due_records("cirrus", cutoff - 7200))
        with patch.object(auto, "command", return_value='{"earliest_expiry": null}'):
            self.assertFalse(auto.has_due_records("cumulus", cutoff))

    def test_hoa_retention_skips_already_changed_record(self):
        hoa_record = {"approved": True, "expires_at": time.time() + 3600}
        self.registry["projects"]["hoa_screening"] = {"evaluations": [hoa_record]}
        doc = json.loads((self.path / "decisions.json").read_text())
        doc["hoa"] = [{"index": 0, "approved": None, "rationale": ""}]
        (self.path / "decisions.json").write_text(json.dumps(doc))
        (self.path / "hoa-retention.json").write_text(json.dumps([{
            "index": 0, "passed": True,
            "old_record_sha256": auto.record_sha(hoa_record)}]))
        _, hoa, _, _ = auto.review_packet(
            self.path, self.registry, time.time() + 72 * 3600,
            lambda *_: {"verdict": "pass", "issue_type": "none", "reason": "supported"})
        self.assertEqual(len(hoa), 1)
        hoa_record["expires_at"] += 100
        _, hoa, _, _ = auto.review_packet(
            self.path, self.registry, time.time() + 72 * 3600,
            lambda *_: {"verdict": "pass", "issue_type": "none", "reason": "supported"})
        self.assertEqual(len(hoa), 0)


if __name__ == "__main__":
    unittest.main()
