import json
import tempfile
import unittest
from pathlib import Path

import approval_grace as G

HOUR = 3600.0


class GraceTests(unittest.TestCase):
    def setUp(self):
        self.record = {"id": "kimi", "model": "kimi-k3", "approved": True, "expires_at": 1000 * HOUR}
        self.sha = G.record_sha(self.record)
        self.data = {"entries": {self.sha: {"tested_at": 990 * HOUR, "source": "cumulus"}}, "revoked": []}

    def test_active_only_after_expiry_and_inside_72_hours(self):
        self.assertFalse(G.active(self.record, 999 * HOUR, self.data))      # not expired: no grace needed
        self.assertTrue(G.active(self.record, 1000 * HOUR, self.data))
        # The daily re-run keeps evidence fresh; the 72 h bound holds regardless.
        self.data["entries"][self.sha]["tested_at"] = 1060 * HOUR
        self.assertTrue(G.active(self.record, 1000 * HOUR + 72 * HOUR - 1, self.data))
        self.assertFalse(G.active(self.record, 1000 * HOUR + 72 * HOUR, self.data))

    def test_evidence_must_be_fresh_and_not_in_the_future(self):
        self.data["entries"][self.sha]["tested_at"] = 1000 * HOUR - 37 * HOUR
        self.assertFalse(G.active(self.record, 1001 * HOUR, self.data))
        self.data["entries"][self.sha]["tested_at"] = 1002 * HOUR
        self.assertFalse(G.active(self.record, 1001 * HOUR, self.data))
        for bad in (None, "990", float("nan"), True):
            self.data["entries"][self.sha]["tested_at"] = bad
            self.assertFalse(G.active(self.record, 1001 * HOUR, self.data), bad)

    def test_any_change_to_the_record_ends_grace(self):
        for field, value in (("approved", False), ("model", "kimi-k3.1-pro"), ("expires_at", 1001 * HOUR)):
            changed = dict(self.record, **{field: value})
            self.assertFalse(G.active(changed, 1001 * HOUR, self.data), field)

    def test_revoked_record_has_no_grace(self):
        self.data["revoked"] = [self.sha]
        self.assertFalse(G.active(self.record, 1001 * HOUR, self.data))

    def test_malformed_or_missing_store_grants_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "g.json"
            self.assertEqual(G.load(path), {"entries": {}, "revoked": []})
            for text in ("not json", "[]", json.dumps({"entries": []}), json.dumps({"entries": {}, "revoked": {}})):
                path.write_text(text)
                self.assertEqual(G.load(path), {"entries": {}, "revoked": []}, text)

    def test_replace_source_keeps_other_writers_and_respects_revocation(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "g.json"
            G.replace_source("cumulus", {"a" * 64: {"tested_at": 1}, "b" * 64: {"tested_at": 1}}, path=path)
            G.replace_source("answer", {"c" * 64: {"tested_at": 2}}, path=path)
            G.revoke(["b" * 64], path=path)
            G.replace_source("cumulus", {"a" * 64: {"tested_at": 3}, "b" * 64: {"tested_at": 3}}, path=path)
            data = G.load(path)
            self.assertEqual(set(data["entries"]), {"a" * 64, "c" * 64})
            self.assertEqual(data["entries"]["a" * 64], {"tested_at": 3, "source": "cumulus"})
            self.assertEqual(data["revoked"], ["b" * 64])
            G.replace_source("cumulus", {}, path=path)
            self.assertEqual(set(G.load(path)["entries"]), {"c" * 64})
            self.assertFalse((Path(td) / "g.json.tmp").exists())


class RenewalEntryTests(unittest.TestCase):
    """foundation_renewal.grace_entries: only records whose every fixture passed."""

    def case(self, index, fid, sha):
        return {"route": "foundation:r", "field": "evaluations", "index": index, "fixture_id": fid,
                "provider": "kimi", "old_record_sha256": sha}

    def row(self, index, fid, started, **bad):
        r = {"route": "foundation:r", "field": "evaluations", "index": index, "fixture_id": fid,
             "provider": "kimi", "started_at": started, "shape_valid": True, "identity_valid": True,
             "finish": "stop"}
        r.update(bad)
        return r

    def entries(self, rows, manifest, hoa=None):
        import foundation_renewal as R
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "run-kimi.json").write_text(json.dumps(rows))
            if hoa is not None:
                (d / "hoa-retention.json").write_text(json.dumps(hoa))
            return R.grace_entries(d, manifest)

    def test_all_fixtures_passing_gives_one_entry_at_the_earliest_test(self):
        manifest = [self.case(0, "f1", "a" * 64), self.case(0, "f2", "a" * 64)]
        out = self.entries([self.row(0, "f1", 20.0), self.row(0, "f2", 10.0)], manifest)
        self.assertEqual(set(out), {"a" * 64})
        self.assertEqual(out["a" * 64]["tested_at"], 10.0)
        self.assertEqual(out["a" * 64]["provider"], "kimi")

    def test_any_gate_failure_or_missing_fixture_gives_no_entry(self):
        manifest = [self.case(0, "f1", "a" * 64), self.case(0, "f2", "a" * 64), self.case(1, "f3", "b" * 64)]
        for bad in ({"shape_valid": False}, {"identity_valid": False}, {"finish": "length"},
                    {"finish": "max_tokens"}, {"error_type": "TimeoutError"}):
            out = self.entries([self.row(0, "f1", 1.0), self.row(0, "f2", 1.0, **bad),
                                self.row(1, "f3", 1.0)], manifest)
            self.assertEqual(set(out), {"b" * 64}, bad)
        out = self.entries([self.row(0, "f1", 1.0), self.row(1, "f3", 1.0)], manifest)
        self.assertEqual(set(out), {"b" * 64})

    def test_latest_attempt_of_a_fixture_decides(self):
        manifest = [self.case(0, "f1", "a" * 64)]
        out = self.entries([self.row(0, "f1", 1.0, finish="length"), self.row(0, "f1", 2.0)], manifest)
        self.assertEqual(set(out), {"a" * 64})
        out = self.entries([self.row(0, "f1", 1.0), self.row(0, "f1", 2.0, error_type="X")], manifest)
        self.assertEqual(out, {})

    def test_hoa_needs_every_decision_to_match(self):
        hoa = [{"index": 0, "passed": True, "started_at": 5.0, "old_record_sha256": "h" * 64},
               {"index": 0, "passed": True, "started_at": 4.0, "old_record_sha256": "h" * 64}]
        self.assertEqual(self.entries([], [], hoa)["h" * 64]["tested_at"], 4.0)
        hoa[1]["passed"] = False
        self.assertEqual(self.entries([], [], hoa), {})


