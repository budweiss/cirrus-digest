import json, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch, Mock
import daily_context_trial as t
import pedagogy_daily as p

class Trial(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'config').mkdir()
        self.cfg={'trial':'s674','enabled':True,'starts_at':time.time()-60,'ends_at':time.time()+86400}
        self.save()
        for target,value in [('daily_context_trial.socket.gethostname','cumulus1'),('daily_context_trial.memory',48)]:
            q=patch(target,return_value=value);q.start();self.addCleanup(q.stop)
    def save(self): (self.root/'config'/t.CONTROL).write_text(json.dumps(self.cfg))
    def select(self,host='http://127.0.0.1:11434',model='gpt-oss:120b'):return t.select(self.root,host,model)
    def good(self,**changes):
        d={'response':'private answer','prompt_eval_count':520,'eval_count':800,'load_duration':1000000,'done_reason':'stop'};d.update(changes);return d
    def test_scope_and_expiry(self):
        self.assertEqual(self.select(),65536)
        for host,model in [('http://remote:11434','gpt-oss:120b'),('http://localhost:8000','gpt-oss:120b'),('http://localhost:11434','other')]:self.assertEqual(self.select(host,model),8192)
        with patch.object(t.socket,'gethostname',return_value='cirrus'):self.assertEqual(self.select(),8192)
        self.cfg['ends_at']=time.time()-1;self.save();self.assertEqual(self.select(),8192)
    def test_rollback_latch_and_low_memory(self):
        with patch.object(t,'memory',return_value=31):self.assertEqual(self.select(),8192)
        self.assertTrue((self.root/'config'/t.STOP).exists());self.assertEqual(self.select(),8192)
    def test_unknown_control_and_memory_do_not_enable(self):
        with patch.object(t,'memory',side_effect=OSError()):self.assertEqual(self.select(),8192)
        (self.root/'config'/t.CONTROL).write_text('{}');self.assertEqual(self.select(),8192)
    def test_good_call_preserved_and_numeric_privacy(self):
        t.record(self.root,65536,self.good(),20,observation={'resident_before':True,'resident_context_before':65536,'private':'secret'})
        raw=(self.root/'out/daily-context-trial.jsonl').read_text();self.assertNotIn('private',raw);self.assertNotIn('secret',raw)
        row=json.loads(raw);self.assertFalse(row['rollback']);self.assertEqual(self.select(),65536)
    def test_each_detectable_regression_stops_subsequent_calls(self):
        cases=[dict(error=True),dict(data=self.good(response='')),dict(data=self.good(done_reason='length')),dict(data=self.good(load_duration=16000000000)),dict(elapsed=36)]
        for changes in cases:
            with self.subTest(changes=list(changes)):
                (self.root/'config'/t.STOP).unlink(missing_ok=True)
                t._STOPPED.discard(str(self.root))
                args={'data':self.good(),'elapsed':20};args.update(changes);t.record(self.root,65536,**args)
                self.assertEqual(self.select(),8192)
    def test_monitor_write_failure_stops(self):
        with patch.object(t.os,'open',side_effect=OSError()):t.record(self.root,65536,self.good(),20)
        self.assertEqual(self.select(),8192)
    def test_inactive_calls_never_write(self):
        t.record(self.root,8192,self.good(),20);self.assertFalse((self.root/'out').exists())
    def retained(self):
        self.cfg.update(mode='retained', retained_from=time.time()-60,
                        starts_at=time.time()-10*86400, ends_at=time.time()-7*86400)
        self.save()
    def test_retained_policy_survives_trial_expiry_with_exact_scope(self):
        self.retained(); self.assertEqual(self.select(),65536)
        with patch.object(t.socket,'gethostname',return_value='cirrus'):
            self.assertEqual(self.select(),8192)
        self.assertEqual(self.select(model='other'),8192)
        self.assertEqual(self.select(host='http://remote:11434'),8192)
        self.cfg['enabled']=False;self.save();self.assertEqual(self.select(),8192)
    def test_retained_metadata_fails_closed(self):
        self.retained()
        for value in [None,True,-1,float('nan'),time.time()+86400]:
            self.cfg['retained_from']=value;self.save();self.assertEqual(self.select(),8192)
        self.cfg.update(mode='unknown',retained_from=time.time()-60)
        self.save();self.assertEqual(self.select(),8192)
    def test_retained_safeguards_still_latch(self):
        cases=[dict(error=True),dict(data=self.good(response='')),
               dict(data=self.good(done_reason='length')),
               dict(data=self.good(load_duration=16000000000)),dict(elapsed=36)]
        for changes in cases:
            with self.subTest(changes=list(changes)),tempfile.TemporaryDirectory() as td:
                root=Path(td);(root/'config').mkdir()
                cfg=dict(self.cfg,mode='retained',retained_from=time.time()-60)
                (root/'config'/t.CONTROL).write_text(json.dumps(cfg))
                args={'data':self.good(),'elapsed':20};args.update(changes)
                t.record(root,65536,**args)
                self.assertEqual(t.select(root,'http://localhost:11434','gpt-oss:120b'),8192)
                self.assertTrue((root/'config'/t.STOP).exists())
    def test_retained_memory_monitor_and_existing_stop(self):
        self.retained()
        with patch.object(t,'memory',return_value=31):self.assertEqual(self.select(),8192)
        self.assertEqual(self.select(),8192)
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'config').mkdir()
            (root/'config'/t.CONTROL).write_text(json.dumps(self.cfg))
            with patch.object(t.os,'open',side_effect=OSError()):
                self.assertEqual(t.select(root,'http://localhost:11434','gpt-oss:120b'),8192)
            # os.open also prevents writing the stop file; the process latch must hold.
            self.assertIn(str(root),t._STOPPED)
            self.assertEqual(t.select(root,'http://localhost:11434','gpt-oss:120b'),8192)
    def test_real_caller_wire_and_accounting(self):
        response=Mock();response.json.return_value=self.good()
        with patch.object(p.requests,'post',return_value=response) as wire, patch('native_observation.before',return_value={}), patch.object(t,'select',return_value=65536),patch.object(t,'record') as record,patch.object(p,'load_json',return_value={}),patch('llm_budget.record_call') as ledger:
            answer=p.ollama('synthetic',{'digest':{'ollama_model':'gpt-oss:120b'}})
        self.assertEqual(answer,'private answer');self.assertEqual(wire.call_args.kwargs['json']['options'],{'num_ctx':65536})
        self.assertEqual(ledger.call_args.kwargs['num_ctx'],65536);record.assert_called_once()
    def test_actual_caller_error_records_without_retry(self):
        with patch.object(p.requests,'post',side_effect=RuntimeError('private')) as wire,patch('native_observation.before',return_value={}),patch.object(t,'select',return_value=65536),patch.object(t,'record') as record:
            self.assertEqual(p.ollama('synthetic',{}),'[Summarization error: RuntimeError]')
        wire.assert_called_once();self.assertTrue(record.call_args.kwargs['error'])

if __name__=='__main__':unittest.main()
