import copy
import unittest
from unittest.mock import patch
import capability_admission as admission

class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.e=[dict(id='ollama',model='m',approved=True,task='t',capability='extract',
                     prompt_sha256=admission.prompt_digest('system'),contract_sha256='a'*64,evidence_id='review-1',
                     evaluated_at=900,expires_at=2000,quality=.9,location='local',usable_input_tokens=4096)]
        self.h=[dict(id='ollama',model='m',healthy=True,checked_at=999,location='local',usable_input_tokens=8192)]
    def call(self):
        return admission.candidates(self.e,self.h,task='t',capability='extract',system='system',contract_sha256='a'*64,now=1000)
    def test_join_keeps_actual_timestamp_and_lower_capacity(self):
        r=self.call()[0]
        self.assertEqual(r['health_checked_at'],999)
        self.assertEqual(r['usable_input_tokens'],4096)
    def test_evaluation_scope_and_approval_required(self):
        for key,value in [('approved',False),('task','other'),('capability','other'),('prompt_sha256','changed'),('contract_sha256','b'*64),('evidence_id','')]:
            with self.subTest(key=key):
                original=copy.deepcopy(self.e)
                self.e[0][key]=value
                self.assertEqual(self.call(),[])
                self.e=original
    def test_stale_future_or_changed_health_rejected(self):
        for key,value in [('checked_at',699),('checked_at',1001),('checked_at',float('nan')),('model','other'),('healthy',False),('location','cloud')]:
            original=copy.deepcopy(self.h);self.h[0][key]=value
            self.assertEqual(self.call(),[]);self.h=original
    def test_expiry_and_nonfinite_quality_rejected(self):
        for key,value in [('expires_at',1000),('expires_at',3000000),('evaluated_at',1001),('quality',float('nan')),('usable_input_tokens',True)]:
            original=copy.deepcopy(self.e);self.e[0][key]=value
            self.assertEqual(self.call(),[]);self.e=original
    def test_ambiguous_and_malformed_records_rejected(self):
        self.e.append(copy.deepcopy(self.e[0]));self.assertEqual(self.call(),[])
        self.e=self.e[:1];self.h.append(copy.deepcopy(self.h[0]));self.assertEqual(self.call(),[])
        self.e=[None,42];self.assertEqual(self.call(),[])
    def test_other_project_evaluations_do_not_disqualify_this_task(self):
        for override in ({'task':'other'}, {'capability':'other'},
                         {'prompt_sha256':'b'*64}, {'contract_sha256':'b'*64}):
            self.e.append(dict(self.e[0], **override))
        self.assertEqual(len(self.call()), 1)
        self.e.append(dict(self.e[0]))
        self.assertEqual(self.call(), [])

    def test_missing_or_invalid_contract_cannot_reuse_evidence(self):
        self.e[0].pop('contract_sha256')
        self.assertEqual(self.call(), [])
        for digest in ('', 'not-a-hash', 'G'*64, None):
            with self.assertRaises(ValueError):
                admission.candidates(self.e,self.h,task='t',capability='extract',system='system',contract_sha256=digest,now=1000)

    def business_call(self, now=1000):
        return admission.candidates(self.e, self.h, task='business-idea-ideate',
            capability='extract', system='system', contract_sha256='a'*64, now=now)

    def test_business_expiry_is_advisory_without_renewing_evidence(self):
        self.e[0].update(task='business-idea-ideate', expires_at=1000)
        original = copy.deepcopy(self.e)
        for now in (1000, 1000 + 365*86400):
            self.h[0]['checked_at'] = now
            with self.assertLogs('capability_admission', level='WARNING') as log:
                self.assertEqual(len(self.business_call(now)), 1)
            self.assertIn('No quality approval was renewed', log.output[0])
        self.assertEqual(self.e, original)

    def test_business_advisory_keeps_revocation_identity_contract_and_health_gates(self):
        self.e[0].update(task='business-idea-ideate', expires_at=1000)
        baseline = copy.deepcopy(self.e)
        for field, value in [('approved', False), ('model', 'different'),
                             ('prompt_sha256', 'b'*64), ('contract_sha256', 'b'*64),
                             ('evidence_id', ''), ('expires_at', float('nan')),
                             ('expires_at', 800), ('expires_at', 3000000),
                             ('evaluated_at', 1001), ('quality', float('nan'))]:
            with self.subTest(field=field):
                self.e = copy.deepcopy(baseline); self.e[0][field] = value
                self.assertEqual(self.business_call(), [])
        self.e = baseline
        self.h[0]['healthy'] = False
        self.assertEqual(self.business_call(), [])
        self.h[0].update(healthy=True, checked_at=699)
        self.assertEqual(self.business_call(), [])

    def test_snow_date_is_advisory_for_both_drafts_and_judge_pool(self):
        self.e[0].update(task='billsnow', capability='billsnow:reviewed-s279', expires_at=1000)
        second = dict(self.e[0], id='kimi', model='k3')
        self.e.append(second)
        self.h.append(dict(self.h[0], id='kimi', model='k3'))
        original = copy.deepcopy(self.e)
        for capability, system in [('billsnow:reviewed-s279', 'system'),
                                   ('billsnow:reviewed-s279:synthesis', 'judge system')]:
            for row in self.e:
                row['capability'] = capability
                row['prompt_sha256'] = admission.prompt_digest(system)
            with self.assertLogs('capability_admission', level='WARNING') as log:
                admitted = admission.candidates(self.e,self.h,task='billsnow',
                    capability=capability,system=system,contract_sha256='a'*64,now=1000)
            self.assertEqual({r['id'] for r in admitted},{'ollama','kimi'})
            self.assertEqual(len(log.output),2)
            self.assertTrue(all('No quality approval was renewed' in s for s in log.output))
        self.assertEqual([r['expires_at'] for r in self.e],
                         [r['expires_at'] for r in original])

    def test_snow_advisory_does_not_override_revocation_contract_or_health(self):
        self.e[0].update(task='billsnow', expires_at=1000)
        baseline = copy.deepcopy(self.e)
        for field, value in [('approved',False),('prompt_sha256','b'*64),
                             ('contract_sha256','b'*64),('evidence_id',''),
                             ('expires_at',float('nan')),('expires_at',800)]:
            with self.subTest(field=field):
                self.e = copy.deepcopy(baseline);self.e[0][field] = value
                self.assertEqual(admission.candidates(self.e,self.h,task='billsnow',
                    capability='extract',system='system',contract_sha256='a'*64,now=1000),[])
        self.e = baseline
        self.h[0]['healthy'] = False
        self.assertEqual(admission.candidates(self.e,self.h,task='billsnow',
            capability='extract',system='system',contract_sha256='a'*64,now=1000),[])

    def test_bill_hoa_date_is_advisory_but_not_its_local_screen(self):
        # S537: Bill's HOA research keeps running past a lapsed review date.
        self.e[0].update(task='hoa-leads', expires_at=1000)
        with self.assertLogs('capability_admission', level='WARNING') as log:
            admitted = admission.candidates(self.e, self.h, task='hoa-leads',
                capability='extract', system='system', contract_sha256='a'*64, now=1000)
        self.assertEqual([r['id'] for r in admitted], ['ollama'])
        self.assertTrue(any('Bill HOA research quality review is overdue' in s for s in log.output))
        self.e[0]['approved'] = False
        self.assertEqual(admission.candidates(self.e, self.h, task='hoa-leads',
            capability='extract', system='system', contract_sha256='a'*64, now=1000), [])

    def test_other_tasks_cannot_use_advisory_expiry(self):
        for task in ('halftime:events', 'halftime:catalogue:variety', 'snow', 'hoa-leads:screen-local',
                     'billnewdev', 'alopecia-brief', 'billsnow:other',
                     'business-idea-feed-discovery', 'business-idea-ideate:client', 't'):
            with self.subTest(task=task):
                self.e[0].update(task=task, expires_at=1000)
                self.assertFalse(admission.expiry_is_advisory(task))
                self.assertEqual(admission.candidates(self.e, self.h, task=task,
                    capability='extract', system='system', contract_sha256='a'*64, now=1000), [])

    def grace_for(self, row, tested_at=995):
        import approval_grace
        return {'entries': {approval_grace.record_sha(row): {'tested_at': tested_at}}, 'revoked': []}

    def test_p3_grace_admits_an_expired_record_with_fresh_passing_fixtures(self):
        self.e[0]['expires_at'] = 1000
        grace = self.grace_for(self.e[0])
        with self.assertLogs('capability_admission', level='WARNING') as log:
            admitted = admission.candidates(self.e, self.h, task='t', capability='extract', system='system',
                                            contract_sha256='a'*64, now=1000, grace=grace)
        self.assertEqual(len(admitted), 1)
        self.assertIn('72-hour grace', log.output[0])
        self.assertEqual(self.e[0]['expires_at'], 1000)
        self.assertEqual(admission.candidates(self.e, self.h, task='t', capability='extract', system='system',
                                              contract_sha256='a'*64, now=1000, grace={'entries': {}, 'revoked': []}), [])

    def test_p3_grace_ends_at_72_hours_and_keeps_every_other_gate(self):
        self.e[0]['expires_at'] = 1000
        baseline = copy.deepcopy(self.e)
        grace = self.grace_for(self.e[0])
        late = 1000 + 72 * 3600
        self.h[0]['checked_at'] = late
        self.assertEqual(admission.candidates(self.e, self.h, task='t', capability='extract', system='system',
                                              contract_sha256='a'*64, now=late, grace=grace), [])
        self.h[0]['checked_at'] = 999
        for field, value in [('approved', False), ('contract_sha256', 'b'*64), ('prompt_sha256', 'b'*64),
                             ('evidence_id', ''), ('model', 'other')]:
            with self.subTest(field=field):
                self.e = copy.deepcopy(baseline); self.e[0][field] = value
                self.assertEqual(admission.candidates(self.e, self.h, task='t', capability='extract',
                    system='system', contract_sha256='a'*64, now=1000, grace=self.grace_for(self.e[0])), [])
        self.e = baseline
        self.h[0]['healthy'] = False
        self.assertEqual(admission.candidates(self.e, self.h, task='t', capability='extract', system='system',
                                              contract_sha256='a'*64, now=1000, grace=grace), [])

    def test_business_dispatch_preserves_privacy_and_budget_arguments(self):
        self.e[0].update(task='business-idea-ideate', expires_at=1000)
        with self.assertLogs('capability_admission', level='WARNING'):
            with patch('capability_dispatch.dispatch', return_value=('ollama', 'ok')) as dispatch:
                admission.dispatch_reviewed('system', 'user', {}, evaluations=self.e,
                    health=self.h, task='business-idea-ideate', capability='extract',
                    contract_sha256='a'*64, now=1000, max_cost_usd=0, privacy='LOCAL_ONLY')
        self.assertEqual(len(dispatch.call_args.kwargs['candidates']), 1)
        self.assertEqual(dispatch.call_args.kwargs['max_cost_usd'], 0)
        self.assertEqual(dispatch.call_args.kwargs['privacy'], 'LOCAL_ONLY')

    def test_wrapper_passes_only_admitted_records(self):
        with patch('capability_dispatch.dispatch',return_value=('ollama','ok')) as dispatch:
            self.assertEqual(admission.dispatch_reviewed('system','user',{},evaluations=self.e,health=self.h,task='t',capability='extract',contract_sha256='a'*64,now=1000,max_cost_usd=0,privacy='LOCAL_ONLY'),('ollama','ok'))
        args=dispatch.call_args.kwargs
        self.assertEqual(len(args['candidates']),1)
        self.assertEqual(args['privacy'],'LOCAL_ONLY')

if __name__=='__main__':unittest.main()
