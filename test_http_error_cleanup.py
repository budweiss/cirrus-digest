"""Response cleanup failure must not hide the safe provider-error result."""
import unittest
import urllib.error
from datetime import datetime,timezone
import stock_paper_market as market
class ErrorCleanup(unittest.TestCase):
    def test_broken_http_error_close_is_safe(self):
        class BrokenClose(urllib.error.HTTPError):
            def close(self):raise KeyError('synthetic missing file')
        def denied(*args,**kwargs):raise BrokenClose('https://example.invalid',403,'denied',{},None)
        result=market.get_quote('TEST',now=datetime(2026,10,19,14,tzinfo=timezone.utc),fetcher=denied)
        self.assertIn('provider_http_403',result['source_errors'])
if __name__=='__main__':unittest.main()
