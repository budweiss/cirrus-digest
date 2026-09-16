"""S181 regressions: configuration drift, missing spend, and ticket authority.

Offline only: temporary files and mocked providers; no live sends or ledgers.
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import llm_budget
import llm_providers
import runtime_config


class ReliabilityTests(unittest.TestCase):
    def test_linux_rejects_mac_paths(self):
        with self.assertRaises(ValueError):
            runtime_config.validate({'digest': {'output_dir': '/Users/x', 'log_dir': '/tmp/log'}}, 'Linux')

    def test_overlay_survives_replaced_git_config(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'sources.json'
            overlay = {'digest': {'output_dir': td + '/digests', 'log_dir': td + '/logs'},
                       'email': {'accounts': [{'label': 'cumulus-research', 'enabled': True}]}}
            p.with_name('runtime.local.json').write_text(json.dumps(overlay))
            for n in range(2):
                p.write_text(json.dumps({'digest': {'output_dir': '/Users/x', 'log_dir': '/Users/y'},
                                         'web_sources': [n], 'email': {'accounts': [{'label': 'other'}]}}))
                data = runtime_config.check(p, 'cumulus-research')
                self.assertEqual(data['digest']['output_dir'], td + '/digests')
                self.assertEqual(data['web_sources'], [n])
                self.assertEqual(len(data['email']['accounts']), 2)

    def test_corrupt_overlay_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'sources.json'
            p.write_text('{}')
            p.with_name('runtime.local.json').write_text('{broken')
            with self.assertRaises(ValueError):
                runtime_config.load_sources(p)

    def test_sdk_cost_is_exact_and_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            pricing = Path(td) / 'pricing.json'
            ledger = Path(td) / 'ledger.jsonl'
            pricing.write_text(json.dumps({'models': {'m': {'in': 1, 'out': 2}}}))
            creds = {'llm_budget': {'pricing_path': str(pricing), 'ledger_path': str(ledger), 'box': 'test'}}
            for _ in range(2):
                llm_budget.record_sdk_cost(creds, .3542, task='alopecia-agent:coordinator', run_id='test-1')
            rows = [json.loads(l) for l in ledger.read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['cost'], .3542)
            from alopecia_agent import budget
            self.assertEqual(budget.spent_this_month(creds), .3542)
            ledger.write_text('{corrupt\n')
            self.assertFalse(budget.allow(1, creds)[0])
            with self.assertRaises(ValueError):
                llm_budget.record_sdk_cost(creds, 1, task='x', run_id='test-2')

    def test_billed_empty_retry_and_success_are_both_counted(self):
        count = 0
        def provider(*args):
            nonlocal count
            count += 1
            llm_providers._LAST.usage = {'input': 10, 'output': 20}
            return '' if count == 1 else 'answer'
        with patch.dict(llm_providers._PROVIDERS, {'test': provider}), patch.object(llm_providers, '_record') as rec:
            self.assertEqual(llm_providers.call('test', '', '', {}, 100, retries=1), 'answer')
            self.assertEqual(rec.call_count, 2)

    def test_transport_error_does_not_reuse_old_usage(self):
        llm_providers._LAST.usage = {'input': 999, 'output': 999}
        with patch.dict(llm_providers._PROVIDERS, {'test': lambda *a: (_ for _ in ()).throw(OSError())}), patch.object(llm_providers, '_record') as rec:
            with self.assertRaises(OSError):
                llm_providers.call('test', '', '', {}, 100)
            rec.assert_not_called()

    def test_failed_job_preserves_last_success(self):
        import job_status
        with tempfile.TemporaryDirectory() as td, patch.object(job_status, 'STATUS_PATH', Path(td)/'jobs.json'):
            job_status.record('routing', True, 'done')
            job_status.record('routing', False, 'failed: config')
            row = json.loads(job_status.STATUS_PATH.read_text())['routing']
            self.assertFalse(row['ok'])
            self.assertIn('last_success', row)

    def test_ticket_policy_covers_batch_without_restart(self):
        folder = Path(__file__).parent / 'supervisor'
        sys.path.insert(0, str(folder))
        try:
            spec = importlib.util.spec_from_file_location('ops_tools', folder / 'tools.py')
            tools = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(tools)
            self.assertIn('halftime-routing.service', tools.ticket_units())
            self.assertIn('alopecia-agent.service', tools.ticket_units())
            self.assertNotIn('halftime-routing.service', tools.ALLOWED_UNITS)
            self.assertNotIn('sshd.service', tools.ticket_units())
        finally:
            sys.path.pop(0)

    def test_sdk_dry_run_has_no_write_or_send_tools(self):
        try:
            from alopecia_agent import agent
        except ModuleNotFoundError as exc:
            if exc.name == 'claude_agent_sdk':
                self.skipTest('SDK check runs in the installed server environment')
            raise
        names = {t.name for t in agent._build_mcp_tools(dry_run=True)}
        self.assertEqual(names, {'read_kb','read_new_etiology_items','read_hypothesis_state','call_local','call_council'})

    def test_failed_completion_is_unhealthy_despite_live_wrapper(self):
        spec = importlib.util.spec_from_file_location('ops_completeness', Path(__file__).parent/'supervisor/completeness.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        result = mod.check({'intake': {'ok': False, 'epoch': __import__('time').time(), 'note': 'missing account'}}, {})
        self.assertFalse(result['ok'])
        self.assertEqual(result['failed_runs'], ['intake'])


if __name__ == '__main__':
    unittest.main()
