import unittest
from unittest.mock import Mock, patch
import pedagogy_daily as p

class Accounting(unittest.TestCase):
    def setUp(self):
        # Existing tests must never probe a live runtime through the new hook.
        observer_patch = patch('native_observation.before', return_value={})
        self.observer = observer_patch.start()
        self.addCleanup(observer_patch.stop)

    def call(self, data, record=None):
        response = Mock()
        response.json.return_value = data
        with patch.object(p.requests, 'post', return_value=response), patch.object(p, 'load_json', return_value={}), patch.object(p, 'log') as log, patch('llm_budget.record_call', return_value=record or {'cost': 0}) as ledger:
            result = p.ollama('private prompt', {})
        return result, ledger, log

    def test_real_tokens_and_identity_without_content(self):
        result, ledger, _ = self.call({'model': 'actual-model', 'response': ' answer ', 'prompt_eval_count': 17, 'eval_count': 4})
        self.assertEqual(result, 'answer')
        ledger.assert_called_once()
        args, kw = ledger.call_args
        self.assertEqual(args[1:3], ('ollama', 'actual-model'))
        self.assertEqual((kw['in_tok'], kw['out_tok']), (17, 4))
        self.assertNotIn('private prompt', str(ledger.call_args))

    def test_invalid_tokens_use_estimate(self):
        _, ledger, _ = self.call({'response': '', 'prompt_eval_count': True, 'eval_count': -1})
        self.assertIsNone(ledger.call_args.kwargs['in_tok'])
        self.assertIsNone(ledger.call_args.kwargs['out_tok'])

    def test_accounting_failure_preserves_answer(self):
        response = Mock()
        response.json.return_value = {'response': 'answer'}
        with patch.object(p.requests, 'post', return_value=response), patch.object(p, 'load_json', return_value={}), patch.object(p, 'log') as log, patch('llm_budget.record_call', side_effect=RuntimeError('private')):
            self.assertEqual(p.ollama('prompt', {}), 'answer')
            log.assert_called_once_with('local usage accounting unavailable')

    def test_http_failure_no_fabricated_usage_or_private_error(self):
        with patch.object(p.requests, 'post', side_effect=RuntimeError('private URL')), patch('llm_budget.record_call') as ledger:
            self.assertEqual(p.ollama('prompt', {}), '[Summarization error: RuntimeError]')
            ledger.assert_not_called()

    def test_observation_precedes_unchanged_request_and_reaches_ledger(self):
        order = []
        observation = dict(resident_before=True, resident_context_before=65536,
                           runtime_uptime_seconds=120)
        self.observer.side_effect = lambda model: order.append(('observe', model)) or observation
        response = Mock()
        response.json.return_value = dict(model='gpt-oss:120b', response=' answer ',
                                         prompt_eval_count=17, eval_count=4, load_duration=12000000000)
        def post(*args, **kwargs):
            order.append(('request', kwargs))
            return response
        cfg = {'digest': {'ollama_host':'http://127.0.0.1:11434', 'ollama_model':'unused'}}
        with patch.object(p.requests, 'post', side_effect=post) as wire, \
             patch.object(p, 'load_json', return_value={}), \
             patch.object(p.knowledge_freshness, 'instruction', return_value='existing instruction'), \
             patch.object(p.time, 'monotonic', side_effect=[10, 25]), \
             patch('llm_budget.record_call', return_value={'cost':0}) as ledger:
            self.assertEqual(p.ollama('synthetic', cfg, timeout=77, model='gpt-oss:120b'), 'answer')
        self.assertEqual(order[0], ('observe', 'gpt-oss:120b'))
        self.assertEqual(wire.call_count, 1)
        self.assertEqual(wire.call_args.args, ('http://127.0.0.1:11434/api/generate',))
        self.assertEqual(wire.call_args.kwargs, dict(json={'model':'gpt-oss:120b',
            'system':'existing instruction', 'prompt':'synthetic', 'stream':False,
            'options':{'num_ctx':8192}}, timeout=77))
        self.assertEqual(ledger.call_args.kwargs['native_observation'], observation)
        self.assertEqual(ledger.call_args.kwargs['wall_seconds'], 15)
        self.assertEqual(ledger.call_args.kwargs['load_seconds'], 12)
        self.assertEqual(ledger.call_args.kwargs['num_ctx'], 8192)

    def test_failed_observation_preserves_answer_and_unknown_fields(self):
        self.observer.side_effect = RuntimeError('private probe error')
        result, ledger, _ = self.call({'response':'answer'})
        self.assertEqual(result, 'answer')
        self.assertEqual(ledger.call_args.kwargs['native_observation'], {})

    def test_other_endpoints_never_borrow_local_residency(self):
        response = Mock(); response.json.return_value = {'response':'answer'}
        for endpoint in ('http://remote.invalid:11434', 'http://127.0.0.1:8000',
                         'https://localhost:11434', 'http://localhost:11434/proxy'):
            with self.subTest(endpoint=endpoint), patch.object(p.requests, 'post', return_value=response), \
                 patch.object(p, 'load_json', return_value={}), \
                 patch('llm_budget.record_call', return_value={'cost':0}) as ledger:
                self.assertEqual(p.ollama('synthetic', {'digest':{'ollama_host':endpoint}}), 'answer')
                self.assertEqual(ledger.call_args.kwargs['native_observation'], {})
        self.observer.assert_not_called()

if __name__ == '__main__':
    unittest.main()
