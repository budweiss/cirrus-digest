"""What the reader receives: useful HTML with plain-text fallback, no raw HTML."""
import unittest
from stock_digest_send import build_message, report_html


class EmailPresentation(unittest.TestCase):
    def test_tables_links_and_internal_marker(self):
        md = '# Our account\n\n<!-- stock-paper-report:' + 'a' * 32 + ' -->\n\n'
        md += '| Stock | Shares |\n|---|---:|\n| SYN | 4 |\n\n**Why:** [Release](https://example.com/release).'
        html = report_html(md)
        self.assertIn('<table', html)
        self.assertIn('<th ', html)
        self.assertIn('>SYN</td>', html)
        self.assertNotIn('stock-paper-report', html)
        self.assertNotIn('|---', html)
        self.assertIn('<a href="https://example.com/release">Release</a>', html)

    def test_source_html_is_escaped_and_unsafe_link_not_activated(self):
        html = report_html('<script>bad()</script>\n\n[click](javascript:bad)')
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('href="javascript:', html)

    def test_mime_has_plain_and_html_versions(self):
        (message, _), _ = build_message('# Readable report\n\nBody.', creds={'outlook_email': 'test@example.com'})
        self.assertEqual(message.get_content_type(), 'multipart/alternative')
        self.assertEqual([part.get_content_type() for part in message.get_payload()], ['text/plain', 'text/html'])
        self.assertIn('<h1>Readable report</h1>', message.get_payload()[1].get_payload(decode=True).decode())


if __name__ == '__main__':
    unittest.main()
