"""SMTP can accept a subset without raising; never report all delivered."""
import smtplib
import unittest
from unittest.mock import patch
import mailer


class RecipientTests(unittest.TestCase):
    def test_partial_refusal_fails_instead_of_confirming_both(self):
        with patch.object(mailer.smtplib, 'SMTP') as smtp:
            smtp.return_value.__enter__.return_value.sendmail.return_value = {'second@example.test': (550, b'refused')}
            with self.assertRaises(smtplib.SMTPRecipientsRefused) as error:
                mailer.send('sender@example.test', 'fake', ['first@example.test', 'second@example.test'],
                            'test', 'body', log=None, watch_promises=False)
            self.assertEqual(set(error.exception.recipients), {'second@example.test'})

    def test_no_refusals_confirms_success(self):
        with patch.object(mailer.smtplib, 'SMTP') as smtp:
            smtp.return_value.__enter__.return_value.sendmail.return_value = {}
            self.assertTrue(mailer.send('sender@example.test', 'fake', ['first@example.test', 'second@example.test'],
                                       'test', 'body', log=None, watch_promises=False))


if __name__ == '__main__':
    unittest.main()
