import json,unittest
from unittest.mock import patch
from types import SimpleNamespace
import business_idea_ideate as B
import foundation_renewal_auto as U
class PrivacyGateTests(unittest.TestCase):
 def test_cloud_receives_no_private_history_and_local_receives_full_requirements(self):
  public={'id':'1','url':'https://vendor.test/pricing','quote':'Published processing category.'}
  response=json.dumps({'ideas':[{'name':'Example '+str(i),'evidence_id':'1','demand_evidence':'Published paid offering; niche demand untested.'} for i in range(4)]})
  def call(system,user,*args,**kwargs):
   self.assertNotIn('PRIVATE_SENTINEL',user);self.assertEqual(kwargs['max_tokens'],16384);return 'fixture',response
  def local(rule,case,**kwargs):
   d=json.loads(case.split('REVIEW EVIDENCE (JSON): ',1)[1]);self.assertIn('PRIVATE_SENTINEL',d['INPUT']);self.assertNotIn('PRIVATE_SENTINEL',d['ACTUAL_PROVIDER_INPUT']);return {'verdict':'pass'}
  with patch.object(B,'demand_sources',return_value=[public]),patch.dict('sys.modules',{'ensemble':SimpleNamespace(best_answer=call)}),patch.object(U,'model_review',side_effect=local) as review:
   self.assertEqual(len(B.generate(B.LENSES[0],{},lessons='PRIVATE_SENTINEL',census='PRIVATE_SENTINEL')),4);self.assertEqual(review.call_count,2)
 def test_nonpass_or_exception_withholds(self):
  for value in ('fail','unsure',None):
   with patch.object(U,'model_review',return_value={'verdict':value}):self.assertFalse(B._private_requirement_gate('answer','private','public'))
  with patch.object(U,'model_review',side_effect=RuntimeError()):self.assertFalse(B._private_requirement_gate('answer','private','public'))
if __name__=='__main__':unittest.main()
