"""End-to-end council boundary tests; real admission, selection and accounting."""
import hashlib
import json
import time
import unittest
from unittest.mock import patch
import ensemble
import llm_providers as L
from capability_admission import prompt_digest
from capability_registry import contract_digest
import test_llm_routing as fixtures


class FoundationTests(unittest.TestCase):
    setUp = fixtures.RoutingTests.setUp

    def route(self, **changes):
        p=self.root/'caller.py';p.write_text('contract = 1\n')
        route=dict(enabled=True, capability='research', max_cost_usd=.1,
                   max_user_bytes=4096, contract_files={'caller.py':hashlib.sha256(p.read_bytes()).hexdigest()},
                   evaluations=[])
        digest=contract_digest(route,self.root)
        self.health=[]
        for provider, quality in [('anthropic',.85),('gemini',.95),('kimi',.9)]:
            route['evaluations'].append(dict(id=provider,model='m',approved=True,
                task='pedagogy-topic',capability='research',prompt_sha256=prompt_digest('sys'),
                contract_sha256=digest,evidence_id='fixture',evaluated_at=time.time()-1,
                expires_at=time.time()+1000,quality=quality,location='cloud',usable_input_tokens=32000))
            self.health.append(dict(id=provider,model='m',location='cloud',healthy=True,
                checked_at=time.time(),usable_input_tokens=32000))
        route.update(changes)
        return route

    def invoke(self, route, **kwargs):
        with patch('capability_registry.foundation_route',return_value=route), \
             patch('capability_health.observe_cloud',side_effect=lambda c,p:next(h for h in self.health if h['id']==p)), \
             patch.object(L,'escalate',side_effect=AssertionError('legacy fallback forbidden')):
            return ensemble.best_answer('sys','PRIVATE PAYLOAD',self.creds,
                task='pedagogy-topic',max_tokens=100,app_dir=self.root,**kwargs)

    def test_default_single_best_specialist_not_provider_order(self):
        meta,text=self.invoke(self.route(),mode='council')
        self.assertEqual(meta['members'],['gemini']);self.assertEqual(text,'valid')
        self.assertEqual(len(self.calls),1)
        self.assertNotIn('PRIVATE',self.audit.read_text())
        self.assertIn('reviewed_quality_then_estimated_cost',self.audit.read_text())

    def test_missing_expired_evidence_never_uses_baseline(self):
        route=self.route()
        for r in route['evaluations']:r['expires_at']=0
        with self.assertRaises(L.ProviderError):self.invoke(route)
        self.assertFalse(self.calls)

    def test_private_work_never_probes_or_calls_cloud(self):
        route=self.route();self.creds['llm_privacy']='LOCAL_ONLY'
        with patch('capability_health.observe_cloud',side_effect=AssertionError('cloud metadata')):
            with self.assertRaises(L.ProviderError):self.invoke(route,privacy='CLOUD_ALLOWED')
        self.assertFalse(self.calls)

    def test_contract_change_blocks_before_inference(self):
        route=self.route();(self.root/'caller.py').write_text('contract = 2')
        with self.assertRaises(L.ProviderError):self.invoke(route)
        self.assertFalse(self.calls)

    def test_bounded_recovery_selects_next_qualified_peer(self):
        route=self.route(max_recovery_attempts=1)
        def failed(*a):self.calls.append('failed');raise L.ProviderError('PRIVATE ERROR')
        with patch.dict(L._PROVIDERS,{'gemini':failed}):
            meta,text=self.invoke(route)
        self.assertEqual(meta['members'],['anthropic']);self.assertTrue(meta['degraded'])
        self.assertEqual(len(self.calls),2)
        self.assertNotIn('PRIVATE',self.audit.read_text())

    def test_unknown_price_does_not_veto_other_qualified_candidates(self):
        route=self.route();self.creds.update(kimi_api_key='fixture',kimi_model='unpriced')
        route['evaluations'][2]['model']='unpriced';self.health[2]['model']='unpriced'
        self.assertEqual(self.invoke(route)[0]['members'],['gemini'])

    def test_kimi_can_win_when_qualified_and_keyed(self):
        route=self.route();route['evaluations'][2]['quality']=1
        self.creds.update(kimi_api_key='fixture',kimi_model='m')
        with patch.dict(L._PROVIDERS,{'kimi':L._PROVIDERS['gemini']}):
            self.assertEqual(self.invoke(route)[0]['members'],['kimi'])

    def test_two_reviewers_require_justification_and_synthesis_evidence(self):
        for fields in ({'reviewers':2},{'reviewers':2,'review_reason':'high stakes'}):
            with self.assertRaises(L.ProviderError):self.invoke(self.route(**fields))
        self.assertFalse(self.calls)

    def test_aggregate_budget_and_input_scope_block(self):
        for fields in ({'max_cost_usd':0},{'max_user_bytes':2}):
            with self.assertRaises(L.ProviderError):self.invoke(self.route(**fields))
        self.assertFalse(self.calls)

    def test_truncated_response_not_accepted(self):
        def truncated(*a):L._LAST.model='m';L._LAST.finish_reason='length';return 'unfinished'
        with patch.dict(L._PROVIDERS,{'gemini':truncated}):
            with self.assertRaises(L.ProviderError):self.invoke(self.route())
        self.assertIn('capability_output_truncated',self.audit.read_text())

    def test_two_reviewers_and_qualified_judge(self):
        route=self.route(reviewers=2,review_reason='reviewed high stakes')
        route['judge_evaluations']=[dict(r,capability='research:synthesis',prompt_sha256=prompt_digest(ensemble._JUDGE_SYSTEM)) for r in route['evaluations']]
        meta,text=self.invoke(route)
        self.assertEqual(meta['members'],['gemini','anthropic'])
        self.assertEqual(meta['judge'],'gemini');self.assertEqual(len(self.calls),3)

    def test_accounting_failure_never_spends_on_recovery(self):
        route=self.route(max_recovery_attempts=1)
        with patch('llm_budget.record_call',return_value=None):
            with self.assertRaises(L.AccountingError):self.invoke(route)
        self.assertEqual(len(self.calls),1)

    def test_recovery_cannot_exceed_route_aggregate_cap(self):
        route=self.route(max_recovery_attempts=1,max_cost_usd=.0015)
        def failed(*a):self.calls.append('failed');raise L.ProviderError('offline')
        with patch.dict(L._PROVIDERS,{'gemini':failed}):
            with self.assertRaises(L.ProviderError):self.invoke(route)
        self.assertEqual(len(self.calls),1)

    def test_approved_local_private_route_uses_only_local(self):
        route=self.route(privacy='LOCAL_ONLY')
        row=dict(route['evaluations'][0],id='vllm',location='local',quality=1)
        route['evaluations']=[row]
        self.creds.update(vllm_url='http://localhost:8000',vllm_model='m')
        state=dict(id='vllm',model='m',location='local',healthy=True,checked_at=time.time(),usable_input_tokens=32000)
        with patch('capability_health.observe',return_value=state):
            meta,_=self.invoke(route)
        self.assertEqual(meta['members'],['vllm']);self.assertEqual(len(self.calls),1)
        self.assertNotIn('PRIVATE',self.audit.read_text())

    def test_project_validator_rejects_before_acceptance(self):
        with self.assertRaises(L.ProviderError):self.invoke(self.route(),validate=lambda text:False)
        self.assertIn('capability_output_rejected',self.audit.read_text())
        self.assertNotIn('capability_output_accepted',self.audit.read_text())

    def test_research_planning_contract_rejects_malformed_and_oversized_plans(self):
        from research_task import _valid_decomposition, PLANNING_SUCCESS
        for text in ('{"subquestions":["x"],"success_looks_like":unquoted"}',
                     '{"subquestions":[1],"success_looks_like":"ok"}',
                     json.dumps({'subquestions':['x']*7,'success_looks_like':'ok'})):
            self.assertFalse(_valid_decomposition(text))
        self.assertTrue(_valid_decomposition(json.dumps({'subquestions':['Which sources?'],'success_looks_like':PLANNING_SUCCESS})))
        self.assertFalse(_valid_decomposition(json.dumps({'subquestions':['Which sources?'],'success_looks_like':'Prove no evidence exists'})))

    def test_post_call_audit_failure_never_spends_on_recovery(self):
        import llm_routing
        real=llm_routing.audit
        for failed_event in ('reply_received','capability_output_accepted'):
            self.calls.clear()
            def audit(task,provider,event,policy,**extra):
                if event==failed_event:raise OSError('disk unavailable')
                return real(task,provider,event,policy,**extra)
            with patch.object(llm_routing,'audit',side_effect=audit):
                with self.assertRaises(L.AccountingError):self.invoke(self.route(max_recovery_attempts=1))
            self.assertEqual(len(self.calls),1)

    def test_kimi_planning_schema_reaches_wire_without_global_mutation(self):
        from research_task import _planning_creds
        original=dict(kimi_api_key='fixture',kimi_model='kimi-k3')
        scoped=_planning_creds(original)
        with patch.object(L,'_http_post',return_value={'model':'kimi-k3','choices':[{'finish_reason':'stop','message':{'content':'{}'}}]}) as post:
            L._kimi(scoped,'s','u',1500)
        body=post.call_args.args[2]
        self.assertTrue(body['response_format']['json_schema']['strict'])
        self.assertEqual(body['max_tokens'],1500)
        self.assertEqual(body['reasoning_effort'],'low')
        self.assertNotIn('kimi_response_format',original)
        with self.assertRaises(L.ProviderError):L._kimi(dict(original,kimi_response_format={'type':'text'}),'s','u',1500)

    def test_judge_stays_pinned_when_actual_prompt_would_admit_better_peer(self):
        route=self.route(reviewers=2,review_reason='reviewed high stakes')
        route['judge_evaluations']=[dict(r,capability='research:synthesis',prompt_sha256=prompt_digest(ensemble._JUDGE_SYSTEM)) for r in route['evaluations']]
        actual=ensemble._judge_prompt('sys','PRIVATE PAYLOAD',[('gemini','valid'),('anthropic','valid')],'')
        self.health[1]['usable_input_tokens']=len(ensemble._JUDGE_SYSTEM.encode())+len(actual.encode())+1024+100+1
        meta,_=self.invoke(route)
        self.assertEqual(meta['judge'],'anthropic')
        self.assertEqual(len(self.calls),3)

    def test_research_caller_passes_scoped_schema_and_validator(self):
        import research_task as R
        raw=json.dumps({'subquestions':['Which primary sources?'],'success_looks_like':R.PLANNING_SUCCESS})
        creds={'kimi_model':'kimi-k3'}
        with patch.object(ensemble,'best_answer',return_value=({},raw)) as call:
            questions,success=R.decompose('A public question',['Use primary sources'],creds)
        self.assertEqual(success,R.PLANNING_SUCCESS)
        self.assertEqual(questions,['Which primary sources?'])
        self.assertEqual(call.call_args.kwargs['max_tokens'],1500)
        self.assertTrue(call.call_args.kwargs['validate'](raw))
        self.assertFalse(call.call_args.kwargs['validate']('{}'))
        self.assertIn(R.PLANNING_SUCCESS,call.call_args.args[0])
        self.assertEqual(call.call_args.args[2]['kimi_reasoning_effort'],'low')
        self.assertNotIn('kimi_reasoning_effort',creds)
