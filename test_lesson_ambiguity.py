import copy
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


if __name__ == '__main__':
    unittest.main()
