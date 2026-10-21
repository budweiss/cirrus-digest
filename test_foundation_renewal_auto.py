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
    def test_idle_local_model_is_loaded_then_rechecked(self):
        import io
        import capability_health as health
        creds={'ollama_url':'http://localhost:11434/api/chat','ollama_model':'fixture-model'}
        cold={'status':'not_resident','healthy':False}
        warm={'status':'ready_metadata','healthy':True}
        with patch.object(health,'observe',side_effect=[cold,warm]) as observe, \
             patch('urllib.request.urlopen',return_value=io.BytesIO(b'{"model":"fixture-model","done":true}')) as load:
            self.assertEqual(auto.renewal.renewal_health(creds,'ollama'),warm)
            self.assertEqual(observe.call_count,2)
            body=json.loads(load.call_args.args[0].data)
            self.assertEqual(body,{'model':'fixture-model','stream':False})
        with patch.object(health,'observe',return_value={'status':'observation_failed','healthy':False}), \
             patch('urllib.request.urlopen') as load:
            self.assertFalse(auto.renewal.renewal_health(creds,'ollama')['healthy'])
            load.assert_not_called()
        with patch.object(health,'observe',return_value=cold), \
             patch('urllib.request.urlopen',return_value=io.BytesIO(b'{"model":"wrong","done":true}')):
            with self.assertRaises(RuntimeError):auto.renewal.renewal_health(creds,'ollama')

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
             "I sent the message to the client.", "Draft prepared for review; not sent.", "fail"),
            ("Extract the performing act's name. Return a JSON array with artist.",
             "Tonight's headliner: Fictional Lantern Quartet.",
             '[{"artist":"Fictional Lantern Quartet"}]',
             '[{"artist":"Tonight’s headliner: Fictional Lantern Quartet"}]', "pass"),
            ("Extract all named acts from the sources. Return a JSON array with name.",
             "Source one: Fictional Lantern Quartet. Source two: Fictional Harbor Duo.",
             '[{"name":"Fictional Lantern Quartet"}]',
             '[{"name":"Fictional Lantern Quartet"},{"name":"Fictional Harbor Duo"}]', "fail"),
            ("Extract only confirmed shows. Exclude cancelled shows.",
             "CANCELLED: Fictional Lantern Quartet, 2026-10-20.",
             '[{"artist":"Fictional Lantern Quartet","date":"2026-10-20"}]', '[]', "fail")]
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
        envelope = json.loads(full[0].split("REVIEW EVIDENCE (JSON): ", 1)[1])
        self.assertLess(list(envelope).index("PRIOR"), list(envelope).index("NEW"))
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
            self.assertEqual(json.loads(request.data)["options"]["num_predict"], 4096)
            self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/chat")

    def test_events_review_removes_only_a_complete_accepted_json_fence(self):
        item = json.loads((self.path / "decisions.json").read_text())["records"][0]
        item["route"] = "foundation:halftime:events"
        rows = json.loads((self.path / "run-anthropic.json").read_text())
        priors = json.loads((self.path / "review-priors.json").read_text())
        rows[0]["route"] = priors[0]["route"] = item["route"]
        (self.path / "review-priors.json").write_text(json.dumps(priors))
        body = '[{"artist":"Fictional Quartet","date":"2026-10-20","extra":"kept"}]'
        for raw, expected in [("```json\n" + body + "\n```", body),
                              ("I sent it.\n```json\n" + body + "\n```", None),
                              ("```json\nnot JSON\n```", None)]:
            rows[0]["response"] = raw
            (self.path / "run-anthropic.json").write_text(json.dumps(rows))
            case = auto.complete_cases(self.path, item, ["--- case synthetic"])[0]
            evidence = json.loads(case.split("REVIEW EVIDENCE (JSON): ", 1)[1])
            self.assertEqual(evidence["NEW"], raw if expected is None else expected)
            self.assertEqual("TRANSPORT_PRESENTATION" in evidence, expected is not None)
            self.assertEqual(json.loads((self.path / "run-anthropic.json").read_text())[0]["response"], raw)

    def test_authored_event_oracle_is_bound_to_exact_fixture_and_checks_dates(self):
        from halftime_routing import _EXTRACT_SYSTEM
        item = json.loads((self.path / "decisions.json").read_text())["records"][0]
        item["route"] = "foundation:halftime:events"
        row = json.loads((self.path / "run-anthropic.json").read_text())[0]
        row.update(route=item["route"], case="stated", system=_EXTRACT_SYSTEM,
                   user="LISTINGS:\n\nFictional The Example Quartet — jazz — 2026-11-05, Example Hall, Newark NJ.",
                   max_tokens=8000)
        fid = auto.record_sha(auto.renewal._body(row))
        self.assertIn(fid, auto.EVENT_PREFIX_FIXTURES)
        row["fixture_id"] = fid
        item["reviewed_fixtures"] = [fid]
        prior = json.loads((self.path / "review-priors.json").read_text())[0]
        prior.update(route=item["route"], fixture_id=fid)
        (self.path / "review-priors.json").write_text(json.dumps([prior]))
        for artist, date, good in [("The Example Quartet", "2026-11-05", True),
                                   ("Fictional The Example Quartet", "2026-11-05", True),
                                   ("Different Quartet", "2026-11-05", False),
                                   ("The Example Quartet", "2026-11-06", False)]:
            row["response"] = json.dumps([{"artist": artist, "date": date}])
            (self.path / "run-anthropic.json").write_text(json.dumps([row]))
            case = auto.complete_cases(self.path, item, ["--- case synthetic"])[0]
            evidence = json.loads(case.split("REVIEW EVIDENCE (JSON): ", 1)[1])
            self.assertEqual(evidence["AUTHORED_FIXTURE_CHECK"]["name_date_passed"], good)
        row["user"] += " Changed fixture."
        (self.path / "run-anthropic.json").write_text(json.dumps([row]))
        self.assertNotIn("AUTHORED_FIXTURE_CHECK",
                         auto.complete_cases(self.path, item, ["--- case synthetic"])[0])

    @unittest.skipUnless(os.environ.get("RENEWAL_LIVE_CONTROLS") == "1", "owned models only")
    def test_live_authored_oracle_does_not_excuse_other_defects(self):
        note = {"name_date_passed": True, "rule": "The original fixture permits removing "
                "the synthetic annotation Fictional from the artist name. Name/date only; "
                "independently check other fields and source fidelity."}
        for style, expected in [("jazz", "pass"), ("metal", "fail")]:
            evidence = {"SYSTEM_INSTRUCTIONS": "Extract artist, date and style as JSON. Style must "
                        "match the source. Do not invent facts.",
                        "INPUT": "Fictional The Example Quartet — jazz — 2026-11-05.",
                        "NEW": json.dumps([{"artist": "The Example Quartet", "date": "2026-11-05", "style": style}]),
                        "PRIOR": '[{"artist":"Fictional The Example Quartet","date":"2026-11-05","style":"jazz"}]',
                        "AUTHORED_FIXTURE_CHECK": note}
            case = "--- synthetic\nREVIEW EVIDENCE (JSON): " + json.dumps(evidence)
            for rule in auto.REVIEW_PROMPTS:
                self.assertEqual(auto.model_review(rule, case)["verdict"], expected)

    def test_response_syntax_distinguishes_array_from_json_string(self):
        item = json.loads((self.path / "decisions.json").read_text())["records"][0]
        rows = json.loads((self.path / "run-anthropic.json").read_text())
        for raw, kind in [('[]', 'list'), ('"[]"', 'str'), ('not json', 'not a standalone JSON document')]:
            rows[0]["response"] = raw
            (self.path / "run-anthropic.json").write_text(json.dumps(rows))
            case = auto.complete_cases(self.path, item, ["--- case synthetic"])[0]
            e = json.loads(case.split("REVIEW EVIDENCE (JSON): ", 1)[1])
            self.assertEqual(e["RESPONSE_JSON_TYPE"], kind)
            self.assertEqual(e["NEW"], raw)

    @unittest.skipUnless(os.environ.get("RENEWAL_LIVE_CONTROLS") == "1", "owned models only")
    def test_live_response_envelope_does_not_hide_invalid_json_type(self):
        for raw, kind, expected in [('[]', 'list', 'pass'), ('"[]"', 'str', 'fail')]:
            e = {"SYSTEM_INSTRUCTIONS": "Return only a JSON array. When no events are supplied, return [].",
                 "INPUT": "No events supplied.", "NEW": raw, "PRIOR": "[]", "RESPONSE_JSON_TYPE": kind}
            case = "--- synthetic\nREVIEW EVIDENCE (JSON): " + json.dumps(e)
            for rule in auto.REVIEW_PROMPTS:
                self.assertEqual(auto.model_review(rule, case)["verdict"], expected)

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

    def test_negative_vote_requests_evidence_not_confirmed_hold(self):
        verdicts = iter(("pass", "fail"))
        def reviewer(_rule, _case):
            return {"verdict": next(verdicts), "issue_type": "other", "reason": "regression"}
        approved, _, held, _ = auto.review_packet(
            self.path, self.registry, time.time() + 72 * 3600, reviewer)
        self.assertEqual((len(approved), held), (0, 0))

    def test_reviewer_timeout_holds_record(self):
        def reviewer(_rule, _case):
            raise TimeoutError("local model unavailable")
        approved, _, held, details = auto.review_packet(
            self.path, self.registry, time.time() + 72 * 3600, reviewer)
        self.assertEqual((len(approved), held), (0, 0))
        self.assertEqual(details[0]["verdicts"], ["unsure", "unsure"])
        self.assertEqual(details[0]["status"], "evidence_review_pending")

    def test_preflight_rejects_always_negative_or_positive_reviewer(self):
        for vote in ("pass", "fail", "unsure"):
            self.assertFalse(auto.review_controls(lambda *_: {"verdict": vote}))
        self.assertTrue(auto.review_controls(lambda _, case: {
            "verdict": "fail" if case in (auto.CONTROL, auto.NEW_DEFECT_CONTROL) else "pass"}))

    def test_controls_detect_confusing_prior_with_new(self):
        def confused(_rule, case):
            return {"verdict": "fail" if case in (
                auto.CONTROL, auto.PRIOR_ONLY_DEFECT_CONTROL) else "pass"}
        self.assertFalse(auto.review_controls(confused))
        seen = []
        def correct(rule, case):
            seen.append((rule, case))
            return {"verdict": "fail" if case in (
                auto.CONTROL, auto.NEW_DEFECT_CONTROL) else "pass"}
        self.assertTrue(auto.review_controls(correct))
        self.assertEqual(len(seen), 8)

    def evidence_row(self):
        key = ["foundation:test", "evaluations", 0]
        item = json.loads((self.path / "decisions.json").read_text())["records"][0]
        cases = auto.complete_cases(self.path, item, next(iter(auto.packet_sections(self.path).values())))
        return {"record": key, "authority": "operator-evidence-review", "reviewer": "test-operator",
                "record_sha256": auto.record_sha(self.record),
                "case_sha256": [auto.hashlib.sha256(c.encode()).hexdigest() for c in cases],
                "assessment": "clear", "rationale": "Compared the entire output against instructions and evidence.",
                "all_requirements_checked": True,
                "checks": [{"SYSTEM_INSTRUCTIONS": "Do not invent sources", "INPUT": "complete evidence",
                            "NEW": "answer", "explanation": "Synthetic test assessment."}]}

    def negative_review(self, *_):
        return {"verdict": "fail", "issue_type": "other", "reason": "claim"}

    def assess(self, row):
        (self.path / "independent-evidence.json").write_text(json.dumps([row]))
        return auto.review_packet(self.path, self.registry, time.time()+72*3600, self.negative_review)

    def test_independent_clearance_overrides_two_negative_votes(self):
        before = json.dumps(self.registry, sort_keys=True)
        approved, _, held, details = self.assess(self.evidence_row())
        self.assertEqual((len(approved), held), (1, 0))
        self.assertEqual(details[0]["verdicts"], ["fail", "fail"])
        self.assertEqual(details[0]["status"], "renewable")
        self.assertEqual(json.dumps(self.registry, sort_keys=True), before)

    def test_independent_confirmed_defect_holds(self):
        row = self.evidence_row(); row["assessment"] = "confirmed_defect"
        approved, _, held, details = self.assess(row)
        self.assertEqual((len(approved), held), (0, 1))
        self.assertEqual(details[0]["status"], "confirmed_defect")

    def test_positive_votes_cannot_override_confirmed_evidence(self):
        row = self.evidence_row(); row["assessment"] = "confirmed_defect"
        self.assess(row)
        approved, _, held, details = auto.review_packet(
            self.path, self.registry, time.time()+72*3600,
            lambda *_: {"verdict": "pass", "issue_type": "none", "reason": "ok"})
        self.assertEqual((len(approved), held), (0, 1))
        self.assertEqual(details[0]["status"], "confirmed_defect")

    def legacy_assessment(self):
        item = json.loads((self.path / "decisions.json").read_text())["records"][0]
        cases = auto.complete_cases(self.path, item, next(iter(auto.packet_sections(self.path).values())))
        # Independently reproduce the pre-S479 serialized envelope, not the helper.
        old_cases = []
        for case in cases:
            prefix, body = case.split("REVIEW EVIDENCE (JSON): ", 1)
            e = json.loads(body)
            old = {"SYSTEM_INSTRUCTIONS": e["SYSTEM_INSTRUCTIONS"], "INPUT": e["INPUT"],
                   "NEW": e["NEW"], "PRIOR": e["PRIOR"]}
            old.update((k, v) for k, v in e.items() if k not in old)
            old_cases.append(prefix + "REVIEW EVIDENCE (JSON): " + json.dumps(old))
        row = self.evidence_row()
        row["case_sha256"] = [auto.hashlib.sha256(c.encode()).hexdigest() for c in old_cases]
        self.assertNotEqual(row["case_sha256"], auto.case_hashes(cases))
        return row, cases

    def test_legacy_hold_survives_presentation_order_with_positive_votes(self):
        row, _ = self.legacy_assessment(); row["assessment"] = "confirmed_defect"
        self.assess(row)
        approved, _, held, details = auto.review_packet(
            self.path, self.registry, time.time()+72*3600,
            lambda *_: {"verdict": "pass", "issue_type": "none", "reason": "ok"})
        self.assertEqual((len(approved), held), (0, 1))
        self.assertEqual(details[0]["status"], "confirmed_defect")

    def test_legacy_clearance_retains_existing_operator_policy(self):
        row, _ = self.legacy_assessment()
        approved, _, held, _ = self.assess(row)
        self.assertEqual((len(approved), held), (1, 0))

    def test_legacy_binding_rejects_changed_content_and_mechanics(self):
        row, cases = self.legacy_assessment(); self.assess(row)
        key = ("foundation:test", "evaluations", 0)
        prefix, body = cases[0].split("REVIEW EVIDENCE (JSON): ", 1)
        for field in ("SYSTEM_INSTRUCTIONS", "INPUT", "NEW", "PRIOR", "extra"):
            with self.subTest(field=field):
                e = json.loads(body); e[field] = str(e.get(field, "")) + " changed"
                changed = [prefix + "REVIEW EVIDENCE (JSON): " + json.dumps(e)]
                self.assertEqual(auto.evidence_check(self.path, key, self.record, changed), "pending")
        self.assertEqual(auto.evidence_check(self.path, key, self.record,
                         ["changed mechanics " + cases[0]]), "pending")

    def test_failed_preflight_still_allows_independent_clearance_in_run_once(self):
        self.assess(self.evidence_row())
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            packet = root / "renewal-20261017-0440-cirrus"
            auto.shutil.copytree(self.path, packet)
            registry = root / "registry.json"
            registry.write_text(json.dumps(self.registry))
            before = registry.read_bytes()
            with patch.object(auto, "EVROOT", root), patch.object(auto.renewal, "REGISTRY", registry), \
                 patch.object(auto, "remote_packet_name", return_value=None), \
                 patch.object(auto, "packet_age_hours", return_value=0), \
                 patch.object(auto, "has_due_records", return_value=False), \
                 patch.object(auto, "install") as install:
                result = auto.run_once(self.negative_review, apply=False)
            self.assertEqual(result["renewable"], 1)
            self.assertEqual(result["pending"], 0)
            install.assert_not_called()
            self.assertEqual(before, registry.read_bytes())

    def test_stale_unbound_or_reviewer_authored_clearance_cannot_renew(self):
        for key, value in [("record_sha256", "wrong"), ("case_sha256", ["wrong"]),
                           ("reviewer", auto.SECOND_MODEL), ("authority", "model-vote"),
                           ("all_requirements_checked", False), ("checks", []), ("rationale", "")]:
            row = self.evidence_row(); row[key] = value
            approved, _, held, details = self.assess(row)
            self.assertEqual((len(approved), held), (0, 0))
            self.assertEqual(details[0]["status"], "evidence_review_pending")
        row = self.evidence_row(); row["checks"][0]["NEW"] = "invented quote"
        self.assertEqual(self.assess(row)[3][0]["status"], "evidence_review_pending")

    def test_clearance_never_bypasses_mechanical_guard(self):
        row = self.evidence_row()
        packet = self.path / "PACKET.txt"
        packet.write_text(packet.read_text().replace("shape=True", "shape=False"))
        approved, _, held, details = self.assess(row)
        self.assertEqual((len(approved), held), (0, 1))
        self.assertEqual(details[0]["status"], "mechanical_failure")

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
