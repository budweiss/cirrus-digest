import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import tailscale_review as r


class ReviewTest(unittest.TestCase):
    def good(self):
        return dict(host='cumulus1', state='OK', mode='shadow', receipt_age=100,
                    hold=False, episodes=0, hygiene=[], server_identity_ok=True,
                    pings_ok=True, selftest_ok=True,
                    history=dict(coverage=.99, bad_runs=0, max_gap_seconds=310))

    def test_promotion_gates(self):
        self.assertEqual(r.gates(self.good(), r.DUE), [])
        self.assertTrue(r.gates(self.good(), r.DUE-1))
        for key, value in [('receipt_age', 1000), ('hold', True), ('pings_ok', False),
                           ('server_identity_ok', False), ('episodes', 1), ('selftest_ok', False),
                           ('mode', 'relogin'), ('history', {})]:
            s = self.good(); s[key] = value
            self.assertTrue(r.gates(s, r.DUE), key)

    def test_history_missing_and_failure(self):
        h = r.history('', r.DUE)
        self.assertEqual(h['coverage'], 0)
        self.assertGreater(h['max_gap_seconds'], 1800)
        h = r.history('2026-11-07 14:00:00 FAIL STOPPED test\n2026-11-08 09:00:00 ok OK cumulus1\n2026-11-08 09:05:00 FAIL STOPPED cumulus1', r.DUE)
        self.assertEqual(h['runs'], 2)
        self.assertEqual(h['bad_runs'], 1)

    def test_no_early_mode_change(self):
        s = self.good(); s['reasons'] = ['not due']
        with tempfile.TemporaryDirectory() as td, patch.object(r.heal, 'LOGS', Path(td)), patch.object(r, 'snapshot', return_value=s):
            r.enable_repair()
            self.assertFalse((Path(td)/'tailscale-heal.mode').exists())
            s['reasons'] = []
            self.assertEqual(r.enable_repair()['mode'], 'repair')

    def test_deadline_watch(self):
        with tempfile.TemporaryDirectory() as td, patch.object(r, 'REPORT', Path(td)/'report.json'), patch.object(r.heal, 'run', return_value=(0, 'active\n', '')):
            self.assertEqual(r.watch(r.DUE-10), {})
            self.assertIn('review:overdue', r.watch(r.DUE+1801))
            r.REPORT.write_text(json.dumps(dict(complete=True, notified=True)))
            self.assertEqual(r.watch(r.DUE+999999), {})

    def test_check_has_no_write_send_or_promotion(self):
        s = self.good(); s['reasons'] = []
        def endpoint(h, action):
            self.assertEqual(action, 'snapshot')
            return dict(s, host=h)
        with patch.object(r.socket, 'gethostname', return_value='cumulus1'), patch.object(r, 'remote', side_effect=endpoint), patch.object(r.heal, 'telegram') as send, patch.object(r.heal, 'save_state') as save:
            r.review(True)
            send.assert_not_called(); save.assert_not_called()

    def test_failed_delivery_retries_without_repromotion(self):
        with tempfile.TemporaryDirectory() as td, patch.object(r, 'REPORT', Path(td)/'r.json'), patch.object(r.socket, 'gethostname', return_value='cumulus1'), patch.object(r.time, 'time', return_value=r.DUE+1), patch.object(r, 'remote') as remote, patch.object(r.heal, 'telegram', return_value=True):
            r.REPORT.write_text(json.dumps(dict(complete=True, notified=False, message='test result')))
            self.assertEqual(r.review(), 0)
            remote.assert_not_called()
            self.assertTrue(json.loads(r.REPORT.read_text())['notified'])


if __name__ == '__main__':
    unittest.main()
