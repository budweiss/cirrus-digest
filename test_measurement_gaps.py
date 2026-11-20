"""S664: synthetic calls only; extracted bot functions cannot start Telegram."""
import ast, io, json, tempfile, types, unittest
from pathlib import Path
from unittest.mock import Mock, patch
import llm_budget as B
import native_observation as N
import request_telemetry as R
import intake_lifecycle as I

class MeasurementTests(unittest.TestCase):
 def test_bot_selftest_suppresses_new_side_effects(self):
  tree=ast.parse(Path(__file__).with_name('cirrus_bot.py').read_text())
  fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='selftest')
  self.assertTrue(any(isinstance(d,ast.Attribute) and d.attr=='no_recording' for d in fn.decorator_list))
  with patch.object(R,'emit') as emit:
   @R.no_recording
   @R.measured
   def demo():R.mark('ready_harness')
   demo();emit.assert_not_called()
 def test_bot_actual_helpers_preserve_wire_and_time(self):
  source=ast.parse(Path(__file__).with_name('cirrus_bot.py').read_text())
  names={'_ledger','call_gemini','call_grok','call_claude'}
  module=ast.Module(body=[n for n in source.body if isinstance(n,ast.FunctionDef) and n.name in names],type_ignores=[])
  for func,provider,response in [('call_gemini','gemini',{'usageMetadata':{'promptTokenCount':10,'candidatesTokenCount':2},'candidates':[{'content':{'parts':[{'text':'answer'}]}}]}),('call_grok','grok',{'usage':{'prompt_tokens':10,'completion_tokens':2},'choices':[{'message':{'content':'answer'}}]}),('call_claude','anthropic',{'usage':{'input_tokens':10,'output_tokens':2},'content':[{'text':'answer'}]})]:
   b=Mock();b.response_metrics=B.response_metrics;resp=Mock();resp.json.return_value=response;requests=Mock();requests.post.return_value=resp
   env=dict(_B=b,CREDS={},requests=requests,time=types.SimpleNamespace(monotonic=Mock(side_effect=[1,3])),request_telemetry=Mock(),knowledge_freshness=Mock())
   for p in ['GEMINI','GROK','ANTHROPIC']:env[p+'_API_KEY']='synthetic'
   for p in ['GEMINI','GROK','CLAUDE']:env[p+'_MODEL']='synthetic-model'
   exec(compile(module,'bot-functions','exec'),env)
   self.assertEqual(env[func]('synthetic'), 'answer')
   self.assertEqual(b.record_call.call_count,1)
   self.assertEqual(b.record_call.call_args.kwargs['wall_seconds'],2)
   self.assertEqual(b.record_call.call_args.kwargs['in_tok'],10)
 def test_native_observation_failure_is_unknown(self):
  with patch.object(N.urllib.request,'build_opener',side_effect=OSError),patch.object(N.subprocess,'run',side_effect=OSError):self.assertEqual(N.before('gpt-oss:120b'),{})
 def test_native_observation_projection(self):
  op=Mock();op.open.return_value=io.BytesIO(json.dumps({'models':[{'name':'gpt-oss:120b','context_length':65536,'private':'SECRET'}]}).encode())
  with patch.object(N.urllib.request,'build_opener',return_value=op),patch.object(N.subprocess,'run',return_value=Mock(returncode=0,stdout='1000000')),patch.object(N.time,'monotonic',return_value=20):
   self.assertEqual(N.before('gpt-oss:120b'),dict(resident_before=True,resident_context_before=65536,runtime_uptime_seconds=19))
 def test_request_lifecycle_no_contents_or_false_delivery(self):
  with tempfile.TemporaryDirectory() as td,patch.object(R,'PATH',Path(td)/'events'):
   @R.measured
   def answer(secret):R.mark('ready_harness');return secret
   self.assertEqual(answer('PRIVATE'),'PRIVATE')
   rows=[json.loads(x) for x in R.PATH.read_text().splitlines()]
   self.assertEqual(rows[0]['request'],rows[1]['request']);self.assertEqual(rows[1]['ready_harness'],1)
   self.assertNotIn('PRIVATE',R.PATH.read_text());self.assertNotIn('delivered_at',rows[1])
 def test_intake_first_milestone_survives_updates(self):
  with tempfile.TemporaryDirectory() as td:
   row=(1,'synthetic@example.test','x','x','synthetic');I.capture(td,row)
   with patch.object(I.time,'time',return_value=100):I.update(td,row,'ready_for_send','test','ready',{'artifact':'synthetic'})
   with patch.object(I.time,'time',return_value=200):I.update(td,row,'ready_for_send','test','still ready',{'artifact':'synthetic'})
   with I.connect(td) as db: evidence=json.loads(db.execute('SELECT evidence FROM requests').fetchone()[0])
   self.assertEqual(evidence['measurement_timing']['ready_at'],100)
if __name__=='__main__':unittest.main()
