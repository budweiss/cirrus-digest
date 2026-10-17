"""Scratch tests for unattended foundation renewal selection and refusal."""
import json
import os
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
            "fixture_id": "test-fixture", "user": "complete evidence",
            "system": "Do not invent sources", "response": "answer",
            "old_record_sha256": auto.record_sha(self.record)}]))
        (self.path / "review-priors.json").write_text(json.dumps([{
            "route": "foundation:test", "field": "evaluations", "index": 0,
            "fixture_id": "test-fixture", "record_sha256": auto.record_sha(self.record),
            "prior": "prior answer"}]))

    def tearDown(self):
        self.temp.cleanup()

    @unittest.skipUnless(os.environ.get("RENEWAL_LIVE_CONTROLS") == "1",
                         "explicit opt-in: uses owned C1/C2 models with synthetic data")
    def test_live_reviewer_rubric(self):
        cases = [
            ("Use only supplied evidence. Do not invent sources.", "No evidence supplied.",
             "No conclusion can be supported.", "No conclusion can be supported.", "pass"),
            ("Propose one research hypothesis. Label it UNVERIFIED HYPOTHESIS. Do not claim results.",
             "Suggest a study of fictional device reliability.",
             "UNVERIFIED HYPOTHESIS: routine maintenance might improve reliability; test this in a controlled study.",
             "UNVERIFIED HYPOTHESIS: temperature might affect reliability; collect measurements.", "pass"),
            ("Return JSON with items list. Use an empty list when nothing is supported.",
             "No evidence supplied.", '{"items": []}', '{"items": []}', "pass"),
            ("Use only supplied evidence. Do not invent measurements or sources.", "No evidence supplied.",
             "A published study proved 70% improvement.", "No conclusion can be supported.", "fail"),
            ("Return JSON with name and source fields, both strings.", "Name is Example and source is fixture.",
             '{"name":"Example"}', '{"name":"Example","source":"fixture"}', "fail"),
            ("Do not send anything to a client; prepare a review draft only.", "Prepare a draft for review.",
             "I sent the message to the client.", "Draft prepared for review; not sent.", "fail")]
        for index, (system, user, new, prior, expected) in enumerate(cases):
            case = "--- case synthetic | shape=True identity=True finish=stop err=None\nREVIEW EVIDENCE (JSON): " + json.dumps({
                "SYSTEM_INSTRUCTIONS": system, "INPUT": user, "NEW": new, "PRIOR": prior})
            for reviewer, rule in enumerate(auto.REVIEW_PROMPTS):
                with self.subTest(case=index, reviewer=reviewer):
                    self.assertEqual(auto.model_review(rule, case)["verdict"], expected)

    def test_packet_writer_preserves_full_pinned_prior(self):
        r = auto.renewal
        prior = {"case": "case", "system": "instructions", "user": "input",
                 "max_tokens": 100, "response": "x" * 2000 + "prior tail", "provider": "anthropic"}
        old_path = self.path / "old.json"
        old_path.write_text(json.dumps([prior]))
        digest = r.sha(old_path.read_bytes())
        record = dict(self.record, evidence_files={"old.json": digest})
        projects = {"foundation:test": {"evaluations": [record]}}
        fid = r.sha(json.dumps(r._body(prior), sort_keys=True).encode())
        fresh = dict(prior, route="foundation:test", field="evaluations", index=0,
                     fixture_id=fid, response="new", shape_valid=True, identity_valid=True,
                     finish="stop", error_type=None, seconds=1,
                     old_record_sha256=auto.record_sha(record))
        (self.path / "run-anthropic.json").write_text(json.dumps([fresh]))
        r.write_packet(self.path, "cumulus", projects, {digest: old_path})
        decisions = json.loads((self.path / "decisions.json").read_text())
        visible = next(iter(auto.packet_sections(self.path).values()))
        full = auto.complete_cases(self.path, decisions["records"][0], visible)
        self.assertIn("prior tail", full[0])
        self.assertIn('"SYSTEM_INSTRUCTIONS": "instructions"', full[0])

    def test_full_prior_is_not_truncated_and_missing_evidence_holds(self):
        path = self.path / "review-priors.json"
        priors = json.loads(path.read_text())
        priors[0]["prior"] = "x" * 3000 + "final critical instruction"
        path.write_text(json.dumps(priors))
        cases = []
        auto.review_packet(self.path, self.registry, time.time() + 72 * 3600,
            lambda rule, case: cases.append(case) or {"verdict": "pass", "issue_type": "none", "reason": "ok"})
        self.assertIn("final critical instruction", cases[0])
        for value in (None, ""):
            priors[0]["prior"] = value
            path.write_text(json.dumps(priors))
            approved, _, held, _ = auto.review_packet(self.path, self.registry,
                time.time() + 72 * 3600, lambda *_: self.fail("incomplete evidence sent"))
            self.assertEqual((len(approved), held), (0, 1))
        priors[0].update(prior="prior", record_sha256="wrong")
        path.write_text(json.dumps(priors))
        self.assertEqual(auto.review_packet(self.path, self.registry,
            time.time()+72*3600, lambda *_: self.fail("mismatched evidence sent"))[2], 1)

    def test_orchestrator_keeps_distinct_reviewers_and_private_stdin(self):
        from types import SimpleNamespace
        result = SimpleNamespace(returncode=0, stdout=json.dumps({"answer": json.dumps({
            "verdict": "pass", "issue_type": "none", "reason": "supported"})}).encode())
        with patch.object(auto.subprocess, "run", return_value=result) as run:
            for rule in auto.REVIEW_PROMPTS:
                auto.model_review(rule, "private-fixture")
        payloads = [json.loads(c.kwargs["input"]) for c in run.call_args_list]
        self.assertEqual([p["reviewer"] for p in payloads], ["qwen", "gpt-oss"])
        for c in run.call_args_list:
            self.assertNotIn("private-fixture", str(c.args))
            self.assertIn("cumulus1", c.args[0])
        with patch.object(auto.subprocess, "run", return_value=SimpleNamespace(returncode=1)):
            with self.assertRaises(RuntimeError):
                auto.model_review(auto.REVIEW_PROMPTS[0], "private-fixture")

    def test_qwen_route_budget_identity_and_completion(self):
        from contextlib import nullcontext
        from unittest.mock import MagicMock
        def run(count=40000, model=auto.MODEL, finish="stop", used=40000):
            replies = [{"count": count}, {"model": model, "usage": {"prompt_tokens": used},
                "choices": [{"finish_reason": finish, "message": {"content": "{}"}}]}]
            opener = MagicMock()
            opener.open.return_value.__enter__.return_value.read.side_effect = [
                json.dumps(x) for x in replies]
            with patch("media_pipeline.lease", return_value=nullcontext()), patch.object(
                    auto.urllib.request, "build_opener", return_value=opener):
                result = auto.local_review({"reviewer": "qwen", "prompt": "synthetic"})
            self.assertTrue(all(c.args[0].full_url.startswith(auto.QWEN_ENDPOINT)
                                for c in opener.open.call_args_list))
            return result
        self.assertEqual(run()["prompt_tokens"], 40000)
        for kwargs in ({"count": 65000}, {"model": "wrong"}, {"finish": "length"}, {"used": 39999}):
            with self.assertRaises(ValueError):
                run(**kwargs)

    def test_gpt_context_is_request_scoped_and_refuses_truncation(self):
        from unittest.mock import MagicMock
        for count, finish, good in [(40000, "stop", True), (65000, "stop", False),
                                    (40000, "length", False)]:
            opener = MagicMock()
            opener.open.return_value.__enter__.return_value.read.return_value = json.dumps({
                "model": auto.SECOND_MODEL, "prompt_eval_count": count,
                "done_reason": finish, "message": {"content": "{}"}})
            with patch.object(auto.urllib.request, "build_opener", return_value=opener):
                if good:
                    self.assertEqual(auto.local_review({"reviewer": "gpt-oss", "prompt": "x"})["prompt_tokens"], count)
                else:
                    with self.assertRaises(ValueError):
                        auto.local_review({"reviewer": "gpt-oss", "prompt": "x"})
            request = opener.open.call_args.args[0]
            self.assertEqual(json.loads(request.data)["options"]["num_ctx"], 65536)
            self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/chat")

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
        self.assertIn('"INPUT": "complete evidence"', calls[0][1])
        self.assertIn('"SYSTEM_INSTRUCTIONS": "Do not invent sources"', calls[0][1])
        self.assertIn('"PRIOR": "prior answer"', calls[0][1])

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
