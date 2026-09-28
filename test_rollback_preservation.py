"""S346 actual historical-writer rehearsals. Scratch state only; no external I/O.

Requires the pinned historical Git object locally; never fetches missing history.
The safe procedure preserves a verified export, not transparent old-code support.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from supervisor import alert_policy, opus_approval as approval, state_checkpoint

BASELINE = 'ba68a03f332872c713b7c3b84be6deb49a6bcf09'
ROOT = Path(__file__).resolve().parent


def historical(name):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    source = subprocess.check_output(
        ['git', '-C', str(ROOT), 'show', BASELINE + ':supervisor/' + name + '.py'], env=env)
    mod = types.ModuleType('historical_' + name)
    exec(compile(source, '<pinned historical ' + name + '>', 'exec'), mod.__dict__)
    return mod


class RollbackPreservation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_policy = historical('alert_policy')
        cls.old_approval = historical('opus_approval')

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        for module in (approval, self.old_approval):
            for name, value in [('STATE_DIR', self.state),
                                ('REQUEST_FILE', self.state / 'pending-request.json'),
                                ('UPDATE_OFFSET_FILE', self.state / 'telegram-update-offset.txt')]:
                p = patch.object(module, name, value); p.start(); self.addCleanup(p.stop)
            for name in ('_api_call', '_load_secrets'):
                p = patch.object(module, name, side_effect=AssertionError('external access forbidden'))
                p.start(); self.addCleanup(p.stop)
        p = patch.object(approval, 'LAST_REQUEST_PATH', None); p.start(); self.addCleanup(p.stop)
        for target in ('socket.socket', 'socket.create_connection', 'subprocess.Popen', 'os.system'):
            p = patch(target, side_effect=AssertionError('network/process forbidden'))
            p.start(); self.addCleanup(p.stop)
        with patch.object(approval.time, 'time', return_value=1000):
            for identity in ('primary', 'queued'):
                approval.create_guidance_request(identity, 'next action?', identity)
                approval.record_request_delivery(True)
        self.rows = approval.requests()
        self.incidents = self.state / 'heartbeat-incidents.json'
        self.incidents.write_text(json.dumps({'incidents': {
            'unit:fixture.service': {'first_seen': 900, 'last_seen': 1000,
                'reviewed': True, 'owner': 'Cowork', 'state': 'action pending',
                'disposition': 'inspect fixture', 'next_attempt': 2000}},
            'resolved': [{'incident': 'older', 'state': 'resolved', 'recovery_evidence': 'two probes'}]}))
        approval.UPDATE_OFFSET_FILE.write_text('55')
        self.export = self.root / 'protected-export'
        self.manifest = state_checkpoint.capture(self.state, self.export)

    def test_actual_old_incident_writer_loses_history_export_preserves_it(self):
        old = self.old_policy.IncidentPolicy(self.incidents)
        original = old.active.copy()
        old.save()
        self.assertFalse(old.persistence_error)
        written = json.loads(self.incidents.read_text())
        self.assertEqual(written['incidents'], original)
        self.assertNotIn('resolved', written)  # negative control: NOT transparent rollback
        self.assertEqual(state_checkpoint.verify(self.export), self.manifest)
        archived = alert_policy.IncidentPolicy(self.export / self.incidents.name)
        self.assertEqual(archived.resolved[0]['recovery_evidence'], 'two probes')
        self.assertEqual(archived.active['unit:fixture.service']['owner'], 'Cowork')

    def test_old_approval_cannot_consume_extra_queue_but_reupgrade_can(self):
        rid = self.rows[1][1]['request_id']
        self.assertTrue(approval.answer(rid, 'inspect queued fixture', at=1001))
        queue_before = self.rows[1][0].read_bytes()
        self.assertIsNone(self.old_approval.consume_guidance())
        self.assertEqual(self.rows[1][0].read_bytes(), queue_before)
        self.assertIn('inspect queued fixture', approval.consume_guidance())
        self.assertIsNone(approval.consume_guidance())
        self.assertEqual(approval.requests()[0][1]['status'], 'pending')
        self.assertEqual(approval.requests()[1][1]['status'], 'consumed')

    def test_newer_consumption_and_receipts_survive_code_only_reupgrade(self):
        primary = self.rows[0][1]['request_id']
        self.assertTrue(approval.answer(primary, 'inspect primary', at=1001))
        self.assertEqual(self.old_approval.consume_guidance(), 'inspect primary')
        self.assertIsNone(approval.consume_guidance())  # must not replay consumed work
        with patch.object(approval.time, 'time', return_value=1100):
            approval.create_guidance_request('third', 'next?', 'third')
            approval.record_request_delivery(True)
        approval.UPDATE_OFFSET_FILE.write_text('99')
        before = {str(p.relative_to(self.state)): p.read_bytes()
                  for p in state_checkpoint.state_files(self.state)}
        self.old_policy.IncidentPolicy(self.incidents).save()
        current = alert_policy.IncidentPolicy(self.incidents)
        self.assertIn('unit:fixture.service', current.active)
        for name, data in before.items():
            if name != self.incidents.name:
                self.assertEqual((self.state / name).read_bytes(), data)
        sender = Mock(side_effect=AssertionError('unexpected delivery replay'))
        with patch.object(approval.time, 'time', return_value=1200):
            approval.retry_failed_deliveries(sender)
            self.assertIsNone(approval.create_guidance_request('third', 'next?', 'third'))
        sender.assert_not_called()
        self.assertEqual(approval.UPDATE_OFFSET_FILE.read_text(), '99')
        self.assertEqual((self.export / 'telegram-update-offset.txt').read_text(), '55')
        self.assertEqual(state_checkpoint.verify(self.export), self.manifest)
        self.assertEqual(len(approval.requests()), 3)

    def test_export_detects_tampered_receipt_and_refuses_overwrite(self):
        with self.assertRaises(FileExistsError):
            state_checkpoint.capture(self.state, self.export)
        receipt = self.export / 'pending-request.json'
        receipt.write_text('{"status":"pending","delivery_attempted_at":0}')
        with self.assertRaisesRegex(ValueError, 'checkpoint hash differs'):
            state_checkpoint.verify(self.export)


if __name__ == '__main__':
    unittest.main()
