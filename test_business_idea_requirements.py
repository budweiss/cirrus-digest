import unittest
from unittest.mock import patch
import business_idea_ideate as B

class RequirementTests(unittest.TestCase):
    def good(self):
        # A synthetic supplied statement, not evidence about a real business.
        # This gate does not certify it; existing scoring still must inspect it.
        return {'name':'CSV format converter','what':'Converts customer files between documented CSV layouts.',
                'who_pays':'Batch file processing customers','demand_evidence':'Supplied fixture source lists conversion at a price per file.',
                'autonomous_loop':'Software validates input and converts supported layouts; refuses unsupported files.',
                'needs_building':'Parser and billing; one-time prelaunch human testing.',
                'why_now':'Proposed design; feasibility needs testing.'}

    def test_good_proposal_and_one_time_testing_are_not_flagged(self):
        self.assertEqual(B.requirement_concerns(self.good()),[])
        self.assertEqual(B.requirement_concerns(dict(self.good(), needs_building='No human review per output. One-time prelaunch testing.')),[])

    def test_explicit_defects_and_missing_evidence_are_flagged(self):
        for change in [dict(demand_evidence='NONE'),dict(demand_evidence='UNVERIFIED HYPOTHESIS: customers might pay'),
                       dict(what='OCR of property deeds for title companies'),
                       dict(what='Daily news audio briefings'),dict(what='A compliance violation monitoring product'),
                       dict(needs_building='Human QC per batch before publishing'),
                       dict(needs_building='One-time prelaunch review plus human review of each document')]:
            with self.subTest(change=change):self.assertTrue(B.requirement_concerns(dict(self.good(),**change)))

    def test_flagged_idea_cannot_be_rescued_by_high_model_scores_but_job_continues(self):
        bad=dict(self.good(),name='Bad proposal',needs_building='Human review per output')
        with patch.object(B,'generate',return_value=[bad,self.good()]),patch.object(B,'past_failures',return_value=''), \
             patch.object(B,'shape_census',return_value=''),patch.object(B,'_relevance',return_value=(10,'test','test')) as fit, \
             patch.object(B,'critique',return_value=(10,'test')) as critique:
            result=B.run(only_lens=B.LENSES[0]['key'],dry_run=True)
        self.assertEqual(result['generated'],2)
        self.assertEqual(len(result['needs_review']),1)
        self.assertEqual(len(result['admitted']),1)
        self.assertEqual(fit.call_count,1);self.assertEqual(critique.call_count,1)
        self.assertNotIn('Bad proposal',fit.call_args.args[0])

if __name__=='__main__':unittest.main()
