"""S340: isolated regression cases for monitoring and durable decisions."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import opus_approval as approval
import completeness
import tools
from alert_policy import IncidentPolicy
from test_alert_policy import hb


class Corrections(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        for key,value in [('STATE_DIR',self.root),('REQUEST_FILE',self.root/'request.json'),
                          ('UPDATE_OFFSET_FILE',self.root/'offset'),('LAST_REQUEST_PATH',None)]:
            p=patch.object(approval,key,value);p.start();self.addCleanup(p.stop)

    def test_two_decisions_and_out_of_order_reply(self):
        for key in ['old','new']:
            approval.create_guidance_request(key,'what next?',key);approval.record_request_delivery(True)
        rows=approval.requests();self.assertEqual(len(rows),2)
        first=rows[0][1]; other=rows[1][1]
        self.assertTrue(approval.answer(first['request_id'],'inspect',at=first['requested_at']+1))
        self.assertEqual(json.loads(rows[1][0].read_text())['status'],'pending')
        self.assertFalse(approval.answer(first['request_id'],'again'))
        self.assertIn('inspect',approval.consume_guidance());self.assertIsNone(approval.consume_guidance())
        self.assertFalse(approval.answer(other['request_id'],'old message',at=other['requested_at']-1))

    def test_legacy_preserved_and_bare_reply_not_applied_to_new_request(self):
        old={'kind':'guidance','issue':'old','requested_at':100,'status':'pending'}
        (self.root/'request.json').write_text(json.dumps(old))  # the temp path setUp patched into REQUEST_FILE
        with patch.object(approval.time,'time',return_value=101):
            approval.create_guidance_request('new','next?','new')
        self.assertEqual(json.loads(approval.REQUEST_FILE.read_text()),old)
        self.assertTrue(approval.answer('100','keep investigating',at=102))
        self.assertEqual(approval.requests()[1][1]['status'],'pending')

    def test_failed_delivery_retry_is_bounded_and_keeps_id(self):
        with patch.object(approval.time,'time',return_value=100):
            approval.create_guidance_request('issue','?','one');approval.record_request_delivery(False)
            self.assertIsNone(approval.create_guidance_request('issue','?','one'))
        rid=approval.requests()[0][1]['request_id']
        with patch.object(approval.time,'time',return_value=21701):
            self.assertIn(rid,approval.create_guidance_request('issue','?','one'))
            approval.record_request_delivery(True)
        self.assertEqual(len(approval.requests()),1)
        self.assertIsNone(approval.create_guidance_request('same','reworded','one'))

    def test_telegram_id_routing_ignores_bare_and_stale_replies(self):
        with patch.object(approval.time,'time',return_value=100):
            approval.create_guidance_request('one','?','one')
            approval.create_guidance_request('two','?','two')
        rows=approval.requests();rid=rows[0][1]['request_id']
        messages=[('bare reply',101), (rid+' too early',99), (rid+' investigate',101)]
        updates={'result':[{'update_id':i,'message':{'from':{'id':7},'text':text,'date':date}}
                           for i,(text,date) in enumerate(messages)]}
        with patch.object(approval.time,'time',return_value=102), \
             patch.object(approval,'_load_secrets',return_value={'telegram_bot_token':'fake','telegram_user_id':7}), \
             patch.object(approval,'_api_call',return_value=updates):
            approval.check_for_reply()
        self.assertEqual(json.loads(rows[0][0].read_text())['reply'],'investigate')
        self.assertEqual(json.loads(rows[1][0].read_text())['status'],'pending')
        self.assertEqual(approval.UPDATE_OFFSET_FILE.read_text(),'3')

    def test_automatic_retry_once_and_no_real_send(self):
        with patch.object(approval.time,'time',return_value=100):
            approval.create_guidance_request('one','?','one');approval.record_request_delivery(False)
        from unittest.mock import Mock
        sender=Mock(return_value='sent')
        with patch.object(approval.time,'time',return_value=200): approval.retry_failed_deliveries(sender)
        sender.assert_not_called()
        with patch.object(approval.time,'time',return_value=21701):
            approval.retry_failed_deliveries(sender);approval.retry_failed_deliveries(sender)
        sender.assert_called_once();self.assertEqual(approval.requests()[0][1]['status'],'pending')

    def test_concurrent_requests_and_interrupted_atomic_write(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i:approval.create_guidance_request(str(i),'?',str(i)),range(12)))
        rows=approval.requests();self.assertEqual(len(rows),12)
        self.assertEqual(len({r['request_id'] for _,r in rows}),12)
        path,original=rows[0]
        with patch.object(approval.os,'replace',side_effect=OSError('simulated interruption')):
            with self.assertRaises(OSError): approval._write_json(path,{'status':'lost'})
        self.assertEqual(json.loads(path.read_text()),original)
        self.assertEqual(len(list(self.root.glob('.request-*'))),0)

    def test_corruption_never_deletes_request(self):
        (self.root/'request.json').write_text('{bad')  # the temp path setUp patched into REQUEST_FILE
        with self.assertRaises(ValueError): approval.check_for_reply()
        self.assertEqual(approval.REQUEST_FILE.read_text(),'{bad')

    def test_queue_full_preserves_all_decisions(self):
        for i in range(32): approval.create_guidance_request(str(i),'?',str(i))
        with self.assertRaises(ValueError): approval.create_guidance_request('overflow','?','overflow')
        self.assertEqual(len(approval.requests()),32)

    def test_review_does_not_close_and_recovery_has_evidence(self):
        p=IncidentPolicy(self.root/'incidents.json');p.observe(hb(['bad.service']),100)
        p.complete(True,101)
        row=p.summary(102)[0];self.assertEqual(row['state'],'action pending')
        self.assertEqual(row['owner'],'Cowork')
        p=IncidentPolicy(p.path);p.observe(hb(),160)
        self.assertEqual(p.summary(160)[0]['state'],'verifying')
        p.observe(hb(['bad.service']),200);self.assertEqual(len(p.active),1)
        p.observe(hb(),260);p.observe(hb(),380)
        self.assertEqual(p.active,{});self.assertEqual(p.resolved[-1]['state'],'resolved')
        self.assertIn('healthy probes',p.resolved[-1]['recovery_evidence'])

    def test_legacy_review_gets_disposition_without_paid_retry(self):
        path=self.root/'incidents.json'
        path.write_text(json.dumps({'incidents':{'unit:bad.service':{'first_seen':100,'reviewed':True,'next_attempt':0}}}))
        p=IncidentPolicy(path);self.assertEqual(p.observe(hb(['bad.service']),1000),[])
        row=p.summary(1000)[0]
        self.assertEqual(row['state'],'action pending');self.assertEqual(row['owner'],'Cowork')
        self.assertFalse(row['disposition_overdue']);self.assertTrue(row['disposition'])

    def test_failed_model_keeps_monitor_and_backoff(self):
        p=IncidentPolicy(self.root/'incidents.json');p.observe(hb(['bad.service']),100);p.complete(False,101)
        self.assertEqual(p.observe(hb(['bad.service']),200),[])
        self.assertTrue(p.summary(1100)[0]['owner'])

    def test_actual_fleet_aliases_use_fixed_feed_and_no_systemctl(self):
        with patch.object(completeness,'supervisor_feed',return_value={'jobs':{'fleetcontroller':{'ok':True,'epoch':approval.time.time()}}}), \
             patch.object(tools,'ledger_append'),patch.object(tools,'_run') as run:
            for name in tools.FLEET_ALIASES:
                result=json.loads(tools.check_service_status(name))
                self.assertTrue(result['ok']);self.assertEqual(result['scope'],'user')
            run.assert_not_called()
        with patch.object(completeness,'supervisor_feed',return_value=None),patch.object(tools,'ledger_append'):
            self.assertFalse(json.loads(tools.check_service_status('fleetcontroller'))['ok'])

    def test_session_ticket_owner_is_cowork_not_builder(self):
        from types import SimpleNamespace
        result=SimpleNamespace(returncode=0,stdout=json.dumps({'id':'example-ticket','status':'session','tier':1}),stderr='')
        with patch.object(tools.subprocess,'run',return_value=result),patch.object(tools,'ledger_append'), \
             patch.object(tools,'INCIDENT_ACTIONS',[]):
            res = tools.file_repair_ticket('cirrus-billsnow.service','diagnosed failure with sufficient evidence and an existing ticket')
            self.assertFalse(str(res).startswith('FAILED'), res)
            self.assertEqual(tools.INCIDENT_ACTIONS[-1]['owner'],'Cowork')
            self.assertEqual(tools.INCIDENT_ACTIONS[-1]['ticket_id'],'example-ticket')

    def test_fleet_diagnostics_reject_bad_and_stale_feed(self):
        rows=[None,[],{'jobs':[]},{'jobs':{'fleetcontroller':[]}},
              {'jobs':{'fleetcontroller':{'ok':True,'epoch':800}}},
              {'jobs':{'fleetcontroller':{'ok':True,'epoch':1001}}},
              {'jobs':{'fleetcontroller':{'ok':'false','epoch':999}}}]
        with patch.object(tools.time,'time',return_value=1000),patch.object(tools,'ledger_append'):
            for feed in rows:
                with self.subTest(feed=feed),patch.object(completeness,'supervisor_feed',return_value=feed):
                    out=json.loads(tools.check_service_status('fleet-controller.service'))
                    self.assertFalse(out['ok']);self.assertEqual(out['scope'],'user')

    def test_new_coverage_is_explicit(self):
        keys=['foundationrenewalcumulus','foundationrenewal','immaculatewednesdayreport']
        self.assertEqual(completeness.unmonitored_jobs(dict.fromkeys(keys,{})),[])
        self.assertEqual(completeness.unmonitored_jobs({'unknown':{}}),['unknown'])


if __name__=='__main__': unittest.main()
