import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import approval_grace as G
import renewal_recovery as R


class Recovery(unittest.TestCase):
    def setUp(self):
        self.grace_sync = patch('foundation_renewal.sync_cumulus_grace').start()
        self.addCleanup(patch.stopall)

    def test_late_cloud_evidence_refreshed_before_alerts_without_duplicate_jobs(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/'logs').mkdir(); now=200000
            (root/'logs/jobs-status.json').write_text(json.dumps({j:{'epoch':now} for j in R.JOBS}))
            old={'groups':[{'route':'foundation:test','field':'evaluations','fingerprint':'f','deadline':now+100,'grace':True}], 'packet_at':now}
            fresh={'groups':[], 'packet_at':now}
            with patch.object(R,'ROOT',root), patch.object(R,'STATE',root/'logs/state.json'), patch.object(R.time,'time',return_value=now), patch.object(R,'snapshot',side_effect=[old,fresh]), patch.object(R,'remote_snapshot',return_value=fresh), patch.object(R,'read_plist',return_value={'StartCalendarInterval':{'Hour':4,'Minute':50}}), patch.object(R,'launch_state',return_value='idle'), patch.object(R,'kick') as kick, patch('foundation_renewal.notify') as notify, patch('job_status.record'):
                result=R.run()
                self.assertEqual(result['warning_count'],0)
                self.grace_sync.assert_called_once_with(); kick.assert_not_called(); notify.assert_not_called()

    def decision(self, **kw):
        args = dict(job='foundationrenewalauto', now=200000, due=190000,
                    next_due=276400, ledger={'foundationrenewal': {'epoch': 180000}},
                    attempts={}, running='idle', local_due=170000, packet_at=195000)
        args.update(kw)
        return R.recovery_decision(**args)

    def test_missed_run_launches_once_but_failed_completed_review_does_not_repeat(self):
        self.assertEqual(self.decision(), 'launch')
        self.assertEqual(self.decision(ledger={'foundationrenewalauto': {'epoch': 195000, 'ok': False}}), 'already_ran')
        self.assertEqual(self.decision(attempts={'foundationrenewalauto': {'due': 190000}}), 'attempted')

    def test_busy_disabled_and_dependencies_never_launch(self):
        for running, want in [('running', 'running'), ('unavailable', 'unavailable')]:
            self.assertEqual(self.decision(running=running), want)
        self.assertEqual(self.decision(ledger={}), 'waiting_local')
        self.assertEqual(self.decision(packet_at=100000), 'waiting_cloud_packet')
        self.assertEqual(self.decision(packet_at=0), 'waiting_cloud_packet')
        self.assertEqual(self.decision(packet_at=0, cloud_checked=True), 'launch')
        self.assertEqual(self.decision(next_due=200500), 'scheduled_window')
        self.assertEqual(self.decision(now=190100), 'scheduled_window')

    def test_daily_occurrence_dst_and_previous_day(self):
        now = datetime(2026, 11, 1, 8, tzinfo=R.TZ).timestamp()
        due, following = R.due_occurrence(now, {'Hour': 10, 'Minute': 0})
        self.assertEqual(datetime.fromtimestamp(due, R.TZ).day, 31)
        self.assertEqual(following - due, 25 * 3600)
        with self.assertRaises(ValueError):
            R.due_occurrence(now, {'Hour': 10, 'Minute': 0, 'Weekday': 1})

    def snapshot(self, record, entry=None, extra=None, revoked=False, now=100000):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root/'config').mkdir()
            route = {'enabled': True, 'evaluations': [record] + (extra or [])}
            (root/'config/model_capabilities.json').write_text(json.dumps({'projects': {'foundation:research:synthesise': route}}))
            sha = G.record_sha(record)
            (root/'config/approval-grace.json').write_text(json.dumps({'entries': {sha: entry} if entry else {}, 'revoked': [sha] if revoked else []}))
            with patch('capability_registry.contract_digest', return_value='valid'):
                return R.snapshot(root, now)['groups']

    def test_grace_alert_uses_earlier_freshness_cutoff(self):
        record = {'approved': True, 'expires_at': 99000, 'contract_sha256': 'valid'}
        group = self.snapshot(record, {'tested_at': 10000})[0]
        self.assertEqual(group['deadline'], 10000 + G.FRESH_S)
        self.assertTrue(group['grace'])
        self.assertEqual(len(R.alerts_for('CIRRUS', [group], 100000)), 1)
        self.assertEqual(self.snapshot(record, {'tested_at': 99000})[0]['deadline'], min(99000+G.GRACE_S, 99000+G.FRESH_S))
        self.assertEqual(self.snapshot(record, {'tested_at': 10000}, revoked=True)[0]['deadline'], 99000)

    def test_stale_or_missing_grace_never_extends_expiry(self):
        record = {'approved': True, 'expires_at': 200000, 'contract_sha256': 'valid'}
        self.assertEqual(self.snapshot(record, {'tested_at': 10000})[0]['deadline'], 200000)
        self.assertEqual(self.snapshot(record)[0]['deadline'], 200000)

    def test_healthy_alternative_prevents_false_outage_warning(self):
        old = {'approved': True, 'expires_at': 90000, 'contract_sha256': 'valid'}
        current = dict(old, expires_at=400000)
        group = self.snapshot(old, extra=[current])[0]
        self.assertEqual(R.alerts_for('CIRRUS', [group], 100000), [])

    def test_warning_escalation_and_no_private_fields(self):
        g = {'route':'foundation:test', 'field':'evaluations','fingerprint':'hash','deadline':110000,'grace':True}
        self.assertTrue(R.alerts_for('CIRRUS',[g],50000)[0][0].endswith(':24h'))
        self.assertTrue(R.alerts_for('CIRRUS',[g],100000)[0][0].endswith(':6h'))
        self.assertTrue(R.alerts_for('CIRRUS',[g],110001)[0][0].endswith(':ended'))
        record = {'approved':True,'expires_at':99000,'contract_sha256':'valid','scope':'PRIVATE_SENTINEL','prompt':'PRIVATE_SENTINEL'}
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(self.snapshot(record)))

    def test_real_wrapper_failed_process_is_not_replayed_after_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'logs').mkdir();(root/'logs/jobs-status.json').write_text('{}')
            with patch.object(R,'ROOT',root), patch.object(R,'read_plist',return_value={'StartCalendarInterval':{'Hour':4,'Minute':50}}), patch.object(R.subprocess,'call',return_value=1) as call:
                self.assertEqual(R.run_job('foundationrenewal'),1)
                self.assertEqual(R.run_job('foundationrenewal'),0)
                self.assertEqual(call.call_count,1)
                receipt=R.read(root/'logs/renewal-recovery-job-foundationrenewal.json')
                self.assertEqual(receipt['status'],'failed')

    def test_plan_no_sends_no_launches_and_failed_send_retries_then_dedupes(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'logs').mkdir();now=200000
            (root/'logs/jobs-status.json').write_text(json.dumps({j:{'epoch':now} for j in R.JOBS}))
            snap={'groups':[{'route':'foundation:test','field':'evaluations','fingerprint':'f','deadline':now+10000,'grace':True}], 'packet_at':now}
            with patch.object(R,'ROOT',root),patch.object(R,'STATE',root/'logs/state.json'),patch.object(R.time,'time',return_value=now),patch.object(R,'snapshot',return_value=snap),patch.object(R,'remote_snapshot',return_value={'groups':[],'packet_at':now}),patch.object(R,'read_plist',return_value={'StartCalendarInterval':{'Hour':4,'Minute':50}}),patch.object(R,'launch_state',return_value='idle'),patch.object(R,'kick') as kick,patch('foundation_renewal.notify',side_effect=['FAILED: network','sent']) as notify,patch('job_status.record'):
                R.run(plan=True);kick.assert_not_called();notify.assert_not_called();self.assertFalse(R.STATE.exists());self.grace_sync.assert_not_called()
                self.assertEqual(R.run()['failed_sends'],1)
                self.assertEqual(R.run()['failed_sends'],0)
                self.assertEqual(R.run()['failed_sends'],0)
                self.assertEqual(notify.call_count,2)

    def test_boot_recovery_orders_jobs_and_persists_across_restarts(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'logs').mkdir();ledger=root/'logs/jobs-status.json';ledger.write_text('{}');now=200000
            snap={'groups':[], 'packet_at':now}
            with patch.object(R,'ROOT',root),patch.object(R,'STATE',root/'logs/state.json'),patch.object(R.time,'time',return_value=now),patch.object(R,'snapshot',return_value=snap),patch.object(R,'remote_snapshot',return_value=snap),patch.object(R,'read_plist',return_value={'StartCalendarInterval':{'Hour':4,'Minute':50}}),patch.object(R,'launch_state',return_value='idle'),patch.object(R,'kick',return_value=True) as kick,patch('foundation_renewal.notify') as notify,patch('job_status.record'):
                first=R.run();self.assertEqual(first['jobs']['foundationrenewal'],'started');self.assertEqual(first['jobs']['foundationrenewalauto'],'waiting_local')
                ledger.write_text(json.dumps({'foundationrenewal':{'epoch':now}}))
                self.assertEqual(R.run()['jobs']['foundationrenewalauto'],'started')
                self.assertEqual(R.run()['jobs']['foundationrenewalauto'],'attempted')
                self.assertEqual([c.args[0] for c in kick.call_args_list],list(R.JOBS));notify.assert_not_called()

    def test_advisory_routes_excluded_and_contract_failure_visible(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'config').mkdir()
            route={'enabled':True,'evaluations':[{'approved':True,'expires_at':1,'contract_sha256':'bad'}]}
            (root/'config/model_capabilities.json').write_text(json.dumps({'projects':{'foundation:business-idea-ideate':route,'foundation:research:synthesise':route,'hoa_screening':route}}))
            with patch('capability_registry.contract_digest',side_effect=ValueError('drift')):
                snap=R.snapshot(root,100000)
                self.assertEqual(snap['groups'],[]);self.assertEqual(snap['issues'],['foundation:research:synthesise'])


if __name__ == '__main__':
    unittest.main()
