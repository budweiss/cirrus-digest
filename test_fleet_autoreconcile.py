"""S497: automatic clearing of stuck fleet jobs uses the real queue and its guards."""

import json
import tempfile
import unittest
from pathlib import Path

import fleet_autoreconcile as a
from fleet_queue import Queue
from test_fleet_queue import policy

W, W2 = 'cumulus1-gptoss', 'cumulus2-qwen'


class AutoReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name); self.log = self.dir / 'auto.jsonl'
        self.now, self.mono, self.p = 1800000000., 1000., policy()
        self.q = Queue(self.dir / 'queue.db', self.p, lambda: self.now, lambda: self.mono, 'boot-1')
        for w, s in self.p['workers'].items():
            self.q.health(w, s['model'], 32768, 20000)

    def tick(self, n): self.now += n; self.mono += n
    def raw(self, busy=False, age=10):
        return {'observed': self.now - age, 'workers': [{'worker': W, 'busy': busy}, {'worker': W2, 'busy': False}]}
    def state(self, job): return next(r for r in self.q.status()['jobs'] if r['id'] == job)['state']

    def stuck(self, silent=700):
        job = self.q.submit(W, 'one', {'fixture': 'sample12'}, 'synthetic-v1'); self.q.claim(W)
        self.tick(silent); self.q.monitor()             # lease expired -> 'unknown', as in production
        self.assertEqual(self.state(job), 'unknown')
        return job

    def test_stuck_job_with_idle_worker_is_reconciled_logged_and_reported(self):
        job = self.stuck()
        acts, left = a.plan(self.q, self.raw(), self.now, {})
        self.assertEqual([(x, r['id']) for x, r in acts], [('reconcile', job)])
        done = a.apply(self.q, acts, self.log, self.now)
        self.assertEqual(self.state(job), 'cancelled')
        self.assertEqual(done[0]['action'], 'reconciled')
        self.assertEqual(json.loads(self.log.read_text())['job'], job)
        msg = a.notice(done)
        self.assertIn('FYI, no action needed', msg); self.assertIn(job[:8], msg)

    def test_recently_stuck_job_waits(self):
        job = self.stuck(silent=300)
        acts, left = a.plan(self.q, self.raw(), self.now, {})
        self.assertEqual(acts, []); self.assertIn((job, 'stuck under 10 min; wait'), left)

    def test_busy_or_unobserved_worker_is_left_for_a_human(self):
        job = self.stuck()
        for raw in (self.raw(busy=True), {'observed': self.now, 'workers': [{'worker': W2, 'busy': False}]}):
            acts, left = a.plan(self.q, raw, self.now, {})
            self.assertEqual(acts, []); self.assertIn((job, 'worker not verified idle; human only'), left)
        self.assertEqual(self.state(job), 'unknown')

    def test_stale_or_unreadable_controller_status_acts_on_nothing(self):
        self.stuck()
        for raw in (self.raw(age=a.FRESH_S + 1), None, {'workers': []}):
            self.assertEqual(a.plan(self.q, raw, self.now, {})[0], [])

    def test_project_that_delivers_is_never_touched(self):
        # The queue itself refuses to OPEN with a delivering project ("no delivery
        # authority"), so this is a second, independent layer: flip it after open.
        job = self.stuck()
        self.q.policy['projects'][W]['delivery'] = 'email'
        acts, left = a.plan(self.q, self.raw(), self.now, {})
        self.assertEqual(acts, []); self.assertIn((job, 'project delivers; human only'), left)

    def test_orphaned_queued_request_is_abandoned_but_fresh_queue_is_not(self):
        job = self.q.submit(W2, 'q', {'fixture': 'sample12'}, 'synthetic-v1')
        self.tick(600)
        self.assertEqual(a.plan(self.q, self.raw(), self.now, {}), ([], []))   # ordinary waiting work
        self.tick(1300)
        acts, _ = a.plan(self.q, self.raw(), self.now, {})
        a.apply(self.q, acts, self.log, self.now)
        self.assertEqual(self.state(job), 'cancelled')

    def test_daily_cap_leaves_the_fourth_for_a_human(self):
        job = self.stuck()
        for i in range(a.CAP):
            with self.log.open('a') as fh:
                fh.write(json.dumps({'ts': self.now - 3600, 'action': 'reconciled', 'project': W, 'job': str(i)}) + '\n')
        acts, left = a.plan(self.q, self.raw(), self.now, a.recent_clears(self.log, self.now))
        self.assertEqual(acts, []); self.assertIn((job, 'daily cap of 3 reached; human needed'), left)
        old = {'ts': self.now - 90000, 'action': 'reconciled', 'project': W, 'job': 'old'}
        self.log.write_text(json.dumps(old) + '\n')                            # older than 24 h: not counted
        self.assertEqual(a.recent_clears(self.log, self.now), {})

    def test_a_refused_step_is_recorded_and_escalated_not_forced(self):
        job = self.stuck()
        acts, _ = a.plan(self.q, self.raw(), self.now, {})
        acts = [(x, dict(r, fence='wrong')) for x, r in acts]                    # guard must refuse
        done = a.apply(self.q, acts, self.log, self.now)
        self.assertEqual(done[0]['action'], 'refused'); self.assertEqual(self.state(job), 'unknown')
        self.assertIn('HUMAN NEEDED', a.notice(done))

    def test_ledger_summary_flags_stale_controller_and_refusals(self):
        self.assertEqual(a.summary([{'action': 'reconciled'}], [('x' * 32, 'stuck under 10 min; wait')]),
                         (True, '1 cleared, 0 refused, 1 left for a human'))
        self.assertFalse(a.summary([], [('*', 'controller status stale or unreadable')])[0])
        self.assertFalse(a.summary([{'action': 'refused'}], [])[0])

    def test_unknown_arguments_are_refused(self):
        self.assertEqual(a.main(['--bogus']), 2)
        self.assertEqual(a.main([]), 2)


if __name__ == '__main__':
    unittest.main()
