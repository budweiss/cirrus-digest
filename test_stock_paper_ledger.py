"""Synthetic, temporary-file checks of the paper ledger's financial invariants."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from stock_paper_ledger import IdempotencyConflict, Ledger, LedgerError, fill_price
from stock_paper_market import get_quote, market_session


UTC = timezone.utc


def stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def quote(at, ticker="SYN", price="100.00", **extra):
    """Normalize a synthetic provider-shaped response; no external requests."""
    session = market_session(at)
    # Off-session cases still provide the published day's regular boundaries.
    opened = session["open"] or at.replace(hour=13, minute=30).isoformat()
    closed = session["close"] or at.replace(hour=20, minute=0).isoformat()
    meta = {"symbol": ticker, "longName": "Synthetic " + ticker + " Corporation",
            "currency": "USD", "regularMarketPrice": price,
            "regularMarketTime": int(at.timestamp()), "exchangeName": "NYQ",
            "exchangeTimezoneName": "America/New_York", "instrumentType": "EQUITY",
            "exchangeDataDelayedBy": 0, "marketState": "REGULAR",
            "currentTradingPeriod": {"regular": {
                "start": int(stamp(opened).timestamp()), "end": int(stamp(closed).timestamp())}}}
    meta.update(extra)
    raw = json.dumps({"chart": {"error": None, "result": [{"meta": meta}]}}).encode()
    return get_quote(ticker, now=at, fetcher=lambda unused: (raw, {}))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-stock-ledger-")
        self.path = Path(self.temp.name) / "ledger.sqlite3"
        self.now = stamp("2026-10-08T15:00:00Z")
        self.ledger = Ledger(self.path, now=lambda: self.now)
        self.ledger.initialize()
        self.serial = 0

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def request(self, side="BUY", shares=1, price="100.00", ticker="SYN", fees="0.00", key=None):
        self.serial += 1
        key = key or "synthetic-" + str(self.serial)
        decision = self.ledger.record_decision({
            "idempotency_key": "decision:" + key, "action": side, "ticker": ticker,
            "reason": "Synthetic fixture decision", "strategy_version": "synthetic-v1",
            "review_dates": ["2026-10-29", "2026-11-05"],
            "risks": ["synthetic fixture, no investment recommendation"],
        })
        observed = quote(self.now, ticker, price)
        return {"idempotency_key": key, "decision_id": decision["decision_id"],
                "ticker": ticker, "side": side, "shares": shares,
                "price": fill_price(observed, side), "fees": fees,
                "executed_at": self.now.isoformat(), "quote": observed,
                "reason": "Synthetic whole-share paper fill"}

    def trade(self, *args, **kwargs):
        return self.ledger.execute_trade(self.request(*args, **kwargs))

    def test_exact_initial_capital_no_reset_no_topups_and_reopen(self):
        before = self.ledger.initialize()
        self.assertEqual(self.ledger.initialize(), before)
        self.assertEqual(self.ledger.snapshot()["cash_cents"], 20_000_000)
        with self.assertRaises(LedgerError):
            self.ledger.initialize(initial_cash="200001.00")
        with self.assertRaises(LedgerError):
            self.ledger.initialize(account_id="old-account")
        with self.assertRaises(LedgerError):
            self.ledger.record_event({"idempotency_key": "topup", "kind": "DEPOSIT"})
        self.trade(shares=1)
        with Ledger(self.path, now=lambda: self.now) as second:
            self.assertEqual(second.initialize(), before)
            self.assertEqual(second.snapshot()["cash_cents"], 19_989_995)

    def test_whole_shares_settled_budget_no_shorting_or_arbitrary_fill(self):
        for shares in (0, -1, True, 1.2):
            with self.subTest(shares=shares), self.assertRaises(LedgerError):
                self.ledger.execute_trade(self.request(shares=shares))
        with self.assertRaisesRegex(LedgerError, "settled"):
            self.trade(shares=2000)
        request = self.request()
        request["price"] = "1.00"
        with self.assertRaisesRegex(LedgerError, "5bp"):
            self.ledger.execute_trade(request)
        self.trade(shares=1999)
        self.assertEqual(self.ledger.snapshot()["cash_cents"], 5)
        with self.assertRaisesRegex(LedgerError, "short"):
            self.trade("SELL", shares=2000)
        self.assertEqual(len(self.ledger.list_trades()), 1)

    def test_fifo_partial_sales_conserve_fees_basis_proceeds_and_realized(self):
        self.trade(shares=3, fees="0.01")  # cost 300.16
        self.now += timedelta(minutes=1)
        self.trade(shares=2, price="200.00", fees="0.03")  # cost 400.23
        self.now += timedelta(minutes=1)
        sale = self.trade("SELL", shares=4, price="300.00", fees="0.07")
        self.assertEqual([row["shares"] for row in sale["dispositions"]], [3, 1])
        self.assertEqual([row["cost_cents"] for row in sale["dispositions"]], [30016, 20011])
        self.assertEqual([row["proceeds_cents"] for row in sale["dispositions"]], [89949, 29984])
        self.assertEqual(sale["realized_pnl_cents"], 69906)
        current = self.ledger.snapshot({"SYN": quote(self.now, price="300.00")})
        self.assertEqual(current["positions"][0]["shares"], 1)
        self.assertEqual(current["positions"][0]["cost_basis_cents"], 20012)
        self.assertEqual(current["equity_cents"], 20_079_894)
        self.assertEqual(current["total_pnl_cents"],
                         current["realized_pnl_cents"] + current["unrealized_pnl_cents"])
        final = self.trade("SELL", shares=1, price="150.00", fees="0.02")
        self.assertEqual(final["dispositions"][0]["cost_cents"], 20012)
        self.assertEqual(final["realized_pnl_cents"], -5021)
        final_snapshot = self.ledger.snapshot()
        self.assertEqual(final_snapshot["positions"], [])
        self.assertEqual(final_snapshot["total_pnl_cents"], 64885)
        self.assertEqual(final_snapshot["cash_cents"], 20_064_885)

    def test_cash_proceeds_reserved_until_t_plus_one_close(self):
        self.trade(shares=1999)
        sale = self.trade("SELL", shares=1999, price="100.10")
        self.assertEqual(sale["settlement_at"], "2026-10-09T20:00:00.000000+00:00")
        state = self.ledger.snapshot()
        self.assertEqual(state["settled_cash_cents"], 5)
        self.assertEqual(state["unsettled_cash_cents"], sale["cash_delta_cents"])
        with self.assertRaisesRegex(LedgerError, "settled"):
            self.trade()
        self.now = stamp("2026-10-09T19:59:59Z")
        self.assertEqual(self.ledger.snapshot()["settled_cash_cents"], 5)
        self.now += timedelta(seconds=1)
        settled = self.ledger.snapshot()
        self.assertEqual(settled["settled_cash_cents"], settled["cash_cents"])
        self.assertEqual(settled["unsettled_cash_cents"], 0)

    def test_settlement_skips_weekend_and_bank_holiday(self):
        self.now = stamp("2026-10-09T15:00:00Z")  # Fri before Columbus Day.
        self.trade()
        sale = self.trade("SELL")
        self.assertEqual(sale["settlement_at"], "2026-10-13T20:00:00.000000+00:00")

    def test_idempotency_replay_survives_quote_expiry_but_not_input_change(self):
        request = self.request()
        first = self.ledger.execute_trade(request)
        self.now += timedelta(days=1)
        self.assertEqual(self.ledger.execute_trade(deepcopy(request)), first)
        self.assertEqual(len(self.ledger.list_trades()), 1)
        request["shares"] = 2
        with self.assertRaises(IdempotencyConflict):
            self.ledger.execute_trade(request)
        decision = {"idempotency_key": "held", "action": "HOLD", "reason": "Synthetic hold"}
        self.assertEqual(self.ledger.record_decision(decision), self.ledger.record_decision(decision))
        decision["reason"] = "Different reason"
        with self.assertRaises(IdempotencyConflict):
            self.ledger.record_decision(decision)

    def test_concurrent_buy_cannot_spend_same_cash_twice(self):
        left, right = self.request(shares=1500), self.request(shares=1500)
        second = Ledger(self.path, now=lambda: self.now)
        barrier = threading.Barrier(2)
        def execute(ledger, request):
            barrier.wait()
            try:
                return ledger.execute_trade(request)
            except LedgerError:
                return None
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(execute, self.ledger, left), pool.submit(execute, second, right)]
                results = [future.result() for future in futures]
            self.assertEqual(sum(result is not None for result in results), 1)
            self.assertEqual(self.ledger.snapshot()["cash_cents"], 4_992_500)
            self.assertEqual(len(self.ledger.list_trades()), 1)
        finally:
            second.close()

    def test_concurrent_duplicate_is_exactly_one_fill(self):
        request = self.request()
        second = Ledger(self.path, now=lambda: self.now)
        barrier = threading.Barrier(2)
        def execute(ledger):
            barrier.wait()
            return ledger.execute_trade(request)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(execute, ledger) for ledger in (self.ledger, second)]
                results = [future.result() for future in futures]
            self.assertEqual(results[0], results[1])
            self.assertEqual(len(self.ledger.list_trades()), 1)
        finally:
            second.close()

    def test_concurrent_sales_cannot_sell_same_share_twice(self):
        self.trade()
        left, right = self.request("SELL"), self.request("SELL")
        second = Ledger(self.path, now=lambda: self.now)
        barrier = threading.Barrier(2)
        def execute(ledger, request):
            barrier.wait()
            try:
                return ledger.execute_trade(request)
            except LedgerError:
                return None
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(execute, self.ledger, left), pool.submit(execute, second, right)]
                results = [future.result() for future in futures]
            self.assertEqual(sum(result is not None for result in results), 1)
            self.assertEqual(self.ledger.snapshot()["positions"], [])
            self.assertEqual(len(self.ledger.list_trades()), 2)
        finally:
            second.close()

    def test_quote_and_execution_freshness_identity_and_price_guards(self):
        bad_fields = {"currency": "EUR", "ticker": "OTHER", "source": "invented source",
                      "url": "https://example.com/synthetic", "price": "NaN",
                      "delay": "delayed", "quote_at": (self.now - timedelta(seconds=121)).isoformat(),
                      "fetched_at": (self.now + timedelta(seconds=6)).isoformat()}
        for field, value in bad_fields.items():
            request = self.request()
            request["quote"][field] = value
            request["quote"]["executable"] = True
            with self.subTest(field=field), self.assertRaises(LedgerError):
                self.ledger.execute_trade(request)
        for delta in (-121, 6):
            request = self.request()
            request["executed_at"] = (self.now + timedelta(seconds=delta)).isoformat()
            with self.subTest(delta=delta), self.assertRaises(LedgerError):
                self.ledger.execute_trade(request)
        request = self.request()
        request["executed_at"] = "2026-10-08T15:00:00"
        with self.assertRaisesRegex(LedgerError, "timezone"):
            self.ledger.execute_trade(request)
        self.assertEqual(self.ledger.snapshot()["cash_cents"], 20_000_000)

    def test_market_calendar_and_early_close_guards(self):
        for when in ("2026-10-10T15:00:00Z", "2026-12-25T15:00:00Z", "2026-11-27T18:01:00Z",
                     "2026-10-08T12:00:00Z", "2026-10-08T20:01:00Z"):
            self.now = stamp(when)
            with self.subTest(when=when), self.assertRaises(LedgerError):
                self.trade()

    def test_bid_ask_price_preference_and_exact_slippage(self):
        request = self.request()
        q = request["quote"]
        q.update(bid="99.90", ask="100.10", bid_at=self.now.isoformat(), ask_at=self.now.isoformat())
        self.assertEqual(fill_price(q, "BUY"), "100.1501")
        self.assertEqual(fill_price(q, "SELL"), "99.8501")
        request["price"] = fill_price(q, "BUY")
        self.assertEqual(self.ledger.execute_trade(request)["gross_cents"], 10015)

    def test_missing_or_stale_prices_do_not_become_cost_or_zero(self):
        self.trade(shares=3)
        no_quote = self.ledger.snapshot()
        self.assertFalse(no_quote["valuation_complete"])
        self.assertIsNone(no_quote["equity_cents"])
        self.assertIsNone(no_quote["unrealized_pnl_cents"])
        self.assertEqual(no_quote["unpriced_tickers"], ["SYN"])
        self.assertIsNone(no_quote["positions"][0]["market_value_cents"])
        old = quote(self.now)
        self.now += timedelta(seconds=121)
        self.assertIsNone(self.ledger.snapshot({"SYN": old})["equity_cents"])
        marked = self.ledger.snapshot({"SYN": quote(self.now)})
        self.assertEqual(marked["unrealized_pnl_cents"], -15)
        self.assertEqual(marked["equity_cents"], 19_999_985)

    def test_premarket_mark_keeps_previous_session_label_and_cannot_fill(self):
        self.now = stamp("2026-10-09T15:00:00Z")
        self.trade()
        previous_close = quote(stamp("2026-10-09T20:00:00Z"), price="105.00")
        self.now = stamp("2026-10-12T12:00:00Z")
        previous_close["fetched_at"] = self.now.isoformat()
        marked = self.ledger.snapshot({"SYN": previous_close})
        self.assertTrue(marked["valuation_complete"])
        self.assertEqual(marked["positions"][0]["valuation_label"], "previous_regular_close")
        self.assertEqual(marked["positions"][0]["valuation_session_date"], "2026-10-09")
        self.assertEqual(marked["positions"][0]["market_value_cents"], 10500)
        with self.assertRaises(LedgerError):
            self.trade()

    def test_decisions_snapshots_and_database_rows_are_immutable(self):
        self.trade()
        decision = self.ledger.list_decisions()[0]
        self.assertEqual(decision["review_dates"], ["2026-10-29", "2026-11-05"])
        saved = self.ledger.record_snapshot(idempotency_key="synthetic-report")
        self.now += timedelta(minutes=1)
        self.assertEqual(self.ledger.record_snapshot(idempotency_key="synthetic-report"), saved)
        with self.assertRaises(IdempotencyConflict):
            self.ledger.record_snapshot({"SYN": quote(self.now)}, idempotency_key="synthetic-report")
        for table in ("account", "decisions", "trades", "lots", "snapshots"):
            with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
                self.ledger._conn.execute("DELETE FROM " + table)
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger._conn.execute("UPDATE trades SET shares=2")

    def test_wash_flags_before_and_after_do_not_block_risk_exit_or_reentry(self):
        self.now = stamp("2026-10-01T15:00:00Z")
        self.trade(shares=2)
        self.now = stamp("2026-10-08T15:00:00Z")
        replacement = self.trade(shares=1, price="110")
        self.trade("SELL", shares=2, price="90")
        export = self.ledger.export_tax(2026)
        self.assertTrue(export["dispositions"][0]["possible_wash_sale"])
        self.assertIn(replacement["trade_id"], export["dispositions"][0]["possible_replacement_trade_ids"])
        self.assertTrue(export["tax_review_required"])
        self.assertTrue(export["not_for_filing"])
        self.now = stamp("2026-10-09T15:00:00Z")
        self.trade("SELL", shares=1, price="90")
        before = self.ledger.export_tax(2026)
        self.assertFalse(before["dispositions"][-1]["possible_wash_sale"])
        self.assertTrue(before["dispositions"][-1]["wash_window_open"])
        self.now = stamp("2026-10-12T15:00:00Z")
        self.trade(shares=1, price="80")
        after = self.ledger.export_tax(2026)
        self.assertTrue(after["dispositions"][-1]["possible_wash_sale"])

    def test_initial_purchase_fully_sold_is_not_its_own_wash_replacement(self):
        self.trade()
        self.trade("SELL", price="90")
        result = self.ledger.export_tax(2026)
        self.assertFalse(result["dispositions"][0]["possible_wash_sale"])
        self.now = stamp("2026-11-09T15:00:00Z")
        self.assertFalse(self.ledger.export_tax(2026)["tax_review_required"])

    def test_same_timestamp_sales_use_append_order_for_wash_review(self):
        self.trade(shares=2)
        self.trade("SELL", price="90")
        self.trade("SELL", price="90")
        rows = self.ledger.export_tax(2026)["dispositions"]
        self.assertTrue(rows[0]["possible_wash_sale"])
        self.assertFalse(rows[1]["possible_wash_sale"])

    def test_new_ledger_file_is_private(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_tax_year_boundary_and_later_january_wash_flag(self):
        self.now = stamp("2026-12-30T15:00:00Z")
        self.trade()
        self.now = stamp("2026-12-31T15:00:00Z")
        self.trade("SELL", price="90")
        before = self.ledger.export_tax(2026)
        self.assertEqual(len(before["dispositions"]), 1)
        self.assertEqual(self.ledger.export_tax(2027)["dispositions"], [])
        self.now = stamp("2027-01-04T15:00:00Z")
        self.trade(price="85")
        self.assertTrue(self.ledger.export_tax(2026)["dispositions"][0]["possible_wash_sale"])

    def test_more_than_one_year_uses_trade_dates(self):
        self.now = stamp("2026-01-02T15:00:00Z")
        self.trade()
        self.now = stamp("2027-01-04T15:00:00Z")
        self.trade("SELL", price="150")
        exported = self.ledger.export_tax(2027)
        self.assertEqual(exported["dispositions"][0]["holding_term"], "LONG")
        self.assertEqual(exported["dispositions"][0]["acquisition_date"], "2026-01-02")
        self.assertEqual(exported["long_term_realized_cents"], 4988)
        self.assertEqual(exported["short_term_realized_cents"], 0)

    def test_old_actions_do_not_block_new_entry_but_exposed_action_persists(self):
        old = self.request()
        old["quote"]["corporate_actions"] = [{"kind": "dividend", "at": "2026-10-07T13:30:00Z", "amount": "0.25"}]
        self.ledger.execute_trade(old)
        self.assertEqual(self.ledger.list_events(), [])
        self.now = stamp("2026-10-09T15:00:00Z")
        action_quote = quote(self.now)
        action_quote["corporate_actions"] = [{"kind": "split", "at": "2026-10-09T13:30:00Z", "numerator": "2", "denominator": "1"}]
        held = self.ledger.snapshot({"SYN": action_quote})
        self.assertFalse(held["valuation_complete"])
        self.assertEqual(len(self.ledger.list_events()), 1)
        self.ledger.snapshot({"SYN": action_quote})
        self.assertEqual(len(self.ledger.list_events()), 1)
        # The short provider window can forget the action; the ledger cannot.
        self.now = stamp("2026-10-19T15:00:00Z")
        self.assertIsNone(self.ledger.snapshot({"SYN": quote(self.now)})["equity_cents"])
        with self.assertRaisesRegex(LedgerError, "accounting hold"):
            self.trade("SELL")

    def test_detected_action_survives_failed_trade_transaction(self):
        self.trade()
        self.now = stamp("2026-10-09T15:00:00Z")
        request = self.request("SELL")
        request["quote"]["corporate_actions"] = [{"kind": "dividend", "at": "2026-10-09T13:30:00Z", "amount": "1.00"}]
        with self.assertRaisesRegex(LedgerError, "accounting hold"):
            self.ledger.execute_trade(request)
        self.assertEqual(len(self.ledger.list_events()), 1)
        self.assertEqual(len(self.ledger.list_trades()), 1)
        self.assertTrue(self.ledger.export_tax(2026)["tax_review_required"])


if __name__ == "__main__":
    unittest.main()
