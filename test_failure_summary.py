"""S497 P5: failure summaries carry fixed labels and codes only -- never private text."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import failure_summary as fs

PRIVATE = ['ZXQW', '$123.45', 'Synthetic revenue', 'example.com/filing', 'Jane Doe', '2026-09-30']
ISSUES = [
    'Trial 2 invents a 2026-09-30 date for ZXQW; the source has no dated content.',
    'The Jane Doe newsletter at example.com/filing is a promotional subscription offer.',
    'Decision treats a delayed 13F disclosure ($123.45 position) as a live trade.',
    'ADOPT quote "Synthetic revenue was 100 units" is not exact evidence.',
    'Something nobody anticipated about ZXQW.',
]


class Summaries(unittest.TestCase):
    def test_categories_are_fixed_labels(self):
        cats = fs.build('stockdiscovery', 'source_adoption_review_failed', issues=ISSUES)['review_categories']
        self.assertEqual(cats, {'invented or unsupported date': 1, 'promotional or subscription source': 1,
                                'delayed disclosure treated as a live trade': 1,
                                'missing or inexact evidence quote': 1, 'other': 1})
        self.assertEqual(fs.category('A TRIAL needs install evidence from the metadata.'),
                         'tool claim without installable evidence')
        self.assertEqual(fs.category('SKIP ignores a useful technique for model routing.'),
                         'useful technique rated SKIP')

    def test_no_private_text_survives(self):
        s = fs.build('stockdiscovery', 'source_adoption_review_failed', issues=ISSUES,
                     errors=['ValueError: Jane Doe ZXQW broke', 'source_decision_reason'])
        blob = json.dumps(s)
        for token in PRIVATE:
            self.assertNotIn(token, blob)
        self.assertEqual(s['validation_errors'], {'unparseable_or_invalid_model_output': 1, 'source_decision_reason': 1})

    def test_tampered_summary_is_cleaned_on_read(self):
        bad = {'job': 'ZXQW position', 'code': 'source_adoption_review_failed', 'attempts': 'many',
               'review_categories': {'Jane Doe said so': 3, 'other': 1}, 'validation_errors': {'$123.45': 1},
               'extra': 'Synthetic revenue'}
        clean = fs.sanitize(bad)
        self.assertEqual(clean['job'], 'unknown'); self.assertIsNone(clean['attempts'])
        self.assertEqual(clean['review_categories'], {'other': 1}); self.assertEqual(clean['validation_errors'], {})
        self.assertNotIn('extra', clean)
        for token in PRIVATE:
            self.assertNotIn(token, json.dumps(clean))

    def test_stock_failure_writes_the_summary(self):
        import stock_schedule as S
        trials = [{'status': 'fetched', 'published': '2026-10-20', 'text': 'Synthetic public source text.'}]
        decision = {'trial': 0, 'decision': 'REJECT', 'reason': 'An index page with no original dated content.', 'quote': ''}
        with tempfile.TemporaryDirectory() as td, patch.object(S, 'ROOT', Path(td)), \
                patch.object(S.media, 'complete', return_value=json.dumps({'decisions': [decision]})), \
                patch.object(S.research, 'structured_review',
                             return_value='{"passed":false,"issues":["Trial 0 invents a publication date for ZXQW."]}'):
            folder = Path(td) / 'run'; folder.mkdir()
            S.adopt_sources(trials, ['need'], folder)
            s = json.loads((folder / 'failure-summary.json').read_text())
        self.assertEqual(s['review_categories'], {'invented or unsupported date': 1})
        self.assertNotIn('ZXQW', json.dumps(s))

    def test_opinionai_failure_writes_the_summary(self):
        import opinionai_review as r
        post = {'id': 1, 'title': 'Jane Doe tool', 'subtitle': 'x', 'truncated_body_text': 'teaser',
                'post_date': '2026-10-13T12:00:00Z', 'canonical_url': 'https://example.com/filing'}
        exc = ValueError('triage_review_failed')
        exc.attempts = [{'attempt': 1, 'draft': 'ZXQW', 'review': {'approved': False,
                         'problems': ['TRIAL for Jane Doe tool has no install evidence.']}},
                        {'attempt': 2, 'draft': 'x', 'error': 'ValueError: Jane Doe broke'}]
        with tempfile.TemporaryDirectory() as tmp, patch.object(r, 'fetch_posts', return_value=([post], {})), \
                patch.object(r, 'triage', side_effect=exc):
            r.run(False, Path(tmp))
            s = json.loads(next(Path(tmp).glob('*-preview-failure-summary.json')).read_text())
        self.assertEqual((s['code'], s['attempts']), ('triage_review_failed', 2))
        self.assertEqual(s['review_categories'], {'tool claim without installable evidence': 1})
        for token in ('Jane Doe', 'ZXQW', 'example.com'):
            self.assertNotIn(token, json.dumps(s))

    def test_show_prints_sanitized_rows_and_refuses_unknown_args(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / 'private/stock-paper/scheduled/2026-10-25-discovery'; run.mkdir(parents=True)
            fs.write(run / 'failure-summary.json', fs.build('stockdiscovery', 'source_adoption_review_failed', issues=ISSUES))
            out = io.StringIO()
            with patch.object(fs, 'ROOT', Path(td)), redirect_stdout(out):
                self.assertEqual(fs.main(['--show']), 0)
        row = json.loads(out.getvalue().splitlines()[0])
        self.assertEqual(row['run'], '2026-10-25-discovery')
        for token in PRIVATE:
            self.assertNotIn(token, out.getvalue())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(fs.main(['--bogus']), 2); self.assertEqual(fs.main([]), 2)


if __name__ == '__main__':
    unittest.main()
