import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import lesson_build as lb
import lesson_build_budget as budget


class Budget(unittest.TestCase):
    def test_selftest_never_opens_network(self):
        import contextlib
        import io
        with patch.object(lb, '_post', side_effect=AssertionError('offline test opened network')) as network:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(lb.selftest())
            network.assert_not_called()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = self.root / 'spend.jsonl'
        self.ledger.write_text(json.dumps({'task': budget.TASK, 'cost': 2.41, 'usage_basis': 'provider_tokens'})+'\n')

    def reserve(self, charge=0.5, job='qualification-module-1', qualification=True):
        return budget.reserve(self.root, self.ledger, job, charge, qualification)

    def test_prior_spend_survives_and_per_module_cap_holds(self):
        for _ in range(5): self.reserve()
        with self.assertRaises(ValueError): self.reserve()
        state = json.loads((self.root / 'dollar-budget.json').read_text())
        self.assertEqual(state['baseline'], 2.41)
        self.assertEqual(len(state['reservations']), 5)

    def test_total_cap_across_jobs(self):
        self.reserve(4, 'a', False)
        self.reserve(4, 'b', False)
        self.reserve(4, 'c', False)
        with self.assertRaises(ValueError): self.reserve(1, 'd', False)

    def test_settle_only_exact_provider_record(self):
        rid = self.reserve()
        with self.ledger.open('a') as f:
            f.write(json.dumps({'session_id': rid, 'task': budget.TASK, 'cost': .1, 'usage_basis': 'provider_tokens'})+'\n')
        budget.settle(self.root, self.ledger, rid)
        state = json.loads((self.root / 'dollar-budget.json').read_text())
        self.assertEqual(state['reservations'][rid]['charge'], .1)

    def test_unknown_usage_stops_further_spending(self):
        rid = self.reserve()
        with self.assertRaises(ValueError): budget.settle(self.root, self.ledger, rid)
        with self.assertRaises(ValueError): self.reserve()

    def test_unresolved_reservation_is_not_refunded_on_restart(self):
        self.reserve(2.5)
        with self.assertRaises(ValueError): self.reserve(.2)

    def test_missing_or_invalid_accounting_fails_closed(self):
        self.ledger.unlink()
        with self.assertRaises(FileNotFoundError): self.reserve()
        self.ledger.write_text('{bad JSON')
        with self.assertRaises(ValueError): self.reserve()
        self.ledger.write_text(json.dumps({'task': budget.TASK, 'cost': float('nan'), 'usage_basis': 'provider_tokens'}))
        with self.assertRaises(ValueError): self.reserve()

    def test_reservation_must_reach_disk(self):
        with patch.object(lb, 'durable_json', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError): self.reserve()
        self.assertFalse((self.root / 'dollar-budget.json').exists())

    def test_teaching_review_requires_complete_grounding(self):
        draft = {'questions': [{}, {}, {}]}
        src = 'The poet describes a red wheelbarrow. Rain water makes it shine.'
        with patch.object(lb, 'local_check', return_value={'issues': [], 'evidence': []}):
            self.assertTrue(lb.teaching_quality('checks', draft, src, {}))
        with patch.object(lb, 'local_check', return_value={'issues': ['date does not match event'], 'evidence': ['The poet describes a red wheelbarrow']*3}):
            self.assertIn('date does not match event', lb.teaching_quality('checks', draft, src, {})[0])
        with patch.object(lb, 'local_check', return_value={'issues': [], 'evidence': ['The poet describes a red wheelbarrow']*3}):
            self.assertEqual(lb.teaching_quality('checks', draft, src, {}), [])

    def test_supported_date_answer_is_not_banned_but_logistics_are(self):
        q = {'tag': 'target', 'stem': 'What is Jack writing about on April 26?',
             'correct': 'His letter to Walter Dean Myers',
             'wrong': ['A poem about his dog Sky', 'A story about his school day', 'A report on the weather']}
        d = {'focus': 'poetry', 'questions': [q, q, q]}
        errors = lb.check_mcq(d, {'printed': [1, 2]}, '')
        # Teach Book p.327 explicitly supports this answer. The original
        # reviewer confused writing ABOUT the letter with writing the letter.
        self.assertFalse(errors)
        q['stem'] = 'Which of the following is an activity students might do to experience storytelling?'
        self.assertTrue(any('logistics' in x for x in lb.check_mcq(d, {'printed': [1, 2]}, '')))
        q['stem'] = 'Why does Jack decide to write to the poet?'
        self.assertFalse(any('calendar-date' in x or 'logistics' in x for x in lb.check_mcq(d, {'printed': [1, 2]}, '')))


if __name__ == '__main__': unittest.main()
