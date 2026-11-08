"""Synthetic tests; no real mail. S592 contact-list Sent Mail proof."""
import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path
from unittest.mock import patch

import contact_delivery_evidence as proof
import intake_lifecycle as life

CC = 'Buddy.Weiss@outlook.com'


def message(subject='Contact list', to='client@example.test', cc=CC, filename='contact-list-cl-1-client.xlsx', stamp=None):
    m = EmailMessage()
    m['From'] = 'box@example.test'; m['To'] = to; m['Cc'] = cc; m['Subject'] = subject
    m['Date'] = formatdate(stamp or time.time(), localtime=False)
    m.set_content('cover note')
    if filename:
        m.add_attachment(b'PK', maintype='application', subtype='octet-stream', filename=filename)
    return m.as_bytes()


class FakeIMAP:
    def __init__(self, raws):
        self.raws = raws
    def __call__(self, *a, **k):
        return self
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def login(self, *a):
        return 'OK', []
    def select(self, *a, **k):
        return 'OK', []
    def uid(self, command, *args):
        if command == 'search':
            return 'OK', [b' '.join(str(i + 1).encode() for i in range(len(self.raws)))]
        raw = self.raws[int(args[0]) - 1]
        if args[1] == '(RFC822.SIZE)':
            return 'OK', [b'1 (UID 1 RFC822.SIZE %d)' % len(raw)]
        return 'OK', [(b'1 (BODY[] {%d}' % len(raw), raw)]


class ContactProof(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup); self.root = Path(t.name)
        runs = self.root / 'logs/contact_lists'; runs.mkdir(parents=True)
        (runs / 'deliveries.jsonl').write_text(json.dumps({
            'event': 'delivered', 'run_id': 'cl-1', 'client': 'fixture', 'subject': 'Contact list',
            'file': 'contact-list-cl-1-client.xlsx', 'ts': time.strftime('%Y-%m-%dT%H:%M:%S')}) + '\n')
        (self.root / 'config').mkdir()
        (self.root / 'config/intake_senders.json').write_text(json.dumps({'fixture': {'emails': ['client@example.test']}}))

    def match(self, raw):
        return proof.matches(raw, 'box@example.test', ['client@example.test'], CC,
                             'Contact list', 'contact-list-cl-1-client.xlsx', time.time() - 600)

    def test_exact_copy_with_workbook_only(self):
        self.assertTrue(self.match(message()))
        for raw in (message(subject='Other'), message(to='wrong@example.test'), message(cc='wrong@example.test'),
                    message(filename=None), message(filename='other.xlsx'), message(stamp=time.time() - 3600)):
            self.assertFalse(self.match(raw))

    def verify(self, raws, client='fixture'):
        import intake
        with ExitStack() as stack:
            stack.enter_context(patch('runtime_config.load_sources', return_value={}))
            stack.enter_context(patch.object(intake, 'find_account', return_value={
                'imap_server': 'x', 'address': 'box@example.test', 'credential_key': 'k'}))
            return proof.verify(self.root, 'cl-1', client, {'k': 'secret'}, connect=FakeIMAP(raws))

    def test_one_copy_is_proof_two_or_none_are_not(self):
        self.assertIsNone(self.verify([]))
        self.assertIsNone(self.verify([message(), message()]))
        self.assertIsNone(self.verify([message()], client='other'))
        receipt = self.verify([message(subject='Other'), message()])
        self.assertEqual(json.loads(Path(receipt).read_text())['copies'][0]['uid'], '2')

    def test_lifecycle_marks_only_proven_contact_sends(self):
        rows = [(1, 'a@example.test', 'Research', 'ask', '<a>'), (2, 'b@example.test', 'Research', 'ask', '<b>'),
                (3, 'c@example.test', 'Re: x', 'ask', '<c>')]
        for row, run in zip(rows, ('cl-1', 'cl-2', None)):
            life.capture(self.root, row)
            evidence = {'client': 'fixture', **({'contact_list_run': run} if run else {})}
            life.update(self.root, row, 'sent_unverified', 'delivery-review', 'verify', evidence)
        calls = []
        fake = lambda root, run, client, creds: calls.append(run) or ('receipt' if run == 'cl-1' else None)
        self.assertEqual(life.verify_contact_sends(self.root, {}, verify=fake), 1)
        self.assertEqual(calls, ['cl-1', 'cl-2'])
        self.assertEqual(life.summary(self.root)['counts'], {'sent_verified': 1, 'sent_unverified': 2})


if __name__ == '__main__':
    unittest.main()
