import unittest
import contextlib
import io
from datetime import date, timedelta
from unittest.mock import patch
import business_idea_scan as B

class DiscoveryDirectionTests(unittest.TestCase):
    def test_rotation_reaches_every_query_without_duplicates(self):
        seen=set()
        for offset in range(8):
            with patch('datetime.date') as day:
                day.today.return_value=date(2026,10,17)+timedelta(days=offset)
                queries=B.todays_queries()
            self.assertEqual(len(queries),3)
            self.assertEqual(len(set(queries)),3)
            seen.update(queries)
        self.assertEqual(seen,set(B.BUSINESS_SEARCH_QUERIES))
        self.assertTrue(any('supplier' in q for q in seen))
        self.assertTrue(any('print' in q for q in seen))
        self.assertFalse(any(any(t in q for t in ('newsletter','alerting','monitoring')) for q in seen))

    def test_actual_gate_packet_preserves_evidence_and_outreach_limits(self):
        packet=B._gate_prompt('Synthetic customer conversion','Listed price only; no verified sales.')
        self.assertIn('Listed price only; no verified sales.',packet)
        self.assertIn('not\n   proof of sales',packet)
        self.assertIn('Customer outreach and paid\n   pilots are paused',packet)
        self.assertNotIn('it proves both',packet)
        self.assertIn('SCORE: <0-10>',packet)

    def test_commercial_search_selftest_accepts_pricing_but_rejects_generic_ideas(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertTrue(B.selftest())
        queries=list(B.BUSINESS_SEARCH_QUERIES)
        queries[0]='best generic startup ideas'
        output=io.StringIO()
        with patch.object(B,'BUSINESS_SEARCH_QUERIES',queries), contextlib.redirect_stdout(output):
            self.assertFalse(B.selftest())
        self.assertIn("FAIL  search terms target revenue/operating businesses, not 'ideas'",output.getvalue())

if __name__=='__main__':unittest.main()
