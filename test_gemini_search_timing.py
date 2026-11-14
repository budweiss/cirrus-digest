"""Exercise the real search function in isolation with synthetic HTTP; no IO."""
import ast
import contextlib
from pathlib import Path
import types
import unittest
from unittest.mock import Mock, patch

class GeminiSearchTiming(unittest.TestCase):
    def run_search(self, record_error=False, http_error=False):
        tree=ast.parse(Path(__file__).with_name('cirrus_daily.py').read_text())
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='gemini_search')
        recorder=Mock(side_effect=RuntimeError('synthetic') if record_error else None)
        response=Mock()
        response.raise_for_status.side_effect=RuntimeError('synthetic') if http_error else None
        response.json.return_value={'usageMetadata':{'promptTokenCount':12,'candidatesTokenCount':3},
            'candidates':[{'groundingMetadata':{'groundingChunks':[{'web':{'uri':'https://example.invalid/result'}}]}}]}
        post=Mock(return_value=response)
        env={'_load_gemini_key':lambda:'synthetic','_load_creds':lambda:{},'_count_search':Mock(),
             '_GEMINI_MODEL':'synthetic-model','hard_deadline':lambda *a:contextlib.nullcontext(),
             'requests':types.SimpleNamespace(post=post),'_LB':types.SimpleNamespace(record_call=recorder),'log':Mock()}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'<actual-search-function>','exec'),env)
        with patch('time.monotonic',side_effect=[10.,12.5]):result=env['gemini_search']('public synthetic query')
        return result,recorder,post
    def test_actual_writer_records_elapsed_and_usage_without_changing_search(self):
        result,recorder,post=self.run_search()
        self.assertEqual(result,['https://example.invalid/result'])
        self.assertEqual(recorder.call_args.kwargs,dict(in_tok=12,out_tok=3,task='cirrus_daily:gemini_search',wall_seconds=2.5))
        self.assertEqual(post.call_args.kwargs['json']['tools'],[{'google_search':{}}])
        self.assertEqual(post.call_args.kwargs['timeout'],30)
    def test_recording_failure_preserves_results(self):
        self.assertEqual(self.run_search(record_error=True)[0],['https://example.invalid/result'])
    def test_http_failure_does_not_invent_usage(self):
        result,recorder,_=self.run_search(http_error=True)
        self.assertEqual(result,[]);recorder.assert_not_called()
if __name__=='__main__':unittest.main()
