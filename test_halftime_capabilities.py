import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import halftime_catalogue as hc
import llm_providers as lp
import capability_admission as admission
import capability_health as health

class CatalogueIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'records.json'
        p=patch.object(hc,'CAPABILITY_RECORDS',self.path);p.start();self.addCleanup(p.stop)
        self.creds={'vllm_url':'http://fixture','vllm_model':'m'}
    def test_absent_or_unmigrated_pool_preserves_local_call(self):
        for doc in (None,{'version':1,'pools':{'program':[]}}):
            if doc:self.path.write_text(json.dumps(doc))
            with patch.object(lp,'call',return_value='[]') as call,patch.object(health,'observe') as observe:
                self.assertEqual(hc.extract_acts('fixture',self.creds)[0],[])
            self.assertEqual(call.call_args.args[0],'vllm');observe.assert_not_called()
    def test_outside_qualified_input_scope_preserves_original_vllm(self):
        self.path.write_text(json.dumps({'version':1,'pools':{'variety':[]},'max_user_bytes':{'variety':1}}))
        with patch.object(lp,'call',return_value='[]') as call,patch.object(health,'observe') as observe:
            self.assertEqual(hc.extract_acts('longer than qualified scope',self.creds)[0],[])
        self.assertEqual(call.call_args.args[0],'vllm')
        observe.assert_not_called()

    def test_enabled_pool_uses_reviewed_dispatch_without_duplicate_call(self):
        self.path.write_text(json.dumps({'version':1,'pools':{'variety':[{'id':'vllm'},{'id':'anthropic'}]}}))
        with patch.object(admission,'dispatch_reviewed',return_value=('vllm',[])) as dispatch,patch.object(health,'observe',return_value={'id':'vllm'}) as observe,patch.object(lp,'call') as call:
            acts,model,escalated=hc.extract_acts('fixture',self.creds)
        self.assertEqual(acts,[]);self.assertFalse(escalated);call.assert_not_called()
        args=dispatch.call_args.kwargs
        self.assertEqual(args['evaluations'],[{'id':'vllm'}]);self.assertEqual(args['privacy'],'LOCAL_ONLY')
        self.assertEqual(args['max_tokens'],hc.LOCAL_EXTRACT_MAX_TOKENS)
        self.assertEqual(args['max_cost_usd'],0);self.assertEqual(len(args['contract_sha256']),64)
        self.assertEqual(args['parse']('[]'),[])
    def test_rejected_reviewed_path_keeps_cold_ollama_fallback(self):
        self.path.write_text(json.dumps({'version':1,'pools':{'variety':[]}}))
        with patch.object(admission,'dispatch_reviewed',side_effect=lp.ProviderError('not eligible')),patch.object(health,'observe',return_value={}),patch.object(lp,'call',return_value='[]') as call,patch.object(lp,'escalate') as cloud:
            stats={};self.assertEqual(hc.extract_acts('fixture',self.creds,stats=stats)[0],[])
        self.assertEqual(call.call_args.args[0],'ollama');cloud.assert_not_called()
        self.assertEqual(stats['vllm_fallback'],1)

if __name__=='__main__':unittest.main()
