import copy,json,unittest
import business_idea_ideate as B
class DemandLiteralTests(unittest.TestCase):
 def test_only_owned_literal_changes(self):
  original={'ideas':[{'name':str(i),'demand_evidence':'Unsupported model assertion','what':'must remain exact','evidence_id':str(i)} for i in range(4)]}
  expected=copy.deepcopy(original)
  for r in expected['ideas']:r['demand_evidence']='Published paid offering; niche demand untested.'
  got=B.normalize_demand_evidence(json.dumps(original));self.assertEqual(json.loads(got),expected);self.assertEqual(original['ideas'][0]['demand_evidence'],'Unsupported model assertion');self.assertEqual(B.normalize_demand_evidence(got),got)
 def test_invalid_or_missing_fields_never_repaired(self):
  for raw in ('bad','{}','[]',json.dumps({'ideas':[{'name':'x'}]*4}),json.dumps({'ideas':[{'demand_evidence':'x'}]*3})):
   self.assertIsNone(B.normalize_demand_evidence(raw))
if __name__=='__main__':unittest.main()
