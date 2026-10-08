import json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import fleet_controller as f
from fleet_queue import Queue
from fleet_pilot import policy as live_policy

FIXTURE_EXPIRY=time.time()+3600
def policy():
 p=live_policy()
 for r in p['projects'].values():r['qualification_until']=FIXTURE_EXPIRY
 return p

class ObserverTests(unittest.TestCase):
 def test_missing_worker_and_ledger_degraded(self):
  with tempfile.TemporaryDirectory() as tmp:
   q=Queue(Path(tmp)/'q.db',policy())
   with patch.object(f,'observe_worker',side_effect=OSError),patch.object(f,'units',return_value=[]):
    d=f.snapshot(q,Path(tmp))
   self.assertFalse(d['ok']);self.assertEqual(len(d['errors']),3)
   self.assertEqual(len(d['queue']['workers']),2)
 def test_no_llm_dependency(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'logs').mkdir();(root/'logs/jobs-status.json').write_text('{"job":{"ok":true,"epoch":1,"private":"do not publish"}}')
   q=Queue(root/'q.db',policy())
   def observation(w):return {'worker':w,'model':f.WORKERS[w]['model'],'context':32768,'available_mib':20000,'busy':False,'observed':time.time()}
   with patch.object(f,'observe_worker',side_effect=observation),patch.object(f,'units',return_value=[]):d=f.snapshot(q,root)
   self.assertTrue(d['ok']);self.assertNotIn('private',d['legacy_jobs']['job'])
   self.assertIn('observed-only',f.render(d))
 def test_expired_enabled_research_and_orphan_queue_are_visible(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'logs').mkdir();(root/'logs/jobs-status.json').write_text('{}')
   (root/'config').mkdir();(root/'config/fleet-media.enabled').touch()
   p=policy();p['projects']['articles-infra']['qualification_until']=time.time()-1
   q=Queue(root/'q.db',p)
   job=q.submit('cumulus1-gptoss','orphan',{},'synthetic-v1')
   with q.db() as c:c.execute('UPDATE jobs SET created=? WHERE id=?',(time.time()-1801,job))
   def observation(w):return {'worker':w,'model':f.WORKERS[w]['model'],'context':32768,'available_mib':20000,'busy':False,'observed':time.time()}
   with patch.object(f,'observe_worker',side_effect=observation),patch.object(f,'units',return_value=[]):d=f.snapshot(q,root)
   self.assertFalse(d['ok'])
   self.assertTrue(any('qualification expired' in e.get('error','') for e in d['errors']))
   self.assertTrue(any(r['id']==job and r['state']=='queued' for r in d['unsettled']))
 def test_endpoint_failure_overrides_old_health(self):
  with tempfile.TemporaryDirectory() as tmp:
   q=Queue(Path(tmp)/'q.db',policy());w='cumulus1-gptoss'
   q.health(w,'gpt-oss:120b',32768,99999)
   q.submit(w,'a',{},'synthetic-v1')
   with patch.object(f,'observe_worker',side_effect=TimeoutError),patch.object(f,'units',return_value=[]):f.snapshot(q,Path(tmp))
   self.assertIsNone(q.claim(w))
 def test_unknown_metrics_fail_closed(self):
  class Response:
   def __enter__(self):return self
   def __exit__(self,*a):pass
   def read(self):return b'not metrics'
  with patch.object(f.urllib.request,'urlopen',return_value=Response()):
   with self.assertRaises(RuntimeError):f.vllm_busy('http://fixture')
class ResourceClockTests(unittest.TestCase):
 def observe(self,stamp):
  responses=[{'data':[{'id':'qwen3.8-27b-fp8','max_model_len':65536}]},
             {'host':'cumulus2','observed':stamp,'available_mib':60000}]
  with patch.object(f,'request',side_effect=responses),patch.object(f.time,'time',return_value=1000),patch.object(f,'vllm_busy',return_value=False):
   return f.observe_worker('cumulus2-qwen')
 def test_small_positive_skew_uses_local_receipt_time(self):
  result=self.observe(1000.0006)
  self.assertEqual(result['observed'],1000)
  self.assertEqual(result['source_observed'],1000.0006)
 def test_stale_large_future_and_nan_refused(self):
  for stamp in [969,1002,float('nan')]:
   with self.subTest(stamp=stamp),self.assertRaisesRegex(RuntimeError,'stale_resource'):self.observe(stamp)

class HermesReviewTests(unittest.TestCase):
 def test_disabled_busy_and_once_per_incident(self):
  from unittest.mock import Mock
  with tempfile.TemporaryDirectory() as tmp:
   state=Path(tmp);review=f.HermesReview(state,state/'media.lock');self.addCleanup(review.close)
   data={'workers':[{'worker':'cumulus2-qwen','busy':False,'context':65536}],
    'queue':{'jobs':[{'id':'a','state':'unknown','attempt':1,'reason':'lost','stage':'inference','progress':0}]}}
   process=Mock();process.poll.return_value=None;process.returncode=0
   with patch.object(f.subprocess,'Popen',return_value=process) as launch:
    review.tick(data);launch.assert_not_called()
    (state/'hermes.enabled').touch();data['workers'][0]['busy']=True
    review.tick(data);launch.assert_not_called()
    data['workers'][0]['busy']=False;review.tick(data);launch.assert_called_once()
    process.poll.return_value=0;review.tick(data);launch.assert_called_once()
    self.assertEqual(json.loads(next((state/'hermes').glob('*/finished.json')).read_text())['exit_code'],0)
    data['queue']['jobs'].append(dict(data['queue']['jobs'][0],id='b'))
    review.tick(data);self.assertEqual(launch.call_count,2)
 def test_existing_media_lock_defers_review(self):
  with tempfile.TemporaryDirectory() as tmp:
   state=Path(tmp);(state/'hermes.enabled').touch();review=f.HermesReview(state,state/'media.lock')
   with (state/'media.lock').open('a') as lock,patch.object(f.subprocess,'Popen') as launch:
    f.fcntl.flock(lock,f.fcntl.LOCK_EX|f.fcntl.LOCK_NB)
    review.tick({'workers':[{'worker':'cumulus2-qwen','busy':False,'context':65536}],
      'queue':{'jobs':[{'id':'a','state':'unknown','attempt':1,'reason':'lost','stage':'inference','progress':0}]}})
    launch.assert_not_called()
if __name__=='__main__':unittest.main()
