"""Synthetic tests; no real mail, research, or model calls."""
import json, tempfile, time, unittest, zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
import intake_lifecycle as life
import list_delivery as lists
ASK="I'm looking for company owner names and phone numbers for snow plow companies in Delaware."
class ContactHandoff(unittest.TestCase):
 def setUp(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name)
  self.row=(1,'fixture@example.test','Research',ASK,'<original@example.test>');self.launch=Mock()
  life.capture(self.root,self.row);life.update(self.root,self.row,'blocked_no_worker','cowork','assign',{'client':'fixture'})
 def start(self,**kw):return lists.start_job('fixture',ASK,'Research',self.row[4],project_dir=self.root,popen=self.launch,**kw)
 def complete(self,run):
  d=self.root/'logs/contact_lists'/run;d.mkdir(parents=True,exist_ok=True)
  (d/'meta.json').write_text(json.dumps({'client':'fixture','message_id':self.row[4]}))
  for n in ('stats.json','summary.json'):(d/n).write_text('{}')
  with zipfile.ZipFile(d/('contact-list-'+run+'-client.xlsx'),'w') as z:z.writestr('xl/workbook.xml','<workbook/>')
  return d
 def test_narrow_routing(self):
  self.assertTrue(lists.wants_contact_list('',ASK))
  self.assertTrue(lists.wants_contact_list('',ASK.replace('names','name')))
  for t in ('Thanks for the company owner names and phone numbers.',"I'm looking for company stock prices.","I'm looking for names of portfolio owners."):
   self.assertFalse(lists.wants_contact_list('',t))
 def test_retry_across_days_and_subjects_reuses_original(self):
  run=self.start(now=datetime(2026,11,7,10))
  self.assertEqual(lists.start_job('fixture','changed','changed',self.row[4],self.root,self.launch,datetime(2026,11,8,10)),run)
  self.launch.assert_called_once()
 def test_same_second_distinct_messages(self):
  t=datetime(2026,11,7,10);a=self.start(now=t)
  b=lists.start_job('fixture',ASK,'Research','<second>',self.root,self.launch,t)
  self.assertNotEqual(a,b);self.assertEqual(self.launch.call_count,2)
 def test_uncertain_launch_never_retried(self):
  self.launch.side_effect=OSError('fixture')
  with self.assertRaises(OSError):self.start()
  self.start();self.launch.assert_called_once()
 def test_manual_completed_job_becomes_ready_without_launch(self):
  self.complete('cl-manual');self.assertEqual(self.start(),'cl-manual');self.launch.assert_not_called()
  self.assertEqual(life.reconcile_contact_jobs(self.root),1)
  self.assertEqual(life.summary(self.root)['counts'],{'ready_for_send':1})
  self.assertEqual(life.reconcile_contact_jobs(self.root),0);self.assertEqual(life.pending(self.root),[])
 def test_subject_not_identity(self):
  d=self.complete('cl-other');(d/'meta.json').write_text(json.dumps({'client':'fixture','message_id':'other','thread_subject':'Research'}))
  self.assertEqual(life.reconcile_contact_jobs(self.root),0)
 def test_stall_clock_survives_polls(self):
  self.start();life.reconcile_contact_jobs(self.root)
  with life.connect(self.root) as db:db.execute('UPDATE requests SET updated=?',(time.time()-7200,))
  self.assertEqual(life.reconcile_contact_jobs(self.root),0)
  h=life.summary(self.root)['attention'][0];self.assertEqual(h['state'],'working');self.assertGreater(h['age_minutes'],119)
 def test_delivery_ledger_is_not_sent_proof(self):
  run=self.start();self.complete(run)
  (self.root/'logs/contact_lists/deliveries.jsonl').write_text(json.dumps({'event':'delivered','run_id':run})+'\n')
  life.reconcile_contact_jobs(self.root);self.assertEqual(life.summary(self.root)['counts'],{'sent_unverified':1})
  self.assertEqual(life.pending(self.root),[])
 def test_bad_artifact_or_ambiguous_jobs_stay_held(self):
  d=self.complete('cl-one');(d/'contact-list-cl-one-client.xlsx').write_bytes(b'bad')
  life.reconcile_contact_jobs(self.root);self.assertEqual(life.summary(self.root)['counts'],{'blocked_on_evidence':1})
  self.complete('cl-two');life.reconcile_contact_jobs(self.root)
  with self.assertRaises(ValueError):self.start()
  self.launch.assert_not_called()
 def test_corrupt_metadata_does_not_abort_poll(self):
  d=self.complete('cl-one');(d/'meta.json').write_text('bad')
  self.assertEqual(life.reconcile_contact_jobs(self.root),1)
  self.assertEqual(life.summary(self.root)['counts'],{'blocked_on_evidence':1})
 def test_verified_never_reopened(self):
  self.complete('cl-one');life.update(self.root,self.row,'sent_verified','review','done',{'sent_receipt':'fixture'})
  self.assertEqual(life.reconcile_contact_jobs(self.root),0)
 def test_conflicting_job_metadata_does_not_attach_another_clients_artifact(self):
  run=self.start();d=self.complete(run)
  (d/'meta.json').write_text(json.dumps({'client':'other','message_id':'<other>'}))
  with self.assertRaises(ValueError):self.start()
  life.reconcile_contact_jobs(self.root)
  self.assertEqual(life.summary(self.root)['counts'],{'blocked_on_evidence':1})
