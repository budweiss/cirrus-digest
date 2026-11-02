import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
import intake_lifecycle as life
import client_request_prepare as worker
from test_answer_workflow import FakeModels, output, SOURCE
from conversation_memory import Memory
import answer_workflow as aw

class Prepare(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.row=(1,'fixture@example.test','Draft','Summarize the planning meeting time.','<fixture>')
        self.rec={'requester':'fixture','projects':['test'],'message_id':self.row[4],
                  'conversation_body':self.row[3],'kind':'feedback'}
        life.capture(self.root,self.row)
    def test_verified_draft_stops_before_send_and_reuses_budget(self):
        models=FakeModels([('ollama',output()),('ollama',output())])
        with patch('mailer.send',side_effect=AssertionError('must never send')):
            result=worker.prepare(self.root,self.row,self.rec,{},models=models,collect=lambda *a:SOURCE)
            self.assertEqual(result,'ready_for_send')
            worker.prepare(self.root,self.row,self.rec,{},models=models,collect=lambda *a:SOURCE)
        self.assertEqual(len(models.calls),2)
        self.assertTrue(all(k['privacy']=='LOCAL_ONLY' for _,k in models.calls))
        self.assertEqual(life.summary(self.root)['attention'][0]['state'],'ready_for_send')
    def test_failed_local_draft_preserves_id_and_calls(self):
        models=FakeModels([('ollama','bad'),('ollama','bad')])
        result=worker.prepare(self.root,self.row,self.rec,{},models=models,collect=lambda *a:SOURCE)
        self.assertEqual(result,'blocked_on_evidence')
        worker.prepare(self.root,self.row,self.rec,{},models=models,collect=lambda *a:SOURCE)
        self.assertEqual(len(models.calls),2)
        row=aw.intake_record(self.root,self.rec,self.row[2],'LOCAL_ONLY')
        self.assertEqual(row['meta']['attempt_budget']['calls'],2)
    def test_existing_public_identity_is_tightened_without_new_budget(self):
        original=aw.intake_record(self.root,self.rec,self.row[2],'CLOUD_ALLOWED')
        models=FakeModels([('ollama','bad'),('ollama','bad')])
        worker.prepare(self.root,self.row,self.rec,{},models=models,collect=lambda *a:SOURCE)
        turn=Memory(self.root).get(original['id'])
        self.assertEqual(turn['privacy'],'LOCAL_ONLY')
        self.assertEqual(turn['meta']['attempt_budget']['calls'],2)
        self.assertTrue(all(k['pool']=='local' and k['privacy']=='LOCAL_ONLY' for _,k in models.calls))
        worker.prepare(self.root,self.row,self.rec,{},models=models,collect=lambda *a:SOURCE)
        self.assertEqual(len(models.calls),2)

    def test_file_or_fresh_research_does_not_fake_completion(self):
        models=FakeModels([])
        row=list(self.row);row[3]='Find current sources and write an Excel workbook.'
        self.assertEqual(worker.prepare(self.root,row,self.rec,{},models=models),'blocked_no_worker')
        self.assertEqual(models.calls,[])
    def test_partial_evidence_holds_without_inference(self):
        def fail(*a):raise ValueError('unsupported attachment')
        models=FakeModels([])
        self.assertEqual(worker.prepare(self.root,self.row,self.rec,{},models=models,collect=fail),'blocked_on_evidence')
        self.assertEqual(models.calls,[])

if __name__=='__main__':unittest.main()
