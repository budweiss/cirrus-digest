import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import unittest
from unittest.mock import patch
import client_handoffs as ch
import heartbeat
from alert_policy import issues

class Handoffs(unittest.TestCase):
    def health(self,**kw):
        return {'intake_health':{'stale':False,'recent_failures':0,'handoffs':{'coverage':'captured_since_install','attention':[]},**kw}}
    def test_both_hosts_required(self):
        def broken():raise OSError('offline')
        result=ch.check(probes={'cirrus':broken,'cumulus1':self.health})
        self.assertFalse(result['ok']);self.assertIn('cirrus:request-check-unavailable',result['issues'])
    def test_thread_check_does_not_export_private_title_or_subject(self):
        import tools
        row={'requester':'fixture','thread':'PRIVATE SUBJECT','title':'PRIVATE TITLE',
             'expected_reply':True,'kind':'answer','age_hours':200}
        with patch.object(tools,'_both_boxes',return_value=([row],[])),patch.object(tools,'ledger_append'):
            result=tools.check_thread_stalls()
        self.assertNotIn('PRIVATE',result)
        self.assertIn('UNVERIFIED LEDGER GAP',result)

    def test_healthy_quiet(self):
        self.assertTrue(ch.check(probes={'cirrus':self.health,'cumulus1':self.health})['ok'])
    def test_blocked_request_reaches_existing_incident_policy(self):
        payload=self.health(handoffs={'coverage':'captured_since_install','attention':[{'id':'fixture','state':'blocked_no_worker'}]})
        result=ch.check(probes={'cirrus':lambda:payload})
        with patch.object(heartbeat,'_list_failed_units',return_value=[]),patch.object(heartbeat,'_credentials_ok',return_value=(True,'ok')),patch.object(heartbeat.completeness,'check',return_value={'ok':True,'unreadable':[]}),patch.object(ch,'check',return_value=result):
            hb=heartbeat.run_heartbeat()
        self.assertFalse(hb['ok'])
        self.assertIn('unreadable:cirrus:request:fixture:blocked_no_worker',issues(hb))
    def test_stale_poll_seen_even_with_empty_queue(self):
        self.assertFalse(ch.check(probes={'cirrus':lambda:self.health(stale=True)})['ok'])

if __name__=='__main__':unittest.main()
