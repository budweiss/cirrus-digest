"""Synthetic checks for the per-email trace: temp roots only."""
import json
import tempfile
import time
import unittest
from pathlib import Path

import client_request_trace as T
import client_research_agent as A
import intake_lifecycle
from conversation_memory import Memory

BODY = 'PRIVATE question words that must never appear in a trace.'


class Trace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, client='bill', meta=None, state='delivered', mid='<m1>'):
        turn = Memory(self.root).add(owner='cumulus1', client=client, project='general', thread='t', message_id=mid,
                                     question=BODY, state=state, meta=meta or {})
        row = [1, 'client@example.test', 'Research', BODY, mid]
        intake_lifecycle.capture(self.root, row)
        return turn, row

    def test_research_agent_answer_names_local_attempts_models_check_and_proof(self):
        turn, row = self.request(meta={'reason': 'Local attempts exhausted; private work stays local.',
            'stages': {'local': {'provider': 'ollama', 'model': 'gpt-oss:120b'},
                       'local_repair': {'provider': 'ollama', 'model': 'gpt-oss:120b'}}})
        intake_lifecycle.update(self.root, row, 'sent_verified', 'delivery-review', 'done',
                                {'client': 'bill', 'answer_request_id': turn['id'], 'sent_receipt': 'r'})
        key = intake_lifecycle.identity(row[1], row[4])
        ledger = A.Ledger(self.root); ledger.start(key)
        ledger.finish(key, 'sent_verified', 0.38, {'hold_label': 'asks for a new report or file',
            'models': {'claude-sonnet-5-5': 0.36, 'claude-haiku-4-5': 0.02},
            'log': [{'attempt': 0, 'stage': 'check', 'passed': True}]})
        t = T.collect(self.root)[0]
        self.assertEqual(t['answered_by'], 'research agent')
        out = T.text(t)
        for want in ('gpt-oss:120b (local model on', 'claude-sonnet-5-5 (Anthropic API) $0.36',
                     'claude-haiku-4-5 (Anthropic API) $0.02', 'passed on try 1', 'proven in Sent Mail',
                     'asks for a new report or file'):
            self.assertIn(want, out)
        self.assertNotIn('PRIVATE', json.dumps(t) + out)

    def test_refused_local_stage_and_manual_session_answer(self):
        turn, row = self.request(meta={'manual_answer': 'S679 session send, verified in Sent Mail',
            'stages': {'local': {'provider': '', 'error_type': 'ValueError',
                                 'error_detail': 'request exceeds qualified input size'}}})
        intake_lifecycle.update(self.root, row, 'sent_verified', 'delivery-review', 'done',
                                {'client': 'bill', 'answer_request_id': turn['id'], 'sent_receipt': 'r'})
        t = T.collect(self.root)[0]
        self.assertEqual(t['answered_by'], 'Cowork session')
        self.assertIn('refused before the model ran (request exceeds qualified input size)', T.text(t))

    def test_thread_hold_worker_owner_and_unanswered(self):
        turn, row = self.request(meta={'reason': "This client thread is awaiting Buddy's review."}, state='needs_review')
        intake_lifecycle.update(self.root, row, 'blocked_on_evidence', 'cowork', 'needs web research',
                                {'client': 'bill', 'answer_request_id': turn['id']})
        out = T.text(T.collect(self.root)[0])
        self.assertIn('stopped before any model ran', out)
        self.assertIn('Answered by: not answered yet', out)
        _, row2 = self.request(client='alyssa', mid='<m2>')
        intake_lifecycle.update(self.root, row2, 'working', 'lesson_build', 'lesson job builds', {'client': 'alyssa'})
        self.assertIn('lesson-build worker', [x for x in T.collect(self.root) if x['client'] == 'alyssa'][0]['answered_by'])

    def test_update_writes_owner_only_files_and_skips_buddy(self):
        _, row = self.request(client='buddy')
        intake_lifecycle.update(self.root, row, 'noted', 'intake', 'x', {'client': 'buddy'})
        self.assertEqual(T.update(self.root), 0)
        turn, row = self.request(mid='<m3>')
        intake_lifecycle.update(self.root, row, 'noted', 'intake', 'thanks', {'client': 'bill'})
        self.assertEqual(T.update(self.root), 1)
        files = list((self.root / T.OUT).glob('*.json'))
        self.assertEqual((len(files), files[0].stat().st_mode & 0o777), (1, 0o600))


if __name__ == '__main__':
    unittest.main()
