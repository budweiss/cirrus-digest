import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import intake_information as info
import task_solver as ts

class InformationTest(unittest.TestCase):
    def record(self):
        return dict(requester='buddy', kind='build', tier=1, status='backlogged',
                    projects=['snow'], from_email='owner@example.com', message_id='test-message',
                    body_head='Where does Stock Picker get its sources?', title='Stock Picker sources')

    def test_question_corrects_static_project_and_tier(self):
        r=self.record()
        self.assertTrue(info.route(r, r['title'], r['body_head']))
        self.assertEqual((r['kind'],r['tier'],r['projects'],r['privacy']),('answer',0,['stock-pickers'],'LOCAL_ONLY'))
        self.assertEqual(r['information_capability'],'stock-source-inventory')

    def test_plain_request_and_project_alias(self):
        r=self.record()
        self.assertTrue(info.route(r,'Stock-picking','I would like to know the sources.'))
        self.assertEqual(r['information_capability'],'stock-source-inventory')

    def test_actions_and_other_senders_keep_existing_gates(self):
        for text in ['Where are sources? Add this feed.', 'How do I buy stocks?', 'Delete my report and explain why.']:
            r=self.record();self.assertFalse(info.route(r,'Stock Picker',text));self.assertEqual(r['tier'],1)
        for changes in [{'requester':'bill'},{'status':'refused'},{'kind':'confirmation'}]:
            r=self.record();r.update(changes)
            self.assertFalse(info.route(r,r['title'],r['body_head']))

    def test_quoted_question_is_not_new_request(self):
        r=self.record()
        self.assertFalse(info.route(r,'Re: Stock Picker sources','Thanks.\n\nOn Monday someone wrote:\n> Where are the sources?'))

    def test_inventory_uses_archive_and_does_not_create_missing_db(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with self.assertRaises(ValueError): info.source_inventory(root)
            home=root/'private/stock-paper/research';home.mkdir(parents=True)
            with sqlite3.connect(home/'research.sqlite3') as db:
                db.execute('CREATE TABLE documents(url,kind,first_seen)')
                db.execute('CREATE TABLE runs(id,finished)')
                db.execute("INSERT INTO documents VALUES ('https://example.com/report','filing','today')")
                db.execute("INSERT INTO runs VALUES (1,'today')")
            ans=info.source_inventory(root)
            self.assertIn('1 collected source documents across 1 hostnames',ans)
            self.assertIn('example.com',ans)
            self.assertIn('none recorded',ans)

    def test_send_once_and_hold_uncertain_delivery(self):
        for outcome in [True,False,RuntimeError('synthetic failure')]:
            with tempfile.TemporaryDirectory() as d, patch.object(ts,'PROJECT_DIR',Path(d)):
                r=self.record();info.route(r,r['title'],r['body_head'])
                send=Mock(side_effect=outcome) if isinstance(outcome,Exception) else Mock(return_value=outcome)
                args=(r,{},r['from_email'],r['title'])
                first=info.solve_and_answer(*args,inventory=lambda _: 'Synthetic source inventory.',sender=send)
                second=info.solve_and_answer(*args,inventory=lambda _: 'Synthetic source inventory.',sender=send)
                self.assertEqual(first['answered'],outcome is True)
                self.assertEqual(second['answered'],outcome is True)
                self.assertEqual(send.call_count,1)

    def test_real_intake_dispatches_without_build_ticket(self):
        from contextlib import ExitStack
        import intake
        with tempfile.TemporaryDirectory() as d, ExitStack() as stack:
            root=Path(d)
            for module in [intake,ts]: stack.enter_context(patch.object(module,'PROJECT_DIR',root))
            stack.enter_context(patch.object(intake,'FAST_POLL_STATE_PATH',root/'fast'))
            stack.enter_context(patch.object(intake,'load_allowlist',return_value={'owner@example.com':dict(name='buddy',projects=['snow'],limit=10,request_kind='build',require_prefix=False)}))
            stack.enter_context(patch('runtime_config.load_sources',return_value={}))
            stack.enter_context(patch.object(intake,'find_account',return_value={'credential_key':'outlook_password'}))
            stack.enter_context(patch.object(intake,'load_json',return_value={'outlook_email':'sender@example.com','outlook_password':'test'}))
            stack.enter_context(patch.object(intake,'load_state',return_value={}))
            for name in ['save_state','_record_status','append_backlog','telegram','log']:
                stack.enter_context(patch.object(intake,name))
            stack.enter_context(patch.object(intake,'scan_inbox',return_value=([(1,'owner@example.com','Stock-picks','I would like to know the sources.','synthetic-id')],[])))
            stack.enter_context(patch.object(intake,'is_confirmation',return_value=[]))
            ack=stack.enter_context(patch.object(intake,'send_ack',return_value=True))
            solve=stack.enter_context(patch.object(info,'solve_and_answer',return_value={'answered':True,'request_id':'synthetic','reason':'synthetic delivery'}))
            ticket=stack.enter_context(patch.object(intake.dev_loop,'ticket_create'))
            self.assertEqual(intake.run(),0)
            ack.assert_called_once();solve.assert_called_once();ticket.assert_not_called()
            self.assertEqual(solve.call_args.args[0]['projects'],['stock-pickers'])

    def test_cannot_send_to_another_recipient(self):
        r=self.record();info.route(r,r['title'],r['body_head'])
        with self.assertRaises(ValueError): info.solve_and_answer(r,{},'other@example.com',r['title'])

if __name__=='__main__': unittest.main()
