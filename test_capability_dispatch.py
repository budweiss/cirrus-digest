import time
from unittest.mock import patch
import unittest
import test_llm_routing as fixtures
import llm_providers as lp
from capability_dispatch import dispatch


class DispatchTests(unittest.TestCase):
    setUp = fixtures.RoutingTests.setUp
    def records(self):
        now=time.time()
        return [dict(id=p, model='m', enabled=True, healthy=True, health_checked_at=now,
                     location='cloud', usable_input_tokens=8192,
                     capabilities={'extract':dict(validated=True,model='m',quality=q)})
                for p,q in [('anthropic',.7),('gemini',.95)]]

    def invoke(self, **kwargs):
        args=dict(candidates=self.records(),capability='extract',task='halftime_catalogue',
                  max_cost_usd=.1,pool='cloud',min_quality=.9)
        args.update(kwargs)
        return dispatch('s','u',self.creds,**args)

    def test_selects_capability_not_first_provider(self):
        provider,reply=self.invoke()
        self.assertEqual((provider,reply),('gemini','valid'))
        self.assertEqual(len(self.calls),1)
        self.assertTrue(self.ledger.exists())

    def test_inherited_privacy_blocks_cloud(self):
        self.creds['llm_privacy']='LOCAL_ONLY'
        with self.assertRaises(lp.ProviderError):self.invoke(privacy='CLOUD_ALLOWED')
        self.assertFalse(self.calls)

    def test_budget_and_model_mismatch_block_before_request(self):
        self.creds['llm_budget']['per_call_usd']=0
        with self.assertRaises(lp.ProviderError):self.invoke()
        self.assertFalse(self.calls)
        self.creds['gemini_model']='changed'
        with self.assertRaises(lp.ProviderError):self.invoke()
        self.assertFalse(self.calls)

    def test_missing_governed_task_is_rejected(self):
        with self.assertRaises(lp.ProviderError):self.invoke(task='unmapped')
        self.assertFalse(self.calls)

    def test_selected_failure_never_tries_another_provider(self):
        with patch.dict(lp._PROVIDERS, {'gemini':lambda *a: (_ for _ in ()).throw(lp.ProviderError('offline'))}):
            with self.assertRaises(lp.ProviderError):self.invoke()
        self.assertFalse(self.calls)

    def test_caller_cannot_understate_estimated_cost(self):
        rows=self.records()
        for row in rows:row['estimated_request_cost_usd']=0
        with self.assertRaises(lp.ProviderError):self.invoke(candidates=rows,max_cost_usd=0)
        self.assertFalse(self.calls)

    def test_parser_rejection_is_audited_without_private_text(self):
        def reject(reply):raise ValueError('PRIVATE-PARSER-DATA')
        with self.assertRaises(lp.ProviderError):self.invoke(parse=reject)
        audit=self.audit.read_text()
        self.assertIn('capability_output_rejected',audit)
        self.assertNotIn('PRIVATE-PARSER-DATA',audit)

    def test_empty_parsed_collection_is_valid_and_audited(self):
        self.assertEqual(self.invoke(parse=lambda reply:[]),('gemini',[]))
        self.assertIn('capability_output_accepted',self.audit.read_text())

    def test_actual_model_mismatch_blocks_result(self):
        def changed(*args):lp._LAST.model='unexpected';return 'valid'
        with patch.dict(lp._PROVIDERS, {'gemini':changed}):
            with self.assertRaises(lp.ProviderError):self.invoke()
        self.assertIn('capability_model_mismatch',self.audit.read_text())

    def test_cheaper_qualified_model_fits_budget_when_best_does_not(self):
        import json
        self.pricing.write_text(json.dumps({'models':{'m':{'in':1,'out':2},'costly':{'in':1000,'out':2000}},'caps_usd':{'per_call':.1,'per_session':1,'per_day':1}}))
        self.creds['gemini_model']='costly'
        rows=self.records();rows[1]['model']='costly'
        rows[1]['capabilities']['extract']['model']='costly'
        provider,_=self.invoke(candidates=rows,min_quality=.5)
        self.assertEqual(provider,'anthropic')

    def test_equal_quality_chooses_lower_cost(self):
        import json
        self.pricing.write_text(json.dumps({'models':{'m':{'in':1,'out':2},'cheap':{'in':.1,'out':.2}},'caps_usd':{'per_call':1,'per_session':1,'per_day':1}}))
        self.creds['gemini_model']='cheap'
        rows=self.records();rows[0]['capabilities']['extract']['quality']=.95
        rows[1]['model']='cheap';rows[1]['capabilities']['extract']['model']='cheap'
        def answer(*a):lp._LAST.model='cheap';return 'valid'
        with patch.dict(lp._PROVIDERS,{'gemini':answer}):
            self.assertEqual(self.invoke(candidates=rows)[0],'gemini')

    def test_other_provider_effort_floor_does_not_eliminate_valid_local(self):
        self.creds.update(anthropic_effort='high',vllm_url='http://localhost:8000',vllm_model='m')
        rows=self.records()
        local=dict(rows[1],id='vllm',location='local',usable_input_tokens=3000)
        provider,_=self.invoke(candidates=rows+[local],pool='auto',privacy='LOCAL_ONLY',max_cost_usd=0)
        self.assertEqual(provider,'vllm')
