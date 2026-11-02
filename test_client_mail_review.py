import copy
from email.message import EmailMessage
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import client_mail_review as review


def message(body='Delivered.', sender='owner@example.test', to='client@example.test', subject='A new subject'):
    msg = EmailMessage()
    msg['From'] = sender; msg['To'] = to; msg['Subject'] = subject
    msg['Message-ID'] = '<fixture@example.test>'
    msg.set_content(body)
    return msg


class ReviewTests(unittest.TestCase):
    def packets(self):
        incoming = review.inspect_message(message('Please send the bank.').as_bytes())
        incoming['direction'] = 'incoming'
        outgoing = review.inspect_message(message('Here is question 124 and its answer.').as_bytes())
        outgoing['direction'] = 'outgoing'
        packets = [dict(mailbox=box, client='fixture', since='01-Jan-2026',
                        search_complete=True, errors=[], messages=[])
                   for box in sorted(review.EXPECTED_MAILBOXES)]
        packets[0]['messages'] = [incoming]; packets[1]['messages'] = [outgoing]
        return packets, incoming, outgoing

    def test_full_inline_bank_and_html_alternative(self):
        bank = '\n'.join(f'Question {n}\nA) first B) second C) third D) fourth\nAnswer: A' for n in range(1,125))
        msg = message(bank)
        msg.add_alternative('<p>Additional answer only in HTML.</p>', subtype='html')
        row = review.inspect_message(msg.as_bytes())
        self.assertFalse(row['issues'])
        self.assertIn('Question 124', row['parts'][0]['text'])
        self.assertIn('Additional answer only in HTML.', row['parts'][1]['text'])
        self.assertGreater(len(row['parts'][0]['text']), 5000)

    def test_text_attachment_is_retained_not_confused_with_body(self):
        msg = message('See attachment.')
        msg.add_attachment(b'Question 124: answer A', maintype='text', subtype='plain', filename='bank.md')
        row = review.inspect_message(msg.as_bytes())
        self.assertFalse(row['issues'])
        self.assertEqual(row['parts'][1]['text'], 'Question 124: answer A')

    def test_images_and_broken_attachment_are_unknown_not_absent(self):
        for filename, mime in [('figure.png','image'), ('broken.pdf','application')]:
            msg = message();msg.add_attachment(b'not text',maintype=mime,subtype='octet-stream',filename=filename)
            row = review.inspect_message(msg.as_bytes())
            self.assertTrue(row['issues'])
            self.assertEqual(len(row['parts']),2)

    def test_invalid_charset_does_not_become_complete(self):
        msg = message();msg.set_param('charset','nonexistent-charset')
        self.assertTrue(review.inspect_message(msg.as_bytes())['issues'])

    def test_both_boxes_all_messages_and_parts_required(self):
        packets, incoming, outgoing = self.packets()
        shas = [incoming['sha256'], outgoing['sha256']]
        check = lambda pp, ss=shas: review.reconcile(pp, incoming['sha256'], ss)
        self.assertEqual(check(packets)['status'],'not_found_in_reviewed_scope')
        self.assertEqual(check(packets[:1])['status'],'unknown')
        self.assertEqual(check(packets,shas[:1])['status'],'unknown')
        for mutation in ('search', 'attachment', 'link', 'client', 'window'):
            p = copy.deepcopy(packets)
            if mutation == 'search':p[1]['search_complete']=False
            if mutation == 'attachment':p[1]['messages'][0]['issues']=['unread_part:2']
            if mutation == 'link':p[1]['messages'][0]['links']=['https://example.test/delivery']
            if mutation == 'client':p[1]['client']='different'
            if mutation == 'window':p[1]['since']='02-Jan-2026'
            self.assertEqual(check(p)['status'],'unknown',mutation)

    def test_delivery_requires_exact_outgoing_evidence_and_never_sends(self):
        packets, incoming, outgoing = self.packets()
        shas = [incoming['sha256'],outgoing['sha256']]
        match = [(outgoing['sha256'], '0', 'question 124 and its answer')]
        result = review.reconcile(packets, incoming['sha256'], shas, match)
        self.assertEqual(result['status'],'delivery_evidence_found')
        self.assertIsNone(result['work_owed']);self.assertFalse(result['automatic_resend_allowed'])
        self.assertEqual(review.reconcile(packets,incoming['sha256'],shas,
                         [(outgoing['sha256'],'0','invented quote')])['status'],'unknown')

    def test_real_collection_all_subjects_readonly_private_and_exact_recipients(self):
        raw = message('Full bank at the end.','owner@example.test').as_bytes()
        mail=MagicMock();mail.select.return_value=('OK',[])
        def uid(command,*args):
            if command=='search':return 'OK',[b'1']
            if args[-1]=='(RFC822.SIZE)':return 'OK',[f'1 (RFC822.SIZE {len(raw)})'.encode()]
            if args[-1]=='(BODY.PEEK[])':return 'OK',[(b'meta',raw)]
            raise AssertionError('unexpected IMAP operation')
        mail.uid.side_effect=uid
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'review'
            packet=review.collect(mail,['client@example.test'],'owner@example.test','01-Jan-2026',path,'fixture')
            self.assertTrue(packet['search_complete'])
            self.assertEqual(len(packet['messages']),1)
            self.assertEqual(packet['messages'][0]['direction'],'outgoing')
            self.assertEqual((path/'packet.json').stat().st_mode & 0o777,0o600)
            safe=json.dumps(review.summary(packet))
            self.assertNotIn('Full bank',safe);self.assertNotIn('A new subject',safe)
        for call in mail.select.call_args_list:self.assertTrue(call.kwargs['readonly'])
        self.assertFalse(any('SUBJECT' in str(c) or 'STORE' in str(c) for c in mail.uid.call_args_list))
        # A similar address returned by IMAP substring search must not match.
        raw=message(to='notclient@example.test').as_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(review.collect(mail,['client@example.test'],'owner@example.test','01-Jan-2026',Path(tmp),'fixture')['messages'])

    def test_large_image_is_inventoried_and_held_without_losing_message(self):
        msg = message('The requested diagram is attached.')
        msg.add_attachment(b'x' * 2_100_000, maintype='image', subtype='png', filename='diagram.png')
        raw = msg.as_bytes()
        row = review.inspect_message(raw)
        self.assertGreater(row['bytes'], 2_000_000)
        self.assertEqual(len(row['parts']), 2)
        self.assertIn('unread_part:2', row['issues'])
        self.assertIn('requested diagram', row['parts'][0]['text'])
        with patch.object(review, 'MAX_REVIEW_MESSAGE_BYTES', 100):
            self.assertIn('message_size_limit_or_empty', review.inspect_message(raw)['issues'])

    def test_partial_fetch_cannot_report_complete(self):
        mail=MagicMock();mail.select.return_value=('OK',[])
        mail.uid.side_effect=[('OK',[b'1']),('OK',[b'1 (RFC822.SIZE 999)']),('OK',[(b'meta',b'partial')])]
        with tempfile.TemporaryDirectory() as tmp:
            packet=review.collect(mail,['client@example.test'],'owner@example.test','01-Jan-2026',Path(tmp),'fixture')
            self.assertFalse(packet['search_complete'])
            self.assertTrue(packet['errors'])


if __name__ == '__main__':unittest.main()
