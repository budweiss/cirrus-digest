import copy
import json
import unittest
from unittest.mock import patch
import lesson_build as lb


class Ambiguity(unittest.TestCase):
    def setUp(self):
        self.q = {'stem': 'How does expression help?', 'correct': 'show feeling',
                  'wrong': ['vary speed', 'vary loudness', 'pause at punctuation']}
        self.source = 'Use voice to show feeling. Read some words slower and some faster.'

    def evaluate(self, altered=None):
        def review(system, prompt, schema, cfg, **kw):
            self.assertNotIn('Correct answer:', prompt)
            self.assertNotIn('usually none', prompt)
            if schema == lb.OPEN_ANSWER_SCHEMA:
                self.assertNotIn('OPTIONS:', prompt)
                self.assertNotIn('vary speed', prompt)
                return {'answer': 'show feeling', 'evidence': 'Use voice to show feeling'}
            self.assertIn('INDEPENDENT OPEN ANSWER', prompt)
            options = prompt.split('OPTIONS:\n', 1)[1].split('\nINDEPENDENT OPEN ANSWER', 1)[0].splitlines()
            rows = [{'letter': line[0], 'answers_stem': 'yes' if line[3:] == self.q['correct'] else 'no',
                     'reason': 'Matches the exact question' if line[3:] == self.q['correct'] else 'Does not answer the stem',
                     'evidence': 'Use voice to show feeling' if line[3:] == self.q['correct'] else ''} for line in options]
            if altered: altered(rows, options)
            return {'options': rows}
        with patch.object(lb, 'local_check', side_effect=review):
            return lb.blind_answer({'questions': [self.q]}, self.source, {}, 'fixture')

    def test_only_supported_key_passes(self):
        self.assertEqual(self.evaluate(), [])

    def test_ungrounded_open_answer_cannot_accept_options(self):
        for answer in ({}, {'answer': '', 'evidence': self.source},
                       {'answer': 'guess', 'evidence': 'made up evidence'}):
            with patch.object(lb, 'local_check', return_value=answer) as checker:
                self.assertTrue(lb.blind_answer({'questions': [self.q]}, self.source, {}, 'fixture'))
                self.assertEqual(checker.call_count, 1)

    def test_second_supported_option_is_rejected_even_if_key_is_best(self):
        def extra(rows, options):
            i = next(i for i, line in enumerate(options) if line.endswith('vary speed'))
            rows[i].update(answers_stem='yes', evidence='Read some words slower and some faster')
        self.assertTrue(any('independently supported options' in x for x in self.evaluate(extra)))

    def test_missing_duplicate_uncertain_and_unquoted_are_not_passes(self):
        mutations = [lambda rows, _: rows.pop(),
                     lambda rows, _: rows.__setitem__(0, copy.deepcopy(rows[1])),
                     lambda rows, _: rows[0].update(answers_stem='uncertain'),
                     lambda rows, _: [row.update(evidence='invented quotation') for row in rows if row['answers_stem']=='yes'],
                     lambda rows, _: rows[0].update(letter=None)]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.assertTrue(self.evaluate(mutation))

    def paid_review(self, mutate=None, context=None):
        ctx = context if context is not None else {'creds': {}, 'dollar_job': 'fixture', 'cloud_calls': 7}
        def reply(prompt, schema, creds, context, **kwargs):
            self.assertNotIn('"correct"', prompt)
            self.assertEqual(creds['claude_dev_model'], 'claude-opus-5')
            question = json.loads(prompt.split('\nQUESTIONS:\n')[1].split('\nYour REVIEW')[0])[0]
            key = question['options'].index(self.q['correct'])
            rows = [{'id': 1, 'supported_options': [key], 'ambiguous': False,
                     'reason': 'Supported by source', 'quality_defects': [],
                     'evidence': ['Use voice to show feeling']}]
            if mutate: mutate(rows, key)
            return 'fixture', {'reviews': rows}
        with patch.object(lb, 'cloud_draft', side_effect=reply):
            failures = lb.independent_answers({'questions': [self.q]}, self.source, ctx, 'fixture')
        return failures, ctx

    def test_paid_gate_retains_counter_and_checks_unique_hidden_key(self):
        failures, ctx = self.paid_review()
        self.assertEqual(failures, [])
        self.assertEqual(ctx['cloud_calls'], 8)
        self.assertNotIn('claude_dev_model', ctx['creds'])
        self.assertTrue(self.paid_review(lambda rows, key: rows[0].update(
            supported_options=[key, (key+1) % 4]))[0])

    def test_paid_gate_fails_closed_on_missing_or_invalid_evidence(self):
        mutations = [lambda r, k: r.clear(), lambda r, k: r.append(copy.deepcopy(r[0])),
                     lambda r, k: r[0].update(id=True),
                     lambda r, k: r[0].update(supported_options=[True]),
                     lambda r, k: r[0].update(ambiguous='false'),
                     lambda r, k: r[0].update(ambiguous=True),
                     lambda r, k: r[0].update(evidence=[]),
                     lambda r, k: r[0].update(evidence=['invented source quote'])]
        for mutation in mutations:
            self.assertTrue(self.paid_review(mutation)[0])

    def test_paid_gate_has_no_unbudgeted_or_disabled_fallback(self):
        for ctx in ({}, {'no_cloud': True, 'dollar_job': 'fixture'},
                    {'creds': {}, 'dollar_job': 'fixture', 'cloud_calls': lb.MAX_CLOUD_CALLS_PER_JOB}):
            with patch.object(lb, 'cloud_draft') as cloud:
                with self.assertRaises(lb.Hold):
                    lb.independent_answers({'questions': [self.q]}, self.source, ctx, 'fixture')
                cloud.assert_not_called()

    def test_review_format_gets_one_retry_without_rewriting_questions(self):
        calls = []
        def malformed_once(rows, key):
            calls.append(key)
            if len(calls) == 1: rows[0]['evidence'] = ['show feeling']
        failures, ctx = self.paid_review(malformed_once)
        self.assertEqual(failures, [])
        self.assertEqual(ctx['cloud_calls'], 9)
        self.assertEqual(len(calls), 2)

    def test_short_literal_gloss_requires_longer_exact_context_quote(self):
        self.assertEqual(self.paid_review(lambda r, k: r[0].update(
            evidence=['Use voice to show feeling', 'show feeling']))[0], [])
        self.assertTrue(self.paid_review(lambda r, k: r[0].update(evidence=['show feeling']))[0])
        self.assertTrue(self.paid_review(lambda r, k: r[0].update(
            evidence=['Use voice to show feeling', 'invented meaning']))[0])

    def test_substantive_quality_rejection_never_disappears_in_format_retry(self):
        def defect(rows, key):
            rows[0].update(quality_defects=['stem discloses the answer'], evidence=[])
        failures, ctx = self.paid_review(defect)
        self.assertTrue(any('stem discloses' in x for x in failures))
        self.assertEqual(ctx['cloud_calls'], 8)
        self.assertTrue(self.paid_review(lambda r, k: r[0].pop('quality_defects'))[0])

    def test_qualification_extension_retains_all_calls_and_remains_bounded(self):
        ctx = {'qualification': True, 'cloud_calls': 199}
        lb.reserve_cloud(ctx)
        self.assertEqual(ctx['cloud_calls'], 200)
        with self.assertRaises(lb.Hold): lb.reserve_cloud(ctx)
        self.assertEqual(ctx['cloud_calls'], 200)
        self.assertEqual(lb.cloud_call_limit({}), 100)

    def test_observed_off_topic_distractor_is_held_before_model_acceptance(self):
        q = dict(self.q, tag='target', stem='How can poetry inspire writers?',
                 correct='Share their feelings about another person.',
                 wrong=['Solve math problems.', 'Forget their feelings.', 'Stop writing poems.'])
        draft = {'focus': 'poetry', 'questions': [q, q, q]}
        with patch.object(lb, 'independent_answers', return_value=[]) as audit:
            failures = lb.lesson_failures('checks', draft, {'module': 1}, {'n': 33},
                                         [], 'Poetry helps people share feelings.', {}, True, {})
        self.assertTrue(any('unrelated topic' in x for x in failures))
        audit.assert_not_called()
        # A real cross-curricular source must not be excluded by keyword alone.
        supported = lb.check_mcq(draft, {'n': 33}, 'Poetry and math can describe patterns.')
        self.assertFalse(any('unrelated topic' in x for x in supported))

    def test_definition_clue_is_held_but_contextual_usage_is_allowed(self):
        q = dict(self.q, tag='vocab',
                 stem='What does endure mean when it is used to describe something that lasts a long time?',
                 correct='to continue to exist; to last for a period of time',
                 wrong=['to accept something unpleasant', 'to survive hardship', 'to persist through pain'])
        draft = {'focus': 'endure', 'questions': [q, q, q]}
        self.assertTrue(any('definition clue' in x for x in lb.check_mcq(draft, {'n': 21}, self.source)))
        q['stem'] = 'These stories endure across many generations. What does endure mean in this sentence?'
        self.assertFalse(any('definition clue' in x for x in lb.check_mcq(draft, {'n': 21}, self.source)))

    def test_full_acceptance_cannot_bypass_independent_gate(self):
        draft = {'questions': [copy.deepcopy(self.q) for _ in range(3)]}
        ctx = {'dollar_job': 'fixture'}
        with patch.object(lb, 'check_mcq', side_effect=lambda *args: []), \
             patch.object(lb, 'claim_check', return_value=[]), \
             patch.object(lb, 'option_quality', return_value=[]), \
             patch.object(lb, 'teaching_quality', return_value=[]), \
             patch.object(lb, 'independent_answers', return_value=['ambiguous']) as audit:
            failures = lb.lesson_failures('checks', draft, {'module': 2}, {'n': 4},
                                         [], self.source, {}, True, ctx)
            self.assertEqual(failures, ['ambiguous'])
            self.assertIs(audit.call_args.args[2], ctx)
            audit.side_effect = lb.Hold('budget exhausted')
            with self.assertRaises(lb.Hold):
                lb.lesson_failures('checks', draft, {'module': 2}, {'n': 4},
                                  [], self.source, {}, True, ctx)

    def test_knowledge_fallback_is_bounded_budgeted_and_still_validated(self):
        source = 'continue to exist; accept something unpleasant; understand new texts'
        claims = [{'tag': 'vocab', 'claim': str(i), 'quote': q} for i, q in enumerate(
            ['continue to exist', 'accept something unpleasant', 'understand new texts'])]
        ctx = {'creds': {}, 'cloud_calls': 7, 'dollar_job': 'fixture'}
        with patch.object(lb, 'local_check', return_value={'claims': []}), \
             patch.object(lb, 'cloud_draft', return_value=('fixture', {'claims': claims})) as cloud:
            self.assertEqual(lb.question_knowledge(source, {}, ctx), {'claims': claims})
            self.assertEqual(ctx['cloud_calls'], 8)
            cloud.assert_called_once()
            cloud.return_value = ('fixture', {'claims': []})
            with self.assertRaises(lb.Hold):
                lb.question_knowledge(source, {}, ctx)
            self.assertEqual(cloud.call_count, 2)  # one call per attempt, no retry loop
        with patch.object(lb, 'local_check', return_value={'claims': []}), \
             patch.object(lb, 'cloud_draft') as cloud:
            with self.assertRaises(lb.Hold):
                lb.question_knowledge(source, {}, {'no_cloud': True})
            cloud.assert_not_called()


if __name__ == '__main__':
    unittest.main()
