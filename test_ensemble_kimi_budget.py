import unittest
from unittest.mock import patch
import ensemble


class KimiBudgetTests(unittest.TestCase):
    def test_configured_kimi_is_priced_in_council_estimate(self):
        creds = {"kimi_model": "kimi-k3", "claude_model": "claude-sonnet-5"}
        with patch.object(ensemble.B, "cost_usd", return_value=0.1) as cost:
            estimate = ensemble._estimate_cost(
                ["kimi", "anthropic"], "anthropic", 400, 100, {}, creds)
        self.assertAlmostEqual(estimate, 0.3)
        self.assertEqual(cost.call_args_list[0].args[0], "kimi-k3")

    def test_missing_kimi_model_still_rejected(self):
        with self.assertRaisesRegex(ValueError, "no model for kimi"):
            ensemble._estimate_cost(["kimi"], "anthropic", 400, 100, {}, {})


if __name__ == "__main__":
    unittest.main()
