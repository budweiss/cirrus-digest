"""S346: real Snow orchestration with scratch state and a simulated sender.

No real credentials, provider, SMTP, subprocess or socket is available. This
rehearses decision/delivery boundaries, not unattended scheduling or live mail.
The provider/admission path is separately exercised by test_snow_failure_pipeline.
"""
import contextlib
import io
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import send_guard
from snowbrief import bill_snow_weekly as snow


class ControlledSnow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in [('OUT', self.root / 'out'), ('DIGEST_DIR', self.root),
                            ('TO', 'recipient@example.invalid'), ('CC', 'copy@example.invalid')]:
            self.stack.enter_context(patch.object(snow, name, value))
        self.stack.enter_context(patch.object(send_guard, 'STAMP_DIR', self.root / 'receipts'))
        # NamedTemporaryFile used by the real sender path stays within our scratch.
        self.stack.enter_context(patch.object(tempfile, 'tempdir', str(self.root)))
        self.stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch('socket.create_connection', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch.object(snow.ensemble, 'best_answer', side_effect=AssertionError('provider forbidden')))
        self.sender = self.stack.enter_context(patch.object(snow.subprocess, 'run'))
        self.sender.return_value = types.SimpleNamespace(returncode=0, stdout='', stderr='')
        self.record = self.stack.enter_context(patch.object(snow, '_rec'))
        self.decision = dict(material_change=True, reason='synthetic changed outlook',
                             refresh_md='synthetic outlook', email_subject='synthetic update',
                             email_body='synthetic body')
        self.decide = self.stack.enter_context(patch.object(snow, 'decide',
            return_value=(self.decision, ['https://example.invalid/evidence'])))
        self.stack.enter_context(patch.dict(snow._draft_state, {'by':'fixture', 'error':''}))

    def run_job(self, dry=False):
        output = io.StringIO()
        with patch.object(snow.sys, 'argv', ['bill_snow_weekly.py'] + (['--dry-run'] if dry else [])), contextlib.redirect_stdout(output):
            snow.main()
        return output.getvalue()

    def test_material_dry_run_has_preview_but_no_receipt_or_sender(self):
        text = self.run_job(dry=True)
        self.assertIn('synthetic update', text)
        self.assertIn('DRY RUN', text)
        self.sender.assert_not_called()
        self.assertFalse((self.root/'receipts').exists())
        self.assertEqual(list((self.root/'out').iterdir()), [])

    def test_successful_simulated_delivery_suppresses_replay_before_decision(self):
        self.run_job()
        receipt = send_guard.stamp_path('billsnow').read_bytes()
        self.run_job()
        self.assertEqual(self.sender.call_count, 1)
        args, kwargs = self.sender.call_args
        argv = args[0]
        self.assertEqual(argv[:4], [snow.sys.executable,
            str(self.root / 'send_bid_email.py'), 'recipient@example.invalid',
            'synthetic update'])
        self.assertEqual(len(argv), 5)
        body_path = Path(argv[4])
        self.assertEqual(body_path.parent, self.root)
        self.assertEqual(body_path.read_text(), 'synthetic body')
        self.assertEqual(kwargs['cwd'], str(self.root))
        self.assertEqual(kwargs['env']['CC_EMAIL'], 'copy@example.invalid')
        self.assertEqual(self.decide.call_count, 1)
        self.assertEqual(send_guard.stamp_path('billsnow').read_bytes(), receipt)
        self.assertIn('duplicate send suppressed', self.record.call_args.args[2])
        self.assertEqual(len(list((self.root/'out').iterdir())), 1)

    def test_failed_delivery_leaves_no_receipt_and_retries_once(self):
        self.sender.return_value.returncode = 1
        self.run_job()
        self.assertIsNone(send_guard.already_sent_today('billsnow'))
        self.assertIs(self.record.call_args.args[1], False)
        self.sender.return_value.returncode = 0
        self.run_job()
        self.run_job()
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(self.decide.call_count, 2)
        self.assertIsNotNone(send_guard.already_sent_today('billsnow'))

    def test_failed_evidence_is_not_healthy_quiet(self):
        self.decide.return_value = ({'material_change':False, 'error':True,
            'reason':'no web sources retrieved'}, [])
        self.run_job()
        self.sender.assert_not_called()
        self.assertIs(self.record.call_args.args[1], False)
        self.assertIsNone(send_guard.already_sent_today('billsnow'))

    def test_healthy_quiet_does_not_create_delivery_receipt(self):
        self.decide.return_value = (dict(material_change=False, reason='unchanged evidence',
            refresh_md='',email_subject='',email_body=''), [])
        self.run_job()
        self.sender.assert_not_called()
        self.assertIs(self.record.call_args.args[1], True)
        self.assertIn('no material change', self.record.call_args.args[2])
        self.assertIsNone(send_guard.already_sent_today('billsnow'))

    def test_corrupt_receipt_preserves_explicit_fail_open_policy(self):
        path = send_guard.stamp_path('billsnow')
        path.parent.mkdir(parents=True)
        path.write_text('{broken')
        self.run_job()
        self.assertEqual(self.sender.call_count, 1)
        self.assertIsNotNone(send_guard.already_sent_today('billsnow'))

    def test_receipt_failure_warning_exposes_duplicate_risk(self):
        with patch.object(send_guard, 'mark_sent', return_value=False):
            text = self.run_job()
        self.assertIn('restart could re-send', text)
        self.assertIsNone(send_guard.already_sent_today('billsnow'))
        self.assertIs(self.record.call_args.args[1], True)  # send succeeded; stamp failed


if __name__ == '__main__':
    unittest.main()
