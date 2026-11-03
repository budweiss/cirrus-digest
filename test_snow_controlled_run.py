"""S346: real Snow orchestration with scratch state and a simulated sender.

No real credentials, provider, SMTP, subprocess or socket is available. This
rehearses decision/delivery boundaries, not unattended scheduling or live mail.
The provider/admission path is separately exercised by test_snow_failure_pipeline.
"""
import ast
import sys
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
        (self.root / 'ref').mkdir()
        self.baseline = self.root / 'ref' / 'baseline-outlook.md'
        self.baseline.write_text('old baseline')
        for name, value in [('OUT', self.root / 'out'), ('DIGEST_DIR', self.root), ('REF', self.root / 'ref'),
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
                             refresh_md=f'# Refresh\n{snow.CHANGE_HEADING}\nSnowfall estimate unchanged [1]; published 11-01.',
                             email_subject='synthetic update',
                             email_body=f'synthetic body\n{snow.CHANGE_HEADING}\nSnowfall estimate unchanged; published 11-01.')
        self.decide = self.stack.enter_context(patch.object(snow, 'decide',
            return_value=(self.decision, ['https://example.invalid/evidence'])))
        self.stack.enter_context(patch.dict(snow._draft_state, {'by':'fixture', 'error':''}))

    def run_job(self, dry=False):
        output = io.StringIO()
        with patch.object(snow.sys, 'argv', ['bill_snow_weekly.py'] + (['--dry-run'] if dry else [])), contextlib.redirect_stdout(output):
            self.exit_code = snow.main()
        return output.getvalue()

    def test_material_dry_run_has_preview_but_no_receipt_or_sender(self):
        text = self.run_job(dry=True)
        self.assertIn('synthetic update', text)
        self.assertIn('DRY RUN', text)
        self.assertEqual(self.exit_code, 0)
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
        self.assertEqual(body_path.read_text(), self.decision['email_body'])
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
        self.assertEqual(self.exit_code, 1)
        self.sender.return_value.returncode = 0
        self.run_job()
        self.run_job()
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(self.decide.call_count, 2)
        self.assertIsNotNone(send_guard.already_sent_today('billsnow'))

    def test_successful_send_makes_the_refresh_the_baseline(self):
        self.run_job()
        text = self.baseline.read_text()
        self.assertIn('Snowfall estimate unchanged; published 11-01.', text)
        self.assertNotIn('[1]', text)
        self.assertIn('Baseline note', text)
        self.assertIs(self.record.call_args.args[1], True)
        self.assertEqual(self.exit_code, 0)
        self.assertEqual(self.record.call_args.args[2].split(';')[0], 'sent material update')

    def test_baseline_stays_when_nothing_reached_bill(self):
        self.run_job(dry=True)
        self.assertEqual(self.baseline.read_text(), 'old baseline')
        self.sender.return_value.returncode = 1
        self.run_job()
        self.assertEqual(self.baseline.read_text(), 'old baseline')
        self.sender.return_value.returncode = 0
        self.decide.return_value = (dict(material_change=False, reason='unchanged evidence',
            refresh_md='',email_subject='',email_body=''), [])
        self.run_job()
        self.assertEqual(self.baseline.read_text(), 'old baseline')

    def test_failed_baseline_update_fails_the_row_without_blocking_the_stamp(self):
        with patch.object(snow, 'promote_baseline', return_value=False):
            self.run_job()
        self.assertIsNotNone(send_guard.already_sent_today('billsnow'))
        self.assertIs(self.record.call_args.args[1], False)
        self.assertEqual(self.exit_code, 1)
        self.assertIn('BASELINE NOT UPDATED', self.record.call_args.args[2])

    def test_failed_baseline_without_a_stamp_cannot_trigger_a_catch_up_resend(self):
        with patch.object(snow, 'promote_baseline', return_value=False), patch.object(send_guard, 'mark_sent', return_value=False):
            self.run_job()
        self.assertIs(self.record.call_args.args[1], True)
        self.assertEqual(self.exit_code, 0)
        self.assertIn('BASELINE NOT UPDATED', self.record.call_args.args[2])

    def test_failed_evidence_is_not_healthy_quiet(self):
        self.decide.return_value = ({'material_change':False, 'error':True,
            'reason':'no web sources retrieved'}, [])
        self.run_job()
        self.sender.assert_not_called()
        self.assertIs(self.record.call_args.args[1], False)
        self.assertEqual(self.exit_code, 1)
        self.assertIsNone(send_guard.already_sent_today('billsnow'))

    def test_real_command_entry_propagates_status(self):
        entry = ast.parse(Path(snow.__file__).read_text()).body[-1]
        for code in (0, 1):
            namespace = {'__name__': '__main__', 'sys': sys, 'main': lambda: code}
            with patch.object(sys, 'argv', ['bill_snow_weekly.py']):
                with self.assertRaises(SystemExit) as result:
                    exec(compile(ast.Module(body=[entry], type_ignores=[]), snow.__file__, 'exec'), namespace)
            self.assertEqual(result.exception.code, code)

    def test_failed_dry_run_exits_nonzero_without_delivery(self):
        self.decide.return_value = ({'material_change': False, 'error': True,
            'reason': 'synthetic admission refusal'}, [])
        self.run_job(dry=True)
        self.assertEqual(self.exit_code, 1)
        self.sender.assert_not_called()
        self.record.assert_not_called()

    def test_healthy_quiet_does_not_create_delivery_receipt(self):
        self.decide.return_value = (dict(material_change=False, reason='unchanged evidence',
            refresh_md='',email_subject='',email_body=''), [])
        self.run_job()
        self.sender.assert_not_called()
        self.assertIs(self.record.call_args.args[1], True)
        self.assertEqual(self.exit_code, 0)
        self.assertIn('no material change', self.record.call_args.args[2])
        self.assertIsNone(send_guard.already_sent_today('billsnow'))

    def test_corrupt_receipt_preserves_explicit_fail_open_policy(self):
        path = send_guard.stamp_path('billsnow', root=self.root / 'receipts')  # explicit root = the temp dir setUp patched into STAMP_DIR
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
        self.assertIs(self.record.call_args.args[1], True)
        self.assertEqual(self.exit_code, 0)  # send succeeded; stamp failed


if __name__ == '__main__':
    unittest.main()
