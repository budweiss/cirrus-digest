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

    def test_wrapper_passes_only_admitted_records(self):
        with patch('capability_dispatch.dispatch',return_value=('ollama','ok')) as dispatch:
            self.assertEqual(admission.dispatch_reviewed('system','user',{},evaluations=self.e,health=self.h,task='t',capability='extract',contract_sha256='a'*64,now=1000,max_cost_usd=0,privacy='LOCAL_ONLY'),('ollama','ok'))
        args=dispatch.call_args.kwargs
        self.assertEqual(len(args['candidates']),1)
        self.assertEqual(args['privacy'],'LOCAL_ONLY')

if __name__=='__main__':unittest.main()
