import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from alopecia_agent import notebook as n, research as r
from test_alopecia_research import XML, QUOTE

class NotebookTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.state=Path(self.tmp.name)
        self.p=patch.object(r,'STATE',self.state);self.p.start();self.addCleanup(self.p.stop)
        self.node=copy.deepcopy(n.nodes()[0])
        r.save(self.state/'sources/pmid-123.json',r.parse_articles(XML)[0])
    def saved_step(self,ident='step-00001'):
        return {'step_id':ident,'avenue_id':self.node['id'],'path_id':'childhood','comparison':'Prior study differs in ascertainment, not proof of biological timing.','hypothesis':'An unproven age-pattern hypothesis.','next_step':'Compare independent diagnosis and onset definitions.','created':n.now()}
    def test_old_memory_is_searchable_and_paginated(self):
        ss=[dict(self.saved_step('step-%05d'%i),hypothesis='older unique marker' if i==1 else 'recent') for i in range(1,13)]
        r.save(self.state/'steps.json',ss)
        self.assertEqual(n.memory('unique marker')['steps'][0]['step_id'],'step-00001')
        self.assertEqual(n.memory()['next_offset'],10)
        self.assertEqual(len(n.memory(offset=10)['steps']),2)
    def test_duplicate_and_false_finality_refused(self):
        data=dict(self.node,id='')
        with self.assertRaisesRegex(ValueError,'duplicate'):n.update(data)
        with self.assertRaises(ValueError):n.update(dict(self.node,status='proven_false'))
        with self.assertRaises(ValueError):n.update(dict(self.node,reason=''))
    def test_pause_reopen_retains_history(self):
        n.update(dict(self.node,status='blocked',reason='Need accessible age-stratified data before comparing.'))
        n.update(dict(self.node,status='active',reason='New public cohort supplies a previously missing comparison.'))
        events=r.load(self.state/'avenue-events.json',[])
        self.assertEqual(events[1]['before']['status'],'blocked')
        self.assertEqual(events[1]['after']['status'],'active')
    def test_exhaustion_needs_distinct_queries_and_comparison(self):
        data=dict(self.node,status='exhausted_current_sources')
        with self.assertRaises(ValueError):n.update(data)
        r.save(self.state/'steps.json',[self.saved_step()])
        searches=[{'avenue_id':data['id'],'query':'same','status':'ok'}]*2
        r.save(self.state/'searches.json',searches)
        with self.assertRaises(ValueError):n.update(data)
        searches[1]=dict(searches[1],query='different');r.save(self.state/'searches.json',searches)
        self.assertEqual(n.update(data)['status'],'exhausted_current_sources')
    def test_search_rejects_arbitrary_egress_before_network(self):
        with patch.object(r,'fetch') as fetch:
            for concepts in [['patient Jane address'],['age','https://example.com'],[]]:
                with self.assertRaises(ValueError):n.search(self.node['id'],concepts)
            fetch.assert_not_called()
    def test_search_cache_and_novelty_are_actual(self):
        with patch.object(r,'fetch',side_effect=[b'{"esearchresult":{"idlist":["123"],"count":"1"}}',XML]) as fetch:
            found=n.search(self.node['id'],['age'])
            again=n.search(self.node['id'],['age'])
            self.assertEqual(fetch.call_count,2)
        self.assertEqual(found['search']['new_source_ids'],[])
        self.assertTrue(again['cached'])
        self.assertIn('alopecia areata',found['search']['query'])
    def test_failure_is_not_zero_hits(self):
        with patch.object(r,'fetch',side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):n.search(self.node['id'],['age'])
        entry=r.load(self.state/'searches.json',[])[0]
        self.assertEqual(entry['status'],'failed');self.assertNotIn('count',entry)
    def test_source_versions_survive_refresh(self):
        with patch.object(r,'fetch',return_value=XML):r.get_articles(['123'],{})
        with patch.object(r,'fetch',return_value=XML.replace(b'observational',b'controlled')):r.get_articles(['123'],{})
        self.assertEqual(len(list((self.state/'source-versions').glob('*.json'))),2)
    def test_medical_handoff_persists_before_call_and_links_result(self):
        def extract(question,ids):
            packets=list((self.state/'medical-packets').glob('*.json'))
            self.assertEqual(r.load(packets[0],{})['status'],'pending')
            self.assertEqual(ids,['pmid:123'])
            return {'claims':[],'abstain':True,'model':'fixture'}
        with patch.object(r,'extract_evidence',side_effect=extract):packet=n.handoff(self.node['id'],'What age was actually reported?',['pmid:123'])
        self.assertEqual(packet['status'],'returned')
        self.assertIn('pmid:123',packet['source_versions'])
        self.assertTrue((self.state/'progress.md').exists())
    def test_medical_failure_keeps_a_blocked_packet(self):
        with patch.object(r,'extract_evidence',side_effect=TimeoutError):packet=n.handoff(self.node['id'],'What age was actually reported?',['pmid:123'])
        self.assertEqual(packet['status'],'blocked');self.assertNotIn('result',packet)
        self.assertEqual(r.load(self.state/'handoffs.json',[])[0]['id'],packet['id'])
    def test_comparison_requires_real_previous_step_and_context(self):
        r.save(self.state/'steps.json',[self.saved_step()])
        d={'avenue_id':self.node['id'],'path_id':'childhood','comparison':'These designs cannot establish the same biological timing.','compare_to':['step-00001'],'supporting':[{'source_id':'pmid:123','quote':QUOTE}],'contradicting':[],
           'study_context':[dict(source_id='pmid:123',**{k:'unknown' for k in ['species','population','age_measure','design','temporality','cohort_key','limitations']})]}
        self.assertTrue(n.validate_comparison(d))
        with self.assertRaises(ValueError):n.validate_comparison(dict(d,compare_to=['invented']))
        with self.assertRaises(ValueError):n.validate_comparison(dict(d,study_context=[]))
    def test_focus_follows_unfinished_avenue(self):
        step=self.saved_step();step['avenue_id']='a-atopy-timing'
        r.save(self.state/'steps.json',[step])
        self.assertEqual(n.summary()['suggested_avenue'],'a-atopy-timing')
    def test_models_rotate_independently_and_cache(self):
        from alopecia_agent import tools,budget
        import llm_providers,llm_routing
        pool=['anthropic','gemini','kimi','openai','grok','deepseek']
        with patch.object(tools,'_load_creds',return_value={}),patch.object(budget,'allow',return_value=(True,1,'ok')),patch.object(llm_routing,'policy',return_value={'privacy':'CLOUD_ALLOWED','cloud_order':pool}),patch.object(llm_providers,'available',return_value=pool),patch.object(llm_providers,'call',return_value='UNIQUE MODEL ANSWER') as call:
            n.consult_models();n.consult_models();self.assertEqual(call.call_count,2)
            for _ in range(2):
                history=r.load(self.state/'model-reviews.json',[]);history[-1]['at']='2000-01-01T00:00:00+00:00';r.save(self.state/'model-reviews.json',history);n.consult_models()
            self.assertEqual({c.args[0] for c in call.call_args_list},set(pool))
            self.assertTrue(all('UNIQUE MODEL ANSWER' not in c.args[2] for c in call.call_args_list))
    def test_model_budget_blocks_before_cloud(self):
        from alopecia_agent import tools,budget
        import llm_providers
        with patch.object(tools,'_load_creds',return_value={}),patch.object(budget,'allow',return_value=(False,30,'monthly cap')),patch.object(llm_providers,'call') as call:
            self.assertEqual(n.consult_models()['status'],'budget_blocked');call.assert_not_called()

if __name__=='__main__':unittest.main()
