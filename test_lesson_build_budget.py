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

    def test_authorized_qualification_increase_preserves_spend_and_scope(self):
        self.reserve()
        p = self.root / 'dollar-budget.json'
        state = json.loads(p.read_text())
        state['qualification_authorization'] = {'reference': 'Buddy S633 explicit $25 ceiling',
                                               'total_usd': 25, 'per_module_usd': 12.5}
        p.write_text(json.dumps(state))
        self.reserve(8)
        self.assertEqual(json.loads(p.read_text())['baseline'], 2.41)
        with self.assertRaises(ValueError): self.reserve(2)
        with self.assertRaises(ValueError): self.reserve(6, 'client-job', False)
        with self.assertRaises(ValueError): self.reserve(6, 'qualification-module-3', True)
        self.reserve(10, 'qualification-module-2')
        with self.assertRaises(ValueError): self.reserve(4, 'other-job', False)

    def test_logistics_rejects_the_actual_cloud_failures(self):
        q = {'tag': 'target', 'correct': 'one', 'wrong': ['two', 'three', 'four']}
        for stem in ('What is the Essential Question students will explore?',
                     'Which of these is one way students could engage with the module topic?'):
            draft = {'focus': 'topic', 'questions': [dict(q, stem=stem)] * 3}
            self.assertTrue(any('logistics' in x for x in lb.check_mcq(draft, {'printed': [1, 2]}, '')))

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

    def test_cloud_receives_all_local_failures_and_latest_draft(self):
        with patch.object(lb, 'task_prompt', return_value='source prompt'), \
             patch.object(lb, 'local_draft', side_effect=[{'draft': 'first'}, {'draft': 'second'}]), \
             patch.object(lb, 'lesson_failures', side_effect=[['missing module connection'], ['missing follow-up'], []]), \
             patch.object(lb, 'cloud_draft', return_value=('fake', {'draft': 'fixed'})) as cloud:
            got = lb.build_lesson('lessons', {}, {'n': 1}, [], '', {}, {'creds': {}})
            prompt = cloud.call_args.args[0]
            self.assertIn('missing module connection', prompt)
            self.assertIn('missing follow-up', prompt)
            self.assertIn('second', prompt)
            self.assertEqual(got['stage'], 'cloud:fake')

    def test_only_one_invalid_optional_question_can_be_removed(self):
        q = {'tag': 'vocab', 'stem': 'What does endure mean?', 'correct': 'to last',
             'wrong': ['to start', 'to fade', 'to end']}
        bad = dict(q, stem='According to the Land section, what will students do?')
        L = {'printed': [1, 2]}
        draft = {'focus': 'vocabulary', 'questions': [q, q, q, bad]}
        fixed, removed = lb.prune_invalid_extra(draft, L, '')
        self.assertEqual(len(fixed['questions']), 3)
        self.assertEqual(removed, [bad])
        for questions in ([q, q, bad], [q, q, bad, bad]):
            d = dict(draft, questions=questions)
            self.assertEqual(lb.prune_invalid_extra(d, L, ''), (d, []))

    def test_pruned_questions_still_require_model_review(self):
        q = {'tag': 'vocab', 'stem': 'What does endure mean?', 'correct': 'to last',
             'wrong': ['to start', 'to fade', 'to end']}
        d = {'focus': 'vocabulary', 'questions': [q, q, q, dict(q, stem='According to the Land section?')]}
        seen = []
        def reject(kind, draft, *args):
            seen.append(len(draft['questions']))
            return ['independent model rejects this set']
        with patch.object(lb, 'task_prompt', return_value='source'), \
             patch.object(lb, 'local_draft', return_value=d), \
             patch.object(lb, 'lesson_failures', side_effect=reject):
            with self.assertRaises(lb.Hold):
                lb.build_lesson('checks', {}, {'n': 1, 'printed': [1, 2]}, [], '', {}, {'no_cloud': True})
        self.assertEqual(seen, [3, 3])

    def test_repair_receives_later_quality_feedback_even_when_earlier_checks_fail(self):
        q = {'stem': 'According to Land?', 'correct': 'one', 'wrong': ['two', 'three', 'four'], 'tag': 'target'}
        d = {'focus': 'test', 'questions': [q, q, q]}
        with patch.object(lb, 'blind_answer', return_value=['wrong key']), \
             patch.object(lb, 'claim_check', return_value=['unsupported claim']), \
             patch.object(lb, 'teaching_quality', return_value=['weak distractors']):
            errors = lb.lesson_failures('checks', d, {'module': 2}, {'n': 1, 'printed': [1, 2]}, [], '', {})
        self.assertTrue(any('logistics' in x for x in errors))
        self.assertIn('wrong key', errors)
        self.assertIn('unsupported claim', errors)
        self.assertIn('weak distractors', errors)

    def test_assessment_review_gets_prior_vocabulary_but_never_future_pages(self):
        pages = ['Definitions\nendure: to last for a long time', 'Assessment administration instructions',
                 'Definitions\nfuture-only word']
        lessons = [{'n': n+1, 'first': n, 'last': n, 'label': 'Assessment' if n==1 else 'Lesson'} for n in range(3)]
        book = {'offset': 0, 'lessons': lessons}
        source = lb.source_for_kind(pages, book, lessons[1], 'checks')
        self.assertIn('endure: to last', source)
        self.assertIn('NOT the assessment text', source)
        self.assertNotIn('future-only', source)
        self.assertNotIn('endure: to last', lb.source_for_kind(pages, book, lessons[1], 'lessons'))
        self.assertNotIn('CUMULATIVE', lb.source_for_kind(pages, book, lessons[0], 'checks'))


if __name__ == '__main__': unittest.main()
