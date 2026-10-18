import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import business_idea_ideate as B
import business_idea_feeds as F

class GroundingTests(unittest.TestCase):
    def test_generation_requires_live_source_and_unchanged_evidence(self):
        src={'id':'1','url':'https://vendor.test/pricing','quote':'Batch conversion costs $2 per GB including input and output bytes.'}
        good={'name':'Batch converter','evidence_id':'1','evidence_quote':'fabricated model quote','demand_evidence':'fabricated claim','why_now':'Stop if fewer than half the buyers report unusable output.'}
        bad=dict(good,name='Unknown source',evidence_id='99')
        call=SimpleNamespace(best_answer=lambda *a,**k: ('fixture',json.dumps({'ideas':[good,bad,dict(good,name='Unsupported API',autonomous_loop='Apply arbitrary vector deltas through Transloadit.')]})))
        with patch.object(B,'demand_sources',return_value=[src]),patch.dict('sys.modules',{'ensemble':call}):
            rows=B.generate(B.LENSES[0],{})
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['evidence_quote'],src['quote'])
        self.assertNotIn('fabricated',rows[0]['demand_evidence'])
        self.assertNotIn('fewer than half',rows[0]['why_now'])
        self.assertIn('at least 2 buyers pay for a second job',rows[0]['why_now'])
        self.assertIn('Otherwise stop and revise',rows[0]['why_now'])
        with patch.object(B,'demand_sources',return_value=[]),patch.dict('sys.modules',{'ensemble':SimpleNamespace(best_answer=lambda *a,**k:self.fail('No source must not call model'))}):
            self.assertEqual(B.generate(B.LENSES[0],{}),[])

    def test_cards_fail_closed_when_official_page_shape_changes(self):
        self.assertEqual(B.evidence_cards([{'url':B._DEMAND_SOURCES[0],'text':'A changed or unavailable page'}]),[])
        text='By default, conversions consume at least one credit, with actual test content.'
        cards=B.evidence_cards([{'url':B._DEMAND_SOURCES[0],'text':text}])
        self.assertEqual(cards[0]['quote'],text)
        self.assertEqual(cards[0]['url'],B._DEMAND_SOURCES[0])

    def test_source_identity_and_quote_reach_both_scoring_inputs(self):
        text=B._idea_as_text({'evidence_url':'https://vendor.test/pricing','evidence_quote':'Exact published commercial offer.'})
        self.assertIn('https://vendor.test/pricing',text)
        self.assertIn('Exact published commercial offer.',text)

if __name__=='__main__':unittest.main()
