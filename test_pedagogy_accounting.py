import unittest
from unittest.mock import Mock, patch
import pedagogy_daily as p

class Accounting(unittest.TestCase):
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

if __name__ == '__main__':
    unittest.main()
