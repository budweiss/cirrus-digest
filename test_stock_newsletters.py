import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
import stock_newsletters as N

NOW = datetime(2026, 10, 29, 17, tzinfo=timezone.utc)


class NewsletterChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def issue(self, name, body='Stock earnings improved.', sender='news@morningstar.com',
              date='Thu, 29 Oct 2026 12:00:00 -0400'):
        p = self.root / name
        p.write_text('# Synthetic issue\n\n> From: `'+sender+'` · Date: '+date+
                     ' · Fetched: 2026-10-29\n\n---\n'+body)

    def collect(self, **kw):
        return N.collect(self.root, NOW, [{'ticker':'EXAMPLE'}],
                         ['.morningstar.com', '.seekingalpha.com', '.investopedia.com'], **kw)

    def test_sender_boundaries(self):
        for addr in ('x@morningstar.com', 'x@mail.morningstar.com'):
            self.assertTrue(N.sender_allowed(addr, ['.morningstar.com']))
        for addr in ('x@notmorningstar.com', 'x@morningstar.com.evil.test'):
            self.assertFalse(N.sender_allowed(addr, ['.morningstar.com']))

    def test_actual_date_not_filename(self):
        self.issue('20261029-stale.md', date='Thu, 01 Oct 2026 12:00:00 -0400')
        self.issue('20261001-current.md')
        self.issue('unknown.md', date='unknown')
        self.issue('future.md', date='Fri, 30 Oct 2026 12:00:00 -0400')
        result = self.collect()['newsletter_coverage']
        self.assertEqual(result['selected'], 1)
        self.assertEqual(result['excluded'], {'stale':1, 'future_date':1, 'unknown_date':1})

    def test_full_body_and_cross_mailbox_duplicates(self):
        body = 'Stock commentary. '+('General context. '*300)+' EXAMPLE earnings in its filing.'
        self.issue('yahoo.md', body)
        self.issue('gmail.md', body)
        result = self.collect()
        row = result['newsletter_excerpts'][0]
        self.assertGreater(row['offset'], 2500)
        self.assertIn('EXAMPLE', row['text'])
        self.assertEqual(result['newsletter_coverage']['excluded'], {'duplicate':1})
        self.assertEqual(body[row['offset']:row['offset']+len(row['text'])], row['text'])

    def test_exclusions(self):
        self.issue('a.md', 'Stock earnings for your portfolio')
        self.issue('b.md', sender='x@unknown.test')
        self.issue('c.md', sender='blocked@morningstar.com')
        self.issue('d.md', sender='x@seekingalpha.com')
        result = self.collect(rejectlist=['blocked@morningstar.com'])['newsletter_coverage']
        self.assertEqual(result['selected'], 0)
        self.assertEqual(set(result['excluded']), {'personal_or_followed','unapproved_sender',
                                                  'rejected_sender','unidentified_sa_product'})

    def test_general_sa_admitted(self):
        self.issue('sa.md', 'Stock earnings. because you subscribed to receive Wall Street Breakfast. If you leave.',
                   sender='news@seekingalpha.com')
        self.assertEqual(self.collect()['newsletter_coverage']['selected'], 1)

    def test_publisher_diversity_and_complete_audit(self):
        for i in range(25):
            self.issue(str(i)+'.md', 'Stock earnings example '+str(i))
        self.issue('second.md', 'Stock dividends.', sender='news@investopedia.com')
        result = self.collect(max_excerpts=2)
        self.assertEqual(len(result['audit']), 26)
        self.assertEqual({r['source'] for r in result['newsletter_excerpts']}, {'Morningstar','Investopedia'})
        self.assertEqual(result['newsletter_coverage']['deferred'], 24)
        self.assertLessEqual(result['newsletter_coverage']['context_chars'], N.MAX_CONTEXT_CHARS)

    def test_safe_links_and_missing_archive(self):
        self.assertEqual(N.public_links('https://example.org/article https://example.org/a?token=secret '
                         'https://click.example.org/a https://u:p@example.org/a https://example.org/login'),
                         ['https://example.org/article'])
        r = N.collect(self.root/'missing', NOW, [], [])['newsletter_coverage']
        self.assertFalse(r['archive_exists'])
        self.assertIn('WARNING', N.coverage_text(r))

    def test_contribution_requires_exact_quote(self):
        import stock_schedule as S
        q = 'Synthetic earnings improved according to this example.'
        pub = {'newsletter_excerpts':[{'id':'a','source':'Morningstar','email_date':NOW.isoformat(),'text':q}]}
        ev = {'companies':[{'documents':[{'id':1,'published':'2026-10-28','url':'https://example.org/filing'}]}]}
        ref = {'secondary_id':'a','primary_id':1,'secondary_quote':q}
        self.assertIn('Morningstar', S.secondary_usage([ref], pub, ev))
        with self.assertRaisesRegex(ValueError, 'secondary_quote_unverified'):
            S.secondary_usage([dict(ref, secondary_quote='Invented quote with sufficiently long content.')], pub, ev)
        with self.assertRaisesRegex(ValueError, 'duplicate_secondary_claim'):
            S.secondary_usage([ref, ref], pub, ev)

    def test_daily_packet_saves_private_audit_without_sending_it_to_model(self):
        import json
        import stock_schedule as S
        from unittest.mock import patch
        archive = self.root/'knowledge-newsletters'
        archive.mkdir()
        self.issue('issue.md')
        (self.root/'issue.md').rename(archive/'issue.md')
        folder = self.root/'run'
        folder.mkdir()
        with patch.object(S, 'ROOT', self.root), \
             patch('runtime_config.load_sources', return_value={'email':{'newsletter_allowlist':['.morningstar.com']}}), \
             patch.object(S, 'datetime') as clock:
            clock.now.return_value = NOW
            public = S.newsletter_evidence({'companies':[]}, folder)
        saved = folder/'newsletter-evidence.json'
        self.assertEqual(len(json.loads(saved.read_text())['audit']), 1)
        self.assertNotIn('audit', public)
        self.assertEqual(public['newsletter_coverage']['selected'], 1)
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        original = saved.read_bytes()
        (folder/'request.json').write_text('{}')
        with patch('stock_newsletters.collect', side_effect=AssertionError('must reuse saved evidence')):
            retry = S.newsletter_evidence({'companies':[]}, folder)
        self.assertEqual(retry, public)
        self.assertEqual(saved.read_bytes(), original)
