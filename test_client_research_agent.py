"""Synthetic checks for the server research agent: temp roots only, no network or mail."""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import client_research_agent as A
import intake_lifecycle
from conversation_memory import Memory

QUESTION = ('Research this topic and give me a list of topics with the science behind each story.\n\n'
            'William Example\nPresident\nExample Property Services\n555-123-4567')
CFG = {'mode': 'live', 'clients': {'bill': {'greeting': 'Bill', 'basis': 'synthetic test'}},
       'model': 'claude-sonnet-5-5', 'agent_budget_usd': 1.25, 'check_budget_usd': 0.4, 'daily_requests': 5,
       'agent_timeout': 900, 'check_timeout': 360, 'alert_clients': ['bill', 'alyssa']}
GOOD = {'status': 'answered', 'reply': 'Here is the list you asked for. ' * 5,
        'sources': [{'title': 'Bibliography', 'url': 'https://example.org/books'}], 'stuck_reason': ''}


def fake_run(agent_results, checks, verifies=None):
    calls = []

    def run(prompt, system, tools, budget, timeout, model, key, max_turns=40):
        calls.append({'tools': tools, 'prompt': prompt, 'budget': budget})
        queue = checks if tools == ['WebFetch'] else agent_results
        if system == A.VERIFY_RULES:
            queue = verifies if verifies is not None else checks
        models = {'claude-sonnet-5-5': 0.08, 'claude-haiku-4-5': 0.02} if tools != ['WebFetch'] else {'claude-sonnet-5-5': 0.1}
        return {'text': json.dumps(queue.pop(0)), 'cost': 0.1, 'turns': 3, 'error': False, 'subtype': 'success',
                'models': models}
    run.calls = calls
    return run


