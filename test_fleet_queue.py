import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from fleet_queue import Queue, Refused


def policy():
    workers={w:{'model':m,'min_available_mib':4096} for w,m in [('cumulus1-gptoss','gpt-oss:120b'),('cumulus2-qwen','qwen3.8-27b-fp8')]}
    projects={}
    for w,spec in workers.items():
        projects[w]={'workers':[w],'model_ids':{w:spec['model']},'validator':'fixture_json',
          'max_retries':2,'stall_seconds':120,'heartbeat_seconds':30,'max_input_bytes':2048,
          'max_output_tokens':1024,'qualification_until':2000000000,'contract':'synthetic-v1',
          'mode':'pilot','owner':'fleet-pilot','resource':w,'delivery':'none'}
    return {'workers':workers,'projects':projects}


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'queue.db';self.now=1800000000.;self.mono=1000.
        self.p=policy();self.q=self.open()
        self.w='cumulus1-gptoss';self.w2='cumulus2-qwen'
        self.healthy()
    def open(self,epoch='boot-1'):
        return Queue(self.path,self.p,lambda:self.now,lambda:self.mono,epoch)
    def healthy(self):
        for w,s in self.p['workers'].items():self.q.health(w,s['model'],32768,20000)
    def submit(self,key='one',worker=None,payload=None):
        return self.q.submit(worker or self.w,key,payload or {'fixture':'sample12'},'synthetic-v1')
    def start(self):
        job=self.submit();r=self.q.claim(self.w);return job,r['fence']
    def tick(self,n):self.now+=n;self.mono+=n
    def result(self,worker=None):
        return {'model':self.p['workers'][worker or self.w]['model'],'finish_reason':'stop',
                'coverage':1,'data':{'sample':12,'control_group':False,'trial_date':None}}
    def get(self,job):return next(r for r in self.q.status()['jobs'] if r['id']==job)
    def reconcile(self,job,fence):
        self.tick(121);self.q.monitor();self.q.cancel(job,fence,self.q.hash,True)
        self.q.stopped(job,fence,True,True)

    def test_B01_duplicate_submission_race(self):
        with ThreadPoolExecutor(8) as pool: ids=list(pool.map(lambda _:self.submit(),range(16)))
        self.assertEqual(len(set(ids)),1)
    def test_B01_same_key_changed_input_refused(self):
        self.submit()
        with self.assertRaises(Refused):self.submit(payload={'changed':True})
    def test_B02_claim_race(self):
        self.submit()
        with ThreadPoolExecutor(8) as pool: rows=list(pool.map(lambda _:self.q.claim(self.w),range(8)))
        self.assertEqual(sum(r is not None for r in rows),1)
    def test_B04_two_slots_third_waits(self):
        self.submit();self.submit('two',self.w2);third=self.submit('three')
        self.assertIsNotNone(self.q.claim(self.w));self.assertIsNotNone(self.q.claim(self.w2))
        self.assertIsNone(self.q.claim(self.w));self.assertEqual(self.get(third)['state'],'queued')
    def test_B05_unqualified_free_worker_cannot_claim(self):
        self.submit();self.assertIsNone(self.q.claim(self.w2))
    def test_B06_no_cloud_worker(self):
        self.submit()
        with self.assertRaises(Refused):self.q.health('cloud','gpt',32768,99999)
        self.assertIsNone(self.q.claim('cloud'))
    def test_A04_missing_contract(self):
        del self.p['projects'][self.w]['validator']
        with self.assertRaises(Refused):self.open()
    def test_A05_competing_owner(self):
        self.p['projects']['other']=copy.deepcopy(self.p['projects'][self.w]);self.p['projects']['other']['owner']='second'
        with self.assertRaises(Refused):self.open()
    def test_A07_wrong_contract(self):
        with self.assertRaises(Refused):self.q.submit(self.w,'x',{},'changed')
    def test_A07_expired_qualification(self):
        self.now=2000000001
        with self.assertRaises(Refused):self.submit()
    def test_A02_policy_drift(self):
        self.p['projects'][self.w]['max_retries']=1
        with self.assertRaises(Refused):self.open()
    def test_abandon_only_stale_never_started_work(self):
        job=self.submit()
        with self.assertRaises(Refused):self.q.abandon_queued(job,self.q.hash)
        self.tick(1801)
        with self.assertRaises(Refused):self.q.abandon_queued(job,'wrong')
        self.q.abandon_queued(job,self.q.hash)
        self.assertEqual(self.get(job)['state'],'cancelled')
        self.assertEqual(self.get(job)['receipt'],'none')
        self.assertIsNone(self.q.claim(self.w,job))
        self.healthy();active=self.submit('active');self.q.claim(self.w,active)
        self.tick(1801)
        with self.assertRaises(Refused):self.q.abandon_queued(active,self.q.hash)
    def test_A07_wrong_identity(self):
        self.submit();self.q.health(self.w,'wrong',32768,20000);self.assertIsNone(self.q.claim(self.w))
    def test_B07_oversized_input(self):
        with self.assertRaises(Refused):self.submit(payload={'text':'a'*3000})
    def test_B07_context_fit(self):
        self.submit();self.q.health(self.w,'gpt-oss:120b',4096,20000);self.assertIsNone(self.q.claim(self.w))
    def test_B07_stale_health(self):
        self.submit();self.tick(91);self.assertIsNone(self.q.claim(self.w))
    def test_B07_future_health(self):
        self.submit();self.q.health(self.w,'gpt-oss:120b',32768,20000,observed=self.now+1);self.assertIsNone(self.q.claim(self.w))
    def test_B08_aging(self):
        old=self.q.submit(self.w,'old',{},'synthetic-v1',-10);self.tick(1260)
        self.q.submit(self.w,'new',{},'synthetic-v1',10);self.healthy()
        self.assertEqual(self.q.claim(self.w)['id'],old)
    def test_B09_drain(self):
        self.submit();self.q.control('drain',self.w);self.assertIsNone(self.q.claim(self.w))
        self.q.control('ready',self.w);self.assertIsNotNone(self.q.claim(self.w))
    def test_B10_memory_and_legacy_busy(self):
        self.submit()
        for mem,busy in [(100,False),(20000,True)]:
            self.q.health(self.w,'gpt-oss:120b',32768,mem,busy);self.assertIsNone(self.q.claim(self.w))
    def test_B11_namespaces(self):
        a=self.submit('same');b=self.submit('same',self.w2);self.assertNotEqual(a,b)
    def test_B12_restart_retains_reservation(self):
        job,fence=self.start();self.q=self.open();self.submit('other');self.assertIsNone(self.q.claim(self.w))
        self.assertTrue(self.q.complete(job,fence,self.result()))
    def test_B13_unknown_retains_slot_and_fences_publication(self):
        job,fence=self.start();self.tick(91);self.q.monitor();self.healthy();self.submit('other')
        self.assertEqual(self.get(job)['state'],'unknown');self.assertIsNone(self.q.claim(self.w))
        with self.assertRaises(Refused):self.q.complete(job,fence,self.result())
    def test_B14_health_recovery(self):
        self.submit();self.tick(91);self.assertIsNone(self.q.claim(self.w));self.healthy();self.assertIsNotNone(self.q.claim(self.w))
    def test_C01_advancing_work_not_cancelled(self):
        job,fence=self.start()
        for i in range(1,8):self.tick(50);self.q.progress(job,fence,i);self.q.monitor()
        self.assertEqual(self.get(job)['state'],'running')
    def test_C02_repetitive_heartbeat_does_not_hide_stall(self):
        job,fence=self.start()
        for _ in range(4):self.tick(40);self.q.progress(job,fence,0);self.q.monitor()
        self.assertEqual(self.get(job)['state'],'suspected_stall')
    def test_C03_missing_heartbeat(self):
        job,fence=self.start();self.tick(91);self.q.monitor();self.assertEqual(self.get(job)['state'],'unknown')
    def test_C05_empty_completion(self):
        job,fence=self.start();self.assertFalse(self.q.complete(job,fence,{}));self.assertEqual(self.get(job)['state'],'failed')
    def test_C06_bad_coverage_and_source(self):
        for i,change in enumerate([{'coverage':0},{'data':{'sample':99}},{'finish_reason':'length'},{'model':'wrong'}]):
            job=self.submit(str(i));r=self.q.claim(self.w);result=self.result();result.update(change)
            self.assertFalse(self.q.complete(job,r['fence'],result))
    def test_C07_missing_evidence_abstention(self):
        job,fence=self.start();self.assertTrue(self.q.complete(job,fence,self.result()))
    def test_C08_corrupt_database_fails(self):
        self.path.write_bytes(b'not sqlite')
        with self.assertRaises(Exception):self.open()
    def test_C09_new_boot_quarantines(self):
        job,fence=self.start();self.q=self.open('boot-2');self.q.monitor();self.assertEqual(self.get(job)['state'],'unknown')
    def test_C11_wall_clock_backward_not_false_stall(self):
        job,fence=self.start();self.now-=3600;self.mono+=30;self.q.monitor();self.assertEqual(self.get(job)['state'],'running')
    def test_C11_wall_clock_forward_not_false_stall(self):
        job,fence=self.start();self.now+=3600;self.mono+=30;self.q.monitor();self.assertEqual(self.get(job)['state'],'running')
    def test_C12_incident_dedup(self):
        job,fence=self.start();self.tick(91)
        for _ in range(5):self.q.monitor()
        self.assertEqual(sum(e['kind']=='unknown' for e in self.q.status()['events']),1)
    def test_D03_backend_busy_retains_reservation(self):
        job,fence=self.start();self.tick(91);self.q.monitor();self.q.cancel(job,fence,self.q.hash,True)
        with self.assertRaises(Refused):self.q.stopped(job,fence,True,False)
        self.assertEqual(self.get(job)['state'],'cancelling')
    def test_D05_budget_survives_restart(self):
        job,fence=self.start()
        for i in range(3):
            self.reconcile(job,fence);self.q=self.open();retry=self.q.retry(job,self.q.hash)
            if i<2:
                self.assertTrue(retry);self.tick(121);self.healthy();r=self.q.claim(self.w);fence=r['fence']
            else:self.assertFalse(retry);self.assertEqual(self.get(job)['state'],'blocked')
    def test_D07_uncertain_delivery_never_retried(self):
        job,fence=self.start();self.tick(91);self.q.monitor();self.q.stopped(job,fence,True,True,'unknown')
        with self.assertRaises(Refused):self.q.retry(job,self.q.hash)
    def test_D08_stale_attempt_refused(self):
        job,fence=self.start();self.reconcile(job,fence);self.q.retry(job,self.q.hash);self.tick(61);self.healthy();self.q.claim(self.w)
        with self.assertRaises(Refused):self.q.complete(job,fence,self.result())
    def test_D09_shared_service_control_absent(self):
        with self.assertRaises(Refused):self.q.control('restart','ollama.service')
    def test_E05_policy_and_scope(self):
        job,fence=self.start()
        with self.assertRaises(Refused):self.q.cancel(job,fence,'wrong',True)
        with self.assertRaises(Refused):self.q.cancel(job,fence,self.q.hash,False)
        with self.assertRaises(Refused):self.q.progress('other',fence,1)
    def test_F09_backup_inflight_is_quarantined_on_new_boot(self):
        job,fence=self.start();dest=Path(self.temp.name)/'restored.db';self.q.backup(dest)
        restored=Queue(dest,self.p,lambda:self.now,lambda:self.mono,'different-boot');restored.monitor()
        self.assertEqual(restored.status()['jobs'][0]['state'],'unknown')
    def test_D10_deleted_database_not_recreated(self):
        self.path.unlink()
        with self.assertRaises(Refused):self.open()
    def test_E08_caller_cannot_mutate_loaded_policy(self):
        self.p['projects'][self.w]['mode']='observed'
        self.assertEqual(self.q.policy['projects'][self.w]['mode'],'pilot')
    def test_F13_pause_resume(self):
        self.submit();self.q.control('pause');self.assertIsNone(self.q.claim(self.w));self.q.control('resume');self.assertIsNotNone(self.q.claim(self.w))

if __name__=='__main__':unittest.main()
