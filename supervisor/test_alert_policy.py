"""Offline tests; temporary state, no Telegram, SDK calls or live services."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from alert_policy import IncidentPolicy, issues, RETRY_SECONDS
import opus_approval


def hb(units=(), **kwargs):
    return dict(ok=not units, failed_units=list(units), credentials_ok=True,
                scan_degraded=False, completeness={'ok': True}, **kwargs)


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.p = IncidentPolicy(self.root/'incidents.json')

    def test_unchanged_reviewed_problem_stays_quiet_across_days_and_restart(self):
        self.assertEqual(self.p.observe(hb(['a.service']), 100), ['unit:a.service'])
        self.p.complete(True, 101)
        for now in [160, 21601, 90000]:
            self.assertEqual(self.p.observe(hb(['a.service']), now), [])
        restarted = IncidentPolicy(self.p.path)
        self.assertEqual(restarted.observe(hb(['a.service']), 95000), [])
        self.assertEqual(restarted.observe(hb(['a.service', 'b.service']), 95060), ['unit:b.service'])

    def test_recovery_rearms_but_single_good_tick_does_not(self):
        self.p.observe(hb(['a.service']), 100); self.p.complete(True, 101)
        self.p.observe(hb(), 160)
        self.assertEqual(self.p.observe(hb(['a.service']), 220), [])
        self.p.observe(hb(), 280); self.p.observe(hb(), 400)
        self.assertEqual(self.p.observe(hb(['a.service']), 460), ['unit:a.service'])

    def test_failure_retries_and_crash_reservation_survives_restart(self):
        self.p.observe(hb(['a.service']), 100)
        p = IncidentPolicy(self.p.path)
        self.assertEqual(p.observe(hb(['a.service']), 160), [])
        p.complete(False, 160)
        self.assertEqual(p.observe(hb(['a.service']), 160 + RETRY_SECONDS), ['unit:a.service'])

    def test_degraded_probe_does_not_fake_recovery(self):
        self.p.observe(hb(['a.service']), 100); self.p.complete(True, 101)
        degraded = hb(); degraded['scan_degraded'] = True
        self.p.observe(degraded, 160); self.p.observe(degraded, 400)
        self.assertIn('unit:a.service', self.p.active)
        self.assertNotIn('unit:a.service', self.p.observe(hb(['a.service']), 460))

    def test_failed_completions_are_distinct_and_text_drift_is_not(self):
        h = hb(); h['completeness'] = {'ok': False, 'failed_runs': ['intake']}
        self.assertEqual(issues(h), {'failed_runs:intake'})
        self.p.observe(h, 100); self.p.complete(True, 101)
        h['detail'] = 'different free text 999'
        self.assertEqual(self.p.observe(h, 160), [])
        h['completeness']['failed_runs'].append('pedagogy')
        self.assertEqual(self.p.observe(h, 220), ['failed_runs:pedagogy'])

    def test_unknown_completeness_error_and_reply_are_visible(self):
        h = hb(); h['completeness'] = {'ok': False, 'detail': 'raised'}
        h['reply_id'] = 'guidance:100'
        self.assertEqual(issues(h), {'completeness-unavailable', 'reply:guidance:100'})

    def test_corrupt_or_unwritable_state_does_not_cause_per_tick_paid_calls(self):
        self.p.path.write_text('{bad')
        p = IncidentPolicy(self.p.path)
        self.assertIn('incident-state-unavailable', p.observe(hb(), 100))
        p.complete(True, 101)
        self.assertEqual(p.observe(hb(), 160), [])
        with patch('alert_policy.os.replace', side_effect=OSError('disk full')):
            p.observe(hb(['a.service']), 220); p.complete(True, 221)
            p.observe(hb(['a.service']), 280); p.complete(True, 281)
            self.assertEqual(p.observe(hb(['a.service']), 340), [])

    def test_guidance_duplicates_and_pending_reply_are_preserved(self):
        with patch.object(opus_approval, 'STATE_DIR', self.root), \
             patch.object(opus_approval, 'REQUEST_FILE', self.root/'request.json'), \
             patch.object(opus_approval.time, 'time', return_value=100):
            text = opus_approval.create_guidance_request('issue', 'question', 'incident@1')
            self.assertIsInstance(text, str)
            opus_approval.record_request_delivery(True)
            original = opus_approval.REQUEST_FILE.read_bytes()
            self.assertIsNone(opus_approval.create_guidance_request('different', 'different', 'other'))
            self.assertIsNone(opus_approval.create_opus_request('reason'))
            self.assertEqual(opus_approval.REQUEST_FILE.read_bytes(), original)
            req = json.loads(original); req['status'] = 'expired'
            opus_approval.REQUEST_FILE.write_text(json.dumps(req))
            self.assertIsNone(opus_approval.create_guidance_request('rephrased', 'again?', 'incident@1'))
            self.assertIsInstance(opus_approval.create_guidance_request('new recurrence', '?', 'incident@2'), str)
            opus_approval.record_request_delivery(False)
            self.assertIsInstance(opus_approval.create_guidance_request('retry send', '?', 'incident@2'), str)
            req = json.loads(opus_approval.REQUEST_FILE.read_text()); req['status'] = 'answered'
            opus_approval.REQUEST_FILE.write_text(json.dumps(req))
            self.assertTrue(opus_approval.ready_reply_id())
            self.assertIsNone(opus_approval.create_opus_request('do not overwrite reply'))


    def test_guidance_waits_seven_days_and_stale_or_negative_approval_is_not_approval(self):
        with patch.object(opus_approval, 'STATE_DIR', self.root), \
             patch.object(opus_approval, 'REQUEST_FILE', self.root/'request.json'), \
             patch.object(opus_approval, 'UPDATE_OFFSET_FILE', self.root/'offset.txt'), \
             patch.object(opus_approval.time, 'time', return_value=100):
            opus_approval.create_guidance_request('issue', 'question', 'one')
            with patch.object(opus_approval.time, 'time', return_value=100 + 3*3600):
                self.assertTrue(opus_approval._request_slot_busy())
            opus_approval.REQUEST_FILE.write_text(json.dumps({'kind': 'opus_upgrade',
                'status': 'pending', 'requested_at': 100}))
            with patch.object(opus_approval, '_load_secrets', return_value={
                    'telegram_bot_token': 'test', 'telegram_user_id': '1'}):
                for date, text in [(99, 'approve'), (101, 'do not approve')]:
                    with patch.object(opus_approval, '_api_call', return_value={'result': [{
                        'update_id': 1, 'message': {'from': {'id': 1}, 'date': date, 'text': text}}]}):
                        opus_approval.check_for_reply()
                    self.assertEqual(json.loads(opus_approval.REQUEST_FILE.read_text())['status'], 'pending')

    def test_sdk_error_does_not_acknowledge_incident_and_keeps_known_cost(self):
        try:
            import supervisor_agent as agent
        except ModuleNotFoundError as exc:
            if exc.name == 'claude_agent_sdk':
                self.skipTest('SDK test runs in the real supervisor venv')
            raise
        import asyncio
        class Result:
            total_cost_usd = .25
            is_error = True
        async def fake_query(**kwargs):
            yield Result()
        with patch.object(agent, '_load_secrets', return_value={'anthropic_api_key': 'test'}), \
             patch.object(agent, '_build_mcp_tools', return_value=[]), \
             patch.object(agent, 'create_sdk_mcp_server', return_value={}), \
             patch.object(agent, 'ResultMessage', Result), \
             patch.object(agent, 'query', fake_query), \
             patch.object(agent.opus_approval, 'consume_opus_approval', return_value=False), \
             patch.object(agent.opus_approval, 'consume_guidance', return_value=None):
            with self.assertRaises(RuntimeError) as caught:
                asyncio.run(agent.run_reasoning_pass('offline test'))
            self.assertEqual(caught.exception.cost, .25)
        with patch.object(agent.budget, 'allow', return_value=(True, 0, 'ok')), \
             patch.object(agent, 'run_reasoning_pass', side_effect=caught.exception), \
             patch.object(agent.budget, 'record') as record, \
             patch.object(agent, 'ledger_append'):
            self.assertFalse(agent._handle_trigger('offline test', False))
            self.assertEqual(record.call_args.args[0], .25)


if __name__ == '__main__':
    unittest.main()
