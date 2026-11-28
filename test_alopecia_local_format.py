"""Offline tests for deterministic, local-only Alopecia brief assembly."""

import json
import re
import unittest

import alopecia_brief as medical
import alopecia_local_format as local
import test_medical_judge as fixtures
import llm_providers as providers
from unittest.mock import patch


class LocalFormatTests(unittest.TestCase):
    def payload(self):
        return {
            "sections": {
                "what_found": "Nothing in the supplied items this period [1].",
                "trials": "The registration is a plan, not an outcome [1].",
                "subgroup": "Applicability is unknown from the supplied material [1].",
                "community": "Nothing in the supplied items this period [1].",
            },
            "standing_answers": ["Nothing supplied changes this question [1]."] * 3,
        }

    def test_schema_and_deterministic_assembly_for_full_and_delta(self):
        schema = local.response_schema()
        self.assertEqual(schema["required"], ["sections", "standing_answers"])
        system = local.structured_system("base system")
        self.assertIn("this is one local model, not a council", system)
        self.assertIn("Do not imply the council agreed", system)
        for full in (False, True):
            user = fixtures.task(full)
            result, error = local.render(json.dumps(self.payload()), user,
                                         [dict(key="fixture")], full, fixtures.DAY)
            self.assertEqual(error, "ok")
            self.assertTrue(result.startswith("Brief date: " + fixtures.DAY + "\n"))
            self.assertEqual(re.findall(r"^## .+$", result, re.M),
                             (medical.STRUCTURE_FULL if full else medical.STRUCTURE_DELTA).splitlines())
            self.assertEqual(result.count("Single-model local synthesis"), 1)
            self.assertEqual(result.count("[1]"), 7)
            self.assertTrue(result.index("2. Why onset at ~age 10?") < result.index("## Council disagreements"))

    def test_rejects_malformed_shape_empty_answer_and_bad_citation(self):
        data = self.payload()
        bad = ["not json", json.dumps(dict(data, extra="no")),
               json.dumps(dict(data, standing_answers=["one"])),
               json.dumps(dict(data, standing_answers=[" ", "ok", "ok"])),
               json.dumps(dict(data, sections=dict(data["sections"], trials="[2]")))]
        expected = ["invalid_json", "wrong_top_level_keys", "wrong_answer_count",
                    "wrong_answer_count", "invalid_citation"]
        for raw, error in zip(bad, expected):
            result, actual = local.render(raw, fixtures.task(), [dict(key="fixture")], False, fixtures.DAY)
            self.assertIsNone(result)
            self.assertEqual(actual, error)

    def test_ollama_structured_schema_is_opt_in_and_reaches_wire(self):
        schema = {"type": "json_schema", "json_schema": {
            "name": "fixture", "schema": local.response_schema()}}
        seen = {}
        def post(url, headers, body, timeout=providers._TIMEOUT):
            seen.update(url=url, body=body)
            return {"model": "gpt-oss:120b", "choices": [{"message": {"content": "{}"}}]}
        with patch.object(providers, "_http_post", side_effect=post):
            providers.call("ollama", "system", "prompt",
                           {"ollama_url": "http://localhost:11434",
                            "ollama_model": "gpt-oss:120b",
                            "ollama_response_format": schema})
        self.assertEqual(seen["body"]["response_format"], schema)
        self.assertEqual(seen["url"], "http://localhost:11434/v1/chat/completions")


if __name__ == "__main__":
    unittest.main()
