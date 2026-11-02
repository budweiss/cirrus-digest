import io
import zipfile
import unittest
from email.message import EmailMessage
from unittest.mock import patch
import workflow_mail_sources as sources

class SourceTests(unittest.TestCase):
 def signature_message(self, html='<div id="Signature"><img src="cid:logo"></div>', disposition='inline', filename='logo.png'):
  msg=EmailMessage();msg.set_content('Find current snow removal opportunities.')
  msg.add_alternative('<html><body><p>Find current snow removal opportunities.</p>'+html+'</body></html>',subtype='html')
  msg.get_payload()[-1].add_related(b'synthetic image',maintype='image',subtype='png',cid='<logo>',disposition=disposition,filename=filename)
  return msg
 def test_only_explicit_exclusively_referenced_signature_images_are_skipped(self):
  for html in ('<div id="Signature"><span><img src="cid:logo"></span></div>', '<div class="gmail_signature"><img src="cid:logo"></div>'):
   msg=self.signature_message(html);cids=sources.signature_image_ids(msg,'Find snow removal bids.')
   self.assertEqual(cids,{'logo'})
   self.assertTrue(sources.is_signature_image(list(msg.walk())[-1],cids))
  for html in ('<p><img src="cid:logo"></p>', '<div id="Signature"><img src="cid:logo"></div><img src="cid:logo">', '<div id="Signature"><img src="cid:logo">'):
   self.assertFalse(sources.signature_image_ids(self.signature_message(html),'Find bids.'))
 def test_image_questions_and_real_attachments_remain_held(self):
  msg=self.signature_message()
  for question in ('Read the attached image.', 'Use this screenshot.', 'What is in this logo?'):
   self.assertFalse(sources.signature_image_ids(msg,question))
  msg=self.signature_message(disposition='attachment')
  self.assertFalse(sources.is_signature_image(list(msg.walk())[-1],{'logo'}))
  with patch.object(sources,'MAX_MESSAGE_BYTES',1):
   self.assertFalse(sources.is_signature_image(list(self.signature_message().walk())[-1],{'logo'}))
 def test_docx_paragraphs_and_end_of_file_are_retained(self):
  stream=io.BytesIO()
  with zipfile.ZipFile(stream,'w') as z:
   z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>First sentence.</w:t></w:r></w:p><w:p><w:r><w:t>Last sentence.</w:t></w:r></w:p></w:body></w:document>')
  msg=EmailMessage();msg.set_content(stream.getvalue(),maintype='application',subtype='octet-stream');msg.add_header('Content-Disposition','attachment',filename='fixture.docx')
  text=sources.attachment_text(msg);self.assertEqual(text,'First sentence.\n\nLast sentence.')
  selected=sources.select_sources({'fixture':text+'\n'},'Last sentence')
  self.assertIn('Last sentence.',selected.values())
 def test_html_attachment_yields_visible_text_only(self):
  msg=EmailMessage();msg.set_content(b'<html><head><style>p{color:red}</style></head><body><p>Vendor &amp; contact</p><script>hide()</script><td>Dover</td></body></html>',maintype='text',subtype='html');msg.add_header('Content-Disposition','attachment',filename='Report.HTML')
  self.assertEqual(sources.attachment_text(msg),'Vendor & contact\nDover')
  empty=EmailMessage();empty.set_content(b'<html><style>x</style></html>',maintype='text',subtype='html');empty.add_header('Content-Disposition','attachment',filename='blank.htm')
  self.assertEqual(sources.attachment_text(empty),'')
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
   if args[-1]=='(RFC822.SIZE)':return 'OK',[('1 (RFC822.SIZE %d)' % len(raw)).encode()]
   if args[-1]=='(BODY.PEEK[])':return 'OK',[(b'meta',raw)]
   raise AssertionError('unexpected mail operation')
  mail.uid.side_effect=uid
  account={'credential_key':'test','address':'owner@example.test','imap_server':'invalid.test'}
  rec={'message_id':'<fixture@example.test>','from_email':'reader@example.test','requester':'Fixture','body_head':'When is the planning meeting?'}
  with patch.object(sources.imaplib,'IMAP4_SSL',return_value=mail),patch.object(intake,'find_account',return_value=account),patch.object(runtime_config,'load_sources',return_value={}),patch.object(intake,'load_allowlist',return_value={'reader@example.test':{'name':'Fixture'}}):
   result=sources.collect(Path('.'),rec,{'test':'synthetic'})
   self.assertTrue(result);self.assertTrue(all('2026-01-01' in key for key in result))
   mail.select.assert_called_once_with('"[Gmail]/All Mail"',readonly=True)
   # Regression: complete inline deliverable, beyond the former 2000-char preview.
   msg.set_content('Introduction.\n' + 'Question and answer.\n' * 300 + 'FINAL-QUESTION-124')
   raw=msg.as_bytes()
   result=sources.collect(Path('.'),rec,{'test':'synthetic'})
   self.assertTrue(any('FINAL-QUESTION-124' in value for value in result.values()))
   # Real collection path: an Outlook signature must not block the request.
   signed=self.signature_message()
   for key in ('From','Message-ID','Date'):signed[key]=msg[key]
   raw=signed.as_bytes()
   result=sources.collect(Path('.'),rec,{'test':'synthetic'})
   self.assertTrue(any('snow removal opportunities' in value for value in result.values()))
   self.assertTrue(any('signature image(s) omitted' in value for value in result.values()))
   # An unnamed non-signature image must not disappear silently either.
   diagram=self.signature_message('<p><img src="cid:logo"></p>')
   for key in ('From','Message-ID','Date'):diagram[key]=msg[key]
   del list(diagram.walk())[-1]['Content-Disposition']
   raw=diagram.as_bytes()
   with self.assertRaisesRegex(ValueError,'supported local extractor'):
    sources.collect(Path('.'),rec,{'test':'synthetic'})
   msg.replace_header('From','stranger@example.test');raw=msg.as_bytes()
   with self.assertRaises(AssertionError):sources.collect(Path('.'),rec,{'test':'synthetic'})
 def test_large_document_is_explicit_failure(self):
  with self.assertRaises(ValueError):sources.select_sources({'fixture':'x'*120001},'x')

if __name__=='__main__':unittest.main()
