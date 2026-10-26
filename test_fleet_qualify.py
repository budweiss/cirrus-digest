"""S497 P2/P8: evidence-based qualification renewal, state file instead of code."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fleet_pilot
import fleet_qualify as fq
import fleet_queue

NOW = 1_800_000_000.0
MODEL = 'qwen3.8-27b-fp8'


def job(state, age=3600, reason='', project='articles-infra'):
    return {'id': (state + 'x' * 32)[:32], 'project': project, 'state': state, 'created': NOW - age, 'reason': reason}


def raw(model=MODEL, age=10):
    return {'observed': NOW - age, 'workers': [{'worker': 'cumulus2-qwen', 'model': model}]}


class Evidence(unittest.TestCase):
    def test_enough_validated_recent_jobs_on_the_qualified_model_pass(self):
        ev = fq.evidence([job('succeeded')] * 5, raw(), fleet_pilot.policy(), NOW)
        self.assertTrue(ev['ok']); self.assertEqual((ev['succeeded'], ev['failed']), (5, 0))

    def test_thin_old_failing_or_unobserved_evidence_fails(self):
        p = fleet_pilot.policy()
        self.assertFalse(fq.evidence([job('succeeded')] * 4, raw(), p, NOW)['ok'])              # too few
        self.assertFalse(fq.evidence([job('succeeded', age=8 * 86400)] * 9, raw(), p, NOW)['ok'])  # too old
        mixed = [job('succeeded')] * 5 + [job('failed')] * 2                                  # 71 %
        self.assertIn('success rate', fq.evidence(mixed, raw(), p, NOW)['why'])
        self.assertFalse(fq.evidence([job('succeeded')] * 5, raw(model='other'), p, NOW)['ok'])
        self.assertFalse(fq.evidence([job('succeeded')] * 5, raw(age=fq.FRESH_S + 1), p, NOW)['ok'])

    def test_reconciled_unknowns_count_against_but_abandoned_requests_do_not(self):
        p = fleet_pilot.policy()
        rows = [job('succeeded')] * 5 + [job('cancelled', reason='operator closed orphaned queued request')] * 9
        self.assertTrue(fq.evidence(rows, raw(), p, NOW)['ok'])
        rows = [job('succeeded')] * 5 + [job('cancelled', reason='reconciled')] * 2
        self.assertFalse(fq.evidence(rows, raw(), p, NOW)['ok'])


class FakeQueue:
    jobs = []
    def __init__(self, *a, **k): pass
    def status(self): return {'jobs': FakeQueue.jobs}


class Decisions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        (self.state / 'status.json').write_text(json.dumps(raw()))
        self.records, self.sent, self.renewed = [], [], []
        FakeQueue.jobs = [job('succeeded')] * 6
        for p in (patch.object(fq.fleet_queue, 'Queue', FakeQueue),
                  patch.object(fq.fleet_pilot, 'QUALIFICATION_STATE', self.state / 'qualification.json')):
            p.start(); self.addCleanup(p.stop)

    def run_main(self, argv, expires, ok=True):
        with patch.object(fq.fleet_pilot, 'QUALIFIED_UNTIL_DEFAULT', expires):
            return fq.main(argv, state=self.state, now=NOW, send=self.sent.append,
                           record=lambda *a: self.records.append(a),
                           renewer=lambda until, note: self.renewed.append(until) or ok)

    def test_not_due_does_nothing_but_record(self):
        self.assertEqual(self.run_main(['--run'], NOW + 72 * 3600), 0)
        self.assertEqual(self.renewed, []); self.assertTrue(self.records[0][1])
        self.assertIn('not due', self.records[0][2])

    def test_due_with_evidence_renews_from_current_expiry_and_sends_fyi(self):
        expires = NOW + 20 * 3600
        self.assertEqual(self.run_main(['--run'], expires), 0)
        self.assertEqual(self.renewed, [expires + fq.EXTEND_S])
        self.assertTrue(self.records[0][1]); self.assertIn('FYI', self.sent[0])

    def test_already_expired_renews_from_now(self):
        self.run_main(['--run'], NOW - 3600)
        self.assertEqual(self.renewed, [NOW + fq.EXTEND_S])

    def test_due_without_evidence_renews_nothing_and_goes_red(self):
        FakeQueue.jobs = [job('succeeded')] * 2
        self.assertEqual(self.run_main(['--run'], NOW + 20 * 3600), 1)
        self.assertEqual(self.renewed, []); self.assertFalse(self.records[0][1])
        self.assertIn('evidence insufficient', self.records[0][2]); self.assertEqual(self.sent, [])

    def test_active_fleet_job_defers_the_restart(self):
        FakeQueue.jobs = [job('succeeded')] * 6 + [job('running')]
        self.assertEqual(self.run_main(['--run'], NOW + 20 * 3600), 0)
        self.assertEqual(self.renewed, []); self.assertIn('deferred', self.records[0][2])

    def test_failed_verification_escalates(self):
        self.assertEqual(self.run_main(['--run'], NOW + 20 * 3600, ok=False), 1)
        self.assertFalse(self.records[0][1]); self.assertIn('HUMAN NEEDED', self.sent[0])

    def test_manual_override_and_argument_refusal(self):
        self.assertEqual(self.run_main(['--renew-now', '--days', '7'], NOW + 3 * 86400), 0)
        self.assertEqual(self.renewed, [NOW + 7 * 86400])                  # not stacked onto the live expiry
        for bad in (['--bogus'], [], ['--renew-now', '--days', '31'], ['--renew-now', '--days', 'x']):
            self.assertEqual(self.run_main(bad, NOW), 2)


class RenewMechanics(unittest.TestCase):
    """The real state-file write and queue-hash migration, with a fake systemctl."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'state'; self.state.mkdir()
        self.qfile = self.state / 'qualification.json'
        p = patch.object(fleet_pilot, 'QUALIFICATION_STATE', self.qfile); p.start(); self.addCleanup(p.stop)
        fleet_queue.Queue(self.state / 'queue.db', fleet_pilot.policy())       # real meta policy row
        self.calls = []

    def renew(self, healthy):
        return fq.renew(NOW + fq.EXTEND_S, 'test', state=self.state, backup=Path(self.tmp.name) / 'bk',
                        systemctl=lambda *a: self.calls.append(a), check=lambda: {'ok': healthy},
                        sleep=lambda s: None, wait_s=0.01 if not healthy else 5)

    def test_success_writes_state_not_code_and_migrates_the_queue_hash(self):
        self.assertTrue(self.renew(True))
        self.assertEqual(json.loads(self.qfile.read_text())['qualified_until'], NOW + fq.EXTEND_S)
        self.assertEqual(fleet_pilot.qualified_until(), NOW + fq.EXTEND_S)
        fleet_queue.Queue(self.state / 'queue.db', fleet_pilot.policy())       # opens: no policy drift
        self.assertEqual(self.calls, [('stop', fq.CONTROLLER), ('start', fq.CONTROLLER)])
        self.assertTrue(list((Path(self.tmp.name) / 'bk').glob('queue.db.bak.*')))

    def test_failed_verification_rolls_back_state_and_hash(self):
        before = fq._meta_hash(self.state / 'queue.db')
        self.assertFalse(self.renew(False))
        self.assertFalse(self.qfile.exists())
        self.assertEqual(fq._meta_hash(self.state / 'queue.db'), before)
        fleet_queue.Queue(self.state / 'queue.db', fleet_pilot.policy())       # still opens on the old policy


class StateFile(unittest.TestCase):
    def test_qualified_until_reads_state_and_falls_back_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'q.json'
            self.assertEqual(fleet_pilot.qualified_until(f), fleet_pilot.QUALIFIED_UNTIL_DEFAULT)
            for bad in ('{not json', '{"qualified_until": "soon"}', '{"qualified_until": true}', '[]'):
                f.write_text(bad); self.assertEqual(fleet_pilot.qualified_until(f), fleet_pilot.QUALIFIED_UNTIL_DEFAULT)
            f.write_text('{"qualified_until": 1900000000}')
            self.assertEqual(fleet_pilot.qualified_until(f), 1900000000.0)


if __name__ == '__main__':
    unittest.main()