class Dispatcher(unittest.TestCase):
 def test_dispatches_owner_contact_request_once(self):
  from contextlib import ExitStack
  import intake,subprocess
  with tempfile.TemporaryDirectory() as d,ExitStack() as st:
   root=Path(d)
   for n,v in [('PROJECT_DIR',root),('FAST_POLL_STATE_PATH',root/'fast')]:st.enter_context(patch.object(intake,n,v))
   entry={'name':'fixture','projects':['snow'],'limit':10,'request_kind':'answer','require_prefix':False}
   st.enter_context(patch.object(intake,'load_allowlist',return_value={'fixture@example.test':entry}))
   st.enter_context(patch('runtime_config.load_sources',return_value={}))
   st.enter_context(patch.object(intake,'find_account',return_value={'credential_key':'password'}))
   st.enter_context(patch.object(intake,'load_json',return_value={'password':'fixture'}))
   st.enter_context(patch.object(intake,'load_state',return_value={}))
   for n in ('save_state','_record_status','append_backlog','telegram','log','_notify'):st.enter_context(patch.object(intake,n,return_value=0))
   st.enter_context(patch.object(intake,'is_confirmation',return_value=[]))
   st.enter_context(patch.object(intake.dev_loop,'ledger_append'))
   st.enter_context(patch.object(intake.dev_loop,'ticket_create',return_value={'id':'fixture'}))
   st.enter_context(patch.object(life,'coordinate',return_value=True))
   row=(1,'fixture@example.test','Research',ASK,'<route-fixture>')
   st.enter_context(patch.object(intake,'scan_inbox',side_effect=[([row],[]),([row],[])]))
   st.enter_context(patch.object(intake,'under_limit',return_value=True))
   ack=st.enter_context(patch.object(intake,'send_ack',return_value=True))
   start=st.enter_context(patch.object(lists,'start_job',return_value='cl-fixture'))
   solve=st.enter_context(patch.object(intake.task_solver,'solve_and_answer'))
   st.enter_context(patch.object(subprocess,'Popen'))
   self.assertEqual(intake.run(),0);self.assertEqual(intake.run(),0)
   ack.assert_called_once();start.assert_called_once();solve.assert_not_called()
   self.assertEqual(life.summary(root)['counts'],{'working':1})
if __name__=='__main__':unittest.main()
