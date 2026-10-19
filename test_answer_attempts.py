import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import answer_workflow as aw
import answer_attempts as limits
from conversation_memory import Memory
from test_answer_workflow import FakeModels,output,SOURCE
from answer_workflow_cli import run

class LimitsTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name);self.m=Memory(self.root)
 def row(self,mid='one',**kw):
  return self.m.add(owner='intake',client='test',project='test',thread='thread',message_id=mid,
   question='When is the planning meeting?',meta={'request_origin':'original'},**kw)
 def test_persistent_ceiling_and_new_recovery_ids(self):
  row=self.row()
  for n in range(6):self.assertEqual(limits.reserve(self.m,row)['calls'],n+1)
  with self.assertRaises(ValueError):limits.reserve(Memory(self.root),self.row('replacement'))
 def test_elapsed_time_survives_restart(self):
  row=self.row()
  with patch.object(limits.time,'time',return_value=100):limits.reserve(self.m,row)
  with patch.object(limits.time,'time',return_value=700),self.assertRaises(ValueError):limits.reserve(Memory(self.root),row)
 def test_failed_calls_count_and_private_never_escalates(self):
  model=FakeModels([('ollama',RuntimeError('failure')),('ollama',output(False))])
  row=aw.compose(self.m,self.row(),model,SOURCE)
  self.assertEqual(row['state'],'needs_review');self.assertEqual(row['meta']['attempt_budget']['calls'],2)
  self.assertTrue(all(x[1]['pool']=='local' for x in model.calls))
  aw.compose(Memory(self.root),row,model,SOURCE);self.assertEqual(len(model.calls),2)
 def test_semantic_checks_cannot_be_replaced_with_confidence(self):
  data=json.loads(output());data['checks']['complete']=False;data['confidence']=1
  self.assertFalse(aw.reviewed(data,'question'))
  data['checks']['complete']=True;data['missing_request_quote']='Invented requirement'
  self.assertFalse(aw.reviewed(data,'question'))
 def test_undelivered_text_is_not_conversation_evidence(self):
  old=self.row();self.m.update(old['id'],state='needs_review',answer='Unsupported invention')
  model=FakeModels([('ollama',output()),('ollama',output())])
  aw.compose(self.m,self.row('two'),model,SOURCE)
  self.assertEqual(model.calls[0][0]['history'],[])
 def test_original_layout_quotes_are_traced(self):
  data=json.loads(output());data['evidence'][0]['quote']='The planning meeting is Thursday at 10 AM.'
  model=FakeModels([('ollama',json.dumps(data)),('ollama',json.dumps(data))])
  row=aw.compose(self.m,self.row(),model,{'note':'The planning meeting is\nThursday at 10 AM.\n'})
  self.assertEqual(row['state'],'ready');self.assertIn('source_spans',row['meta'])
 def test_desktop_entry_reuses_limits_without_exposing_answer(self):
  model=FakeModels([('ollama',output(False)),('ollama',output(False))])
  first=run(self.root,'stable','When is the meeting?',SOURCE,models=model)
  second=run(self.root,'stable','When is the meeting?',SOURCE,models=model)
  self.assertEqual(first,second);self.assertEqual(len(model.calls),2)
  self.assertNotIn('answer',first);self.assertEqual(first['state'],'needs_review')
 def test_public_coverage_scorer_handles_unicode_spaces_without_dropping_asks(self):
  import answer_workflow_eval as evaluation
  payload=dict(evaluation.fixtures())['coverage']
  data=json.loads(output());data['answer']='The meeting is Thursday at 10 AM in Room\u202f4. The owner is Lee.'
  data['evidence']=[{'source':'note','quote':payload['sources']['note']}]
  self.assertTrue(evaluation.score('coverage',json.dumps(data),payload))
  data['answer']='The meeting is Thursday at 10 AM in Room 4.'
  self.assertFalse(evaluation.score('coverage',json.dumps(data),payload))
 def test_expired_transport_never_opens_network(self):
  import llm_providers as lp
  with patch.object(lp._LAST,'request_deadline',1,create=True),patch.object(lp.urllib.request,'urlopen') as network:
   with self.assertRaises(lp.ProviderError):lp._http_post('https://example.test',{}, {})
   network.assert_not_called()
 def test_full_ladder_never_exceeds_six_calls(self):
  model=FakeModels([('ollama',output()),('ollama',output(False)),('ollama',output()),('ollama',output(False)),('openai',output()),('ollama',output())])
  row=aw.compose(self.m,self.row(privacy='CLOUD_ALLOWED'),model,SOURCE)
  self.assertEqual(row['state'],'ready');self.assertEqual(len(model.calls),6)
  with self.assertRaises(ValueError):limits.reserve(self.m,row)
 def test_astra_capacity_pin_requires_exact_authenticated_identity(self):
  import capability_health as health
  creds={'openai_model':'gpt-6-astra','openai_api_key':'synthetic'}
  good=health.observe_cloud(creds,'openai',get=lambda *a:{'data':[{'id':'gpt-6-astra'}]})
  self.assertTrue(good['healthy']);self.assertEqual(good['basis'],'authenticated_identity_and_published_capacity')
  bad=health.observe_cloud(creds,'openai',get=lambda *a:{'data':[{'id':'different'}]})
  self.assertFalse(bad['healthy'])
  bad=health.observe_cloud(dict(creds,openai_model='unknown'),'openai',get=lambda *a:{'data':[{'id':'unknown'}]})
  self.assertEqual(bad['status'],'capacity_unknown')
 def test_cloud_draft_gets_local_verification(self):
  model=FakeModels([('ollama',output(False)),('ollama',output(False)),('openai',output()),('ollama',output())])
  row=aw.compose(self.m,self.row(privacy='CLOUD_ALLOWED'),model,SOURCE)
  self.assertEqual(row['state'],'ready')
  self.assertEqual([x[1]['pool'] for x in model.calls],['local','local','cloud','local'])

if __name__=='__main__':unittest.main()
