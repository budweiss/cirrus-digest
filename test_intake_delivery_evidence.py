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

if __name__=='__main__':unittest.main()
