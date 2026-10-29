"""S539: an undelivered intake Telegram notice is kept, retried, and reported."""
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import intake


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.outbox = root / "logs/intake/undelivered-notices.json"
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(intake, "NOTICE_OUTBOX_PATH", self.outbox))
        self.stack.enter_context(patch.object(intake, "LOG_PATH", root / "intake.log"))
        self.root = root

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def saved(self):
        return json.loads(self.outbox.read_text())

    def test_failed_notice_is_kept_then_delivered_as_delayed(self):
        with patch.object(intake, "telegram", return_value=False):
            self.assertEqual(intake._notify("held: request 1", {}), 1)
        self.assertEqual([x["text"] for x in self.saved()], ["held: request 1"])
        with patch.object(intake, "telegram", return_value=True) as tg:
            self.assertEqual(intake._notify(None, {}), 0)
        self.assertTrue(tg.call_args[0][0].startswith("(delayed notice from "))
        self.assertIn("held: request 1", tg.call_args[0][0])
        self.assertEqual(self.saved(), [])

    def test_outage_costs_one_attempt_per_pass_and_keeps_order(self):
        with patch.object(intake, "telegram", return_value=False) as tg:
            intake._notify("first", {})
            intake._notify("second", {})
            self.assertEqual(intake._notify("third", {}), 3)
        self.assertEqual(tg.call_count, 3)          # one try per pass, never per item
        self.assertEqual([x["text"] for x in self.saved()], ["first", "second", "third"])

    def test_unreadable_outbox_is_set_aside_not_trusted(self):
        self.outbox.parent.mkdir(parents=True)
        self.outbox.write_text("{not json")
        with patch.object(intake, "telegram", return_value=True):
            self.assertEqual(intake._notify("new", {}), 0)
        self.assertTrue(self.outbox.with_suffix(".unreadable").exists())

    def test_outbox_is_capped(self):
        with patch.object(intake, "NOTICE_OUTBOX_MAX", 2), \
             patch.object(intake, "telegram", return_value=False):
            for t in ("a", "b", "c"):
                n = intake._notify(t, {})
        self.assertEqual(n, 2)
        self.assertEqual([x["text"] for x in self.saved()], ["b", "c"])

    def quiet_pass(self, telegram_ok):
        with ExitStack() as s:
            s.enter_context(patch.object(intake, "PROJECT_DIR", self.root))
            s.enter_context(patch.object(intake, "FAST_POLL_STATE_PATH", self.root / "fast"))
            s.enter_context(patch.object(intake, "load_allowlist",
                                         return_value={"c@example.com": {"name": "client"}}))
            s.enter_context(patch("runtime_config.load_sources", return_value={}))
            s.enter_context(patch.object(intake, "find_account",
                                         return_value={"credential_key": "pw", "address": "box@example.com"}))
            s.enter_context(patch.object(intake, "load_json", return_value={"pw": "test"}))
            s.enter_context(patch.object(intake, "load_state", return_value={}))
            s.enter_context(patch.object(intake, "save_state"))
            s.enter_context(patch.object(intake, "scan_inbox", return_value=([], [])))
            s.enter_context(patch.object(intake, "telegram", return_value=telegram_ok))
            status = s.enter_context(patch.object(intake, "_record_status"))
            self.assertEqual(intake.run(), 0)
        return status.call_args[0]

    def test_quiet_pass_retries_and_job_status_stays_red_until_delivered(self):
        with patch.object(intake, "telegram", return_value=False):
            intake._notify("held: request 2", {})
        ok, note = self.quiet_pass(telegram_ok=False)
        self.assertFalse(ok)
        self.assertIn("1 Telegram notice(s) undelivered", note)
        ok, note = self.quiet_pass(telegram_ok=True)
        self.assertTrue(ok)
        self.assertNotIn("undelivered", note)
        self.assertEqual(self.saved(), [])


if __name__ == "__main__":
    unittest.main()
