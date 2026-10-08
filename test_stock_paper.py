"""Workflow integration checks: temporary account, synthetic source data, no sends.

These tests call the real workflow and ledger together. Only the wall clock and
public quote transport are replaced; cash, lots, order persistence, replay and
report rendering use their production implementations.
"""

from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from datetime import timedelta
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import stock_paper as workflow
from stock_paper_ledger import Ledger
from stock_paper_market import market_session, validate_mark
from test_stock_paper_market import AT, quote as source_quote


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-paper-workflow-')
        self.home = Path(self.temp.name)
        self.at = AT
        self.prices = {'SYN': '100.00', 'SPY': '200.00'}
        self.quote_times = {}
        self.fetched = []
        self.ledger = Ledger(self.home / 'ledger.sqlite3', now=lambda: self.at)
        self.patches = [patch.object(workflow, 'now', side_effect=lambda: self.at),
                        patch.object(workflow, 'get_quote', side_effect=self.provider),
                        patch.object(workflow, 'market_session', side_effect=lambda: market_session(self.at))]
        for item in self.patches:
            item.start()
        workflow.initialize(self.home, self.ledger)
        self.fetched.clear()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.ledger.close()
        self.temp.cleanup()

    def provider(self, ticker, archive_dir=None):
        self.fetched.append(ticker)
        return source_quote(at=self.at, ticker=ticker,
                            longName='Synthetic ' + ticker + ' Corporation',
                            regularMarketPrice=self.prices.get(ticker, '100.00'),
                            regularMarketTime=int(self.quote_times.get(ticker, self.at).timestamp()))

    def request(self, key='fixture', action='BUY', shares=10, budget=None, limit='101.00'):
        order = {'idempotency_key': key + ':order', 'limit_price': limit}
        order.update({'shares': shares} if budget is None else {'budget': budget})
        data = {'idempotency_key': key + ':decision', 'ticker': 'SYN', 'action': action,
                'reason': 'Synthetic workflow verification; not an investment recommendation',
                'strategy_version': 'synthetic-test-v1', 'valuation': 'Synthetic price constraint',
                'counterargument': 'Synthetic adverse case', 'exit_trigger': 'Synthetic thesis fails',
                'review_dates': ['2026-10-29'],
                'evidence': [{'url': 'https://example.org/synthetic-source',
                              'published': '2026-10-08', 'claim': 'Synthetic fixture only'}],
                'order': order}
        return {'request_id': key, 'decisions': [data]}

    def execute(self, request=None):
        return workflow.apply(self.home, self.ledger, request or self.request())

    def test_request_replay_returns_original_outcome_without_refetch_or_cash_change(self):
        request = self.request()
        first = self.execute(request)
        self.assertEqual(first['outcomes'][0]['status'], 'filled')
        cash = self.ledger.snapshot()['cash_cents']
        self.at += timedelta(days=1)
        self.prices['SYN'] = '900.00'
        self.fetched.clear()
        second = self.execute(deepcopy(request))
        self.assertEqual(second, first)
        self.assertEqual(self.fetched, [])
        self.assertEqual(len(self.ledger.list_trades()), 1)
        self.assertEqual(len(self.ledger.list_decisions()), 1)
        self.assertEqual(self.ledger.snapshot()['cash_cents'], cash)

    def test_changed_request_or_order_ids_cannot_repurpose_a_completed_fill(self):
        request = self.request()
        self.execute(request)
        changed = deepcopy(request)
        changed['decisions'][0]['order']['shares'] = 11
        with self.assertRaisesRegex(ValueError, 'changed_instructions'):
            self.execute(changed)
        changed['request_id'] = 'different-request'
        changed['decisions'][0]['idempotency_key'] = 'different-decision'
        with self.assertRaisesRegex(ValueError, 'order_id_reused_with_changed_decision'):
            self.execute(changed)
        self.assertEqual(len(self.ledger.list_trades()), 1)

    def test_crash_after_ledger_commit_recovers_once_even_after_quote_expiry(self):
        request = self.request()
        execute = self.ledger.execute_trade
        def commit_then_interrupt(payload):
            execute(payload)
            raise RuntimeError('synthetic interrupted process after durable ledger commit')
        with patch.object(self.ledger, 'execute_trade', side_effect=commit_then_interrupt):
            with self.assertRaisesRegex(RuntimeError, 'durable ledger commit'):
                self.execute(request)
        self.assertEqual(len(self.ledger.list_trades()), 1)
        self.assertFalse((self.home / 'requests' / 'fixture' / 'result.json').exists())
        order_path = next((self.home / 'orders').glob('*.json'))
        self.assertNotIn('outcome', json.loads(order_path.read_text()))
        cash = self.ledger.snapshot()['cash_cents']
        self.at += timedelta(days=1)
        self.fetched.clear()
        recovered = self.execute(request)
        self.assertEqual(recovered['outcomes'][0]['status'], 'filled')
        self.assertEqual(self.fetched, [])
        self.assertEqual(len(self.ledger.list_trades()), 1)
        self.assertEqual(self.ledger.snapshot()['cash_cents'], cash)
        self.assertEqual(json.loads(order_path.read_text())['outcome'], recovered['outcomes'][0])

    def test_insufficient_cash_is_held_and_retry_never_spends_new_money(self):
        request = self.request(shares=2000)
        result = self.execute(request)
        self.assertEqual(result['outcomes'][0]['status'], 'held')
        self.assertIn('settled', result['outcomes'][0]['reason'])
        self.assertEqual(self.ledger.list_trades(), [])
        self.assertEqual(self.ledger.snapshot()['cash_cents'], 20_000_000)
        self.prices['SYN'] = '1.00'
        self.assertEqual(self.execute(request), result)
        self.assertEqual(self.ledger.list_trades(), [])

    def test_price_ceiling_applies_to_adverse_fill_not_unadjusted_last_trade(self):
        result = self.execute(self.request(limit='100.00'))
        outcome = result['outcomes'][0]
        self.assertEqual(outcome['status'], 'held')
        self.assertEqual(outcome['reason'], 'price_outside_limit')
        self.assertEqual(outcome['observed_price'], '100.0500')
        self.assertEqual(self.ledger.list_trades(), [])

    def test_sell_price_floor_and_owned_shares_are_enforced(self):
        self.execute(self.request(key='buy', shares=10))
        held = self.execute(self.request(key='sale-floor', action='SELL', shares=10, limit='100.00'))
        self.assertEqual(held['outcomes'][0]['reason'], 'price_outside_limit')
        short = self.execute(self.request(key='short-sale', action='SELL', shares=11, limit='99.00'))
        self.assertEqual(short['outcomes'][0]['status'], 'held')
        self.assertIn('short', short['outcomes'][0]['reason'])
        self.assertEqual(len(self.ledger.list_trades()), 1)

    def test_caller_fill_and_timestamp_fields_refused_before_recording(self):
        for field, value in (('price', '1.00'), ('quote', {'executable': True}),
                              ('executed_at', self.at.isoformat()), ('fees', '-1.00')):
            request = self.request(key='injected-' + field)
            request['decisions'][0]['order'][field] = value
            with self.assertRaisesRegex(ValueError, 'caller_fill_price'):
                self.execute(request)
        self.assertEqual(self.ledger.list_decisions(), [])
        self.assertEqual(self.ledger.list_trades(), [])
        self.assertFalse((self.home / 'requests').exists())
        self.assertEqual(self.fetched, [])

    def test_budget_buys_only_whole_shares_below_exact_budget(self):
        result = self.execute(self.request(budget='1000.00'))
        trade = result['outcomes'][0]['trade']
        self.assertEqual(trade['shares'], 9)
        self.assertEqual(trade['gross_cents'], 90_045)
        self.assertEqual(self.ledger.snapshot()['cash_cents'], 19_909_955)

    def test_refresh_report_numbers_equal_recorded_ledger_snapshot(self):
        self.execute()
        self.at += timedelta(minutes=1)
        self.prices.update(SYN='110.00', SPY='210.00')
        report = workflow.refresh(self.home, self.ledger, 'pm')
        snapshot = report['snapshot']
        self.assertEqual(snapshot, self.ledger.list_snapshots()[0])
        self.assertEqual(snapshot['cash_cents'], 19_899_950)
        self.assertEqual(snapshot['positions'][0]['cost_basis_cents'], 100_050)
        self.assertEqual(snapshot['positions'][0]['market_value_cents'], 110_000)
        self.assertEqual(snapshot['total_pnl_cents'], 9_950)
        self.assertIn('| Cash | $198,999.50 |', report['report'])
        self.assertIn('| Shares plus cash | $200,099.50 |', report['report'])
        self.assertIn('| SYN | 10 | $1,000.50 | $110.00 | $1,100.00 | $99.50 |', report['report'])
        self.assertEqual(report['benchmark']['return_percent'], '5.00')
        self.assertEqual(json.loads((self.home / 'latest.json').read_text()), report)
        self.assertEqual(json.loads((self.home / 'reports' / (report['report_id'] + '.json')).read_text()), report)

    def test_missing_current_mark_does_not_fabricate_equity_or_gain(self):
        self.execute()
        self.at += timedelta(minutes=5)
        self.quote_times['SYN'] = AT
        report = workflow.refresh(self.home, self.ledger, 'trial')
        self.assertIsNone(report['snapshot']['equity_cents'])
        self.assertIsNone(report['snapshot']['total_pnl_cents'])
        self.assertIn('| Shares plus cash | unavailable |', report['report'])
        self.assertIn('valuation incomplete', report['report'])
        self.assertNotIn('Account return:', report['report'])

    def test_premarket_quote_can_mark_previous_close_but_cannot_execute(self):
        self.execute(self.request(key='day-one', shares=10))
        self.at = AT.replace(day=9, hour=11, minute=45)
        close = AT.replace(hour=20, minute=0)
        self.quote_times.update(SYN=close, SPY=close)
        self.prices.update(SYN='110.00', SPY='210.00')
        result = self.execute(self.request(key='morning-order'))
        self.assertEqual(result['outcomes'][0]['status'], 'held')
        self.assertEqual(len(self.ledger.list_trades()), 1)
        report = workflow.refresh(self.home, self.ledger, 'am')
        self.assertEqual(report['snapshot']['equity_cents'], 20_009_950)
        self.assertEqual(report['snapshot']['positions'][0]['valuation_label'], 'previous_regular_close')
        self.assertIn('previous_regular_close', report['report'])
        self.assertIn('not a new executable price', report['report'])

    def test_benchmark_uses_eligible_closed_mark_with_preserved_quote_date(self):
        self.at = AT.replace(day=9, hour=11, minute=45)
        close = AT.replace(hour=20, minute=0)
        self.quote_times['SPY'] = close
        self.prices['SPY'] = '210.00'
        quote = self.provider('SPY')
        self.assertFalse(quote['executable'])
        self.assertTrue(validate_mark(quote, now=self.at)['valuation_eligible'])
        reference = workflow.benchmark(self.home, quote)
        self.assertTrue(reference['available'], reference)
        self.assertEqual(reference['return_percent'], '5.00')
        self.assertEqual(reference['quote_at'], '2026-10-08T20:00:00Z')

    def test_benchmark_revalidates_identity_and_age_instead_of_trusting_flag(self):
        wrong = self.provider('SYN')
        self.assertTrue(wrong['executable'])
        self.assertFalse(workflow.benchmark(self.home, wrong)['available'])
        stale = self.provider('SPY')
        self.at += timedelta(minutes=3)
        self.assertTrue(stale['executable'])  # Stored old flag is deliberately stale.
        self.assertFalse(workflow.benchmark(self.home, stale)['available'])


class BridgeTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'runner' / 'stock_paper.py'
        if not path.exists():
            self.skipTest('Mac bridge is maintained outside the deployed server checkout')
        spec = importlib.util.spec_from_file_location('stock_paper_bridge_fixture', path)
        self.bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bridge)
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-paper-bridge-')
        self.private = Path(self.temp.name)
        self.bridge.PRIVATE = self.private

    def tearDown(self):
        if hasattr(self, 'temp'):
            self.temp.cleanup()

    def run_bridge(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        previous_umask = os.umask(0o077)
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = self.bridge.main(argv)
        finally:
            os.umask(previous_umask)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_apply_forwards_exact_private_json_and_archives_remote_result(self):
        folder = self.private / 'requests'
        folder.mkdir()
        request = {'request_id': 'bridge-fixture', 'decisions': [{'action': 'HOLD'}]}
        raw = json.dumps(request)
        (folder / 'request.json').write_text(raw)
        result = {'status': 'complete', 'request_id': 'bridge-fixture', 'outcomes': []}
        completed = subprocess.CompletedProcess([], 0, json.dumps(result), '')
        with patch.object(self.bridge.subprocess, 'run', return_value=completed) as transport:
            code, stdout, stderr = self.run_bridge(['apply', '--file', 'request.json'])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(transport.call_args.kwargs['input'], raw)
        self.assertIn('cumulus1', transport.call_args.args[0])
        summary = json.loads(stdout)
        self.assertEqual(json.loads(Path(summary['result_file']).read_text()), result)
        self.assertEqual(json.loads((self.private / 'apply-latest.json').read_text()), result)

    def test_unsafe_request_filename_never_reaches_transport(self):
        with patch.object(self.bridge.subprocess, 'run') as transport:
            code, stdout, stderr = self.run_bridge(['apply', '--file', '../config.json'])
        self.assertEqual(code, 2)
        self.assertFalse(transport.called)
        self.assertEqual(stdout, '')
        self.assertIn('simple_private_json_request_filename_required', stderr)

    def test_transport_timeout_retains_unknown_outcome_for_safe_replay(self):
        with patch.object(self.bridge.subprocess, 'run', side_effect=subprocess.TimeoutExpired('synthetic', 1)):
            code, stdout, stderr = self.run_bridge(['status'])
        self.assertEqual(code, 2)
        self.assertEqual(stdout, '')
        self.assertEqual(json.loads(stderr)['status'], 'unknown')
        self.assertIn('same_request_and_order_ids', json.loads(stderr)['reason'])


if __name__ == '__main__':
    unittest.main()
