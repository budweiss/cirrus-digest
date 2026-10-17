import unittest
import tempfile
from pathlib import Path
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

    def test_non_dry_run_preserves_concern_for_future_generation(self):
        bad=dict(self.good(),name='Manual batch proposal',needs_building='Human QC per batch')
        with tempfile.TemporaryDirectory() as tmp:
            db=str(Path(tmp)/'ideas.db')
            with patch.object(B,'generate',return_value=[bad]),patch.object(B,'_relevance') as fit:
                result=B.run(only_lens=B.LENSES[0]['key'],db_path=db)
            fit.assert_not_called()
            self.assertFalse(result['admitted'])
            lessons=B.past_failures(db_path=db)
            self.assertIn('Manual batch proposal',lessons)
            self.assertIn('human work',lessons)

    def test_existing_candidate_state_is_preserved(self):
        bad=dict(self.good(),needs_building='Human QC per batch')
        with tempfile.TemporaryDirectory() as tmp:
            db=str(Path(tmp)/'ideas.db')
            B.entity_kb.upsert_entity(B.KB_PROJECT, 'existing', bad['name'],
                                     entity_type='business_idea', lead_state='qualified', db_path=db)
            with patch.object(B,'generate',return_value=[bad]), \
                 patch.object(B,'resolve_slug',return_value=('existing',False)), \
                 patch.object(B,'_relevance') as fit:
                result=B.run(only_lens=B.LENSES[0]['key'],db_path=db)
            fit.assert_not_called()
            self.assertFalse(result['admitted'])
            self.assertEqual(B.entity_kb.get_entity(B.KB_PROJECT,'existing',db_path=db)['lead_state'],'qualified')

if __name__=='__main__':unittest.main()
