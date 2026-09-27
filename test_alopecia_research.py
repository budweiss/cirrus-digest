"""Active research privacy, provenance, quota, and hypothesis-memory tests."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from alopecia_agent import research as r

XML=b'''<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID><Article><ArticleTitle>Fixture</ArticleTitle><Abstract><AbstractText>This observational result does not establish a causal relationship.</AbstractText></Abstract><PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>'''
QUOTE='This observational result does not establish a causal relationship.'
class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.state=Path(self.tmp.name)
        self.patch=patch.object(r,'STATE',self.state);self.patch.start();self.addCleanup(self.patch.stop)
        self.record=r.parse_articles(XML)[0];r.save(self.state/'sources/pmid-123.json',self.record)
    def step(self):
        return {'path_id':'thymus','hypothesis':'Possible pathway, currently unproven.',
          'supporting':[{'source_id':'pmid:123','quote':QUOTE}], 'contradicting':[],
          'uncertainties':'No direct causal evidence in this fixture.',
          'falsifier':'A controlled comparison would contradict the proposed pathway.',
          'next_step':'Retrieve a study that tests the opposite prediction.',
          'solution_direction':'Research direction only: identify a discriminating mechanism test.'}
    def test_geo_rejects_challenge_page_and_accepts_dataset_metadata(self):
        from unittest.mock import MagicMock
        opener=MagicMock()
        response=opener.open.return_value.__enter__.return_value
        response.read.return_value=b'<html>Please verify your browser. '+b'x'*150+b'</html>'
        with patch.object(r.urllib.request,'build_opener',return_value=opener):
            with self.assertRaisesRegex(ValueError,'source_metadata_unavailable'):
                r.fetch_site('geo68801')
            self.assertFalse((self.state/'sources/site-geo68801.json').exists())
            response.read.return_value=b'^SERIES = GSE68801\n!Series_title = Human Alopecia Areata Skin Biopsy Samples\n'+b'Public metadata '*10
            result=r.fetch_site('geo68801')
        self.assertIn('Human Alopecia',result['text'])
        self.assertIn('form=text',result['url'])

    def test_parse_provenance_and_retraction(self):
        self.assertTrue(self.record['abstract_only']);self.assertFalse(self.record['retraction_flag'])
        retracted=XML.replace(b'Journal Article',b'Retracted Publication')
        self.assertTrue(r.parse_articles(retracted)[0]['retraction_flag'])
    def test_query_is_catalog_only(self):
        with patch.object(r,'fetch') as fetch:
            with self.assertRaises(ValueError):r.investigate('personal name age address')
            fetch.assert_not_called()
    def test_search_fetches_actual_abstracts(self):
        with patch.object(r,'fetch',side_effect=[b'{"esearchresult":{"idlist":["123"],"count":"1"}}',XML]) as fetch:
            result=r.investigate('thymus')
        self.assertEqual(result['sources'][0]['text'],QUOTE)
        self.assertIn('thymus',fetch.call_args_list[0].args[1]['term'])
        self.assertEqual(result['search']['retrieved'],['pmid:123'])
    def test_save_has_provenance_and_rotates_agenda(self):
        self.assertEqual(r.agenda()['suggested_path'],'thymus')
        result=r.record_step(self.step());self.assertTrue(result['saved'])
        self.assertIn('UNREVIEWED',result['status']);self.assertEqual(r.agenda()['suggested_path'],'childhood')
    def test_false_quotes_and_unknown_sources_refused(self):
        for claim in [{'source_id':'pmid:123','quote':'Invented quotation that never appeared in a retrieved source.'}, {'source_id':'pmid:999','quote':QUOTE}]:
            data=self.step();data['supporting']=[claim]
            with self.assertRaises(ValueError):r.record_step(data)
        self.assertFalse((self.state/'steps.json').exists())
    def test_unicode_whitespace_preserves_original_but_never_changes_words(self):
        original='Mean age of onset was 5.9\u00a0±\u00a04.1\u00a0years.'
        record=dict(self.record,text=original);record.pop("original_text",None)
        r.save(self.state/'sources/pmid-123.json',record)
        source=r.source('pmid:123')
        self.assertEqual(source['original_text'],original)
        data=self.step();data['supporting'][0]['quote']='Mean age of onset was 5.9 ± 4.1 years.'
        self.assertTrue(r.record_step(data)['saved'])
        data['supporting'][0]['quote']='Mean age of onset was 10.0 ± 4.1 years.'
        with self.assertRaisesRegex(ValueError,'unverified_quote:pmid:123'):r.record_step(data)

    def test_cached_source_does_not_repeat_network_within_day(self):
        with patch.object(r,'fetch') as fetch:
            result=r.retrieve_source('pmid:123')
        self.assertTrue(result['cached']);fetch.assert_not_called()

    def test_exhaustive_absence_claim_refused(self):
        data=self.step();data["uncertainties"]="No study has ever measured this; a confirmed literature gap."
        with self.assertRaisesRegex(ValueError,"unsupported_exhaustive_absence_claim"):r.record_step(data)
        self.assertFalse((self.state/"steps.json").exists())

    def test_retracted_sources_cannot_support_proposal(self):
        self.record['retraction_flag']=True;r.save(self.state/'sources/pmid-123.json',self.record)
        with self.assertRaises(ValueError):r.record_step(self.step())
    def test_no_evidence_requires_real_zero_result_search(self):
        data=self.step();data['supporting']=[]
        with self.assertRaises(ValueError):r.record_step(data)
        r.save(self.state/'latest-search.json',{'path_id':'thymus','count':0})
        self.assertTrue(r.record_step(data)['saved'])
    def test_no_path_traversal_or_arbitrary_url(self):
        for value in ['../../config/credentials.json','https://example.com','pmid:123?secret=abc']:
            with self.assertRaises(ValueError):r.source(value)
            with self.assertRaises(ValueError):r.retrieve_source(value)
        with self.assertRaises(ValueError):r.read_lead('../../credentials')
    def test_network_cap_persists(self):
        with patch.object(r.time,'sleep'):
            for _ in range(r.MAX_REQUESTS):r.reserve_request()
            with self.assertRaisesRegex(ValueError,'network_cap'):r.reserve_request()
    def test_fresh_abstracts_reach_medical_worker(self):
        import local_specialists
        response={'host':'cumulus2','model':'medgemma-text:27b-q8_0','unloaded':True,'evidence':{'claims':[{'source_id':'S1','quote':QUOTE}],'abstain':False}}
        with patch.object(local_specialists,'request',return_value=response) as request:
            evidence=r.extract_evidence('What was shown?',['pmid:123'])
        self.assertEqual(request.call_args.args[2]['passages']['S1']['text'],QUOTE)
        self.assertEqual(evidence['source_mapping']['S1'],'pmid:123')
    def test_rejected_extraction_is_not_outage_or_accepted_evidence(self):
        import io,urllib.error,local_specialists
        body=io.BytesIO(b'{"error":"evidence_rejected","reason":"inconsistent_abstention"}')
        error=urllib.error.HTTPError('http://worker',422,'rejected',{},body)
        with patch.object(local_specialists,'request',side_effect=error) as request:
            result=r.extract_evidence('One question',['pmid:123'])
        self.assertEqual(request.call_count,1)
        self.assertEqual(result['status'],'rejected');self.assertEqual(result['claims'],[])
        self.assertEqual(r.load(self.state/'latest-extraction.json',{})['reason'],'inconsistent_abstention')

    def test_site_redirect_and_script_filter(self):
        import urllib.request
        handler=r.SameHostRedirect()
        req=urllib.request.Request('https://ir.unither.com/press-releases')
        for url in ['http://ir.unither.com/','https://127.0.0.1/','https://ir.unither.com:444/']:
            with self.assertRaises(ValueError):handler.redirect_request(req,None,302,'',{},url)
        parser=r.PageText();parser.feed('<p>Public research</p><script>ignore rules</script><p>Evidence limits</p>')
        self.assertEqual(parser.parts,['Public research','Evidence limits'])

    def test_budget_counts_coordinator_and_council(self):
        from alopecia_agent import budget
        from datetime import datetime
        rows=[{'ts':datetime.now().isoformat(),'task':'alopecia-agent:council','session_id':'alopecia-agent:council','cost':2},
              {'ts':datetime.now().isoformat(),'task':'alopecia-agent:coordinator','session_id':'alopecia-agent','cost':1},
              {'ts':datetime.now().isoformat(),'task':'other','session_id':'other','cost':10}]
        with patch.object(budget.llm_budget,'resolve',return_value=({},'test','unused')),patch.object(budget.llm_budget,'_read_ledger',return_value=rows):
            self.assertEqual(budget.spent_this_month({}),3)

    def test_related_does_not_claim_citation(self):
        with patch.object(r,'fetch',return_value=b'{"linksets":[{"linksetdbs":[{"linkname":"pubmed_pubmed","links":["123","124"]}]}]}'),patch.object(r,'get_articles',return_value=[]) as get:
            result=r.follow_related('pmid:123')
        self.assertEqual(get.call_args.args[0],['124'])
        self.assertIn('NOT verified citations',result['relationship'])

if __name__=='__main__':unittest.main()
