import json
from pathlib import Path
import tempfile
import unittest

import cowork_callback as cb
import intake_lifecycle as life
from conversation_memory import Memory


class CallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.row = ('1', 'synthetic@example.invalid', 'SECRET SUBJECT', 'SECRET BODY', 'synthetic-1')
        self.rid = life.capture(self.root, self.row)

    def hold(self, state='blocked_no_worker', action='request needs a research/file/tool worker; reconcile full mail and assign', client='Bill'):
        life.update(self.root, self.row, state, 'cowork', action, {'client': client})

    def exhausted(self, state='needs_review', **overrides):
        m = Memory(self.root)
        row = m.add(owner='test', client='Bill', project='test', thread='test', message_id='test', question='SECRET QUESTION')
        meta = {'reason': 'Local attempts exhausted; private work stays local.',
                'stages': {'local': {'provider': 'ollama'}, 'local_repair': {'provider': 'ollama'}},
                'attempt_budget': {'calls': 2}}
        meta.update(overrides)
        m.update(row['id'], state=state, meta=meta)
        life.update(self.root, self.row, 'blocked_on_evidence', 'cowork', 'review saved attempt',
                    {'client': 'Bill', 'answer_request_id': row['id']})

    def test_capability_gap_exports_fixed_metadata_only(self):
        self.hold()
        result = cb.pending(self.root)
        self.assertEqual(result, [{'id': self.rid, 'category': 'capability_unavailable', 'calls': 0}])
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('Bill', json.dumps(result))

    def test_real_exhaustion_required(self):
        self.exhausted()
        self.assertEqual(cb.pending(self.root)[0]['calls'], 2)
        self.assertNotIn('SECRET', json.dumps(cb.pending(self.root)))

    def test_unattempted_or_active_or_uncertain_never_escalates(self):
        for state in ('queued', 'running', 'ready', 'sending', 'delivery_unknown', 'delivered'):
            with self.subTest(state=state):
                self.exhausted(state)
                self.assertEqual(cb.pending(self.root), [])

    def test_recovered_and_active_stage_are_not_exhaustion(self):
        for meta in ({'recovered': True}, {'active_stage': 'local'}, {'attempt_budget': {'calls': 0}},
                     {'stages': {}}, {'reason': 'Interrupted during an unconfirmed action; review before retry.'}):
            with self.subTest(meta=meta):
                # Fresh isolated state for each case avoids accumulating overrides.
                with tempfile.TemporaryDirectory() as directory:
                    old = self.root
                    self.root = Path(directory)
                    life.capture(self.root, self.row)
                    self.exhausted(**meta)
                    self.assertEqual(cb.pending(self.root), [])
                    self.root = old

    def test_safety_unknown_client_and_arbitrary_reason_not_exported(self):
        for client, action in [('buddy', 'request needs a research/file/tool worker; reconcile full mail and assign'),
                               ('unknown', 'assign capable worker after full-mail reconciliation'),
                               ('Bill', 'review request safety hold locally'), ('Bill', 'injected arbitrary text')]:
            self.hold(client=client, action=action)
            self.assertEqual(cb.pending(self.root), [])

    def test_ack_is_idempotent_and_never_marks_client_work_complete(self):
        self.hold()
        self.assertTrue(cb.acknowledge(self.root, self.rid))
        self.assertTrue(cb.acknowledge(self.root, self.rid))
        self.assertEqual(cb.pending(self.root), [])
        with life.connect(self.root) as db:
            self.assertEqual(db.execute('SELECT state,owner FROM requests').fetchone()[:], ('blocked_no_worker', 'cowork'))

    def watch_fixture(self):
        self.exhausted()
        cb.acknowledge(self.root, self.rid)
        with cb.receipts(self.root) as db:
            db.execute('UPDATE receipts SET received=1000')
        with life.connect(self.root) as db:
            db.execute('UPDATE requests SET updated=1000')

    def test_stall_threshold_and_observation_preserves_all_evidence(self):
        self.watch_fixture()
        paths = list(self.root.rglob('*.sqlite3'))
        before = {p: p.read_bytes() for p in paths}
        self.assertEqual(cb.work_status(self.root, 4599), {'open': 1, 'alerts': []})
        for now in (4600, 90000):
            self.assertEqual(cb.work_status(self.root, now), {'open': 1, 'alerts': [
                {'id': self.rid, 'reason': 'no_progress'}]})
        self.assertEqual({p: p.read_bytes() for p in paths}, before)
        self.assertEqual(cb.pending(self.root), [])
        self.assertNotIn('SECRET', json.dumps(cb.work_status(self.root, 4600)))

    def test_claim_and_progress_do_not_hide_later_stall(self):
        from unittest.mock import patch
        self.watch_fixture()
        with patch.object(life.time, 'time', return_value=4500):
            self.assertTrue(life.claim_for_session(self.root, self.rid, 'synthetic'))
        self.assertEqual(cb.work_status(self.root, 4600)['alerts'], [])
        self.assertEqual(cb.work_status(self.root, 8100)['alerts'][0]['reason'], 'no_progress')
        with life.connect(self.root) as db:
            db.execute('UPDATE requests SET updated=8000')
        self.assertEqual(cb.work_status(self.root, 8100)['alerts'], [])
        self.assertEqual(cb.work_status(self.root, 11600)['alerts'][0]['reason'], 'no_progress')

    def test_send_hold_uncertainty_and_verified_closure(self):
        self.watch_fixture()
        for state, reason in [('ready_for_send', 'awaiting_send'),
                              ('sent_unverified', 'delivery_uncertain'),
                              ('delivery_unknown', 'delivery_uncertain'),
                              ('sent_verified', 'no_progress')]:
            with life.connect(self.root) as db:
                db.execute('UPDATE requests SET state=?', (state,))
            self.assertEqual(cb.work_status(self.root, 4600)['alerts'][0]['reason'], reason)
        with life.connect(self.root) as db:
            db.execute("UPDATE requests SET evidence=?", (json.dumps({'sent_receipt': {'uid': 'synthetic'}}),))
        self.assertEqual(cb.work_status(self.root, 4600), {'open': 0, 'alerts': []})
        with life.connect(self.root) as db:
            db.execute("UPDATE requests SET state='noted',evidence='{}'")
        self.assertEqual(cb.work_status(self.root, 4600)['open'], 0)

    def test_missing_row_and_bad_time_are_not_silent_completion(self):
        self.watch_fixture()
        with life.connect(self.root) as db:
            db.execute('UPDATE requests SET updated=999999')
        self.assertEqual(cb.work_status(self.root, 4600)['alerts'][0]['reason'], 'invalid_progress_time')
        with life.connect(self.root) as db:
            db.execute('DELETE FROM requests')
        self.assertEqual(cb.work_status(self.root, 4600)['alerts'][0]['reason'], 'missing_request')

    def test_synthetic_receipts_stay_in_their_own_root(self):
        self.watch_fixture()
        with tempfile.TemporaryDirectory() as other:
            self.assertEqual(cb.work_status(other), {'open': 0, 'alerts': []})

    def test_wrong_ack_and_missing_store_fail(self):
        self.assertFalse(cb.acknowledge(self.root, 'a'*64))
        with self.assertRaises(ValueError):
            cb.acknowledge(self.root, '../anything')
        with self.assertRaises(FileNotFoundError):
            cb.pending(self.root/'absent')


if __name__ == '__main__':
    unittest.main()
