import datetime,json,pathlib,tempfile,unittest
from unittest.mock import patch
import client_watch as cw

class HistoricalGapTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.p=pathlib.Path(self.tmp.name)/'ledger.jsonl'
 def row(self,days,event='user-intake',client='fixture',kind='build'):
  return {'ts':(datetime.datetime.now()-datetime.timedelta(days=days)).strftime(cw.TS_FMT),'event':event,'requester':client,'thread':'same','kind':kind}
 def check_rows(self,*rows):
  self.p.write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
  return cw.stalled_threads(self.p)
 def test_old_request_stays_visible(self):
  hits=self.check_rows(self.row(30))
  self.assertEqual(len(hits),1)
  self.assertIsNone(hits[0]['work_owed'])
  self.assertFalse(hits[0]['automatic_resend_allowed'])
 def test_old_recorded_reply_closes_gap(self):
  self.assertEqual(self.check_rows(self.row(30),self.row(29,'client-mail-sent')),[])
 def test_reply_before_request_does_not_close_gap(self):
  self.assertEqual(len(self.check_rows(self.row(30),self.row(31,'client-mail-sent'))),1)
 def test_other_client_cannot_close_gap(self):
  self.assertEqual(len(self.check_rows(self.row(30),self.row(29,'client-mail-sent','other'))),1)
 def test_new_followup_after_delivery_stays_open(self):
  self.assertEqual(len(self.check_rows(self.row(40),self.row(35,'client-mail-sent'),self.row(30))),1)
 def test_ack_does_not_close_gap(self):
  self.assertEqual(len(self.check_rows(self.row(30),self.row(29,'ack'))),1)
 def test_young_request_is_not_stale(self):
  self.assertEqual(self.check_rows(self.row(1)),[])
 def test_other_checks_retain_window(self):
  self.check_rows(self.row(30,'auto-answered'))
  self.assertEqual(cw.read_ledger(self.p),[])

if __name__=='__main__':unittest.main()
