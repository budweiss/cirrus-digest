"""Network-free boundary tests using the real shared provider API."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import llm_providers as lp
import llm_routing as routing


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pricing = self.root/'pricing.json'
        self.ledger = self.root/'spend.jsonl'
        self.pricing.write_text(json.dumps({'models': {'m': {'in': 1, 'out': 2}},
            'caps_usd': {'per_call': 1, 'per_session': 1, 'per_day': 1}}))
        self.creds = {'anthropic_api_key':'fixture', 'claude_model':'m',
            'gemini_api_key':'fixture', 'gemini_model':'m',
            'llm_budget': {'pricing_path': str(self.pricing), 'ledger_path':str(self.ledger)}}
        self.audit = self.root/'audit.jsonl'
        p=patch.object(routing, 'AUDIT_PATH', self.audit);p.start();self.addCleanup(p.stop)
        self.calls=[]
        def answer(c,s,u,n):
            self.calls.append((s,u));lp._LAST.model='m';return 'valid'
        p=patch.dict(lp._PROVIDERS, {'anthropic':answer,'gemini':answer,'ollama':answer,'vllm':answer})
        p.start();self.addCleanup(p.stop)

    def test_local_only_blocks_direct_cloud_even_with_explicit_weaker_argument(self):
        self.creds['llm_privacy']='LOCAL_ONLY'
        with self.assertRaises(lp.ProviderError):
            lp.call('anthropic','secret','private',self.creds,privacy='CLOUD_ALLOWED')
        self.assertEqual(self.calls,[])

    def test_local_failure_never_escapes_privacy_boundary(self):
        self.creds['ollama_url']='http://localhost:11434'
        with patch.dict(lp._PROVIDERS, {'ollama':lambda *a: (_ for _ in ()).throw(lp.ProviderError('offline'))}):
            with self.assertRaises(lp.ProviderError):
                lp.call_local_first('s','u',self.creds,privacy='LOCAL_ONLY',task='alopecia-agent')
        self.assertEqual(self.calls,[])

    def test_explicit_ollama_specialist_skips_general_vllm(self):
        c=dict(self.creds,ollama_url='http://localhost:11434',vllm_url='http://localhost:8000')
        seen=[]
        def specialist(creds,*args):
            seen.append(creds['ollama_model']);return 'valid'
        with patch.dict(lp._PROVIDERS, {'ollama':specialist,
                'vllm':lambda *a: self.fail('wrong general model')}):
            value,tier=lp.call_local_first('s','u',c,local_provider='ollama',
                local_model='specialist-fixture',privacy='LOCAL_ONLY')
        self.assertEqual((value,tier),('valid','ollama'))
        self.assertEqual(seen,['specialist-fixture'])

    def test_selected_provider_failure_does_not_substitute(self):
        c=dict(self.creds,ollama_url='http://localhost:11434',vllm_url='http://localhost:8000')
        with patch.dict(lp._PROVIDERS, {'vllm':lambda *a: (_ for _ in ()).throw(lp.ProviderError('offline'))}):
            with self.assertRaises(lp.ProviderError):
                lp.call_local_first('s','u',c,local_provider='vllm')
        self.assertFalse(self.calls)

    def test_invalid_or_missing_target_fails_closed(self):
        for kwargs in ({'local_provider':'typo'}, {'local_provider':'vllm','local_model':'ignored'},
                       {'local_provider':'ollama'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(lp.ProviderError):
                lp.call_local_first('s','u',self.creds,**kwargs)
        self.assertFalse(self.calls)

    def test_unknown_privacy_fails_closed(self):
        with self.assertRaises(lp.ProviderError):
            lp.call('ollama','s','u',self.creds,privacy='typo')
        self.assertFalse(self.calls)

    def test_valid_empty_collection_does_not_escalate(self):
        c=dict(self.creds,ollama_url='http://localhost:11434')
        value,tier=lp.call_local_first('s','u',c,parse=lambda _:[],task='alopecia-agent')
        self.assertEqual((value,tier),([],'ollama'))
        self.assertEqual(len(self.calls),1)

    def test_bad_local_output_uses_one_selected_cloud_provider(self):
        c=dict(self.creds,ollama_url='http://localhost:11434')
        with patch.dict(lp._PROVIDERS, {'ollama':lambda *a:'bad'}):
            value,tier=lp.call_local_first('s','u',c,parse=lambda s:s if s=='valid' else None,task='alopecia-agent')
        self.assertEqual(tier,'anthropic')
        self.assertEqual(len(self.calls),1)
        self.assertTrue(self.ledger.exists())
        self.assertIn('local_unavailable_or_rejected',self.audit.read_text())

    def test_unpriced_model_blocks_before_network(self):
        self.creds['claude_model']='not-priced'
        with self.assertRaises(lp.ProviderError):
            lp.call('anthropic','s','u',self.creds,task='halftime_catalogue')
        self.assertFalse(self.calls)

    def test_corrupt_spend_ledger_blocks_cloud(self):
        self.ledger.write_text('{broken')
        with self.assertRaises(lp.ProviderError):
            lp.call('anthropic','s','u',self.creds,task='halftime_routing')
        self.assertFalse(self.calls)

    def test_cap_blocks_cloud_but_not_local(self):
        self.creds['llm_budget']['per_call_usd']=0
        with self.assertRaises(lp.ProviderError):
            lp.call('anthropic','s','u',self.creds,task='halftime_routing')
        self.assertEqual(lp.call('ollama','s','u',self.creds,task='halftime_routing'),'valid')

    def test_explicit_order_cannot_bypass_pool(self):
        self.creds['grok_api_key']='fixture'
        with self.assertRaises(lp.ProviderError):
            lp.escalate('s','u',self.creds,task='halftime_catalogue',order=['grok'])
        self.assertFalse(self.calls)

    def test_paid_empty_reply_has_no_hidden_retry(self):
        def empty(*a):self.calls.append('attempt');return ''
        with patch.dict(lp._PROVIDERS,{'anthropic':empty}):
            self.assertEqual(lp.call('anthropic','s','u',self.creds,task='halftime_catalogue',retries=4),'')
        self.assertEqual(len(self.calls),1)

    def test_research_council_is_bounded(self):
        self.creds['kimi_api_key']='fixture'
        with patch.dict(lp._PROVIDERS,{'kimi':lambda *a: self.fail('third paid member')}):
            out=lp.escalate('s','u',self.creds,task='alopecia-agent',mode='council')
        self.assertEqual([p for p,_ in out],['anthropic','gemini'])

    def test_corrupt_policy_fails_closed_without_outputting_contents(self):
        p=self.root/'bad.json';p.write_text('{private content')
        with patch.object(routing,'POLICY_PATH',p):
            with self.assertRaisesRegex(lp.ProviderError,'policy unavailable'):
                lp.call('anthropic','s','u',self.creds)
        self.assertFalse(self.calls)

    def test_empty_policy_is_not_a_silent_disable(self):
        p=self.root/'bad.json';p.write_text('{}')
        with patch.object(routing,'POLICY_PATH',p):
            with self.assertRaises(lp.ProviderError):
                lp.call('anthropic','s','u',self.creds)
        self.assertFalse(self.calls)

    def test_blocked_call_clears_previous_model(self):
        lp._LAST.model='previous'
        with self.assertRaises(lp.ProviderError):
            lp.call('anthropic','s','u',self.creds,privacy='LOCAL_ONLY')
        self.assertIsNone(lp.last_model())

    def test_audit_contains_no_prompt(self):
        lp.call('anthropic','sensitive-system','sensitive-user',self.creds,task='halftime_catalogue')
        data=self.audit.read_text()
        self.assertNotIn('sensitive',data)
        self.assertNotIn('fixture',data)


if __name__=='__main__':
    unittest.main()
