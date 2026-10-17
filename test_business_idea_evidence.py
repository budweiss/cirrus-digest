import unittest
from unittest.mock import patch
import business_idea_ideate as B

class DemandEvidenceTests(unittest.TestCase):
    def test_demand_statement_is_preserved_without_promotion(self):
        for statement in ('NONE', 'UNVERIFIED HYPOTHESIS: a proposed niche', 'Dated source: paid offering only; sales unknown'):
            text=B._idea_as_text({'what':'Synthetic proposal','demand_evidence':statement})
            self.assertIn('Demand evidence: '+statement+'\n',text)
        self.assertIn('Demand evidence: NONE\n',B._idea_as_text({}))

    def test_both_scoring_stages_receive_evidence(self):
        statement='Supplied fixture source: listed price per converted file'
        with patch.object(B,'generate',return_value=[{'name':'Synthetic candidate','what':'Synthetic proposal','demand_evidence':statement}]), \
             patch.object(B,'past_failures',return_value=''),patch.object(B,'shape_census',return_value=''), \
             patch.object(B,'_relevance',return_value=(10,'synthetic','synthetic')) as relevance, \
             patch.object(B,'critique',return_value=(10,'synthetic')) as critique:
            B.run(only_lens=B.LENSES[0]['key'],dry_run=True)
        for call in (relevance,critique):
            self.assertEqual(call.call_count,1)
            self.assertIn('Demand evidence: '+statement,call.call_args.args[1])

if __name__=='__main__':unittest.main()
