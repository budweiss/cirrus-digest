"""Offline regressions for the public Apple schema observed 2026-10-12."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import mac_studio_watch as watch


def pages(memory_512=False, price=5499, notice="512GB memory coming late October"):
    memory = {"96gb": {"header": "96GB"}, "variantOrder": ["96gb"]}
    if memory_512:
        memory["512gb"] = {"header": "512GB"}
    data = {
        "configDisplayValues": {watch.MEMORY: memory,
                                "storage-dimensionCapacity": {"512gb": {"header": "512GB"}}},
        "mainDisplayValues": {"prices": {"m5ultra-30-64": {"amount": price}}},
        "configSections": [{"formFieldName": watch.MEMORY, "footer": notice}],
        "products": [{"priceKey": "m5ultra-30-64", "isComingSoon": False}],
    }
    texts = {"chip": "Apple M5&nbsp;Ultra chip 30-core CPU 64-core GPU 1.2TB/s bandwidth",
             "memory": "96GB unified memory Configurable to 256GB or 512GB unified memory",
             "storage": "512GB SSD or 1TB SSD Configurable to 2TB 4TB 8TB or 16TB SSD"}
    return {
        "store": "<script>window.PRODUCT_SELECTION_BOOTSTRAP = { productSelectionData: "
                 + json.dumps(data) + " }</script>",
        "specs": "".join('<div class="techspecs-section section-' + key + '">'
                           '<div><p>' + value + '</p></div></div>' for key, value in texts.items()),
    }


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.sent = []
        self.records = []

    def run_watch(self, sample=None, sender=None, fetch=None):
        return watch.run_once(self.path, fetch or (lambda: sample or pages()),
                              sender or (lambda message: self.sent.append(message) or True),
                              lambda *args: self.records.append(args))

    def test_storage_and_spec_sheet_512_are_not_memory_availability(self):
        result = watch.parse_pages(pages())
        self.assertNotIn("512gb", result["memory_options"])
        self.assertIn("not listed", watch.describe(result))
        self.assertIn("late October", watch.describe(result))

    def test_new_memory_option_is_not_a_checkout_or_configured_price_claim(self):
        result = watch.parse_pages(pages(memory_512=True, notice=""))
        text = watch.describe(result)
        self.assertIn("exact configured price and checkout need confirmation", text)
        self.assertIn("not a 512GB configuration quote", text)

    def test_missing_or_challenge_pages_fail_closed(self):
        for sample in ({"store": "Access denied", "specs": "Please wait"},
                       {**pages(), "specs": "<p>512GB M5 Ultra</p>"}):
            with self.assertRaises((ValueError, KeyError)):
                watch.parse_pages(sample)

    def test_footer_and_script_noise_are_ignored(self):
        first = pages()
        second = copy.deepcopy(first)
        second["store"] += "<footer>Copyright 2027 session 999</footer>"
        second["specs"] += "<script>tracking=500</script><footer>New navigation</footer>"
        self.assertEqual(watch.parse_pages(first), watch.parse_pages(second))

    def test_unchanged_is_quiet_but_price_and_memory_changes_send_once(self):
        self.assertEqual(0, self.run_watch()[0])
        self.assertEqual(0, self.run_watch()[0])
        self.assertEqual(len(self.sent), 1)
        self.run_watch(pages(price=5599))
        self.run_watch(pages(price=5599, memory_512=True))
        self.run_watch(pages(price=5599, memory_512=True))
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(json.loads(self.path.read_text())["alert_count"], 3)

    def test_failed_telegram_does_not_advance_snapshot_and_retries(self):
        self.run_watch()
        previous = json.loads(self.path.read_text())["sent_fingerprint"]
        code, _ = self.run_watch(pages(price=5599), sender=lambda _: False)
        self.assertEqual(code, 1)
        self.assertFalse(self.records[-1][1])
        self.assertEqual(json.loads(self.path.read_text())["sent_fingerprint"], previous)
        self.run_watch(pages(price=5599))
        self.assertEqual(len(self.sent), 2)

    def test_failure_alert_is_deduplicated_and_recovery_reported(self):
        self.run_watch()
        baseline = json.loads(self.path.read_text())["sent_fingerprint"]
        def fail():
            raise RuntimeError("private diagnostic text must never be copied")
        self.assertEqual(self.run_watch(fetch=fail)[0], 1)
        self.assertEqual(self.run_watch(fetch=fail)[0], 1)
        self.assertEqual(len(self.sent), 2)
        self.assertNotIn("private diagnostic", self.sent[-1])
        self.assertEqual(json.loads(self.path.read_text())["sent_fingerprint"], baseline)
        self.run_watch()
        self.assertIn("recovered", self.sent[-1])
        self.assertEqual(len(self.sent), 3)
        self.assertIsNone(json.loads(self.path.read_text())["last_error"])

    def test_corrupt_state_is_not_reset_into_a_duplicate_activation(self):
        self.path.write_text("{broken")
        with self.assertRaises(json.JSONDecodeError):
            self.run_watch()
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