class Agent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'config').mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def held(self, client='bill', label='needs web research; no job started, so it waits for you', age=600, mid='<m1>'):
        turn = Memory(self.root).add(owner='cumulus1', client=client, project='general', thread='research',
                                     message_id=mid, question=QUESTION, state='needs_review',
                                     meta={'requires_buddy': True})
        row = [1, 'client@example.test', 'Research', QUESTION, mid]
        intake_lifecycle.capture(self.root, row)
        intake_lifecycle.update(self.root, row, 'blocked_on_evidence', 'cowork',
                                label + '; review saved attempt; preserve budget and send uncertainty',
                                {'client': client, 'answer_request_id': turn['id']})
        with intake_lifecycle.connect(self.root) as db:
            db.execute('UPDATE requests SET created=?', (time.time() - age,))
        return turn['id'], row

    def state(self):
        with intake_lifecycle.connect(self.root) as db:
            r = db.execute('SELECT state,owner,next_action,evidence FROM requests').fetchone()
        return r['state'], r['owner'], r['next_action'], json.loads(r['evidence'])

    def test_own_words_drop_signature_and_contact_lines(self):
        text = A.own_words(QUESTION + '\nwrite to me at someone@example.test')
        self.assertTrue(text.startswith('Research this topic'))
        for gone in ('William', 'President', '555-123-4567', 'someone@example.test'):
            self.assertNotIn(gone, text)

    def test_one_line_email_keeps_the_question_and_loses_the_signature(self):
        one = ('I want new story ideas in his style. Provide a brief synopsis. William Example President '
               'Example Property Services 555-123-4567')
        self.assertEqual(A.own_words(one, 'William Example'), 'I want new story ideas in his style. Provide a brief synopsis.')
        self.assertEqual(A.own_words('Call 555-123-4567 about the plow schedule, please.'),
                         'Call  about the plow schedule, please.')

    def test_unreadable_question_is_stuck_not_sent(self):
        aid, _ = self.held()
        Memory(self.root).update(aid, answer='')
        with sqlite3.connect(self.root / 'data/conversations/memory.sqlite3') as db:
            db.execute("UPDATE turns SET question='William Example' WHERE id=?", (aid,))
        run, notes = fake_run([], []), []
        row = A.candidates(self.root, CFG, A.Ledger(self.root))[0]
        cfg = {**CFG, 'clients': {'bill': dict(CFG['clients']['bill'], signature='William Example')}}
        self.assertEqual(A.process(self.root, cfg, row, 'k', {}, run=run, notify=notes.append), 'stuck')
        self.assertEqual(run.calls, [])
        self.assertIn('could not be read', notes[0])

    def test_candidates_take_only_research_or_review_holds_for_opted_in_clients(self):
        ledger = A.Ledger(self.root)
        self.held()
        self.assertEqual(len(A.candidates(self.root, CFG, ledger)), 1)
        self.tearDown(); self.setUp(); self.held(label='asks for a new report or file; your decision')
        self.assertEqual(len(A.candidates(self.root, CFG, A.Ledger(self.root))), 1)
        for kwargs in ({'label': 'interrupted handler: reconcile effects before retry'}, {'client': 'alyssa'},
                       {'age': 4 * 86400}):
            with self.subTest(**kwargs):
                self.tearDown(); self.setUp(); self.held(**kwargs)
                self.assertEqual(A.candidates(self.root, CFG, A.Ledger(self.root)), [])
        self.tearDown(); self.setUp()
        _, row = self.held()
        intake_lifecycle.update(self.root, row, 'blocked_on_evidence', 'cowork', 'review request safety hold locally')
        self.assertEqual(A.candidates(self.root, CFG, A.Ledger(self.root)), [])

    def test_parse_json_and_gate(self):
        self.assertEqual(A.parse_json('Done.\n```json\n{"status":"stuck"}\n```')['status'], 'stuck')
        with self.assertRaises(ValueError):
            A.parse_json('no json here')
        self.assertEqual(A.gate(GOOD), [])
        self.assertIn('sources', A.gate(dict(GOOD, sources=[])))
        # S679 rehearsal: a complete list cited 16+ pages; that is not a defect.
        self.assertEqual(A.gate(dict(GOOD, sources=[{'url': 'https://example.org/%d' % i} for i in range(30)])), [])
        self.assertIn('sources', A.gate(dict(GOOD, sources=[{'url': 'https://example.org/%d' % i} for i in range(41)])))
        self.assertIn('commitment', A.gate(dict(GOOD, reply=GOOD['reply'] + ' We will submit a bid for you.')))
        self.assertEqual(A.gate({'status': 'stuck'}), ['not_answered'])

    def test_answered_request_is_sent_once_and_proven(self):
        aid, _ = self.held()
        run = fake_run([GOOD], [{'passed': True, 'issues': []}])
        sent, notes = [], []
        row = A.candidates(self.root, CFG, A.Ledger(self.root))[0]
        with patch('task_solver.dev_loop.ledger_append'):
            out = A.process(self.root, CFG, row, 'synthetic-key', {}, run=run,
                            send=lambda *a: sent.append(a) or True, verify=lambda *a: {'uid': '7'},
                            notify=notes.append)
        self.assertEqual(out, 'sent_verified')
        self.assertEqual(len(sent), 1)
        frm, pw, to, cc, subject, body = sent[0]
        self.assertEqual((to, subject), ('client@example.test', 'Re: Research'))
        self.assertTrue(body.startswith('Hi Bill,') and body.rstrip().endswith('-- CUMULUS'))
        self.assertIn('https://example.org/books', body)
        self.assertNotIn('555-123-4567', run.calls[0]['prompt'])
        self.assertNotIn('William', run.calls[0]['prompt'])
        turn = Memory(self.root).get(aid)
        self.assertEqual((turn['state'], turn['answer'], turn['meta']['requires_buddy']), ('delivered', body, False))
        self.assertEqual(self.state()[:2], ('sent_verified', 'delivery-review'))
        self.assertEqual(notes, [])
        self.assertEqual(A.candidates(self.root, CFG, A.Ledger(self.root)), [])

    def test_a_follow_up_sees_our_earlier_reply_in_the_same_thread(self):
        memory = Memory(self.root)
        first = memory.add(owner='cumulus1', client='bill', project='general', thread='research', message_id='<m0>',
                           question='Give me story ideas.\n\nWilliam Example\n555-123-4567', state='delivered')
        memory.update(first['id'], answer='Hi Bill, IDEA 1: MIRROR, about mirror-image bacteria.')
        with sqlite3.connect(self.root / 'data/conversations/memory.sqlite3') as db:
            db.execute('UPDATE turns SET created=created-100 WHERE id=?', (first['id'],))
        self.held()
        run = fake_run([GOOD], [{'passed': True, 'issues': []}])
        row = A.candidates(self.root, CFG, A.Ledger(self.root))[0]
        with patch('task_solver.dev_loop.ledger_append'):
            A.process(self.root, CFG, row, 'k', {}, run=run, send=lambda *a: True, verify=lambda *a: {'uid': '1'},
                      notify=lambda t: None)
        for call in run.calls:
            self.assertIn('IDEA 1: MIRROR', call['prompt'])
            self.assertNotIn('555-123-4567', call['prompt'])
        self.assertLess(run.calls[0]['prompt'].index('IDEA 1'), run.calls[0]['prompt'].index("Client's question"))

    def test_each_run_records_the_models_it_used_and_their_cost(self):
        self.held()
        run = fake_run([GOOD], [{'passed': True, 'issues': []}])
        row = A.candidates(self.root, CFG, A.Ledger(self.root))[0]
        with patch('task_solver.dev_loop.ledger_append'), patch('llm_budget.record_sdk_cost') as cost:
            A.process(self.root, CFG, row, 'k', {}, run=run, send=lambda *a: True, verify=lambda *a: {'uid': '1'},
                      notify=lambda t: None)
        with sqlite3.connect(self.root / A.STATE / 'ledger.sqlite3') as db:
            detail = json.loads(db.execute('SELECT detail FROM attempts').fetchone()[0])
        self.assertEqual(detail['models'], {'claude-sonnet-5-5': 0.18, 'claude-haiku-4-5': 0.02})
        self.assertEqual([e['models'] for e in detail['log'] if e.get('stage') in ('research', 'source-check')],
                         [['claude-haiku-4-5', 'claude-sonnet-5-5'], ['claude-sonnet-5-5']])
        self.assertTrue(detail['hold_label'].startswith('needs web research'))
        self.assertEqual(sorted(c.kwargs['model'] for c in cost.call_args_list), ['claude-haiku-4-5', 'claude-sonnet-5-5'])

    def test_other_foundation_models_try_before_anyone_is_told(self):
        self.held()
        run = fake_run([{'status': 'stuck', 'stuck_reason': 'no source'}], [],
                       verifies=[{'passed': True, 'issues': [], 'sources': [{'title': 'Checked', 'url': 'https://example.org/ok'}]}])
        drafts = {'gemini': {'status': 'stuck'}, 'openai': dict(GOOD, sources=[])}
        calls, sent, notes = [], [], []
        def call(provider, system, user, creds, **kw):
            calls.append((provider, kw.get('task'), system == A.FALLBACK_RULES))
            return json.dumps(drafts[provider])
        creds = {'gemini_api_key': 'x', 'openai_api_key': 'y'}
        row = A.candidates(self.root, CFG, A.Ledger(self.root))[0]
        with patch('task_solver.dev_loop.ledger_append'):
            out = A.process(self.root, CFG, row, 'k', creds, run=run, call=call, send=lambda *a: sent.append(a) or True,
                            verify=lambda *a: {'uid': '1'}, notify=notes.append)
        self.assertEqual(out, 'sent_verified')
        self.assertEqual([c[0] for c in calls], ['gemini', 'openai'])
        self.assertTrue(all(c[1] == 'client-research-fallback:bill' and c[2] for c in calls))
        self.assertIn('https://example.org/ok', sent[0][5])
        self.assertEqual(notes, [])
        with sqlite3.connect(self.root / A.STATE / 'ledger.sqlite3') as db:
            detail = json.loads(db.execute('SELECT detail FROM attempts').fetchone()[0])
        self.assertEqual(detail['answered_by_fallback'], 'openai')
        self.assertEqual([t['outcome'] for t in detail['fallback']], ['stuck', 'verified'])

    def test_telegram_only_after_every_model_fails(self):
        self.held()
        bad = {'passed': False, 'issues': ['contact name not found on any page'], 'sources': []}
        run = fake_run([{'status': 'stuck', 'stuck_reason': 'no source'}], [], verifies=[bad, bad])
        calls, notes = [], []
        def call(provider, system, user, creds, **kw):
            calls.append(provider)
            return json.dumps(dict(GOOD, reply=GOOD['reply'] + (' We will submit a bid.' if provider == 'grok' else '')))
        creds = {k: 'x' for k in ('gemini_api_key', 'openai_api_key', 'grok_api_key')}
        row = A.candidates(self.root, CFG, A.Ledger(self.root))[0]
        out = A.process(self.root, CFG, row, 'k', creds, run=run, call=call, send=lambda *a: self.fail('sent'),
                        notify=notes.append)
        self.assertEqual(out, 'stuck')
        self.assertEqual(calls, ['gemini', 'openai', 'grok'])
        self.assertEqual(len(notes), 1)
        self.assertIn('3 other foundation models (gemini, openai, grok)', notes[0])
        with sqlite3.connect(self.root / A.STATE / 'ledger.sqlite3') as db:
            detail = json.loads(db.execute('SELECT detail FROM attempts').fetchone()[0])
        self.assertEqual([t['outcome'] for t in detail['fallback']], ['check_failed', 'check_failed', 'gate:commitment'])

    def test_stuck_goes_to_the_help_queue_with_one_message_and_no_send(self):
        self.held()
        run = fake_run([{'status': 'stuck', 'stuck_reason': 'no public source names a contact'}], [])
        sent, notes = [], []
        row = A.candidates(self.root, CFG, A.Ledger(self.root))[0]
        out = A.process(self.root, CFG, row, 'k', {}, run=run, send=lambda *a: sent.append(a), notify=notes.append)
        self.assertEqual(out, 'stuck')
        self.assertEqual(sent, [])
        state, owner, action, evidence = self.state()
        self.assertEqual((state, owner, action), ('blocked_no_worker', 'cowork', A.BRIDGE_ACTION))
        self.assertEqual(evidence['research_agent']['outcome'], 'stuck')
        self.assertEqual(len(notes), 1)
        self.assertIn('no public source names a contact', notes[0])

    def test_stuck_message_says_why_in_plain_words(self):
        self.held()
        run = fake_run([dict(GOOD, sources=[]), dict(GOOD, sources=[])], [])
        notes = []
        row = A.candidates(self.root, CFG, A.Ledger(self.root))[0]
        self.assertEqual(A.process(self.root, CFG, row, 'k', {}, run=run, notify=notes.append), 'stuck')
        self.assertIn('the draft had no usable source list', notes[0])

    def test_failed_check_gets_one_revision_then_is_stuck(self):
        self.held()
        bad = {'passed': False, 'issues': ['phone number not on the cited page']}
        run = fake_run([GOOD, GOOD], [bad, bad])
        notes = []
        row = A.candidates(self.root, CFG, A.Ledger(self.root))[0]
        self.assertEqual(A.process(self.root, CFG, row, 'k', {}, run=run, send=lambda *a: True,
                                   notify=notes.append), 'stuck')
        agent_calls = [c for c in run.calls if c['tools'] != ['WebFetch']]
        self.assertEqual(len(agent_calls), 2)
        self.assertIn('phone number not on the cited page', agent_calls[1]['prompt'])

    def test_revision_that_passes_is_sent(self):
        self.held()
        run = fake_run([GOOD, GOOD], [{'passed': False, 'issues': ['unsupported date']}, {'passed': True, 'issues': []}])
        row = A.candidates(self.root, CFG, A.Ledger(self.root))[0]
        with patch('task_solver.dev_loop.ledger_append'):
            out = A.process(self.root, CFG, row, 'k', {}, run=run, send=lambda *a: True,
                            verify=lambda *a: {'uid': '1'}, notify=lambda t: None)
        self.assertEqual(out, 'sent_verified')

    def test_uncertain_send_is_held_and_never_retried(self):
        aid, _ = self.held()
        run = fake_run([GOOD], [{'passed': True, 'issues': []}])
        notes, sent = [], []
        row = A.candidates(self.root, CFG, A.Ledger(self.root))[0]
        out = A.process(self.root, CFG, row, 'k', {}, run=run, send=lambda *a: sent.append(a) or False,
                        notify=notes.append)
        self.assertEqual(out, 'delivery_unknown')
        self.assertEqual(Memory(self.root).get(aid)['state'], 'delivery_unknown')
        self.assertIn('never resend', self.state()[2])
        self.assertEqual(A.candidates(self.root, CFG, A.Ledger(self.root)), [])
        self.assertEqual((len(sent), len(notes)), (1, 1))

    def test_a_claimed_request_is_not_processed_twice(self):
        self.held()
        row = A.candidates(self.root, CFG, A.Ledger(self.root))[0]
        self.assertTrue(A.claim(self.root, row['id']))
        run = fake_run([GOOD], [{'passed': True, 'issues': []}])
        self.assertEqual(A.process(self.root, CFG, row, 'k', {}, run=run, send=lambda *a: True), 'skipped_claimed')
        self.assertEqual(run.calls, [])

    def test_dry_run_writes_the_draft_and_leaves_the_request_held(self):
        aid, _ = self.held()
        run = fake_run([GOOD], [{'passed': True, 'issues': []}])
        row = A.candidates(self.root, CFG, A.Ledger(self.root))[0]
        out = A.process(self.root, dict(CFG, mode='dry-run'), row, 'k', {}, run=run,
                        send=lambda *a: self.fail('dry run sent mail'))
        self.assertEqual(out, 'dry_run_ready')
        self.assertEqual(self.state()[:2], ('blocked_on_evidence', 'cowork'))
        self.assertEqual(Memory(self.root).get(aid)['state'], 'needs_review')
        drafts = list((self.root / A.STATE / 'dry-run').glob('*.txt'))
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].stat().st_mode & 0o777, 0o600)
        # Still held for a person, but never picked up again automatically.
        self.assertEqual(A.candidates(self.root, CFG, A.Ledger(self.root)), [])

    def test_unanswered_alert_fires_once_after_two_hours(self):
        self.held(age=3 * 3600)
        ledger, notes = A.Ledger(self.root), []
        self.assertEqual(A.alerts(self.root, CFG, ledger, notify=notes.append), 1)
        self.assertEqual(A.alerts(self.root, CFG, ledger, notify=notes.append), 0)
        self.assertIn('bill', notes[0])
        self.assertIn('"Research"', notes[0])
        self.assertIn('after 2 hours', notes[0])

    def test_a_worker_owned_request_alerts_after_a_day_not_two_hours(self):
        _, row = self.held(age=3 * 3600)
        intake_lifecycle.update(self.root, row, 'working', 'lesson_build', 'lesson job builds, checks and sends each arc')
        notes = []
        self.assertEqual(A.alerts(self.root, CFG, A.Ledger(self.root), notify=notes.append), 0)
        with intake_lifecycle.connect(self.root) as db:
            db.execute('UPDATE requests SET created=?', (time.time() - 25 * 3600,))
        self.assertEqual(A.alerts(self.root, CFG, A.Ledger(self.root), notify=notes.append), 1)
        self.assertIn('after 24 hours', notes[0])

    def test_no_alert_for_young_finished_or_unwatched_requests(self):
        ledger, notes = A.Ledger(self.root), []
        self.held(age=600)
        self.assertEqual(A.alerts(self.root, CFG, ledger, notify=notes.append), 0)
        self.tearDown(); self.setUp()
        _, row = self.held(age=3 * 3600)
        intake_lifecycle.update(self.root, row, 'sent_verified', 'delivery-review', 'done', {'sent_receipt': {'uid': '1'}})
        self.assertEqual(A.alerts(self.root, CFG, A.Ledger(self.root), notify=notes.append), 0)
        self.tearDown(); self.setUp()
        self.held(client='aggie', age=3 * 3600)
        self.assertEqual(A.alerts(self.root, CFG, A.Ledger(self.root), notify=notes.append), 0)
        self.assertEqual(notes, [])

    def _question_only(self, age):
        aid, row = self.held(age=age)
        Memory(self.root).update(aid, state='delivered', meta={'progress_check': True})
        intake_lifecycle.update(self.root, row, 'sent_verified', 'delivery-review', 'done',
                                {'client': 'bill', 'answer_request_id': aid, 'sent_receipt': {'uid': '1'}})
        return aid

    def test_a_progress_check_question_alone_alerts_until_the_client_writes_again(self):
        self._question_only(age=3 * 3600)
        notes = []
        self.assertEqual(A.alerts(self.root, CFG, A.Ledger(self.root), notify=notes.append), 1)
        self.assertIn('progress-check question', notes[0])
        # The client wrote again: the newer request is the one to answer, so no alert here.
        self.tearDown(); self.setUp()
        self._question_only(age=3 * 3600)
        later = [1, 'client@example.test', 'Re: Research', QUESTION, '<m2>']
        intake_lifecycle.capture(self.root, later)
        intake_lifecycle.update(self.root, later, 'working', 'answer', 'answering', {'client': 'bill'})
        self.assertEqual(A.alerts(self.root, CFG, A.Ledger(self.root), notify=notes.append), 0)
        # A session answered it by hand: no alert.
        self.tearDown(); self.setUp()
        aid = self._question_only(age=3 * 3600)
        Memory(self.root).update(aid, meta={'manual_answer': 'session send, verified'})
        self.assertEqual(A.alerts(self.root, CFG, A.Ledger(self.root), notify=notes.append), 0)
        self.assertEqual(len(notes), 1)

    def test_reply_never_carries_a_second_greeting(self):
        body = A.compose('Bill', 'Hi Bill,\n\nWe wrote Chapters 9 to 13.', [{'title': 't', 'url': 'https://x.test'}])
        self.assertEqual(body.count('Hi Bill'), 1)
        self.assertIn('We wrote Chapters 9 to 13.', body)
        self.assertIn('Hi there is', A.compose('Bill', 'Hi there is a catch.', []))   # not a greeting line

    def test_thread_context_keeps_the_end_of_a_long_earlier_reply(self):
        memory = Memory(self.root)
        first = memory.add(owner='cumulus1', client='bill', project='general', thread='research',
                           message_id='<a>', question='Continue the story.', state='delivered', meta={})
        story = 'Chapter 6. Start.\n' + 'word ' * 1800 + '\nLAST LINE OF CHAPTER 11.'
        memory.update(first['id'], answer=story)
        turn = memory.add(owner='cumulus1', client='bill', project='general', thread='research',
                          message_id='<b>', question='Continue again.', state='needs_review', meta={})
        text = A.thread_context(memory, memory.get(turn['id']))
        self.assertIn('Chapter 6. Start.', text)
        self.assertIn('LAST LINE OF CHAPTER 11.', text)
        self.assertIn('client received all of it', text)

    def test_live_run_refuses_when_the_proof_mailbox_is_not_this_box(self):
        (self.root / A.CONFIG).write_text(json.dumps(CFG))
        (self.root / 'config/credentials.json').write_text('{}')
        with patch.object(A, 'ROOT', self.root), patch.object(A, 'proof_account_ready', return_value=False), \
             patch('sys.argv', ['client_research_agent.py', 'run']), patch('job_status.record') as record, \
             patch('builtins.print'):
            self.assertEqual(A.main(), 2)
        self.assertIn('T214', record.call_args[0][2])

    def test_proof_account_check_needs_the_password_and_the_sending_address(self):
        account = {'label': 'x', 'address': 'Box@Example.test', 'credential_key': 'box_password'}
        with patch('intake.find_account', return_value=account), patch('runtime_config.load_sources', return_value={}):
            self.assertTrue(A.proof_account_ready({'box_password': 'p', 'outlook_email': 'box@example.test'}))
            self.assertFalse(A.proof_account_ready({'outlook_email': 'box@example.test'}))
            self.assertFalse(A.proof_account_ready({'box_password': 'p', 'outlook_email': 'other@example.test'}))
        with patch('intake.find_account', return_value=None), patch('runtime_config.load_sources', return_value={}):
            self.assertFalse(A.proof_account_ready({'box_password': 'p'}))

    def test_a_proof_error_is_recorded_not_swallowed(self):
        self.held()
        run = fake_run([GOOD], [{'passed': True, 'issues': []}])
        row = A.candidates(self.root, CFG, A.Ledger(self.root))[0]
        def broken(*a):
            raise KeyError('mailbox password')
        with patch('task_solver.dev_loop.ledger_append'), patch.object(A.time, 'sleep'):
            out = A.process(self.root, CFG, row, 'k', {}, run=run, send=lambda *a: True, verify=broken,
                            notify=lambda t: None)
        self.assertEqual(out, 'sent_unverified')
        with sqlite3.connect(self.root / A.STATE / 'ledger.sqlite3') as db:
            detail = json.loads(db.execute('SELECT detail FROM attempts').fetchone()[0])
        self.assertEqual(detail['verify_errors'], ['KeyError'])

    def test_missing_config_means_off(self):
        self.assertIsNone(A.load_config(self.root))
        (self.root / A.CONFIG).write_text(json.dumps({'mode': 'sometimes', 'clients': {}}))
        with self.assertRaises(ValueError):
            A.load_config(self.root)


if __name__ == '__main__':
    unittest.main()
