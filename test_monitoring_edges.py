"""S341 offline failure injection: no models, credentials, sends or live state."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import fleet_status
import cumulus_daily_brief as brief
import foundation_renewal as renewal

class MonitoringEdges(unittest.TestCase):
    def test_fleet_malformed_status_cannot_be_healthy_or_raise(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'status.json'
            for data in [None,[],{'observed':999,'workers':'ab','ok':True},
                         {'observed':999,'workers':[1,2],'ok':'false'},
                         {'observed':999,'workers':42,'ok':True}]:
                with self.subTest(data=data):
                    path.write_text(json.dumps(data))
                    self.assertFalse(fleet_status.check(path,1000)['ok'])

    def test_brief_malformed_incidents_report_unknown_without_crashing(self):
        for data in [{'incidents':[]},{'incidents':{'bad':None}},
                     {'incidents':{'bad':{'first_seen':'yesterday'}}}]:
            with self.subTest(data=data),patch.object(brief,'gather_client_events',return_value={}), \
                 patch.object(brief,'gather_job_lines',return_value={}),patch.object(brief,'gather_skywarden',return_value=[]), \
                 patch.object(brief,'_sudo_cat',return_value=json.dumps(data)):
                _,body=brief.compose()
                self.assertIn('follow-through UNKNOWN',body)
                self.assertNotIn('Unresolved incidents: 0',body)

    def test_brief_shows_open_owner_and_quiet_jobs_without_closing_incident(self):
        row={'incidents':{'failed_runs:billsnow':{'first_seen':100,'state':'action pending','owner':'Cowork','next_check':200}}}
        with patch.object(brief,'gather_client_events',return_value={}), \
             patch.object(brief,'gather_job_lines',return_value={'Bill':['quiet: no material change','failed: upstream unavailable']}), \
             patch.object(brief,'gather_skywarden',return_value=[]),patch.object(brief,'_sudo_cat',return_value=json.dumps(row)):
            _,body=brief.compose()
            for expected in ['Unresolved incidents: 1','owner Cowork','next check 200','quiet: no material change','failed: upstream unavailable']:
                self.assertIn(expected,body)

    def test_renewal_none_wait_remind_and_packet_outcomes(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);creds=root/'creds';creds.write_text('{}')
            registry=root/'registry';registry.write_text('{"projects":{}}')
            with patch.object(renewal,'CREDS',creds),patch.object(renewal,'REGISTRY',registry), \
                 patch.object(renewal,'STATE',root/'state'),patch.object(renewal,'providers_here',return_value=[]), \
                 patch.object(renewal,'served_models',return_value={}),patch.object(renewal,'log'), \
                 patch.object(renewal.time,'time',return_value=100):
                cases=[('none',None,0,0,[],None,True),('wait',200,0,0,[],None,True),
                       ('remind',200,0,0,[],'sent',True),('remind',99,0,0,[],'sent',False),
                       ('remind',200,0,0,[],None,False),('run',200,3,0,[],'sent',True),
                       ('run',200,0,0,[],'sent',False),('run',99,3,0,[],'sent',False),
                       ('run',200,3,1,[],'sent',False),('run',200,3,0,['missing'],'sent',False),
                       ('run',200,3,0,[],'FAILED: network',False),('run',200,3,0,[],None,False)]
                for action,expiry,count,bad,missing,delivery,expected in cases:
                    with self.subTest(action=action,expiry=expiry,count=count,delivery=delivery,bad=bad,missing=missing), \
                         patch.object(renewal,'decide',return_value=action),patch.object(renewal,'earliest_expiry',return_value=expiry), \
                         patch.object(renewal,'notify',return_value=delivery) as notify, \
                         patch.object(renewal,'run_all',return_value=(root,count,bad,missing)) as run:
                        self.assertEqual(renewal.main()[0],expected)
                        if action in ('none','wait'):notify.assert_not_called();run.assert_not_called()

if __name__=='__main__':unittest.main()
