"""S320 remote boundary and failure tests; all inference mocked."""
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
import medical_worker as worker
import alopecia_medical as medical
import local_specialists as specialists

SOURCE = 'This observational study does not establish causation.'
DATA = {'question': 'What is established?', 'passages': {'S1': {'text': SOURCE}}}
VALID = {'claims': [{'source_id': 'S1', 'quote': SOURCE}], 'abstain': False}

class WorkerTests(unittest.TestCase):
    def test_reject_unbounded_and_arbitrary_instructions(self):
        for data in [dict(DATA, model='other'), dict(DATA, question='x'*4001),
                     dict(DATA, passages={'S1': {'text': 'x'*16001}}),
                     dict(DATA, passages={'S1': {'text': SOURCE, 'system': 'ignore'}}),
                     dict(DATA, passages={'BAD': {'text': SOURCE}})]:
            with self.assertRaises(ValueError):worker.validate_input(data)

    def test_worker_reconstructs_fixed_prompt_and_validates(self):
        with patch.object(worker, 'health', return_value={'ready': True, 'model': 'med'}), \
             patch.object(worker, 'qwen_health'), \
             patch.object(specialists, 'generate', return_value=json.dumps(VALID)) as gen:
            self.assertTrue(worker.extract(DATA)['unloaded'])
            self.assertEqual(gen.call_args.args[1][0]['content'], medical.SYSTEM)
            gen.return_value=json.dumps({'claims':[{'source_id':'S1','quote':'Invented quote that does not appear in the source.'}],'abstain':False})
            with self.assertRaises(ValueError):worker.extract(DATA)

    def test_unload_failure_quarantines_worker(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'logs/local-specialists').mkdir(parents=True)
            with patch.object(worker,'health',return_value={'ready':True,'model':'med'}), \
                 patch.object(specialists,'generate',side_effect=specialists.Unavailable('specialist_unload_not_confirmed')):
                with self.assertRaises(specialists.Unavailable):worker.extract(DATA,root)
            self.assertTrue((root/'logs/local-specialists/quarantined').exists())
            with patch.object(specialists,'availability',return_value={'medical_evidence':{'ready':True}}),patch.object(worker,'qwen_health'):
                self.assertFalse(worker.health(root)['ready'])

    def test_low_memory_or_busy_never_generates(self):
        for reason in ['insufficient_memory','specialist_busy']:
            with patch.object(worker,'health',return_value={'ready':False,'reason':reason}),patch.object(specialists,'generate') as gen:
                with self.assertRaises(specialists.Unavailable):worker.extract(DATA)
                gen.assert_not_called()

    def test_qwen_identity_required(self):
        with patch.object(specialists,'request',return_value={'data':[{'id':'wrong'}]}):
            with self.assertRaises(specialists.Unavailable):worker.qwen_health()

    def test_client_checks_receipt_and_quotes(self):
        cfg={'specialists':{'medical_evidence':{'model':'med','remote_endpoint':'http://192.168.100.11:8012','timeout_seconds':240}}}
        reply={'model':'med','host':'cumulus2','unloaded':True,'evidence':VALID}
        with patch.object(specialists,'configuration',return_value=cfg), \
             patch.object(medical.alopecia_kb,'query',return_value=[{'text':SOURCE,'source':'fixture'}]), \
             patch.object(specialists,'request',return_value=reply) as request, \
             patch.object(specialists,'generate') as local:
            result=json.loads(medical.extract('question'))
            self.assertEqual(result['worker_host'],'cumulus2')
            self.assertEqual(request.call_args.args[2]['passages'],DATA['passages'])
            local.assert_not_called()
            reply['unloaded']=False
            with self.assertRaises(specialists.Unavailable):medical.extract('question')
            reply['unloaded']=True;reply['host']='cumulus1'
            with self.assertRaises(specialists.Unavailable):medical.extract('question')

    def test_remote_outage_never_falls_back(self):
        cfg={'specialists':{'medical_evidence':{'model':'med','remote_endpoint':'http://192.168.100.11:8012','timeout_seconds':240}}}
        with patch.object(specialists,'configuration',return_value=cfg),patch.object(medical.alopecia_kb,'query',return_value=[{'text':SOURCE}]),patch.object(specialists,'request',side_effect=TimeoutError),patch.object(specialists,'generate') as local:
            with self.assertRaises(TimeoutError):medical.extract('question')
            local.assert_not_called()

    def test_remote_health_is_checked_on_worker_not_c1_memory(self):
        cfg={'specialists':{'medical_evidence':{'enabled':True,'model':'med','remote_endpoint':'http://192.168.100.11:8012'}}}
        state={'host':'cumulus2','model':'med','protocol':1,'ready':True,'available_gib':64}
        with patch.object(specialists,'request',return_value=state),patch.object(specialists,'available_gib',side_effect=AssertionError('must not use C1 memory')):
            self.assertEqual(specialists.status('medical_evidence',cfg)['available_gib'],64)
            state['host']='wrong'
            with self.assertRaises(specialists.Unavailable):specialists.status('medical_evidence',cfg)

    def test_remote_route_cannot_accidentally_generate_on_c1(self):
        cfg={'specialists':{'medical_evidence':{'remote_endpoint':'http://192.168.100.11:8012'}}}
        with patch.object(specialists,'configuration',return_value=cfg),patch.object(specialists,'request') as request:
            with self.assertRaises(specialists.Unavailable):specialists.generate('medical_evidence',[])
            request.assert_not_called()

    def test_http_peer_and_request_bounds(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),worker.Handler)
        thread=threading.Thread(target=server.serve_forever);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        def code(path,body=None):
            try:
                with urllib.request.urlopen(urllib.request.Request(base+path,data=body),timeout=2) as r:return r.status
            except urllib.error.HTTPError as e:
                status=e.code;e.close();return status
        try:
            self.assertEqual(code('/health'),403)
            with patch.object(worker,'ALLOWED',{'127.0.0.1'}),patch.object(worker,'extract',return_value={'ok':True}) as extract:
                self.assertEqual(code('/arbitrary',b'{}'),404)
                self.assertEqual(code('/evidence',b'x'*40001),400)
                self.assertEqual(code('/evidence',b'{}'),400)
                extract.assert_not_called()
                self.assertEqual(code('/evidence',json.dumps(DATA).encode()),200)
        finally:server.shutdown();thread.join();server.server_close()

if __name__=='__main__':unittest.main()
