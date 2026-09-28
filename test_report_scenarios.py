"""S346 real report readers/composers, scratch SQLite and state; no sends."""
import contextlib
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import cumulus_daily_brief as daily
import entity_kb as kb
import entity_kb_weekly_digest as weekly

class Clock(datetime):
    @classmethod
    def now(cls, tz=None): return cls(2026,9,28,20,0,0,tzinfo=tz)

class ReportScenarios(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.jobs=self.root/'jobs.json';self.sky=self.root/'sky';self.sky.mkdir()
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        for target in ['socket.socket.connect','socket.create_connection','urllib.request.urlopen','subprocess.run','subprocess.Popen','smtplib.SMTP','smtplib.SMTP_SSL']:
            self.stack.enter_context(patch(target,side_effect=AssertionError('external operation forbidden')))
        for mod,name,value in [(daily,'JOBS_STATUS_PATH',self.jobs),(daily,'SKY_STATE_DIR',self.sky),(daily,'TODAY','2026-09-28'),(daily,'datetime',Clock),(daily,'CREDS_PATH',self.root/'absent-creds'),(kb,'DATA_DIR',self.root/'db'),(weekly,'datetime',Clock),(weekly,'CREDS_PATH',self.root/'absent-creds')]:
            self.stack.enter_context(patch.object(mod,name,value))
        self.stack.enter_context(patch.object(daily,'_sudo_cat',side_effect=self.read_sky))
        self.stack.enter_context(patch.object(daily,'send_telegram',side_effect=AssertionError('send forbidden')))
        self.stack.enter_context(patch.object(weekly,'_send_mail',side_effect=AssertionError('send forbidden')))
        self.write_sky('heartbeat-incidents.json',{'incidents':{},'resolved':[]})
        self.write_sky('ledger.jsonl',{'ts':'2026-09-28 19:59:00','event':'check','detail':'healthy'})
        self.write_sky('spend-ledger.jsonl',{'ts':'2026-09-28 19:59:00','cost_usd':0})
        self.status={name:dict(ok=True,last_run='2026-09-28 06:00',note='valid quiet: no changes') for names in daily.CLIENT_JOBS.values() for name in names}
        self.jobs.write_text(json.dumps(self.status))
        kb.upsert_entity('hoa_leads_bill','fixture','Fixture HOA',fields={'county':'Kent'})
    def read_sky(self,path):
        try:return path.read_text()
        except FileNotFoundError:return ''
    def write_sky(self,name,data): (self.sky/name).write_text(json.dumps(data))
    def output(self):
        out=io.StringIO()
        with patch('sys.argv',['cumulus_daily_brief.py','--dry-run']),contextlib.redirect_stdout(out):daily.main()
        return out.getvalue()
    def test_healthy_quiet_counts(self):
        body=self.output();self.assertEqual(body.count('✅'),4);self.assertEqual(body.count('valid quiet: no changes'),4)
        self.assertIn('1 routine check(s), 0 anomaly/issue flag(s), 0 escalation(s)',body)
        self.assertIn('Unresolved incidents: 0',body);self.assertIn('$0.00 spent today',body);self.assertNotIn('UNKNOWN',body)
    def test_mixed_daily_weekly_failure_stale_missing(self):
        self.status['billsnow']['last_run']='2026-09-21 04:00';self.status['billnewdev'].update(ok=False,note='source artifact missing');del self.status['pedagogy'];self.jobs.write_text(json.dumps(self.status))
        body=self.output();self.assertEqual(body.count('✅'),1)
        for value in ['billsnow: no run recorded today; last 2026-09-21','cadence not evaluated here','⚠️ billnewdev: 06:00 — source artifact missing','pedagogy: evidence UNKNOWN']:self.assertIn(value,body)
        self.assertNotIn('**Quiet day**',body)
    def test_malformed_current_day_job_timestamp_not_healthy(self):
        self.status['hoaleads']['last_run']='2026-09-28 garbage'
        self.jobs.write_text(json.dumps(self.status))
        body=self.output()
        self.assertIn('hoaleads: evidence UNKNOWN',body)
        self.assertEqual(body.count('✅'),3)
    def test_daily_missing_crm_database_unknown_without_creation(self):
        path=self.root/'db'/'hoa_leads_bill.db';path.unlink()
        self.assertIn('CRM coverage UNKNOWN: source database missing',self.output())
        self.assertFalse(path.exists())
    def test_missing_job_file_not_quiet(self):
        self.jobs.unlink();self.assertIn('Job evidence UNKNOWN',self.output())
    def test_missing_stale_malformed_supervisor_ledger(self):
        for data in ['', '[]', '{bad', json.dumps({'ts':'not-a-date','event':'check'}), json.dumps({'ts':'2026-09-20 06:00','event':'check'})]:
            with self.subTest(data=data):
                (self.sky/'ledger.jsonl').write_text(data);self.assertIn('Supervisor ledger coverage UNKNOWN or incomplete',self.output())
    def test_missing_malformed_spend_not_zero(self):
        for data in ['', '[]',json.dumps({'ts':'2026-09-28'}),json.dumps({'ts':'invalid','cost_usd':0}),json.dumps({'ts':'2026-09-28','cost_usd':'NaN'})]:
            with self.subTest(data=data):
                (self.sky/'spend-ledger.jsonl').write_text(data);body=self.output();self.assertIn('Spend coverage UNKNOWN',body);self.assertNotIn('$0.00 spent today',body)
    def repair(self,result,ts='2026-09-28 10:01:00'):
        rows=[dict(ts='2026-09-28 10:00:00',event='check',detail='issue: fixture.service'),dict(ts=ts,event='action',tool='restart_service',detail='fixture.service',result=result)]
        (self.sky/'ledger.jsonl').write_text('\n'.join(map(json.dumps,rows)))
    def test_failed_refused_repair_not_healed(self):
        for result in ['FAILED: denied','REFUSED',None]:
            self.repair(result);body=self.output();self.assertIn('NOT repaired',body);self.assertNotIn('repair completed',body);self.assertNotIn('healed',body)
    def test_repair_before_failure_not_recovery(self):
        self.repair('restarted','2026-09-28 09:59:00');self.assertIn('NOT repaired',self.output())
    def test_successful_repair_not_verified_recovery(self):
        self.repair('restarted');body=self.output();self.assertIn('repair completed 10:01',body);self.assertIn('recovery requires healthy probes',body);self.assertNotIn('healed',body)
    def test_incident_age_owner_next_action_recovery(self):
        now=Clock.now().timestamp()
        self.write_sky('heartbeat-incidents.json',{'incidents':{'failed_runs:fixture':dict(first_seen=now-7200,state='action pending',owner='Cowork',next_check=now+60,next_action='inspect source artifact')},'resolved':[dict(incident='unit:recovered',resolved_at=now-300,recovery_evidence='absent from healthy probes for 120 seconds')]})
        body=self.output()
        for value in ['Unresolved incidents: 1','2.0h; owner Cowork','next check '+str(now+60),'next action inspect source artifact','Recovered unit:recovered: absent from healthy probes for 120 seconds']:self.assertIn(value,body)
    def test_daily_weekly_real_database_windows(self):
        kb.upsert_entity('hoa_leads_bill','fixture','Fixture HOA',fields={'county':'Kent'})
        for date,text in [('2026-09-01','old-excluded'),('2026-09-24','week-only'),('2026-09-28','today-visible')]:kb.add_signal('hoa_leads_bill','fixture','distress',text,occurred_at=date+' 05:00:00')
        body=self.output();week=weekly.compose_digest(['hoa_leads_bill'],'2026-09-21 00:00:00',opportunities_only=True)
        self.assertIn('today-visible',body);self.assertNotIn('week-only',body);self.assertIn('2 findings',week);self.assertIn('week-only',week);self.assertIn('today-visible',week);self.assertNotIn('old-excluded',week)
    def test_weekly_valid_quiet_no_delivery(self):
        kb.upsert_entity('hoa_leads_bill','fixture','Fixture HOA')
        self.assertEqual(weekly.run('bill',dry_run=True)['reason'],'nothing to report in the last 7d')
    def test_weekly_missing_database_fails_without_creating_source(self):
        (self.root/'db'/'hoa_leads_bill.db').unlink()
        with self.assertRaisesRegex(FileNotFoundError,'source database missing'):
            weekly.run('bill',dry_run=True)
        self.assertFalse((self.root/'db'/'hoa_leads_bill.db').exists())
    def test_database_initialization_failure_closes_connection(self):
        conn=MagicMock()
        conn.executescript.side_effect=ValueError('fixture schema failure')
        with patch.object(kb.sqlite3,'connect',return_value=conn):
            with self.assertRaisesRegex(ValueError,'fixture schema failure'):
                kb._connect('fixture',str(self.root/'fixture.db'))
        conn.close.assert_called_once_with()
    def test_weekly_corrupt_database_fails(self):
        path=self.root/'corrupt.db';path.write_bytes(b'not a sqlite database')
        with self.assertRaises(Exception) as caught:weekly.run('bill',dry_run=True,db_path=str(path))
        self.assertIn('database',str(caught.exception))

if __name__=='__main__':unittest.main()
