import io
import zipfile
import unittest
from email.message import EmailMessage
from unittest.mock import patch
import workflow_mail_sources as sources

class SourceTests(unittest.TestCase):
 def test_docx_paragraphs_and_end_of_file_are_retained(self):
  stream=io.BytesIO()
  with zipfile.ZipFile(stream,'w') as z:
   z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>First sentence.</w:t></w:r></w:p><w:p><w:r><w:t>Last sentence.</w:t></w:r></w:p></w:body></w:document>')
  msg=EmailMessage();msg.set_content(stream.getvalue(),maintype='application',subtype='octet-stream');msg.add_header('Content-Disposition','attachment',filename='fixture.docx')
  text=sources.attachment_text(msg);self.assertEqual(text,'First sentence.\n\nLast sentence.')
  selected=sources.select_sources({'fixture':text+'\n'},'Last sentence')
  self.assertIn('Last sentence.',selected.values())
 def test_unknown_or_oversize_attachment_does_not_become_empty_success(self):
  msg=EmailMessage();msg.set_content(b'image',maintype='image',subtype='png');msg.add_header('Content-Disposition','attachment',filename='figure.png')
  with self.assertRaises(ValueError):sources.attachment_text(msg)
  with patch.object(sources,'MAX_MESSAGE_BYTES',1),self.assertRaises(ValueError):sources.attachment_text(msg)
 def test_selection_does_not_fragment_wrapped_sentence(self):
  text='The planning meeting is on\nThursday at ten.\n'
  selected=sources.select_sources({'fixture':text},'planning meeting')
  self.assertIn(text.strip(),selected.values())
 def test_historical_date_survives_selection(self):
  selected=sources.select_sources({'historical-correspondence-2020-01-01-mail-fixture':'The meeting was on Thursday.'},'meeting')
  self.assertTrue(all('historical-correspondence-2020-01-01' in key for key in selected))
 def test_readonly_exact_thread_and_original_sender(self):
  from pathlib import Path
  import intake, runtime_config
  from unittest.mock import MagicMock
  msg=EmailMessage();msg['From']='reader@example.test';msg['Message-ID']='<fixture@example.test>'
  msg['Date']='Thu, 01 Jan 2026 10:00:00 +0000';msg.set_content('The planning meeting is Thursday at ten.')
  raw=msg.as_bytes();mail=MagicMock();mail.__enter__.return_value=mail
  mail.select.return_value=('OK',[])
  def uid(command,*args):
   if command=='search':return 'OK',[b'1']
   if args[-1]=='(X-GM-THRID RFC822.SIZE)':return 'OK',[b'1 (X-GM-THRID 123 RFC822.SIZE 500)']
   if args[-1]=='(RFC822.SIZE)':return 'OK',[b'1 (RFC822.SIZE 500)']
   if args[-1]=='(BODY.PEEK[])':return 'OK',[(b'meta',raw)]
   raise AssertionError('unexpected mail operation')
  mail.uid.side_effect=uid
  account={'credential_key':'test','address':'owner@example.test','imap_server':'invalid.test'}
  rec={'message_id':'<fixture@example.test>','from_email':'reader@example.test','requester':'Fixture','body_head':'When is the planning meeting?'}
  with patch.object(sources.imaplib,'IMAP4_SSL',return_value=mail),patch.object(intake,'find_account',return_value=account),patch.object(runtime_config,'load_sources',return_value={}),patch.object(intake,'load_allowlist',return_value={'reader@example.test':{'name':'Fixture'}}):
   result=sources.collect(Path('.'),rec,{'test':'synthetic'})
   self.assertTrue(result);self.assertTrue(all('2026-01-01' in key for key in result))
   mail.select.assert_called_once_with('"[Gmail]/All Mail"',readonly=True)
   msg.replace_header('From','stranger@example.test');raw=msg.as_bytes()
   with self.assertRaises(AssertionError):sources.collect(Path('.'),rec,{'test':'synthetic'})
 def test_large_document_is_explicit_failure(self):
  with self.assertRaises(ValueError):sources.select_sources({'fixture':'x'*120001},'x')

if __name__=='__main__':unittest.main()
