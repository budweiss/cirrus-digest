"""Bounded read-only exact-thread evidence. Private contents never leave the host."""
import email
import hashlib
import imaplib
import io
import re
import zipfile
from email.utils import parseaddr
from xml.etree import ElementTree
from inbox_evidence import evidence_spans, verify_span

MAX_MESSAGE_BYTES=2_000_000
MAX_THREAD_MESSAGES=12


def attachment_text(part):
    raw=part.get_payload(decode=True) or b''
    if len(raw)>MAX_MESSAGE_BYTES:raise ValueError('Attachment exceeds evidence limit')
    name=(part.get_filename() or '').lower()
    if name.endswith('.txt'):return raw.decode(part.get_content_charset() or 'utf-8')
    if name.endswith('.docx'):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            info=archive.getinfo('word/document.xml')
            if info.file_size>10_000_000:raise ValueError('Expanded attachment exceeds limit')
            tree=ElementTree.fromstring(archive.read(info))
        ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        return '\n\n'.join(''.join(p.itertext()) for p in tree.findall('.//w:p',ns))
    if name.endswith('.pdf'):
        from pypdf import PdfReader
        doc=PdfReader(io.BytesIO(raw))
        if len(doc.pages)>30:raise ValueError('PDF exceeds evidence limit')
        return '\n\n'.join(p.extract_text() or '' for p in doc.pages)
    raise ValueError('Attachment needs a supported local extractor')


def select_sources(documents, question, budget=5000):
    terms=set(re.findall(r'[a-z]{4,}',question.lower()))
    options=[]
    for key,text in documents.items():
        if len(text)>120000:raise ValueError('Document exceeds evidence limit')
        spans=evidence_spans(key,text,'correspondence')
        for span in spans:
            assert verify_span(span,text)
            if len(span['text'])<=900:
                options.append((sum(span['text'].lower().count(w) for w in terms),key,span))
    selected={};used=0
    # Give each actual document an opportunity before spending on extra matches.
    ranked=sorted(options,key=lambda x:x[0],reverse=True)
    first=[];seen=set()
    for item in ranked:
        if item[1] not in seen:first.append(item);seen.add(item[1])
    for _,document,span in first+ranked:
        key=document+':'+span['id']
        if key not in selected and used+len(span['text'])<=budget:
            selected[key]=span['text'];used+=len(span['text'])
    return selected


def collect(root, rec, creds):
    import intake
    import task_solver
    from runtime_config import load_sources
    account=intake.find_account(load_sources(intake.CONFIG_PATH))
    password=creds.get(account['credential_key'])
    if not password:raise ValueError('Missing mailbox evidence credential')
    mid=re.sub(r'\s+','',rec['message_id'])
    # Recovery suffix is not part of the actual RFC Message-ID.
    mid=mid.split('#information-recovery-',1)[0]
    escaped=mid.replace('\\','\\\\').replace('"','\\"')
    documents={}
    with imaplib.IMAP4_SSL(account['imap_server'],account.get('imap_port',993),timeout=15) as mail:
        mail.login(account['address'],password)
        assert mail.select('"[Gmail]/All Mail"',readonly=True)[0]=='OK'
        status,ids=mail.uid('search',None,'HEADER','Message-ID','"'+escaped+'"')
        assert status=='OK' and len(ids[0].split())==1
        original_uid=ids[0].split()[0]
        status,meta=mail.uid('fetch',original_uid,'(X-GM-THRID RFC822.SIZE)')
        assert status=='OK'
        header=b' '.join(x for x in meta if isinstance(x,bytes))
        match=re.search(rb'X-GM-THRID (\d+)',header);assert match
        status,ids=mail.uid('search',None,'X-GM-THRID',match.group(1).decode());assert status=='OK'
        uids=ids[0].split()
        if len(uids)>MAX_THREAD_MESSAGES:raise ValueError('Thread exceeds evidence limit')
        original_seen=False
        sender=(rec.get('from_email') or parseaddr(rec.get('from',''))[1]).lower()
        assert sender and intake.load_allowlist().get(sender,{}).get('name','').lower()==rec['requester'].lower()
        for uid in uids:
            status,info=mail.uid('fetch',uid,'(RFC822.SIZE)');assert status=='OK'
            size=re.search(rb'RFC822.SIZE (\d+)',b' '.join(x for x in info if isinstance(x,bytes)));assert size
            if int(size.group(1))>MAX_MESSAGE_BYTES:raise ValueError('Message exceeds evidence limit')
            status,parts=mail.uid('fetch',uid,'(BODY.PEEK[])');assert status=='OK'
            raw=b''.join(p[1] for p in parts if isinstance(p,tuple));assert len(raw)<=MAX_MESSAGE_BYTES
            msg=email.message_from_bytes(raw)
            author=parseaddr(msg.get('From',''))[1].lower()
            if author not in (sender,account['address'].lower()):continue
            key=re.sub(r'\s+','',msg.get('Message-ID',''))
            if uid==original_uid:
                assert key==mid and author==sender
                original_seen=True
            # Evidence is dated correspondence, never proof of current runtime.
            date=msg.get('Date','');stamp=email.utils.parsedate_to_datetime(date);assert stamp.tzinfo
            docid=hashlib.sha256((key+date).encode()).hexdigest()[:20]
            body=task_solver.strip_quoted_reply(intake.body_text(msg))
            if body.strip():documents['historical-correspondence-'+stamp.isoformat()+'-mail-'+docid]=body
            for n,part in enumerate(msg.walk()):
                if part.get_filename():
                    text=attachment_text(part)
                    if not text.strip():raise ValueError('Attachment yielded no text')
                    documents['historical-correspondence-'+stamp.isoformat()+'-attachment-'+docid+'-'+str(n)]=text
        assert original_seen
    return select_sources(documents,rec.get('conversation_body') or rec.get('body_head',''))
