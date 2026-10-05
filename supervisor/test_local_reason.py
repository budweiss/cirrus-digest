"""S375 Phase-1 shadow-reason tests. Hermetic: urllib is stubbed; no marker file,
no network, no live state writes (SHADOW_LOG/marker are patched into a tempdir
per the tooling-traps tempfile rule)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_reason as L

RESPONSE_OK = {"choices": [{"message": {"content": "verdict: real. action: restart. confidence: medium"}}],
               "usage": {"prompt_tokens": 300, "completion_tokens": 60}}


class TestShadowReason(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="shadow-reason-")
        self.state = Path(self._td.name)
        patcher = patch.multiple(L, STATE_DIR=self.state, SHADOW_MARK=self.state / "shadow-reason.enabled",
                                 SHADOW_LOG=self.state / "shadow-reason.jsonl")
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._td.cleanup()

    def test_off_without_marker(self):
        self.assertEqual(L.maybe_shadow("daily check", "d", "e"), {"ran": False})
        self.assertFalse((self.state / "shadow-reason.jsonl").exists())

    def test_first_endpoint_wins_and_row_is_written(self):
        (self.state / "shadow-reason.enabled").write_text("on")
        with patch.object(L, "_post", return_value={**RESPONSE_OK, "ok": True,
                                                    "wall_seconds": 3.2,
                                                    "model": "qwen3.8-27b-fp8"}) as post:
            r = L.maybe_shadow("incident review", "unit failed", '{"job_id":"x"}', key="cirrus-hoaleads.service")
        self.assertTrue(r["ran"])
        self.assertTrue(r["ok"])
        self.assertEqual(r["endpoint"], "c2-qwen-64k")
        post.assert_called_once()
        row = json.loads((self.state / "shadow-reason.jsonl").read_text().strip())
        self.assertEqual(row["endpoint"], "c2-qwen-64k")
        self.assertEqual(row["ok"], True)

    def test_first_endpoint_failure_falls_back_to_second(self):
        (self.state / "shadow-reason.enabled").write_text("on")
        good = {**RESPONSE_OK, "ok": True, "model": "gpt-oss:120b", "wall_seconds": 4.0}
        with patch.object(L, "_post", side_effect=[TimeoutError("t"), good]) as post:
            r = L.shadow_reason("incident review", "d", "e")
        self.assertTrue(r["ok"])
        self.assertEqual(r["endpoint"], "c1-gptoss-32k")
        self.assertEqual(post.call_count, 2)

    def test_all_endpoints_down_fail_open_and_log(self):
        (self.state / "shadow-reason.enabled").write_text("on")
        with patch.object(L, "_post", side_effect=OSError("refused")) as post:
            r = L.shadow_reason("incident review", "d", "e")
        self.assertFalse(r["ok"])
        self.assertEqual(post.call_count, len(L.ENDPOINTS))
        row = json.loads((self.state / "shadow-reason.jsonl").read_text().strip())
        self.assertEqual(row["ok"], False)

    def test_maybe_shadow_never_raises(self):
        (self.state / "shadow-reason.enabled").write_text("on")
        with patch.object(L, "shadow_reason", side_effect=RuntimeError("boom")):
            r = L.maybe_shadow("t", "d", "e")
        self.assertTrue(r["ran"])
        self.assertFalse(r["ok"])   # recorded as failed, never a fabricated verdict
        self.assertEqual(r["error"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
