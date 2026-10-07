"""Hermetic checks for source identity, dates, immutable memory and safe abstention."""
import copy
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import stock_research as s

AT = '2026-10-07T16:00:00+00:00'
COMPANY = {'ticker': 'ACME', 'cik': '123', 'name': 'Acme Test Company',
           'hosts': ['investors.example.com'], 'seeds': []}
TEXT = 'The company reported revenue growth while management expects higher construction costs.'


def document():
    return {'id': 1, 'ticker': 'ACME', 'kind': 'company_publication',
            'title': 'Results', 'published': '2026-08-01', 'first_seen': AT,
            'sha': s.digest(TEXT), 'url': 'https://investors.example.com/results', 'text': TEXT}


def draft():
    return {'outlook': 'mixed', 'consider': 'wait', 'horizon_days': 90,
            'paragraphs': [{'text': 'Revenue grew, but higher costs could put pressure on profit.',
                            'evidence': [{'id': 1, 'quote': TEXT}]} for _ in range(3)],
            'next_check': 'Check whether the next quarter reports completed capacity.',
            'would_change_view': 'A delay in completing capacity would weaken this assessment.'}


def quote_fetch(meta):
    return lambda *args: (json.dumps({'chart': {'result': [{'meta': meta}]}}).encode(), 'application/json')


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.conn = s.database(self.home)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_manifest_never_accepts_account_weights(self):
        c = copy.deepcopy(COMPANY)
        c['weight_pct'] = 50
        with self.assertRaisesRegex(ValueError, 'exclude_account'):
            s.validate_manifest({'companies': [c]})

    def test_wrong_ticker_or_cik_is_not_a_similar_company(self):
        for wrong in ({'cik': 123, 'tickers': ['OTHER'], 'name': 'Acme'},
                      {'cik': 321, 'tickers': ['ACME'], 'name': 'Acme'}):
            with self.assertRaisesRegex(ValueError, 'identity_mismatch'):
                s.verify_identity(COMPANY, wrong)

    def test_public_source_boundaries(self):
        for url in ('http://investors.example.com/a', 'https://evil.example/a',
                    'https://user@investors.example.com/a', 'https://investors.example.com/a?token=x'):
            with self.assertRaises(ValueError):
                s.safe_url(url, COMPANY['hosts'], resolve=False)
        with patch('socket.getaddrinfo', return_value=[(None, None, None, None, ('127.0.0.1', 443))]):
            with self.assertRaisesRegex(ValueError, 'nonpublic'):
                s.safe_url('https://investors.example.com/a', COMPANY['hosts'])

    def test_publication_date_not_modified_date(self):
        page = s.Page('<script>{"dateModified":"2026-10-07","datePublished":"2025-11-03"}</script>'
                      '<title>Report</title><p>Old company news.</p>')
        self.assertEqual(page.published, '2025-11-03')
        self.assertNotIn('dateModified', page.text)

    def test_duplicate_sources_and_revisions_keep_original(self):
        def save(text):
            return s.save_document(self.conn, self.home, 'ACME', document()['url'], 'article',
                                   'Results', '2026-08-01', text, text.encode(), AT)
        first = save(TEXT)
        self.assertEqual(save(TEXT)['id'], first['id'])
        self.assertNotEqual(save(TEXT + ' Revised.')['id'], first['id'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0], 2)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE documents SET text='rewritten' WHERE id=1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute('DELETE FROM documents')

    def test_future_publication_and_stale_evidence_refused(self):
        with self.assertRaisesRegex(ValueError, 'future_publication'):
            s.save_document(self.conn, self.home, 'ACME', document()['url'], 'article', 'Future',
                            '2026-10-08', TEXT, TEXT.encode(), AT)
        old = dict(document(), published='2025-11-03')
        with self.assertRaisesRegex(ValueError, 'older_than'):
            s.evidence_ready([old], AT)

    def test_periods_exclude_future_filings_and_cumulative_half_year(self):
        base = {'start': '2026-04-01', 'end': '2026-06-30', 'filed': '2026-08-01',
                'form': '10-Q', 'val': 100, 'accn': 'x'}
        rows = [base, dict(base, start='2026-01-01', val=200),
                dict(base, filed='2026-10-08', val=999)]
        data = {'cik': 123, 'entityName': 'Acme', 'facts': {'us-gaap': {
            'Revenues': {'units': {'USD': rows}}}}}
        text, date = s.financial_text(COMPANY, data, AT)
        self.assertIn('91 days', text)
        self.assertIn('USD 100;', text)
        self.assertNotIn('USD 200;', text)
        self.assertNotIn('999', text)
        self.assertEqual(date, '2026-08-01')

    def test_currency_and_future_quote_refused_stale_quote_labeled(self):
        meta = {'symbol': 'ACME', 'currency': 'USD', 'regularMarketPrice': 30,
                'regularMarketTime': s.dt(AT).timestamp() - 60}
        for bad in (dict(meta, currency='CAD'), dict(meta, symbol='OTHER'),
                    dict(meta, regularMarketTime=s.dt(AT).timestamp() + 3600)):
            with self.assertRaises(ValueError):
                s.market_quote('ACME', AT, quote_fetch(bad))
        old = dict(meta, regularMarketTime=s.dt(AT).timestamp() - 4 * 86400)
        self.assertFalse(s.market_quote('ACME', AT, quote_fetch(old))['fresh_for_research'])

    def test_fabricated_or_wrong_source_quotes_refused(self):
        for reference in ({'id': 9, 'quote': TEXT}, {'id': 1, 'quote': TEXT.replace('growth', 'decline')}):
            value = draft()
            value['paragraphs'][0]['evidence'] = [reference]
            with self.assertRaisesRegex(ValueError, 'unmatched_source_quote'):
                s.parse_draft(json.dumps(value), [document()])

    def test_trade_suggestion_without_valuation_refused(self):
        value = draft()
        value['consider'] = 'research_add'
        with self.assertRaisesRegex(ValueError, 'valuation_missing'):
            s.make_draft(COMPANY, [document()], None, None, AT, lambda *args: json.dumps(value))

    def test_model_cannot_cite_text_it_was_not_shown(self):
        long_doc = dict(document(), text='x' * 18001 + TEXT)
        with self.assertRaisesRegex(ValueError, 'unmatched_source_quote'):
            s.make_draft(COMPANY, [long_doc], None, None, AT, lambda *args: json.dumps(draft()))

    def test_legacy_import_preserves_original_and_is_idempotent(self):
        text = 'date,run,ticker,call,price,reason_one_line,rules_fired\n2026-10-05,am,ACME,HOLD,30,Original mistaken reason,\n'
        self.assertEqual(s.import_legacy(self.conn, text), 1)
        self.assertEqual(s.import_legacy(self.conn, text), 0)
        original = json.loads(self.conn.execute('SELECT data FROM legacy_calls').fetchone()[0])
        self.assertEqual(original['original']['reason_one_line'], 'Original mistaken reason')
        self.assertEqual(original['status'], 'legacy_unreviewed')
        with self.assertRaises(ValueError):
            s.import_legacy(self.conn, text.replace('Original mistaken reason', 'bad,comma'))

    def test_repeat_run_links_prior_draft_and_does_not_regenerate_a_pick(self):
        (self.home / 'universe.json').write_text(json.dumps({'companies': [COMPANY]}))
        self.conn.close()
        with patch.object(s, 'now', return_value=AT), patch.object(s, 'market_quote', return_value=None), \
                patch.object(s, 'collect', return_value=([document()], None, [])), \
                patch.object(s, 'make_draft', return_value=draft()) as model, redirect_stdout(io.StringIO()):
            self.assertEqual(s.refresh(self.home, 'am'), 0)
            self.assertEqual(s.refresh(self.home, 'pm'), 0)
        self.conn = s.database(self.home)
        self.assertEqual(model.call_count, 1)
        rows = self.conn.execute('SELECT * FROM reviews ORDER BY id').fetchall()
        self.assertEqual(rows[1]['previous_id'], rows[0]['id'])
        self.assertIn('repeated_evidence', rows[1]['validation'])
        self.assertIn('no new independent pick', (self.home / 'reports/latest.md').read_text())

    def test_collection_failure_never_becomes_hold(self):
        rendered = s.render_company(COMPANY, [], None, None, None, ['http_403'], False)
        self.assertIn('Research incomplete', rendered)
        self.assertNotIn('hold for review', rendered)


if __name__ == '__main__':
    unittest.main()
