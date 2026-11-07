"""No-send regressions for report confirmations and actual delivery requests."""
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import client_promises as promises
import intake
import promise_detect
import report_inventory
import task_solver

SUBJECT = 'Your weekly reports from us: please confirm'
BODY = '''Hi Test,
These are the reports our servers send you. All arrive Monday mornings (Eastern):
- Snow outlook, 4:00 a.m. It is sent only in weeks when the forecast changes.
- Development leads, 4:30 a.m.
- HOA digest, 5:00 a.m.
- Bids, 5:15 a.m., starting November 9.
Is this everything you expect from us? If something is missing or no longer needed, just reply and tell me.
-- CUMULUS'''


class InventoryTests(unittest.TestCase):
    def test_format_and_mixed_offers(self):
        self.assertTrue(report_inventory.is_inventory_email(BODY))
        for extra in ['We will build a workbook.', 'We can send the missing list.',
                      'I will add a new report.', 'If you want, we can prepare a file.']:
            self.assertFalse(report_inventory.is_inventory_email(BODY.replace('-- CUMULUS', extra + '\n-- CUMULUS')))
            self.assertFalse(report_inventory.is_inventory_email(BODY.replace('- HOA digest, 5:00 a.m.', '- HOA digest, 5:00 a.m. ' + extra)))
        self.assertFalse(report_inventory.is_inventory_email('We will send a weekly report starting Monday.'))

    def test_mailer_success_records_inventory_without_inference(self):
        import mailer
        with tempfile.TemporaryDirectory() as d, patch.object(promises, 'LEDGER', Path(d)/'ledger'):
            with patch('smtplib.SMTP') as smtp, patch.object(promise_detect, 'detect_promise', side_effect=AssertionError('no model')):
                smtp.return_value.__enter__.return_value.sendmail.return_value = {}
                self.assertTrue(mailer.send('sender@example.com', 'synthetic', 'test@example.com', SUBJECT, BODY, client='test'))
                smtp.return_value.__enter__.return_value.sendmail.assert_called_once()
            self.assertTrue(promises.is_inventory_thread('test', SUBJECT))
            self.assertEqual(promises.list_promises(), [])

    def test_failed_send_does_not_establish_context(self):
        import mailer
        with tempfile.TemporaryDirectory() as d, patch.object(promises, 'LEDGER', Path(d)/'ledger'):
            with patch('smtplib.SMTP', side_effect=OSError('synthetic failure')):
                self.assertFalse(mailer.send('sender@example.com', 'synthetic', 'test@example.com', SUBJECT, BODY, client='test', on_error='false', log=None))
            self.assertFalse(promises.is_inventory_thread('test', SUBJECT))

    def test_original_reviewed_inventory_format(self):
        source = Path(__file__).parent/'mail/Bill-projects-confirm-2026-11-03.md'
        self.assertTrue(report_inventory.is_inventory_email(source.read_text().split('\n',1)[1]))

    def test_assent_does_not_swallow_requests(self):
        for text in ['Yes.', 'Correct, thanks.', "Yes, that's everything.", 'The list is correct.', 'Confirmed!']:
            self.assertTrue(report_inventory.is_plain_assent(text), text)
        for text in ['Yes, add a report.', 'No.', 'Looks good but change Monday to Tuesday.',
                     'Please resend the list', 'Yes?', 'Why is the report missing',
                     'Yes, prepare the workbook', '', 'Thanks, remove snow']:
            self.assertFalse(report_inventory.is_plain_assent(text), text)

    def test_outbound_record_and_later_offer(self):
        with tempfile.TemporaryDirectory() as d, patch.object(promises, 'LEDGER', Path(d)/'ledger'):
            with patch.object(promise_detect, 'detect_promise', side_effect=AssertionError('inventory needs no model')):
                self.assertIsNone(report_inventory.record_outbound(BODY, {}, client='test', subject=SUBJECT))
            self.assertEqual(promises.list_promises(), [])
            self.assertTrue(promises.is_inventory_thread('test', 'Re: '+SUBJECT))
            self.assertFalse(promises.is_inventory_thread('other', 'Re: '+SUBJECT))
            self.assertFalse(promises.is_inventory_thread('test', 'Re: different subject'))
            with patch.object(promise_detect, 'detect_promise', return_value={'what':'New workbook','by':'test','escalated':False}):
                pid = report_inventory.record_outbound('We will prepare a workbook.', {}, client='test', subject=SUBJECT)
            self.assertTrue(pid)
            self.assertFalse(promises.is_inventory_thread('test', 'Re: '+SUBJECT))

    def dispatch(self, reply, *, inventory=True, offer=False, project='snow', kind='answer', subject=None):
        subject = subject or 'Re: '+SUBJECT
        with tempfile.TemporaryDirectory() as d, ExitStack() as stack:
            root=Path(d)
            stack.enter_context(patch.object(promises, 'LEDGER', root/'promises.jsonl'))
            stack.enter_context(patch.object(promise_detect, 'detect_promise', side_effect=AssertionError('no inference in synthetic dispatch')))
            if inventory:
                report_inventory.record_outbound(BODY, {}, client='test', subject=SUBJECT)
            if offer:
                promises.open_promise('test', project, SUBJECT, 'Prepare and send a new workbook')
            for module in (intake, task_solver):
                stack.enter_context(patch.object(module, 'PROJECT_DIR', root))
            for name, value in [('FAST_POLL_STATE_PATH',root/'fast'), ('NOTICE_OUTBOX_PATH',root/'outbox.json')]:
                stack.enter_context(patch.object(intake, name, value))
            stack.enter_context(patch.object(intake,'load_allowlist',return_value={'test@example.com':dict(name='test',projects=[project],limit=10,request_kind=kind,require_prefix=False)}))
            stack.enter_context(patch('runtime_config.load_sources',return_value={}))
            stack.enter_context(patch.object(intake,'find_account',return_value={'credential_key':'outlook_password'}))
            stack.enter_context(patch.object(intake,'load_json',return_value={'outlook_email':'sender@example.com','outlook_password':'synthetic'}))
            stack.enter_context(patch.object(intake,'load_state',return_value={}))
            for name in ('save_state','_record_status','append_backlog','telegram','log','append_topic'):
                stack.enter_context(patch.object(intake,name))
            stack.enter_context(patch.object(intake,'scan_inbox',return_value=([(1,'test@example.com',subject,reply,'synthetic-id')],[])))
            stack.enter_context(patch('intake_lifecycle.coordinate',return_value=True))
            stack.enter_context(patch('intake_delivery_evidence.verify',return_value=None))
            stack.enter_context(patch('list_delivery.start_job',side_effect=AssertionError('no real jobs')))
            stack.enter_context(patch.object(task_solver,'try_auto_apply_source',return_value=None))
            stack.enter_context(patch.object(task_solver,'solve_and_answer',return_value={'answered':False,'reason':'synthetic'}))
            stack.enter_context(patch.object(task_solver,'try_auto_resend',return_value={'resent':False,'count':0,'reason':'synthetic'}))
            worker=stack.enter_context(patch('subprocess.Popen'))
            ack=stack.enter_context(patch.object(intake,'send_ack',return_value=True))
            ticket=stack.enter_context(patch.object(intake.dev_loop,'ticket_create',return_value={'id':'synthetic-ticket'}))
            self.assertEqual(intake.run(),0)
            ack.assert_called_once()
            rec = ack.call_args.args[1]
            if rec.get('confirmation_scope') == 'existing_reports':
                import intake_lifecycle
                summary = intake_lifecycle.summary(root)
                self.assertEqual(summary['counts'], {'noted': 1})
                self.assertEqual(summary['attention'], [])
                worker.assert_not_called()
            return rec, ticket.call_count, promises.list_promises()

    def test_real_intake_confirmation_no_ticket_or_owed_promise(self):
        for project,kind in [('snow','answer'),('pedagogy','research'),('snow','build')]:
            rec, count, ledger = self.dispatch('Yes, correct. Thanks.\n\nOn Monday sender wrote:\n> Please build something?',project=project,kind=kind)
            self.assertEqual(rec['confirmation_scope'],'existing_reports')
            self.assertEqual(rec['kind'],'confirmation')
            self.assertFalse(rec['dev_spec']['auto_eligible'])
            self.assertEqual(count,0)
            self.assertEqual(ledger,[])
            self.assertIn('Thanks, noted.',intake.ack_body(rec))

    def test_requests_keep_normal_routing(self):
        for body in ['Yes, add another report.', 'No, change the schedule.', 'Why is one missing?', 'Please resend the reports.']:
            rec, _, _ = self.dispatch(body)
            self.assertNotEqual(rec.get('confirmation_scope'),'existing_reports')
            self.assertNotEqual(rec['kind'],'confirmation')

    def test_actual_offer_still_creates_delivery_ticket(self):
        for inventory in (False, True):
            rec, count, ledger = self.dispatch('Yes, clean it up and send it.',inventory=inventory,offer=True)
            self.assertEqual(count,1)
            self.assertEqual(rec['kind'],'confirmation')
            self.assertEqual(ledger[0]['state'],'confirmed')

    def test_unrelated_yes_is_not_inventory_confirmation(self):
        for kwargs in [dict(inventory=False),dict(subject='Re: Another subject'),dict(subject=SUBJECT)]:
            rec, _, _ = self.dispatch('Yes.',**kwargs)
            self.assertNotEqual(rec.get('confirmation_scope'),'existing_reports')

if __name__ == '__main__':
    unittest.main()
