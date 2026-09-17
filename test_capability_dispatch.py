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
