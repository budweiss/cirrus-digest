import unittest
from unittest.mock import patch
import environment_scan as scan

ROWS=[{'url':'https://amd.com/example','title':'Synthetic release','published':'2026-10-14','text':'A synthetic release supports a bounded 128 GB configuration.'}]
GOOD={'findings':[{'source_index':0,'quote':'supports a bounded 128 GB configuration.','summary':'A synthetic configuration needs review before use.','verdict':'DEFER'}]}

class EnvironmentChecks(unittest.TestCase):
    def test_article_fetch_flag_is_paywall_not_title(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        fetch=Mock(side_effect=[('Blocked excerpt '*40,True),('Verified public source '*40,False)])
        module=SimpleNamespace(search_web=lambda *a,**k:['https://amd.com/blocked','https://amd.com/allowed'],fetch_article_content=fetch)
        with patch.dict('sys.modules',{'cirrus_daily':module}),patch.object(scan,'FEEDS',{}),patch.object(scan,'QUERIES',('synthetic',)):
            rows,gaps=scan.gather()
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['url'],'https://amd.com/allowed')
        self.assertIsInstance(rows[0]['title'],str);self.assertEqual(fetch.call_args.kwargs['max_chars'],3500)

    def test_sources_are_allowlisted(self):
        self.assertTrue(scan.permitted('https://amd.com/product'))
        self.assertTrue(scan.permitted('https://huggingface.co/google/model'))
        self.assertFalse(scan.permitted('https://amd.com.evil.example/'))
        self.assertFalse(scan.permitted('https://huggingface.co/random/model'))
        self.assertFalse(scan.permitted('https://user:pass@amd.com/product'))

    def test_exact_evidence(self):
        import copy
        x=copy.deepcopy(GOOD);x['findings'][0]['quote']='Made up source text with enough characters.'
        with self.assertRaises(ValueError):scan.validate(x,ROWS)
        self.assertEqual(len(scan.validate(GOOD,ROWS)),1)

    def test_review_rejection_never_publishes(self):
        import json
        answers=iter([json.dumps(GOOD),'{"approved":false,"problems":["unsupported"]}']*2)
        with self.assertRaisesRegex(ValueError,'environment_review_failed'):
            scan.review(ROWS,'',lambda _:next(answers))

    def test_untrusted_indices_and_stale_plan_rejected(self):
        import copy
        for key,value in [('source_index',True),('source_index',1),('summary','Order in November')]:
            x=copy.deepcopy(GOOD);x['findings'][0][key]=value
            with self.assertRaises(ValueError):scan.validate(x,ROWS)

    def test_review_needs_explicit_approval(self):
        import json
        answers=iter([json.dumps(GOOD),'{"approved":true,"problems":[]}'])
        prompts=[]
        def call(prompt):prompts.append(prompt);return next(answers)
        self.assertEqual(scan.review(ROWS,'prior',call),GOOD['findings'])
        self.assertIn('no order date',prompts[0]);self.assertIn('separate official Apple',prompts[1])

if __name__=='__main__':unittest.main()
