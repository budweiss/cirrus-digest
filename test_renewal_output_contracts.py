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
        # S534: S490 replaced the S478 brief format (exact "Brief date:
        # YYYY-MM-DD" first line, standing questions, council line). Same
        # intent, current shape; a long-form date is no longer accepted.
        import test_medical_judge as M
        good, user = M.brief(), M.task()
        missing = good.replace('## Community pulse\nNothing in the supplied items this period.\n\n', '')
        self.assertNotEqual(missing, good)
        for field in ['evaluations', 'judge_evaluations']:
            envelope = (M.ensemble._judge_prompt(M.medical.SYSTEM, user, [('a', 'old draft')], '')
                        if field == 'judge_evaluations' else user)
            case = self.case('alopecia-brief', field, envelope)
            self.assertTrue(R.qualification_shape_valid(case, good))
            self.assertFalse(R.qualification_shape_valid(case, missing))
            self.assertFalse(R.qualification_shape_valid(case, good.split('\n', 1)[1]))
            self.assertFalse(R.qualification_shape_valid(case, good.replace(M.DAY, 'September 24, 2026')))
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
