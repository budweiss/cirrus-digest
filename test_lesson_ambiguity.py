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
            question = json.loads(prompt.split('\nQUESTIONS:\n')[1])[0]
            key = question['options'].index(self.q['correct'])
            rows = [{'id': 1, 'supported_options': [key], 'ambiguous': False,
                     'reason': 'Supported by source', 'evidence': ['Use voice to show feeling']}]
            if mutate: mutate(rows, key)
            return 'fixture', {'reviews': rows}
        with patch.object(lb, 'cloud_draft', side_effect=reply):
            failures = lb.independent_answers({'questions': [self.q]}, self.source, ctx, 'fixture')
        return failures, ctx

    def test_paid_gate_retains_counter_and_checks_unique_hidden_key(self):
        failures, ctx = self.paid_review()
        self.assertEqual(failures, [])
        self.assertEqual(ctx['cloud_calls'], 8)
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


if __name__ == '__main__':
    unittest.main()
