"""S364: record_call's optional engine-metric extras land on the ledger row.

Hermetic: tempdir pricing + ledger only (T77 — tests never touch live files).
"""
import json
import tempfile
import unittest
from pathlib import Path

import llm_budget as B


class TestRecordCallExtras(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="bud-timing-")
        td = Path(self._td.name)
        self.pricing = td / "pricing.json"
        self.ledger = td / "ledger.jsonl"
        self.pricing.write_text(json.dumps({
            "models": {"stub-model": {"in": 0.0, "out": 0.0}},
            "caps_usd": {"per_session": 100.0, "per_day": 200.0, "per_call": 10.0},
            "discounts": {"batch": 0.5, "cache_read": 0.1, "cache_write": 1.25},
            "unknown_model_out_per_m": 25.0,
        }))
        self.creds = {"llm_budget": {"pricing_path": str(self.pricing),
                                     "ledger_path": str(self.ledger),
                                     "box": "selftest"}}

    def tearDown(self):
        self._td.cleanup()

    def _row(self):
        return json.loads(self.ledger.read_text().strip().splitlines()[-1])

    def test_extras_written_when_present(self):
        r = B.record_call(self.creds, "ollama", "stub-model", 100, 50, task="t1",
                          in_tok=3200, out_tok=300, num_ctx=32768, cached_tok=68,
                          prompt_eval_seconds=1.0, eval_seconds=10.0, wall_seconds=12.3456)
        self.assertIsNotNone(r)
        row = self._row()
        self.assertEqual(row["num_ctx"], 32768)
        self.assertEqual(row["cached_tok"], 68)
        self.assertEqual(row["prompt_eval_seconds"], 1.0)
        self.assertEqual(row["eval_seconds"], 10.0)
        self.assertEqual(row["wall_seconds"], 12.346)  # rounded to 3dp
        self.assertEqual(row["in_tok"], 3200)
        self.assertEqual(row["out_tok"], 300)

    def test_none_extras_omit_fields(self):
        r = B.record_call(self.creds, "openai", "stub-model", 100, 50, task="t2",
                          in_tok=10, out_tok=5)
        self.assertIsNotNone(r)
        row = self._row()
        for absent in ("num_ctx", "cached_tok", "prompt_eval_seconds",
                       "eval_seconds", "wall_seconds"):
            self.assertNotIn(absent, row)

    def test_zero_seconds_is_a_real_value_and_negative_is_dropped(self):
        B.record_call(self.creds, "ollama", "stub-model", 1, 1, task="t3",
                      in_tok=8, out_tok=4, eval_seconds=0.0, wall_seconds=-1)
        row = self._row()
        self.assertEqual(row["eval_seconds"], 0.0)
        self.assertNotIn("wall_seconds", row)


if __name__ == "__main__":
    unittest.main()
