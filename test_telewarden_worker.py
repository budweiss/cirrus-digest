"""Offline safety/ownership regression tests; never touch live cases or models."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import telewarden as tw
import telewarden_worker as w


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, TELEWARDEN_DIR=self.tmp.name)
        self.env.start(); self.addCleanup(self.env.stop)
        self.case = {'id':'test-case', 'status':'new', 'history':[], 'first':0,
                     'job':'telegram_outbox', 'private':False}
        tw._save('cases.json', {'test-case': self.case})

    def test_claim_is_exclusive_against_desktop(self):
        w.claim('test-case', 'server')
        with self.assertRaises(w.Held): w.claim('test-case', 'second')
        with self.assertRaises(SystemExit): tw.set_status('test-case', 'fixing', 'desktop')
        with self.assertRaises(SystemExit): tw.set_status('test-case', 'accepted', 'desktop')
        w.finish('test-case', 'server', 'explained', 'test')
        self.assertNotIn('worker_lease', tw._load('cases.json', {})['test-case'])

    def test_enabled_marker_blocks_desktop_claim(self):
        (Path(self.tmp.name)/'server-worker-enabled').touch()
        with self.assertRaises(SystemExit): tw.set_status('test-case','fixing','desktop')

    def test_private_rejected_before_credentials(self):
        self.case['private'] = True
        with self.assertRaisesRegex(w.Held, 'protected'): w.guarded_packet(self.case,'a.py','')

    def test_mixed_digest_private_job_rejected(self):
        self.case['sample'] = 'stockscorecard failed'
        with self.assertRaisesRegex(w.Held, 'protected'): w.guarded_packet(self.case,'a.py','')

    def test_small_patch_and_assertions_preserved(self):
        src='def f(x):\n    return x - 1\ndef selftest():\n    assert f(1) == 0\n'
        p={'edits':[{'find':'return x - 1','replace':'return x + 1'}]}
        self.assertIn('return x + 1',w.apply_patch(src,p))
        p['edits']=[{'find':'assert f(1) == 0','replace':'assert True'}]
        with self.assertRaisesRegex(w.Held,'existing_tests'):w.apply_patch(src,p)

    def test_new_import_and_exec_blocked(self):
        src='def f(x):\n    return x\n'
        for replacement in ['import os\n'+src,'def f(x):\n    return eval(x)\n']:
            with self.assertRaises(w.Held):w.apply_patch(src,{'edits':[{'find':src,'replace':replacement}]})

    def test_ambiguous_and_noop_patch_blocked(self):
        for src,old,new in [('a=1\nb=1\n','1','2'),('a=1\n','a=1','a=1')]:
            with self.assertRaises(w.Held):w.apply_patch(src,{'edits':[{'find':old,'replace':new}]})

    def test_deploy_refuses_pins_drift_or_changed_checkout(self):
        spec={'base':'a','file':'x.py'}
        good={'head':'a','dirty':[],'staged':[],'pins':[],'drift':[]}
        w.assert_deployable(spec,[good,good])
        for k,v in [('head','b'),('dirty',['x.py']),('staged',['z.py']),('pins',['x.py']),('drift',['y.py'])]:
            with self.assertRaises(w.Held):w.assert_deployable(spec,[good,dict(good,**{k:v})])

    def test_cloud_budget_preflight_is_cumulative(self):
        import llm_budget as b
        with patch.object(b,'resolve',return_value=({'caps_usd':{}},'cirrus','unused')), \
             patch.object(b,'cost_usd',return_value=2.0), patch.object(b,'allow',return_value=(True,'')):
            w.reserve_cloud({'claude_model':'test'},'s','p',10)
            w.reserve_cloud({'claude_model':'test'},'s','p',10)
            with self.assertRaisesRegex(w.Held,'daily_budget'):w.reserve_cloud({'claude_model':'test'},'s','p',10)

    def test_local_failure_precedes_cloud_attempt(self):
        p=Path(self.tmp.name);(p/'telegram_outbox.py').write_text('x=1\n')
        routes=[]
        def failure(prompt,route,phase):
            routes.append(route)
            raise w.Held('synthetic_provider_failure')
        with patch.object(w,'ROOT',p),patch.object(w,'guarded_packet',return_value='synthetic'), \
             patch.object(w,'git',return_value='base'),patch.object(w,'inspect',return_value={}), \
             patch.object(w,'remote',return_value={}),patch.object(w,'assert_deployable'), \
             patch.object(w,'model',side_effect=failure):
            with self.assertRaises(w.Held):w.handle(self.case,'owner')
        self.assertEqual(routes,['cirrus','cloud'])

    def test_no_production_notice_from_canary(self):
        with patch.object(w,'ROOT',Path(self.tmp.name)), patch.object(tw,'set_status') as status:
            w.finish('test-case','owner','needs-buddy','synthetic')
            self.assertFalse(status.call_args.kwargs['send']('test'))

    def test_cloud_cannot_review_its_own_proposal(self):
        p=Path(self.tmp.name);(p/'telegram_outbox.py').write_text('x=1\n')
        routes=[]
        def model(prompt,route,phase):
            routes.append((route,phase))
            if route=='cloud':return {'action':'resolved'}
            raise w.Held('synthetic_local_unavailable')
        with patch.object(w,'ROOT',p),patch.object(w,'guarded_packet',return_value='synthetic'), \
             patch.object(w,'git',return_value='base'),patch.object(w,'inspect',return_value={}), \
             patch.object(w,'remote',return_value={}),patch.object(w,'candidate',return_value={}), \
             patch.object(w,'assert_deployable'),patch.object(w,'model',side_effect=model):
            with self.assertRaisesRegex(w.Held,'independent_local_review_unavailable'):
                w.handle(self.case,'owner')
        self.assertEqual(routes,[('cirrus','patch'),('cloud','patch'),('cumulus1','review')])

    def test_context_removes_tests_but_keeps_real_functions(self):
        source='def broken(x):\n    return x-1\ndef selftest():\n    assert broken(1)==0\n'
        context=w.source_context(source,{'title':'broken counter'})
        self.assertIn('return x-1',context)
        self.assertNotIn('def selftest',context)
        self.assertEqual(w.CASE_TARGETS['case-f3'],'job_status')
        self.assertEqual(w.CASE_TARGETS['case-f4'],'foundation_renewal')
        self.assertEqual(len(w.TARGETS),8)

    def test_reviewer_has_review_instructions_not_patch_instructions(self):
        import llm_providers as l
        root=Path(self.tmp.name);(root/'config').mkdir()
        (root/'config/credentials.json').write_text('{}')
        with patch.object(w,'CODE_ROOT',root),patch.object(l,'call',return_value='{"approved":true}') as call, \
             patch.object(l,'last_model',return_value='gpt-oss:120b'), \
             patch.object(l,'last_finish_reason',return_value='stop'):
            self.assertTrue(w.model('synthetic review','peer-local','review')['approved'])
            self.assertEqual(call.call_args.args[1],w.REVIEW_SYSTEM)


if __name__ == '__main__': unittest.main()
