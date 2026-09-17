import json,tempfile,time,hashlib,inspect
import unittest
from pathlib import Path
from unittest.mock import patch
from hoa_leads import hoa_monitor as h
import llm_providers as lp

class ScreeningTests(unittest.TestCase):
    def row(self,i=0,lead=False):return {'idx':i,'lead':lead,'type':'other','community':'Test HOA' if lead else '', 'why':'test'}
    def test_rejects_missing_duplicate_and_false_string(self):
        for rows in [[],[self.row(),self.row()], [dict(self.row(),lead='false')],[dict(self.row(),idx=True)]]:
            self.assertIsNone(h.parse_screening(json.dumps(rows),1))
        self.assertEqual(h.parse_screening(json.dumps([self.row()]),1),[self.row()])
    def test_local_empty_leads_is_success_not_paid_escalation(self):
        c=[{'source':'test','url':'https://example.com','text':'test'}]
        with patch.object(h,'_qualified_local_screen',return_value=[self.row()]),patch.object(h.ensemble,'best_answer') as council:
            self.assertEqual(h.council_filter(c,{}),[])
        council.assert_not_called()
    def test_local_rejection_uses_one_existing_council(self):
        c=[{'source':'test','url':'https://example.com','text':'test'}]
        with patch.object(h,'_qualified_local_screen',return_value=None),patch.object(h.ensemble,'best_answer',return_value=({},json.dumps([self.row()]))) as council:
            self.assertEqual(h.council_filter(c,{}),[])
        council.assert_called_once()
    def test_batches_keep_global_indexes_and_reported_model(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'config').mkdir()
            (root/'config/hoa_model_routing.json').write_text(json.dumps({'enabled':True,'evaluations':[]}))
            def dispatch(system,user,creds,**kwargs):
                self.assertEqual(kwargs['pool'],'local');self.assertEqual(kwargs['privacy'],'LOCAL_ONLY')
                self.assertEqual(kwargs['max_cost_usd'],0)
                self.assertEqual(creds['vllm_timeout'],120)
                self.assertEqual(creds['vllm_response_format']['type'],'json_schema')
                count=12 if '[11]' in user else 1
                return 'vllm',kwargs['parse'](json.dumps([self.row(i,True) for i in range(count)]))
            c=[{'source':'test','url':'https://example.com','text':'test'}]*13
            with patch.object(h,'DIGEST_DIR',root),patch('capability_health.observe',return_value={'healthy':True}),patch('capability_admission.dispatch_reviewed',side_effect=dispatch) as calls:
                rows=h._qualified_local_screen(c,{'vllm_model':'m'})
            self.assertEqual([r['idx'] for r in rows],list(range(13)))
            self.assertEqual(calls.call_count,2)

    def test_schema_reaches_vllm_transport_without_changing_other_requests(self):
        schema=h.screening_format(2)
        with patch.object(lp,'_openai_compatible',return_value='[]') as wire:
            lp._vllm({'vllm_url':'http://fixture','vllm_model':'m','vllm_response_format':schema},'s','u',8000)
            self.assertEqual(wire.call_args.kwargs['extra']['response_format'],schema)
            lp._vllm({'vllm_url':'http://fixture','vllm_model':'m'},'s','u',8000)
            self.assertNotIn('response_format',wire.call_args.kwargs['extra'])
