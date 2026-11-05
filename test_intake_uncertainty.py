"""S567: a useful uncertainty answer must pass without relaxing length checks."""
import unittest
import task_solver as T
import foundation_renewal as R


class UncertaintyTests(unittest.TestCase):
    def test_observed_generic_apology_remains_held(self):
        answer = 'I’m sorry, but I don’t have that information.'
        self.assertFalse(T._quality_ok(answer))
        self.assertFalse(R.qualification_shape_valid(
            {'task': 'intake-answer', 'field': 'evaluations'}, answer))

    def test_useful_plain_uncertainty_is_admissible(self):
        answer = ('No source document or date was supplied, so the deadline cannot be determined. '
                  'Check the official solicitation and any later addenda, or ask the issuing office to confirm it.')
        self.assertTrue(T._quality_ok(answer))
        self.assertTrue(R.qualification_shape_valid(
            {'task': 'intake-answer', 'field': 'evaluations'}, answer))
        self.assertFalse(R.qualification_shape_valid(
            {'task': 'intake-answer', 'field': 'evaluations'}, '## Answer\n' + answer))


if __name__ == '__main__':
    unittest.main()
