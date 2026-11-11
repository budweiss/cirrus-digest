"""S609: actual adapters preserve replies and measure each attempt, without live IO."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import llm_budget as B
import llm_providers as L
import media_pipeline as M


class TelemetryTests(unittest.TestCase):
    def test_request_milestones_survive_recovery_and_metadata_updates(self):
        from conversation_memory import Memory
        with tempfile.TemporaryDirectory() as td:
            m=Memory(td)
            with patch('conversation_memory.time.time',return_value=100):
                row=m.add(owner='test',client='test',project='test',thread='test',message_id='1',question='synthetic')
            with patch('conversation_memory.time.time',return_value=105):m.claim('test')
            with patch('conversation_memory.time.time',return_value=110):m.update(row['id'],state='ready')
            with patch('conversation_memory.time.time',return_value=120):m.update(row['id'],meta={'note':'synthetic'})
            self.assertEqual(m.get(row['id'])['meta']['timing'],{'started_at':105,'ready_at':110})
            self.assertFalse(m.transition(row['id'],'queued','delivered'))
            with patch('conversation_memory.time.time',return_value=125):
                self.assertTrue(m.transition(row['id'],'ready','delivered'))
            self.assertEqual(m.get(row['id'])['meta']['timing']['delivered_at'],125)

    def test_old_ready_request_is_not_backfilled(self):
        from conversation_memory import Memory
        with tempfile.TemporaryDirectory() as td:
            m=Memory(td);row=m.add(owner='t',client='t',project='t',thread='t',message_id='old',question='synthetic',state='ready')
            m.update(row['id'],state='ready',meta={'note':'later edit'})
            self.assertNotIn('timing',m.get(row['id'])['meta'])

    def test_native_metrics_allowlist_units_and_missing(self):
        value = B.response_metrics({'eval_duration':2_000_000_000,
            'prompt_eval_duration':500_000_000,'load_duration':0,'total_duration':3_000_000_000,
            'done_reason':'stop','message':{'content':'PRIVATE'},'credential':'SECRET'}, 3.2)
        self.assertEqual(value,dict(eval_seconds=2,prompt_eval_seconds=.5,load_seconds=0,
            server_seconds=3,wall_seconds=3.2,finish_reason='stop'))
        self.assertEqual(B.response_metrics({'eval_duration':float('nan'),'load_duration':-1,
            'total_duration':True,'done_reason':'PRIVATE'}),{})
        self.assertEqual(B.response_metrics({}),{})

    def test_record_metrics_are_finite_and_content_free(self):
        cfg={'models':{},'discounts':{},'unknown_model_out_per_m':25}
        with tempfile.TemporaryDirectory() as td, patch.object(B,'resolve',return_value=(cfg,'selftest',td+'/ledger')):
            row=B.record_call({},'ollama','synthetic',0,0,in_tok=10,out_tok=2,
                load_seconds=0,server_seconds=2,wall_seconds=float('inf'),eval_seconds=True,
                finish_reason='length')
            self.assertEqual(row['load_seconds'],0)
            self.assertEqual(row['server_seconds'],2)
            self.assertEqual(row['finish_reason'],'length')
            self.assertEqual(row['telemetry_version'],2)
            self.assertNotIn('wall_seconds',row)
            self.assertNotIn('eval_seconds',row)

    def test_all_provider_attempts_get_independent_timing(self):
        rows=[]
        def adapter(*args):
            L._LAST.usage={'input':10,'output':1}
            L._LAST.finish_reason='length' if not rows else 'stop'
            return '' if not rows else 'answer'
        def capture(*args):
            rows.append((L._LAST.wall_seconds,L.last_finish_reason()))
            return {}
        with patch('llm_routing.authorize',return_value={}),patch.dict(L._PROVIDERS,anthropic=adapter), \
             patch.object(L,'_record',side_effect=capture),patch.object(L.time,'monotonic',side_effect=[1,2,5,10,14]):
            self.assertEqual(L.call('anthropic','system','user',{},retries=1),'answer')
        self.assertEqual(rows,[(3,'length'),(4,'stop')])

    def test_transport_failure_does_not_invent_usage(self):
        with patch('llm_routing.authorize',return_value={}),patch.dict(L._PROVIDERS,anthropic=Mock(side_effect=L.ProviderError('failed'))), \
             patch.object(L,'_record') as rec:
            with self.assertRaises(L.ProviderError):L.call('anthropic','system','user',{},retries=0)
            rec.assert_not_called()

    def test_media_transport_measures_response_without_changing_wire(self):
        payload={'model':M.MODEL,'messages':[{'role':'user','content':'synthetic'}]}
        reply={'model':M.MODEL,'choices':[{'message':{'content':'answer'},'finish_reason':'stop'}]}
        opener=Mock();opener.open.return_value=io.BytesIO(json.dumps(reply).encode())
        with patch.object(M.urllib.request,'build_opener',return_value=opener),patch.object(M.time,'monotonic',side_effect=[2,4.5]):
            result=M.request('/v1/chat/completions',payload)
        self.assertEqual(result['_cowork_wall_seconds'],2.5)
        self.assertEqual(json.loads(opener.open.call_args[0][0].data),payload)
        result.pop('_cowork_wall_seconds');self.assertEqual(result,reply)

    def test_media_retries_record_each_attempt_not_sleep(self):
        replies=[{'model':M.MODEL,'usage':{'prompt_tokens':10,'completion_tokens':3},
                  '_cowork_wall_seconds':x,'choices':[{'message':{'content':'answer'},'finish_reason':finish}]}
                 for x,finish in [(2,'length'),(3,'stop')]]
        with patch.object(M,'token_count',return_value=10),patch.object(M,'request',side_effect=replies), \
             patch.object(M.time,'sleep'),patch.object(B,'record_call') as rec:
            self.assertEqual(M.complete('system','synthetic','telemetry-selftest',attempts=2),'answer')
        self.assertEqual([c.kwargs['wall_seconds'] for c in rec.call_args_list],[2,3])
        self.assertEqual([c.kwargs['finish_reason'] for c in rec.call_args_list],['length','stop'])

    def test_structured_review_preserves_accounting(self):
        import stock_research as s
        reply={'model':M.MODEL,'usage':{'prompt_tokens':10,'completion_tokens':5},'_cowork_wall_seconds':4,
               'choices':[{'message':{'content':'{"passed":true,"issues":[]}'},'finish_reason':'stop'}]}
        with patch.object(M,'token_count',return_value=10),patch.object(M,'request',return_value=reply),patch.object(B,'record_call') as rec:
            self.assertEqual(s.structured_review('system','synthetic','telemetry-selftest'),' {"passed":true,"issues":[]}'.strip())
        self.assertEqual(rec.call_args.kwargs['wall_seconds'],4)
        self.assertEqual(rec.call_args.kwargs['out_tok'],5)

    def test_native_personal_caller_measures_decode(self):
        import personal_schedule as p
        reply={'model':'gpt-oss:120b','done':True,'done_reason':'stop','message':{'content':'synthetic'},
               'prompt_eval_count':10,'eval_count':2,'eval_duration':1_000_000_000}
        with patch.object(M,'token_count',return_value=10),patch.object(p.urllib.request,'urlopen',return_value=io.BytesIO(json.dumps(reply).encode())), \
             patch.object(p.time,'monotonic',side_effect=[1,4]),patch.object(B,'record_call') as rec:
            self.assertEqual(p.c1_draft('system','synthetic'),'synthetic')
        self.assertEqual(rec.call_args.kwargs['wall_seconds'],3)
        self.assertEqual(rec.call_args.kwargs['eval_seconds'],1)
        self.assertEqual(rec.call_args.kwargs['num_ctx'],65536)

    def test_context_alignment_only_changes_gpt_oss_allocation(self):
        import personal_schedule as p
        for model,ctx in [('gpt-oss:120b',65536),('qwen3.8:27b',32768)]:
            reply={'model':model,'done':True,'done_reason':'stop','message':{'content':json.dumps(
                {'verdict':'invalid','draft_contradicts_evidence':False})}}
            with patch.object(M,'token_count',return_value=10),patch.object(p.urllib.request,'urlopen',return_value=io.BytesIO(json.dumps(reply).encode())) as wire,patch.object(B,'record_call'):
                p.arbiter_call(model,'medium',{},'synthetic','synthetic',10)
            body=json.loads(wire.call_args[0][0].data)
            self.assertEqual(body['options'],{'temperature':0,'num_ctx':ctx,'num_predict':4096})
        with patch.object(M,'token_count',return_value=30000),patch.object(p.urllib.request,'urlopen') as wire:
            with self.assertRaisesRegex(ValueError,'context_budget'):p.c1_draft('system','synthetic')
            wire.assert_not_called()

if __name__=='__main__':unittest.main()
