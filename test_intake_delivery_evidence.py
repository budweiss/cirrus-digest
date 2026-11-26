import unittest
from email.message import EmailMessage
from email.utils import formatdate
import time
from intake_delivery_evidence import matches

class Delivery(unittest.TestCase):
    def message(self, body='full answer', recipient='client@example.test', cc='owner@example.test', stamp=None):
        m=EmailMessage();m['From']='sender@example.test';m['To']=recipient
        m['Cc']=cc;m['Subject']='Re: request';m['Date']=formatdate(stamp or time.time(),localtime=False)
        m.set_content(body);return m.as_bytes()
    def match(self,raw):
        return matches(raw,'sender@example.test','client@example.test','owner@example.test','Re: request','full answer',time.time()-120)
    def test_full_body_and_all_parties(self):
        self.assertTrue(self.match(self.message()))
        for raw in (self.message(body='ack only'),self.message(recipient='wrong@example.test'),self.message(cc='wrong@example.test'),self.message(stamp=time.time()-3600)):
            self.assertFalse(self.match(raw))

    def test_a_large_send_in_the_window_does_not_block_proof(self):
        # S686: a 3.7 MB reply to Bill made verify() return None for every answer in his window.
        import tempfile
        from unittest import mock
        import intake_delivery_evidence as E
        answer = self.message()
        sizes = {b'1': 12_000_000, b'3': len(answer)}
        bodies = {b'2': self.message(body='a book with attachments'), b'3': answer}
        sizes[b'2'] = len(bodies[b'2'])
        class Box:
            def __init__(s, *a, **k): pass
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def login(s, *a): return 'OK', []
            def select(s, *a, **k): return 'OK', []
            def uid(s, cmd, *a):
                if cmd == 'search': return 'OK', [b'1 2 3']
                uid, what = a
                if what == '(RFC822.SIZE)': return 'OK', [b'%s (RFC822.SIZE %d)' % (uid, sizes[uid])]
                assert uid != b'1', 'oversized message must not be fetched'
                return 'OK', [(b'%s (BODY[] {%d}' % (uid, len(bodies[uid])), bodies[uid]), b')']
        row = {'answer': 'full answer', 'created': time.time() - 120}
        rec = {'solved': {'request_id': 'abc123'}, 'title': 'request', 'from_email': 'client@example.test'}
        account = {'imap_server': 'x', 'address': 'sender@example.test', 'credential_key': 'k'}
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(E.imaplib, 'IMAP4_SSL', Box), \
                mock.patch.object(E, 'Memory', lambda r: mock.Mock(get=lambda i: row)), \
                mock.patch('intake.find_account', return_value=account), \
                mock.patch('runtime_config.load_sources', return_value={}), \
                mock.patch('task_solver.CC_ADDR', 'owner@example.test'):
            receipt = E.verify(root, rec, {'k': 'placeholder'})
            self.assertTrue(receipt)
            self.assertIn("'uid': '3'", str(__import__('json').load(open(receipt))['copies']).replace('"', "'"))

if __name__=='__main__':unittest.main()
