import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
import intake_maintenance as m

class MaintenanceTest(unittest.TestCase):
    def test_hold_dedup_and_release(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(m.active(d))
            m.atomic(Path(d)/'config/project_maintenance.json', {'active':True})
            self.assertTrue(m.active(d))
            send=Mock(return_value=True)
            creds={'outlook_email':'sender@example.com','outlook_password':'test'}
            rows=[(1,'client@example.com','Question','full body','a')]
            thread=lambda s:s.lower().removeprefix('re: ')
            self.assertEqual(m.defer(d,rows,creds,send,thread),0)
            m.defer(d,rows+[(2,'client@example.com','Re: Question','follow-up','b')],creds,send,thread)
            self.assertEqual(send.call_count,1)
            self.assertTrue(send.call_args.kwargs['auto_submitted'])
            self.assertEqual(len(m.pending(d)),2)
            m.complete(d,'client@example.com','a')
            self.assertEqual(len(m.pending(d)),1)
            m.atomic(Path(d)/'config/project_maintenance.json', {'active':False})
            self.assertFalse(m.active(d))
    def test_send_failure_preserves_queue_and_does_not_retry(self):
        with tempfile.TemporaryDirectory() as d:
            send=Mock(return_value=False)
            creds={'outlook_email':'x','outlook_password':'test'}
            row=[(1,'client@example.com','Question','body','a')]
            self.assertEqual(m.defer(d,row,creds,send,str.lower),1)
            self.assertEqual(m.defer(d,row,creds,send,str.lower),1)
            self.assertEqual(send.call_count,1)
            self.assertEqual(len(m.pending(d)),1)
    def test_bad_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            m.atomic(Path(d)/'config/project_maintenance.json', {'active':'yes'})
            with self.assertRaises(ValueError):m.active(d)
    def test_release_batch_is_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(8):m.atomic(Path(d)/f'data/intake-maintenance/pending/{i}.json',[i])
            self.assertEqual(len(m.pending(d)),5)

    def test_real_intake_hold_bypasses_solver(self):
        from unittest.mock import patch
        from contextlib import ExitStack
        import intake
        with tempfile.TemporaryDirectory() as d, ExitStack() as stack:
            m.atomic(Path(d)/'config/project_maintenance.json', {'active':True})
            stack.enter_context(patch.object(intake,'PROJECT_DIR',Path(d)))
            stack.enter_context(patch.object(intake,'load_allowlist',return_value={'client@example.com':{'name':'client'}}))
            stack.enter_context(patch('runtime_config.load_sources',return_value={}))
            stack.enter_context(patch.object(intake,'find_account',return_value={'credential_key':'outlook_password'}))
            stack.enter_context(patch.object(intake,'load_json',return_value={'outlook_email':'sender@example.com','outlook_password':'test'}))
            stack.enter_context(patch.object(intake,'load_state',return_value={}))
            save=stack.enter_context(patch.object(intake,'save_state'))
            stack.enter_context(patch.object(intake,'scan_inbox',return_value=([(1,'client@example.com','Help','body','id')],[])))
            stack.enter_context(patch.object(intake,'_record_status'))
            classify=stack.enter_context(patch.object(intake,'classify'))
            send=stack.enter_context(patch.object(intake.mailer,'send',return_value=True))
            self.assertEqual(intake.run(),0)
            classify.assert_not_called()
            send.assert_called_once()
            save.assert_called_once()
            self.assertEqual(len(m.pending(d)),1)
