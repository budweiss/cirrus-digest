"""Synthetic delivery safety checks: no real credentials or network sends."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import stock_digest_send as delivery
from stock_paper import account_section
from stock_paper_ledger import Ledger, fill_price
from test_stock_paper_ledger import quote


class PaperDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-stock-delivery-")
        self.home = Path(self.temp.name)
        self.at = datetime.fromisoformat("2026-10-08T19:00:00+00:00")
        self.ledger = Ledger(self.home / "ledger.sqlite3", now=lambda: self.at)
        self.ledger.initialize()
        self.report_id = "a" * 32
        snapshot = self.ledger.record_snapshot(idempotency_key=self.report_id)
        bench = {"available": False, "reason": "synthetic fixture"}
        section = account_section(snapshot, [], bench, "pm")
        marker = "<!-- stock-paper-report:" + self.report_id + " -->"
        self.report = {"status": "complete", "account_id": "independent-200k-v1",
                       "report_id": self.report_id, "date": "2026-10-08", "slot": "pm",
                       "created_at": snapshot["as_of"], "snapshot": snapshot,
                       "benchmark": bench, "trades": [], "decisions": [],
                       "account_section": section, "marker": marker,
                       "report": marker + "\n\n" + section}
        self.md = "# Stock Pickers — 2026-10-08 PM\n\n" + self.report["report"] + "\nSynthetic research notes.\n"
        self.path = self.home / "reports" / (self.report_id + ".json")
        self.path.parent.mkdir()
        self.save_report()
        self.creds = {"outlook_email": "synthetic-sender@example.test",
                      "outlook_password": "synthetic-test-placeholder"}

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def save_report(self):
        self.path.write_text(json.dumps(self.report))

    def check(self, md=None, at=None):
        return delivery.check_paper_report(self.md if md is None else md,
                                           at=self.at if at is None else at, home=self.home)

    def send(self, sender, md=None, report=None):
        with redirect_stdout(io.StringIO()):
            return delivery.deliver_once(self.md if md is None else md,
                                         self.creds, "Synthetic paper report",
                                         self.report if report is None else report,
                                         send_fn=sender, home=self.home)

    def receipt(self):
        return json.loads((self.home / "delivery" / "2026-10-08-pm.json").read_text())

    def append_trade(self):
        decision = self.ledger.record_decision({"idempotency_key": "synthetic-decision",
                                               "ticker": "SYN", "action": "BUY",
                                               "reason": "Synthetic post-report purchase"})
        observed = quote(self.at)
        return self.ledger.execute_trade({"idempotency_key": "synthetic-fill",
                                          "decision_id": decision["decision_id"],
                                          "ticker": "SYN", "side": "BUY", "shares": 1,
                                          "price": fill_price(observed, "BUY"),
                                          "fees": "0.00", "executed_at": self.at.isoformat(),
                                          "quote": observed, "reason": "Synthetic fill"})

    def test_valid_exact_report_is_accepted(self):
        self.assertEqual(self.check()["report_id"], self.report_id)

    def test_missing_duplicate_or_unresolvable_report_marker_is_rejected(self):
        for md in ("Synthetic letter with no account section", self.md + self.report["marker"],
                   self.md.replace(self.report_id, "b" * 32)):
            with self.subTest(md_length=len(md)), self.assertRaises((ValueError, FileNotFoundError)):
                self.check(md)

    def test_stale_or_future_report_is_rejected(self):
        for at in (self.at + timedelta(seconds=1801), self.at - timedelta(seconds=6)):
            with self.subTest(at=at), self.assertRaises(ValueError):
                self.check(at=at)
        self.assertEqual(self.check(at=self.at + timedelta(seconds=1800))["report_id"], self.report_id)

    def test_altered_cash_section_is_rejected(self):
        self.assertIn("$200,000.00", self.md)
        altered = self.md.replace("$200,000.00", "$900,000.00")
        with self.assertRaises(ValueError):
            self.check(altered)

    def test_wrong_account_date_or_daily_slot_is_rejected(self):
        for field, value in (("account_id", "legacy-paper-account"), ("date", "2026-10-07"),
                             ("slot", "ad-hoc")):
            saved = self.report[field]
            self.report[field] = value
            self.save_report()
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.check()
            self.report[field] = saved
        self.save_report()

    def test_trade_after_snapshot_requires_a_new_report(self):
        self.at += timedelta(seconds=1)
        self.append_trade()
        with self.assertRaises(ValueError):
            self.check()

    def test_trade_after_snapshot_same_timestamp_is_still_rejected(self):
        self.append_trade()
        with self.assertRaises(ValueError):
            self.check()

    def test_new_corporate_action_hold_invalidates_previously_complete_report(self):
        self.ledger.record_event({"idempotency_key": "synthetic-late-action",
                                  "kind": "CORPORATE_ACTION_HOLD", "ticker": "SYN",
                                  "effective_at": self.at.isoformat(), "source": "Synthetic source",
                                  "url": "https://example.com/synthetic-action",
                                  "reason": "Synthetic unsupported distribution requires review"})
        with self.assertRaises(ValueError):
            self.check()

    def test_report_snapshot_cannot_be_rewritten_outside_the_ledger(self):
        self.report["snapshot"]["cash_cents"] = 90_000_000
        self.report["snapshot"]["cash"] = "900000.00"
        self.save_report()
        with self.assertRaises(ValueError):
            self.check()

    def test_empty_canonical_section_cannot_validate_arbitrary_letter(self):
        self.report["account_section"] = ""
        self.save_report()
        with self.assertRaises(ValueError):
            self.check(self.report["marker"] + "\nNo account figures.")

    def test_one_acknowledged_send_then_identical_retry_never_sends_twice(self):
        sender = Mock(return_value=True)
        self.assertTrue(self.send(sender))
        self.assertTrue(self.send(sender))
        self.assertEqual(sender.call_count, 1)
        args, kwargs = sender.call_args
        self.assertEqual(args[2], delivery.TO_EMAIL)
        self.assertEqual(args[4], self.md)
        self.assertEqual(kwargs["on_error"], "raise")
        receipt = self.receipt()
        self.assertEqual(receipt["status"], "sent")
        self.assertEqual(receipt["body_sha256"], hashlib.sha256(self.md.encode()).hexdigest())
        self.assertEqual((self.home / "delivery" / "2026-10-08-pm.json").stat().st_mode & 0o777, 0o600)

    def test_changed_body_cannot_reuse_an_acknowledged_daily_slot(self):
        sender = Mock(return_value=True)
        self.send(sender)
        with self.assertRaises(ValueError):
            self.send(sender, md=self.md + "Changed report.")
        self.assertEqual(sender.call_count, 1)

    def test_timeout_retains_unknown_receipt_and_blocks_blind_retry(self):
        sender = Mock(side_effect=TimeoutError("synthetic ambiguous transport outcome"))
        with self.assertRaises(TimeoutError):
            self.send(sender)
        self.assertEqual(self.receipt()["status"], "sending_outcome_unknown_until_confirmed")
        sender.side_effect = None
        sender.return_value = True
        with self.assertRaises(ValueError):
            self.send(sender)
        self.assertEqual(sender.call_count, 1)

    def test_false_send_acknowledgment_does_not_become_sent(self):
        sender = Mock(return_value=False)
        with self.assertRaises(ValueError):
            self.send(sender)
        self.assertEqual(self.receipt()["status"], "sending_outcome_unknown_until_confirmed")
        with self.assertRaises(ValueError):
            self.send(sender)
        self.assertEqual(sender.call_count, 1)

    def test_concurrent_sender_is_held_while_first_send_is_in_flight(self):
        entered, release = threading.Event(), threading.Event()
        def slow_sender(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("synthetic test synchronization timed out")
            return True
        sender = Mock(side_effect=slow_sender)
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.send, sender)
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaises(BlockingIOError):
                    self.send(sender)
            finally:
                release.set()
            self.assertTrue(first.result(timeout=5))
        self.assertEqual(sender.call_count, 1)

    def test_am_and_pm_have_independent_once_daily_receipts(self):
        sender = Mock(return_value=True)
        self.send(sender)
        morning = dict(self.report, slot="am", report_id="c" * 32)
        self.send(sender, report=morning, md="Synthetic morning report")
        self.assertEqual(sender.call_count, 2)
        self.assertTrue((self.home / "delivery" / "2026-10-08-am.json").exists())

    def test_cli_dry_run_uses_no_live_delivery_or_real_credentials(self):
        credentials = self.home / "synthetic-credentials.json"
        credentials.write_text(json.dumps(self.creds))
        with patch.object(delivery, "CREDS_PATH", credentials), \
                patch.object(delivery, "check_paper_report", return_value=self.report), \
                patch.object(delivery.sys, "stdin", io.StringIO(self.md)), \
                patch.object(delivery.mailer, "send", return_value=True) as send, \
                patch.object(delivery, "deliver_once") as live, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(delivery.main(["stock_digest_send.py", "--mode=dry-run"]), 0)
        live.assert_not_called()
        send.assert_called_once()
        self.assertTrue(send.call_args.kwargs["dry_run"])


if __name__ == "__main__":
    unittest.main()
