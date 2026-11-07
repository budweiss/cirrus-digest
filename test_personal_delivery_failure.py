import json
import smtplib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import personal_digest_send as sender


class DeliveryFailureTests(unittest.TestCase):
    def test_real_smtp_frames_classify_connect_data_and_quit(self):
        for phase in ('connect', 'data', 'quit'):
            with self.subTest(phase=phase):
                smtp = smtplib.SMTP()
                error = smtplib.SMTPServerDisconnected('SENSITIVE response text')
                with patch.object(smtp, '_get_socket', return_value=Mock()), \
                     patch.object(smtp, 'send'), \
                     patch.object(smtp, 'getreply', side_effect=error):
                    try:
                        if phase == 'connect':
                            smtp.connect('synthetic.invalid')
                        elif phase == 'data':
                            smtp.data('PRIVATE synthetic report')
                        else:
                            smtp.quit()
                    except smtplib.SMTPServerDisconnected as exc:
                        self.assertEqual(sender.delivery_failure(exc),
                                         {'kind': 'disconnected', 'phase': phase})
                    else:
                        self.fail('SMTP call did not raise')

    def test_failed_send_keeps_slot_and_sanitizes_receipt(self):
        for failure in (smtplib.SMTPServerDisconnected('PRIVATE content'),
                        smtplib.SMTPAuthenticationError(535, b'SECRET'),
                        TimeoutError('PRIVATE address')):
            with self.subTest(kind=type(failure).__name__), tempfile.TemporaryDirectory() as td, \
                 patch.object(sender, 'RECEIPTS', Path(td)):
                send = Mock(side_effect=failure)
                creds = {'outlook_email': 'synthetic@example.com',
                         'outlook_password': 'SECRET'}
                args = ('PRIVATE body', creds, 'PRIVATE subject', '2026-11-06')
                with self.assertRaises(type(failure)):
                    sender.deliver_once(*args, send_fn=send)
                receipt = Path(td) / '2026-11-06.json'
                raw = receipt.read_text()
                record = json.loads(raw)
                self.assertEqual(record['status'], 'sending_outcome_unknown_until_confirmed')
                self.assertIn('failed_at', record)
                self.assertEqual(record['failure']['phase'], 'unknown')
                self.assertNotIn('PRIVATE', raw)
                self.assertNotIn('SECRET', raw)
                self.assertNotIn('synthetic@example.com', raw)
                self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
                with self.assertRaisesRegex(ValueError, 'daily_slot_already_reserved'):
                    sender.deliver_once(*args, send_fn=send)
                self.assertEqual(send.call_count, 1)

    def test_diagnostic_write_failure_preserves_original_exception_and_lock(self):
        with tempfile.TemporaryDirectory() as td, patch.object(sender, 'RECEIPTS', Path(td)):
            error = smtplib.SMTPServerDisconnected('PRIVATE')
            with patch.object(Path, 'write_text', side_effect=OSError('disk full')):
                with self.assertRaises(smtplib.SMTPServerDisconnected):
                    sender.deliver_once('synthetic', {'outlook_email': 'x', 'outlook_password': 'x'},
                                        's', '2026-11-06', send_fn=Mock(side_effect=error))
            record = json.loads((Path(td) / '2026-11-06.json').read_text())
            self.assertEqual(record['status'], 'sending_outcome_unknown_until_confirmed')

    def test_no_false_success_when_sender_returns_false(self):
        with tempfile.TemporaryDirectory() as td, patch.object(sender, 'RECEIPTS', Path(td)):
            with self.assertRaisesRegex(ValueError, 'send_unconfirmed'):
                sender.deliver_once('synthetic', {'outlook_email': 'x', 'outlook_password': 'x'},
                                    's', '2026-11-06', send_fn=lambda *a, **k: False)
            record = json.loads((Path(td) / '2026-11-06.json').read_text())
            self.assertEqual(record['status'], 'sending_outcome_unknown_until_confirmed')
            self.assertEqual(record['failure'], {'kind': 'other', 'phase': 'unknown'})


if __name__ == '__main__':
    unittest.main()
