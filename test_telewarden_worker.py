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

    def test_jobs_check_line_targets_the_job_it_reports(self):
        """S685: 7 of 11 'Needs you' came from repairing jobs_check.py for another job's failure."""
        def case(sample, job='jobs_check'):
            return {'id': 'tw-x', 'job': job, 'sample': sample, 'title': sample}
        self.assertEqual(w.target(case('⚠️ environmentscan: 2026-11-20T06:50 FAILED — Review held')),
                         'reported:environmentscan')
        self.assertFalse(w.in_scope(case('⚠️ alopeciabrief (CUMULUS): 2026-11-13T07:01 OVERDUE — brief')))
        self.assertEqual(w.target(case('⚠️ foundationrenewalcumulus (CUMULUS): 2026-11-22T04:57 FAILED — x')),
                         'foundation_renewal')
        self.assertEqual(w.target(case('⚠️ modelhealth: 2026-11-22T05:30 FAILED — x')), 'model_health')
        # CIRRUS's foundationrenewal is foundation_renewal_cirrus.py, outside the scope
        self.assertFalse(w.in_scope(case('⚠️ foundationrenewal: 2026-11-24T04:54 FAILED — answer workflow')))
        self.assertEqual(w.target(case('jobs_check could not read the CUMULUS ledger')), 'jobs_check')
        self.assertEqual(w.target(case('anything', job='telegram_outbox')), 'telegram_outbox')
        self.assertEqual(w.target({'id': 'case-f3', 'job': 'jobs_check', 'sample': '⚠️ x: 2026-11-01T00:00 F'}),
                         'job_status')

    def test_private_stock_line_is_protected_and_never_cloud(self):
        stock = {'id': 'tw-y', 'job': 'jobs_check', 'private': False, 'history': [], 'status': 'new',
                 'title': '⚠️ stockam: 2026-11-19T07:45 FAILED — status=failed error=SMTPDataError',
                 'sample': '⚠️ stockam: 2026-11-19T07:45 FAILED — status=failed error=SMTPDataError'}
        self.assertFalse(w.in_scope(stock))
        self.assertIn('protected', w.protected_reason(stock))
        with self.assertRaisesRegex(w.Held, 'protected'): w.guarded_packet(stock, 'jobs_check.py', '')

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

    def test_model_failure_retries_silently_before_needs_buddy(self):
        # S621: 11-12/13 a failed model call messaged Buddy "could not be corrected".
        case = {'history': []}
        status, note = w.hold_outcome(case, 'model_identity_or_completion_failed')
        self.assertEqual(status, 'explained')
        case['history'].append({'note': note})
        self.assertEqual(w.hold_outcome(case, 'invalid_model_json')[0], 'explained')
        case['history'].append({'note': note})
        status, note = w.hold_outcome(case, 'model_needs_more_evidence')
        self.assertEqual(status, 'needs-buddy')
        self.assertIn('3 tries', note)

    def test_scope_holds_need_buddy_and_budget_waits_never_escalate(self):
        for reason in ('protected_case_requires_owned_host_workflow', 'outside_automatic_repair_scope:x'):
            self.assertEqual(w.hold_outcome({'history': []}, reason)[0], 'needs-buddy')
        retried = {'history': [{'note': w.RETRY_NOTE + 'x'}] * 5}
        self.assertEqual(w.hold_outcome(retried, 'worker_daily_budget_exhausted')[0], 'explained')

    def test_untried_cases_go_before_retries_and_session_holds_wait(self):
        # S623: three retried 11-10 cases took every slot for days; a session's
        # "next run is Tuesday" would have been retried and escalated.
        tried = lambda cid, first: {'id': cid, 'status': 'explained', 'first': first, 'last': first,
            'history': [{'t': 5, 'status': 'fixing', 'note': 'CIRRUS repair worker claimed case'},
                        {'t': 6, 'status': 'explained', 'note': w.RETRY_NOTE + 'x'}]}
        new = lambda cid, first: {'id': cid, 'status': 'new', 'first': first, 'last': first,
            'history': [{'t': first, 'status': 'new', 'note': 'first seen'}]}
        held = {'id': 'held', 'status': 'explained', 'first': 1, 'last': 2,
                'history': [{'t': 9, 'status': 'explained', 'note': 'Next run Tue; reopens if it fails'}]}
        cases = [tried('t1', 1), tried('t2', 2), tried('t3', 3), new('n1', 4), new('n2', 5), held]
        self.assertEqual([c['id'] for c in w.queue(cases)], ['n1', 'n2', 't1'])
        held['last'] = 10                                  # the warning came back
        self.assertIn('held', [c['id'] for c in w.queue(cases)])
        self.assertFalse(w.session_hold(tried('t4', 1)))   # worker retries keep their turn

    def test_out_of_scope_waits_then_escalates_only_while_still_happening(self):
        # S663: 13 "Needs you" in 3 days for Nimbus gateway lines, Skywarden and
        # bill_watch; a session fixed or closed all of them.
        H = 3600
        case = {'id': 'g', 'job': 'gateway', 'status': 'new', 'private': False, 'title': 'gateway: x failed',
                'last': 0, 'history': [{'t': 0, 'status': 'new', 'note': 'first seen'}]}
        status, note = w.scope_outcome(case, 10)
        self.assertEqual(status, 'explained')
        case.update(status=status); case['history'].append({'t': 10, 'status': status, 'note': note})
        self.assertTrue(tw.waiting_for_session(case))
        self.assertIsNone(w.scope_outcome(case, 20 * H))               # still waiting, no rewrite
        self.assertIsNone(w.scope_outcome(case, 80 * H))               # one-off, never seen again
        case['last'] = 60 * H                                          # seen again, but not lately
        self.assertIsNone(w.scope_outcome(case, 90 * H))
        case['last'] = 79 * H                                          # still happening
        self.assertEqual(w.scope_outcome(case, 80 * H)[0], 'needs-buddy')
        self.assertIsNone(w.scope_outcome(dict(case, last=40 * H), 40 * H + 30))  # not yet 3 days

    def test_protected_and_lost_sends_still_reach_buddy_at_once(self):
        base = {'id': 'p', 'status': 'new', 'private': False, 'title': 'x', 'last': 0,
                'history': [{'t': 0, 'status': 'new', 'note': 'first seen'}]}
        for extra in ({'job': 'stock_schedule'}, {'job': 'gateway', 'private': True},
                      {'job': 'skywarden', 'sample': 'holdings file missing'}, {'job': 'telegram_send'}):
            self.assertEqual(w.scope_outcome(dict(base, **extra), 10)[0], 'needs-buddy', extra)

    def test_run_parks_out_of_scope_silently_and_keeps_repair_slots(self):
        # End to end through main(): no Telegram for the waiting case, one for the
        # protected case, and the repair queue only ever gets in-scope cases.
        (Path(self.tmp.name)/'server-worker-enabled').touch()
        first = {'t': 0, 'status': 'new', 'note': 'first seen'}
        mk = lambda cid, job, **k: dict({'id': cid, 'job': job, 'status': 'new', 'private': False,
                                         'title': cid, 'first': 0, 'last': 0, 'history': [dict(first)]}, **k)
        cases = {c['id']: c for c in (mk('g1', 'gateway'), mk('g2', 'skywarden'), mk('g3', 'bill_watch'),
                                       mk('p1', 'stock_schedule', private=True), dict(self.case, last=0))}
        tw._save('cases.json', cases)
        sent, handled = [], []
        def fake_handle(case, owner, allow_deploy=False):
            handled.append(case['id']); return {'status': 'tested', 'case': case['id']}
        import job_status, sys
        with patch.object(sys, 'argv', ['telewarden_worker.py', '--run']), \
             patch.object(tw, 'notify', side_effect=lambda text, **k: sent.append(text) or True), \
             patch.object(w, 'handle', side_effect=fake_handle), patch.object(job_status, 'record'), \
             patch('builtins.print'):
            w.main()
        after = tw._load('cases.json', {})
        self.assertEqual(handled, ['test-case'])
        for cid in ('g1', 'g2', 'g3'):
            self.assertTrue(tw.waiting_for_session(after[cid]), cid)
        self.assertEqual(after['p1']['status'], 'needs-buddy')
        self.assertEqual(len(sent), 1)
        self.assertIn('[p1]', sent[0])
        lines, att = tw.brief_lines()
        self.assertTrue(any(l.startswith('• Waiting for a session (3)') for l in lines))

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
