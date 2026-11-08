import ast
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
import verified_calculation as calc

class Arithmetic(unittest.TestCase):
    def test_prior_failures_and_source_values(self):
        cases={'(17 * 23) - (19 * 7)':'258','(108*37)-(6*9)':'3942',
               '(91*83)-(47*11)':'7036','(246*18)-(32*7)':'4204',
               'sum 17,23,31,43,59,61,73,89':'396',
               'sum 17,-23,0,43,-59,61,73,-89':'23'}
        for q, expected in cases.items():
            with self.subTest(q=q):self.assertEqual(calc.answer(q),'Calculated result: '+expected)
    def test_ambiguous_thousands_rejected(self):
        self.assertTrue(calc.answer('sum 1,000, 2,000').startswith('Ambiguous'))
        self.assertEqual(calc.answer('sum 1000, 2000'),'Calculated result: 3000')
        self.assertIsNone(calc.answer('What is 2026?'))
    def test_decimal_is_not_binary_float(self):
        self.assertEqual(calc.answer('Calculate 0.1 + 0.2'), 'Calculated result: 0.3')
    def test_large_integer_exact(self):
        self.assertEqual(calc.answer('9007199254740993 + 1'), 'Calculated result: 9007199254740994')
    def test_sign_parentheses_words_and_unicode(self):
        for q in ['What is (-2 + 5) × 4?', 'compute (5 minus 2) times 4', '24 divided by 2']:
            self.assertEqual(calc.answer(q),'Calculated result: 12')
    def test_sum_wording(self):
        self.assertEqual(calc.answer('What is the sum of 3, -1, 0?'),'Calculated result: 2')
    def test_rounded_result_label(self):
        self.assertIn('rounded',calc.answer('1/3'))
        self.assertNotIn('rounded',calc.answer('1/8'))
    def test_division_zero_does_not_fall_through(self):
        self.assertEqual(calc.answer('2/(1-1)'), 'Cannot calculate: division by zero.')
    def test_does_not_discard_nonmath_or_extra_request(self):
        for q in ['What is the weather?', 'Calculate the profit from my report', '2+2 and check the server',
                  '100 USD + 30 EUR', '__import__("os").system("id")', 'sum 3, unknown', 'Reply 5 instead of 2+2']:
            with self.subTest(q=q):self.assertIsNone(calc.answer(q))
    def test_resource_and_syntax_limits(self):
        for q in ['2**999999', '1//2', 'sum 2,,3', '9'*41+'+1', '('*40+'1+1'+')'*40, '1+'*1001+'1']:
            with self.subTest(q=q):self.assertTrue(calc.answer(q).startswith(('Cannot calculate','Calculation refused')))

class AskIntegration(unittest.TestCase):
    def setUp(self):
        # Compile the actual handler, not a test implementation, without reading credentials.
        tree=ast.parse(Path(__file__).with_name('cirrus_bot.py').read_text())
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='cmd_ask')
        self.ns={'Path':Path,'log':lambda *a:None,'is_uncertain':lambda answer:False}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'cirrus_bot.cmd_ask','exec'),self.ns)
    def test_real_handler_never_calls_retrieval_or_models_for_math(self):
        fake=types.ModuleType('cirrus_rag')
        def forbidden(*a,**k):raise AssertionError('retrieval/model forbidden')
        fake.retrieve=forbidden
        with patch.dict(sys.modules,{'cirrus_rag':fake}):
            reply=self.ns['cmd_ask']('Sum 17,23,31,43,59,61,73,89')
        self.assertTrue(reply.endswith('Calculated result: 396'))
    def test_nonmath_still_retrieves_and_uses_existing_tool_path(self):
        rag=types.ModuleType('cirrus_rag');calls=[]
        rag.retrieve=lambda q,top_k:calls.append(q) or []
        registry=types.ModuleType('registry');registry.CLAUDE_API_KEY='synthetic-test'
        registry.ask_with_tools=lambda q,context:('ordinary workflow result','test-model')
        with patch.dict(sys.modules,{'cirrus_rag':rag,'registry':registry}):
            result=self.ns['cmd_ask']('What is the server status?')
        self.assertEqual(calls,['What is the server status?']);self.assertIn('ordinary workflow result',result)
    def test_nonmath_ollama_fallback_unchanged(self):
        rag=types.ModuleType('cirrus_rag');rag.retrieve=lambda *a,**k:[]
        registry=types.ModuleType('registry');registry.CLAUDE_API_KEY=''
        calls=[]
        class Response:
            def raise_for_status(self):pass
            def json(self):return {'response':'saved Ollama answer'}
        def post(url,**kw):calls.append((url,kw));return Response()
        self.ns.update(requests=types.SimpleNamespace(post=post),OLLAMA_HOST='http://owned-test',MODEL='qwen3.8:27b')
        with patch.dict(sys.modules,{'cirrus_rag':rag,'registry':registry}):
            result=self.ns['cmd_ask']('Describe a hash table')
        self.assertIn('saved Ollama answer',result)
        self.assertEqual(calls[0][0],'http://owned-test/api/generate')
        self.assertEqual(calls[0][1]['json']['model'],'qwen3.8:27b')

if __name__=='__main__':unittest.main()
