import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parent / "supervisor"))
import opus_approval as approval
from telegram_handoff import forward

class HandoffTests(unittest.TestCase):
    def test_single_receiver_and_preserved_approval_rules(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); inbox=root/'inbox'; inbox.mkdir(); now=time.time()
            request=root/'pending.json'
            request.write_text(json.dumps(dict(kind='opus_upgrade',status='pending',requested_at=now-5,request_id='abcdef123456')))
            with patch.object(approval,'STATE_DIR',root), patch.object(approval,'REQUEST_FILE',request), patch.object(approval,'UPDATE_OFFSET_FILE',root/'offset'), patch.object(approval,'_load_secrets',return_value={'telegram_bot_token':'test','telegram_user_id':7}), patch.dict(os.environ,SKYWARDEN_TELEGRAM_INBOX=str(inbox)), patch.object(approval,'_api_call',side_effect=AssertionError('second poller')):
                for uid, sender, date, text in [(1,8,now,'abcdef123456 approve'),(2,7,now-10,'abcdef123456 approve'),(3,7,now,'abcdef123456 do not approve')]:
                    forward({'update_id':uid,'message':{'from':{'id':sender},'date':date,'text':text}},7)
                    approval.check_for_reply()
                    self.assertEqual(json.loads(request.read_text())['status'],'pending')
                update={'update_id':4,'message':{'from':{'id':7},'date':now,'text':'abcdef123456 approve'}}
                self.assertTrue(forward(update,7))
                self.assertEqual((inbox/'4.json').stat().st_mode & 0o777,0o640)
                approval.check_for_reply()
                self.assertEqual(json.loads(request.read_text())['status'],'approved')
                forward(update,7); approval.check_for_reply()
                self.assertEqual(json.loads(request.read_text())['status'],'approved')
                self.assertEqual(list(inbox.glob('*.json')),[])
    def test_failed_handoff_does_not_claim_delivery(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(FileNotFoundError):
                forward({'update_id':1,'message':{'from':{'id':7},'text':'abcdef123456 yes'}},7,str(Path(temp)/'missing'))
    def test_no_handoff_on_unconfigured_host(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertFalse(forward({'message':{'from':{'id':7}}},7))
if __name__=='__main__': unittest.main()
