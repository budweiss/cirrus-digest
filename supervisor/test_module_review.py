"""Fictional module-review regressions: no sends, commands or live state writes."""
from datetime import datetime
import json
import unittest
from unittest.mock import patch, MagicMock
import urllib.error
import completeness as c
import tools

class ModuleReviewTests(unittest.TestCase):
    def test_repeated_stalled_run_stays_unhealthy_without_increment(self):
        now=datetime.now();state={}
        rule=c.Rule('fictional',[r'(\d+) items'],1,'fictional failure')
        row={'fictional':{'epoch':now.timestamp(),'ok':True,'note':'0 items'}}
        with patch.object(c,'RULES',{'fictional':rule}),patch.object(c,'_save_state'),patch.object(c,'_cadence_table',return_value={'fictional':26}):
            first=c.check(row,state,now);again=c.check(row,state,now)
        self.assertFalse(first['ok']);self.assertFalse(again['ok'])
        self.assertEqual(state['fictional']['zero_runs'],1)
        self.assertEqual(first['stalled'],again['stalled'])

    def test_unreadable_note_stays_unhealthy_until_new_productive_run(self):
        now=datetime.now();state={}
        rule=c.Rule('fictional',[r'(\d+) items'],1,'fictional failure')
        row={'fictional':{'epoch':now.timestamp(),'ok':True,'note':'unknown format'}}
        with patch.object(c,'RULES',{'fictional':rule}),patch.object(c,'_save_state'),patch.object(c,'_cadence_table',return_value={'fictional':26}):
            self.assertFalse(c.check(row,state,now)['ok'])
            self.assertFalse(c.check(row,state,now)['ok'])
            row['fictional'].update(epoch=now.timestamp()+1,note='1 items')
            self.assertTrue(c.check(row,state,now)['ok'])

    def test_telegram_requires_confirmed_response_and_hides_error_text(self):
        cases=[({'ok':True,'result':{'message_id':12}},True),({'ok':False,'description':'PRIVATE'},False),({},False),('invalid JSON',False)]
        for body,sent in cases:
            with self.subTest(body=body):
                response=MagicMock();response.__enter__.return_value=response
                response.read.return_value=(body if isinstance(body,str) else json.dumps(body)).encode()
                with patch.object(tools,'_load_secrets',return_value={'telegram_bot_token':'fixture','telegram_user_id':'1'}),patch.object(tools,'ledger_append') as ledger,patch.object(tools.urllib.request,'urlopen',return_value=response):
                    result=tools.send_telegram('fictional')
                self.assertEqual(result=='sent',sent)
                self.assertNotIn('PRIVATE',str(ledger.call_args))
        with patch.object(tools,'_load_secrets',return_value={'telegram_bot_token':'fixture','telegram_user_id':'1'}),patch.object(tools,'ledger_append') as ledger,patch.object(tools.urllib.request,'urlopen',side_effect=urllib.error.URLError('PRIVATE credential')):
            result=tools.send_telegram('fictional')
        self.assertTrue(result.startswith('FAILED:'))
        self.assertNotIn('PRIVATE',result+str(ledger.call_args))

    def test_invalid_and_future_timestamps_are_not_healthy(self):
        now=datetime.now()
        with patch.object(c,'_cadence_table',return_value={'fictional':26}):
            for stamp in (None, True, 'bad', float('nan'), float('inf'), now.timestamp()+3600):
                with self.subTest(stamp=stamp):
                    self.assertTrue(c.overdue_jobs({'fictional':{'epoch':stamp}}, now))
            self.assertEqual(c.overdue_jobs({'fictional':{'epoch':now.timestamp()}},now),[])

    def test_unlisted_actions_cannot_execute(self):
        with patch.object(tools,'_run') as run,patch.object(tools,'ledger_append'):
            for action in (tools.restart_service,tools.reset_failed):
                self.assertTrue(action('fictional-unapproved.service').startswith('REFUSED:'))
            run.assert_not_called()

if __name__=='__main__':unittest.main()
