import json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import fleet_media as f
import media_pipeline as m
from fleet_queue import Queue,Refused
from fleet_pilot import policy as live_policy
from learn_watch import INSTRUCTIONS

FIXTURE_EXPIRY=time.time()+3600
def policy():
 p=live_policy()
 for r in p['projects'].values():r['qualification_until']=FIXTURE_EXPIRY
 return p

class MediaAdmissionTests(unittest.TestCase):
 def setUp(self):
  guard=patch.object(f,'policy',side_effect=policy);guard.start();self.addCleanup(guard.stop)
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name);self.state=self.root/'state';self.state.mkdir()
  (self.state/'status.json').write_text(json.dumps({'ok':True,'observed':time.time(),'workers':[{},{}]}))
  self.text='The sample included twelve participants and there was no control group.'
  self.payload={'domain':'articles-infra','claims':True,'instructions':INSTRUCTIONS,'text':self.text}
 def observe(self,_):return {'model':m.MODEL,'context':65536,'available_mib':60000,'busy':False,'observed':time.time()}
 def analyze(self,*args,**kwargs):
  return m.analyze(*args,**kwargs,caller=lambda *a:json.dumps({'claims':[]}),counter=len)
 def run_media(self,analyzer=None,payload=None):
  return f.run_analysis(payload or self.payload,root=self.root,state=self.state,observer=self.observe,analyzer=analyzer or self.analyze)
 def test_F04_F07_duplicate_uses_verified_result_without_reexecution(self):
  self.assertEqual(self.run_media(),[])
  self.assertEqual(self.run_media(analyzer=lambda *a,**kw:self.fail('duplicate analysis')),[])
  q=Queue(self.state/'queue.db',policy());self.assertEqual(len(q.status()['jobs']),1)
  self.assertEqual(q.status()['jobs'][0]['state'],'succeeded')
 def test_controller_outage_stops_admission(self):
  (self.state/'status.json').unlink()
  with self.assertRaises(Refused):self.run_media()
  self.assertFalse((self.state/'queue.db').exists())
 def test_A07_changed_prompt_refused(self):
  p=dict(self.payload);p['instructions']='different'
  with self.assertRaises(Refused):self.run_media(payload=p)
 def test_C06_F05_archive_damage_refuses_replay(self):
  self.run_media();p=next((self.root/'media').rglob('transcript.txt'));p.write_text('different')
  with self.assertRaises(Refused):self.run_media()
 def test_C06_gap_fails(self):
  self.run_media();p=next((self.root/'media').rglob('coverage.json'));d=json.loads(p.read_text());d['spans']=[[1,len(self.text)]];p.write_text(json.dumps(d))
  with self.assertRaises(Refused):self.run_media()
 def test_C07_zero_findings_valid(self):
  self.assertEqual(self.run_media(),[])
  result=Queue(self.state/'queue.db',policy()).status()['jobs'][0]
  self.assertEqual(result['state'],'succeeded');self.assertTrue(json.loads(result['result'])['source_verified'])
 def test_B13_exception_quarantines_slot(self):
  def fail(*a,**kw):raise TimeoutError('fixture')
  with self.assertRaises(TimeoutError):self.run_media(fail)
  q=Queue(self.state/'queue.db',policy());self.assertEqual(q.status()['jobs'][0]['state'],'unknown')
  with self.assertRaises(Refused):self.run_media()
 def test_F05_unsupported_quote_refused(self):
  self.run_media();p=next((self.root/'media').rglob('result.json'));p.write_text(json.dumps({'result':[{'quote':'This unsupported quotation is entirely invented and must fail.'}]}))
  with self.assertRaises(ValueError):self.run_media()
 def test_F04_only_selected_domain_routes(self):
  with patch.object(m.socket,'gethostname',return_value='cumulus1'),patch.object(m,'ROOT',self.root),patch.object(m,'analyze',return_value='legacy') as old:
   (self.root/'config').mkdir();(self.root/'config/fleet-media.enabled').touch()
   p=dict(self.payload,action='analyze',domain='pedagogy');self.assertEqual(m.dispatch(p),'legacy');old.assert_called_once()
if __name__=='__main__':unittest.main()