class SyncAndRevokeTests(unittest.TestCase):
    """CIRRUS adopts only CUMULUS's cloud entries, and keeps none if the copy fails;
    a confirmed defect revokes on both boxes."""

    def test_sync_adopts_cloud_entries_and_fails_closed(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import foundation_renewal as R
        remote = {"entries": {"a" * 64: {"source": "cumulus", "provider": "kimi", "tested_at": 1},
                              "b" * 64: {"source": "cumulus", "provider": "vllm", "tested_at": 1},
                              "c" * 64: {"source": "answer", "provider": "openai", "tested_at": 1}},
                  "revoked": ["d" * 64]}
        with tempfile.TemporaryDirectory() as td:
            registry = Path(td) / "model_capabilities.json"
            path = registry.with_name("approval-grace.json")
            ok = SimpleNamespace(returncode=0, stdout=json.dumps(remote))
            with patch.object(R, "REGISTRY", registry), patch.object(R.subprocess, "run", return_value=ok):
                self.assertEqual(R.sync_cumulus_grace(), 1)
            data = G.load(path)
            self.assertEqual(set(data["entries"]), {"a" * 64})
            self.assertEqual(data["revoked"], ["d" * 64])
            failed = SimpleNamespace(returncode=255, stdout="")
            with patch.object(R, "REGISTRY", registry), patch.object(R.subprocess, "run", return_value=failed), \
                 patch.object(R, "log"):
                self.assertEqual(R.sync_cumulus_grace(), 0)
            self.assertEqual(G.load(path)["entries"], {})

    def test_confirmed_defect_revokes_on_both_boxes(self):
        from unittest.mock import patch
        import foundation_renewal_auto as A
        with tempfile.TemporaryDirectory() as td:
            registry = Path(td) / "model_capabilities.json"
            path = registry.with_name("approval-grace.json")
            G.replace_source("cumulus", {"e" * 64: {"tested_at": 1}, "f" * 64: {"tested_at": 1}}, path=path)
            calls = []
            with patch.object(A.renewal, "REGISTRY", registry):
                self.assertEqual(A.revoke_defects([{"status": "evidence_review_pending", "record_sha256": "f" * 64}],
                                                  run=lambda *a: calls.append(a)), [])
                self.assertEqual(calls, [])
                A.revoke_defects([{"status": "confirmed_defect", "record_sha256": "e" * 64}],
                                 run=lambda *a: calls.append(a))
            self.assertEqual(set(G.load(path)["entries"]), {"f" * 64})
            self.assertEqual(calls[0][0][-2:], ["revoke", "e" * 64])


class AnswerWorkflowGateTests(unittest.TestCase):
    """answer_workflow_eval.mechanical: the same four gates on the pilot's evidence."""

    def setUp(self):
        import answer_workflow_eval as E
        self.E = E
        raw = json.dumps({"answer": "No supplied source answers this.", "confidence": 0.5,
                          "verdict": "needs_review", "kind": "grounded", "reason": "r", "evidence": [],
                          "missing_request_quote": ""})
        self.record = {"id": "ollama", "model": "m", "approved": True, "contract_sha256": "c" * 64}
        self.evidence = {"model": "m", "contract_sha256": "c" * 64, "started": 5.0,
                         "rows": [{"case": c, "raw": raw, "actual_model": "m", "finish_reason": "stop"}
                                  for c, _ in E.fixtures()]}

    def test_complete_identified_parsed_rows_pass_even_if_an_answer_is_scored_wrong(self):
        self.assertTrue(self.E.mechanical(self.record, self.evidence))

    def test_each_gate_and_binding_fails_closed(self):
        import copy
        for mutate in (lambda e: e["rows"][0].update(error_type="TimeoutError"),
                       lambda e: e["rows"][1].update(actual_model="other"),
                       lambda e: e["rows"][2].update(finish_reason="length"),
                       lambda e: e["rows"][3].update(raw="not json"),
                       lambda e: e["rows"].pop(),
                       lambda e: e.update(model="other"),
                       lambda e: e.update(contract_sha256="d" * 64),
                       lambda e: e.pop("started")):
            evidence = copy.deepcopy(self.evidence)
            mutate(evidence)
            self.assertFalse(self.E.mechanical(self.record, evidence))


if __name__ == "__main__":
    unittest.main()
