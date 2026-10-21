"""S478 synthetic regressions: no inference, credentials or real reports."""
import json
import unittest
from unittest.mock import patch
import foundation_renewal as R


class OutputContracts(unittest.TestCase):
    def case(self, task, field='evaluations', user=''):
        return dict(task=task, field=field, user=user)

    def test_json_only_rejects_wrappers_even_if_consumer_accepts(self):
        for task, body in [('billsnow', '{}'), ('business-idea-estimate', '{}'),
                           ('research:decompose', '{}'),
                           ('halftime:catalogue:program', '[]'),
                           ('halftime:catalogue:variety', '[]')]:
            with self.subTest(task=task), patch('foundation_contracts.valid', return_value=True):
                case = self.case(task)
                self.assertTrue(R.qualification_shape_valid(case, body))
                for bad in ['```json\n'+body+'\n```', 'Here it is: '+body,
                            body+' trailing', json.dumps(body)]:
                    self.assertFalse(R.qualification_shape_valid(case, bad))

    def test_content_validator_still_must_accept(self):
        with patch('foundation_contracts.valid', return_value=False):
            self.assertFalse(R.qualification_shape_valid(self.case('business-idea-estimate'), '{}'))

    def test_events_keep_authored_wrapper_allowance(self):
        self.assertTrue(R.qualification_shape_valid(self.case('halftime:events'), '```json\n[]\n```'))

    def test_judge_cannot_omit_required_sections_or_date(self):
        headings = ['## What changed', '## Trials watch', '## Subgroup focus',
                    '## Community pulse', '## Standing questions — where they stand',
                    '## Council disagreements']
        user = ("Write this week's brief, dated 2026-10-21.\n\n"
                "Use EXACTLY these sections, in this order:\n" + '\n'.join(headings) +
                '\n\nSection rules:\n')
        for field in ['evaluations', 'judge_evaluations']:
            case = self.case('alopecia-brief', field, user)
            good = '2026-10-21\n'+'\n\n'.join(h+'\nNo supplied update.' for h in headings)
            self.assertTrue(R.qualification_shape_valid(case, good))
            self.assertFalse(R.qualification_shape_valid(case, good.replace(
                '## Community pulse\nNo supplied update.\n\n', '')))
            self.assertFalse(R.qualification_shape_valid(case, good.replace('2026-10-21', '')))
            self.assertTrue(R.qualification_shape_valid(case, good.replace('2026-10-21', 'October 21, 2026')))
            self.assertFalse(R.qualification_shape_valid(case, 'nonempty'))

    def test_intake_plain_prose_rejects_headers_and_fences(self):
        case = self.case('intake-answer')
        body = 'This is a synthetic plain prose answer. '*4
        self.assertTrue(R.qualification_shape_valid(case, body))
        self.assertFalse(R.qualification_shape_valid(case, '## Answer\n'+body))
        self.assertFalse(R.qualification_shape_valid(case, '```\n'+body+'\n```'))
        self.assertFalse(R.qualification_shape_valid(case, '**Answer**\n'+body))
        self.assertFalse(R.qualification_shape_valid(case, '| Topic | Answer |\n|---|---|\n'+body))

    def test_invalid_and_other_judge_behavior(self):
        for text in ['', None, []]:
            self.assertFalse(R.qualification_shape_valid(self.case('alopecia-brief'), text))
        self.assertTrue(R.qualification_shape_valid(self.case('unrelated', 'judge_evaluations'), 'nonempty'))


if __name__ == '__main__':
    unittest.main()
