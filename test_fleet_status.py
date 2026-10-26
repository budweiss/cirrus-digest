import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import fleet_status as f
class StatusTests(unittest.TestCase):
 def test_fresh_stale_future_and_missing(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'status.json';self.assertFalse(f.check(p,1000)['ok'])
   for stamp,ok,workers,expected in [(999,True,[1,2],True),(800,True,[1,2],False),(1001,True,[1,2],False),(999,False,[1,2],False),(999,True,[1],False)]:
    p.write_text(json.dumps({'observed':stamp,'ok':ok,'workers':workers}));self.assertEqual(f.check(p,1000)['ok'],expected)
 def test_independent_observer_no_mutation(self):
  import cirrus_watchdog as w
  with patch.object(w.subprocess,'run') as run:
   run.return_value.returncode=0;run.return_value.stdout='{"ok":true}'
   self.assertTrue(w.fleet_controller_health())
   self.assertIn('fleet_status.py',run.call_args.args[0][-1]);self.assertNotIn('restart',str(run.call_args))
   run.return_value.stdout='broken';self.assertFalse(w.fleet_controller_health())
   run.side_effect=TimeoutError;self.assertFalse(w.fleet_controller_health())
 def test_fresh_status_passes_through_stuck_jobs_and_qualifications(self):
  # S497: the watchdog can only name a stuck job or a lapsing qualification
  # if check() passes through what the controller already reports.
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'status.json'
   stuck=[{'id':'a'*32,'state':'unknown'}];quals=[{'project':'articles-infra','expires':5000,'remaining_hours':1.1}]
   p.write_text(json.dumps({'observed':999,'ok':False,'workers':[1,2],'unsettled':stuck,'qualifications':quals}))
   r=f.check(p,1000);self.assertFalse(r['ok']);self.assertEqual((r['unsettled'],r['qualifications']),(stuck,quals))
   p.write_text(json.dumps({'observed':500,'ok':True,'workers':[1,2],'unsettled':stuck}))
   self.assertNotIn('unsettled',f.check(p,1000))   # stale data names nothing
 def test_watchdog_names_stuck_jobs_and_expiry_with_the_fix(self):
  import cirrus_watchdog as w
  jid='55ef5b5a17094ae0b05cca51f55d4a9c';qid='ec6ba104f02d454da77a02fa6c567993';now=1_800_000_000
  data={'ok':False,'unsettled':[{'id':jid,'state':'unknown'},{'id':qid,'state':'queued'},{'id':'x','state':'unknown'}],
        'qualifications':[{'project':'articles-infra','expires':now+3600*20}]}
  found=w.fleet_findings(data,now)
  self.assertTrue(any('fleet-reconcile args.id='+jid in x and 'HUMAN NEEDED' in x for x in found))
  self.assertTrue(any('fleet-abandon args.id='+qid in x for x in found))
  self.assertFalse(any("'x'" in x or ' x ' in x for x in found))          # malformed id ignored
  exp=[x for x in found if 'qualification' in x];self.assertEqual(len(exp),1)
  self.assertIn('expires',exp[0]);self.assertIn('fleet-qualify-renew',exp[0])
  self.assertEqual(found,w.fleet_findings(data,now+600))                 # stable wording -> one alert per episode
  data['qualifications'][0]['expires']=now-60
  self.assertTrue(any('EXPIRED' in x for x in w.fleet_findings(data,now)))
  far={'ok':True,'unsettled':[],'qualifications':[{'project':'articles-infra','expires':now+3600*72}]}
  self.assertEqual(w.fleet_findings(far,now),[])                         # healthy and >48 h: quiet
  auto=dict(far,qualifications=[{'project':'articles-infra','expires':now+3600*30}])
  self.assertEqual(w.fleet_findings(auto,now),[])                        # 24-48 h: automatic renewal's turn (P2)
  self.assertEqual(w.fleet_findings({'ok':False},now),[w.FLEET_DEGRADED])  # unexplained degradation stays visible
  self.assertEqual(w.fleet_findings(None,now),[w.FLEET_DEGRADED])
 def test_watchdog_gives_the_auto_clearer_time_before_alerting(self):
  # S497 P1: fleet_autoreconcile clears no-delivery stuck jobs within ~20 min;
  # the human alert waits STUCK_GRACE_S so it fires only if that did not happen.
  import cirrus_watchdog as w
  jid='55ef5b5a17094ae0b05cca51f55d4a9c';now=1_800_000_000;seen={}
  data={'ok':False,'unsettled':[{'id':jid,'state':'unknown'}],'qualifications':[]}
  self.assertEqual(w.fleet_findings(data,now,seen),[])                 # no stuck alert, no vague "degraded" either
  self.assertEqual(seen,{jid:now})
  self.assertEqual(w.fleet_findings(data,now+w.STUCK_GRACE_S-1,seen),[])
  late=w.fleet_findings(data,now+w.STUCK_GRACE_S,seen)
  self.assertTrue(any(jid in x and 'HUMAN NEEDED' in x for x in late))
  w.fleet_findings({'ok':True,'unsettled':[],'qualifications':[]},now+w.STUCK_GRACE_S+60,seen)
  self.assertEqual(seen,{})                                             # cleared job forgotten
if __name__=='__main__':unittest.main()
