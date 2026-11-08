"""Synthetic report arithmetic regressions: no model calls or deliveries."""
import json
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import business_idea_report as B
import personal_schedule as P


class ReportCalculationTests(unittest.TestCase):
    def opinion(self, quantity):
        return {'opinion': 'Synthetic quoted quantity is '+quantity+'; current conditions remain unverified.',
                'rules': [{'rule': i, 'status': 'not_established',
                           'assessment': 'Synthetic evidence is not established.'} for i in range(1, 8)]}

    def test_signed_quantities_match_source_sign_and_unit(self):
        for positive, negative in [('5%', '-5%'), ('$5', '-$5'), ('$5', '$-5'),
                                   ('5 dollars', '-5 dollars'), ('5 shares', '-5 shares'),
                                   ('5x', '-5x'), ('5%', '−5\u202f%')]:
            with self.subTest(negative=negative):
                for source, output in [(positive, negative), (negative, positive)]:
                    with self.assertRaisesRegex(ValueError, 'canonical_table'):
                        P.validate(self.opinion(output), [], 'Synthetic source '+source)
                self.assertEqual(P.validate(self.opinion(negative), [], 'Synthetic source '+negative),
                                 self.opinion(negative))
        with self.assertRaisesRegex(ValueError, 'canonical_table'):
            P.validate(self.opinion('-5 shares'), [], 'Synthetic source -5%')

    def spend(self, rows):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'synthetic.jsonl'
            path.write_text('\n'.join(json.dumps(row) for row in rows))
            with patch.object(B, 'SPEND_LEDGER', path):
                return B._spend_today('2026-11-08')

    def row(self, cost):
        return {'task': 'business-idea-test', 'ts': '2026-11-08T01:00:00', 'cost': cost}

    def test_exact_spend_and_final_rounding(self):
        self.assertEqual(self.spend([self.row('0.10'), self.row('0.20')]), (Decimal('.30'), 2))
        self.assertEqual(self.spend([self.row('.004'), self.row('.004')]), (Decimal('.01'), 2))
        self.assertEqual(self.spend([self.row('.005')]), (Decimal('.01'), 1))
        self.assertEqual(self.spend([self.row(0)]), (Decimal('0.00'), 1))

    def test_bad_rows_never_become_plausible_partial_total(self):
        for cost in ('bad', None, 'NaN', 'Infinity', '-0.1', True):
            with self.subTest(cost=cost):
                self.assertEqual(self.spend([self.row('.10'), self.row(cost), self.row('.20')]), (None, 3))
        self.assertEqual(self.spend([self.row('.10'), [], self.row('.20')]), (None, 2))
        self.assertEqual(self.spend([{'task': 'business-idea-test', 'ts': '2026-11-08'}]), (None, 1))
        with tempfile.TemporaryDirectory() as td, patch.object(B, 'SPEND_LEDGER', Path(td)/'absent'):
            self.assertEqual(B._spend_today('2026-11-08'), (None, 0))

    def test_other_dates_and_tasks_excluded(self):
        self.assertEqual(self.spend([self.row('.10'),
            dict(self.row('5'), ts='2026-11-07'), dict(self.row('7'), task='other')]), (Decimal('.10'), 1))

    def test_report_labels_unknown_spend(self):
        with patch.object(B, '_spend_today', return_value=(None, 3)), \
             patch.object(B.entity_kb, 'list_entities', return_value=[]), \
             patch.object(B.entity_kb, 'get_events', return_value=[]), \
             patch.object(B.entity_kb, 'project_counts', return_value={}):
            _, body = B.compose()
        self.assertIn('spending unavailable', body)
        self.assertNotIn('$None', body)
        self.assertNotIn('$0', body)

if __name__ == '__main__':
    unittest.main()
