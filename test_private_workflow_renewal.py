import json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import foundation_renewal as R
import foundation_renewal_auto as U
class PrivateWorkflowRenewalTests(unittest.TestCase):
 def test_private_requirements_survive_future_renewal_but_never_reach_provider(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td);system='Public system';route='foundation:business-idea-ideate'
   row={'case':'synthetic','system':system,'user':'PUBLIC_QUESTION','max_tokens':100,'provider':'anthropic','task':'business-idea-ideate','response':'prior','private_workflow_input':'PRIVATE_SENTINEL','private_workflow_version':'public-generation-private-review-v1'}
   evidence=p/'old.json';evidence.write_text(json.dumps([row]));h=R.sha(evidence.read_bytes())
   record={'approved':True,'id':'anthropic','model':'fixture-model','prompt_sha256':R.sha(system.encode()),'contract_sha256':'contract','evidence_files':{'old.json':h}}
   projects={route:{'enabled':True,'evaluations':[record]}}
   manifest,missing=R.build_manifest(projects,['anthropic'],{}, {h:evidence},{})
   self.assertEqual(missing,[]);self.assertEqual(len(manifest),1);self.assertEqual(manifest[0]['private_workflow_input'],'PRIVATE_SENTINEL');self.assertEqual(manifest[0]['user'],'PUBLIC_QUESTION')
   altered=dict(row,private_workflow_input='DIFFERENT_PRIVATE');self.assertNotEqual(R._body(row),R._body(altered))
   m=p/'manifest.json';m.write_text(json.dumps(manifest));reg=p/'registry.json';reg.write_text(json.dumps({'projects':projects}));creds=p/'credentials.json';creds.write_text('{}');out=p/'run-anthropic.json'
   def provider(provider,system,user,creds,**kwargs):self.assertEqual(user,'PUBLIC_QUESTION');self.assertNotIn('PRIVATE_SENTINEL',str((system,user,creds)));return json.dumps({'ideas':[{'demand_evidence':'model-added words'} for _ in range(4)]})
   with patch.object(R,'REGISTRY',reg),patch.object(R,'CREDS',creds),patch('llm_budget.resolve',return_value=({'caps_usd':{'per_session':2.5,'per_call':.5}},'fixture',str(p/'ledger'))),patch('capability_registry.contract_digest',return_value='contract'),patch('llm_providers.call',side_effect=provider),patch('llm_providers.last_model',return_value='fixture-model'),patch('llm_providers.last_finish_reason',return_value='stop'),patch('llm_providers._LAST',SimpleNamespace(usage={})),patch('capability_health.observe_cloud',return_value={'healthy':True,'model':'fixture-model'}),patch.object(R,'qualification_shape_valid',return_value=True),patch('llm_budget.session_spent',return_value=0),patch.object(R,'log'):
    R.run_provider(m,'anthropic',out,'synthetic')
   fresh=json.loads(out.read_text())[0];self.assertTrue(fresh['shape_valid']);self.assertTrue(fresh['format_normalized']);self.assertIn('model-added words',fresh['raw_response']);self.assertNotIn('model-added words',fresh['response']);self.assertEqual(fresh['private_workflow_input'],'PRIVATE_SENTINEL')
   item={'route':route,'field':'evaluations','index':0,'reviewed_fixtures':[fresh['fixture_id']],'evidence_file':out.name}
   prior={'route':route,'field':'evaluations','index':0,'fixture_id':fresh['fixture_id'],'record_sha256':fresh['old_record_sha256'],'prior':'prior answer'}
   (p/'review-priors.json').write_text(json.dumps([prior]));case=U.complete_cases(p,item,['--- case synthetic'])[0];body=json.loads(case.split('REVIEW EVIDENCE (JSON): ',1)[1]);self.assertEqual(body['INPUT'],'PRIVATE_SENTINEL');self.assertEqual(body['ACTUAL_PROVIDER_INPUT'],'PUBLIC_QUESTION')
 def test_malformed_private_fixture_refused(self):
  base={'case':'x','system':'s','user':'p','max_tokens':100,'private_workflow_input':'private'}
  self.assertIsNone(R._body(base));self.assertIsNone(R._body(dict(base,private_workflow_version='unknown')))
  self.assertIsNone(R._body(dict(base,private_workflow_input='',private_workflow_version='public-generation-private-review-v1')))
if __name__=='__main__':unittest.main()
