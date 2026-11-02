"""Read-only exact Sent Mail proof for plain-text answer drafts. Never resend."""
import email
import hashlib
import imaplib
import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from conversation_memory import Memory
from workflow_mail_sources import full_body_documents
from intake_maintenance import atomic


def matches(raw, sender, recipient, cc, subject, body, after):
    msg=email.message_from_bytes(raw)
    addresses=lambda name:{a.lower() for _,a in getaddresses(msg.get_all(name,[]))}
    import intake
    stamp=parsedate_to_datetime(msg.get('Date',''))
    if stamp.tzinfo is None and msg.get('Date','').endswith('-0000'):
        stamp=stamp.replace(tzinfo=timezone.utc)
    if not stamp.tzinfo or stamp.timestamp()<after:return False
    if addresses('From')!={sender.lower()} or recipient.lower() not in addresses('To') or cc.lower() not in addresses('Cc'):
        return False
    if intake.decode_hdr(msg.get('Subject',''))!=subject:return False
    normalize=lambda s:s.replace('\r\n','\n').strip()
    return any(normalize(value)==normalize(body) for value in full_body_documents(msg).values())


def verify(root, rec, creds):
    request_id=(rec.get('solved') or {}).get('request_id')
    if not request_id:return None
    row=Memory(root).get(request_id)
    if not row or not row['answer']:return None
    import intake
    import task_solver
    from runtime_config import load_sources
    account=intake.find_account(load_sources(intake.CONFIG_PATH))
    subject=rec.get('original_subject') or rec['title']
    if not subject.lower().startswith('re:'):subject='Re: '+subject
    since=(datetime.fromtimestamp(row['created'])-timedelta(days=1)).strftime('%d-%b-%Y')
    proofs=[]
    with imaplib.IMAP4_SSL(account['imap_server'],account.get('imap_port',993),timeout=15) as mail:
        mail.login(account['address'],creds[account['credential_key']])
        if mail.select('"[Gmail]/Sent Mail"',readonly=True)[0]!='OK':return None
        from client_mail_review import quoted
        status,ids=mail.uid('search',None,'SINCE',since,'TO',quoted(rec['from_email']))
        if status!='OK' or len(ids[0].split())>50:return None
        for uid in ids[0].split():
            status,info=mail.uid('fetch',uid,'(RFC822.SIZE)')
            if status!='OK':return None
            size=re.search(rb'RFC822.SIZE (\d+)',b' '.join(x for x in info if isinstance(x,bytes)))
            if not size or int(size.group(1))>2_000_000:return None
            status,parts=mail.uid('fetch',uid,'(BODY.PEEK[])')
            if status!='OK':return None
            raw=b''.join(x[1] for x in parts if isinstance(x,tuple))
            if not raw or len(raw)!=int(size.group(1)):return None
            if matches(raw,account['address'],rec['from_email'],task_solver.CC_ADDR,subject,row['answer'],row['created']):
                proofs.append({'uid':uid.decode(),'raw_sha256':hashlib.sha256(raw).hexdigest()})
    if len(proofs)!=1:return None
    path=Path(root)/'data/intake-lifecycle'/('sent-'+request_id+'.json')
    atomic(path,{'verified':True,'sent_copies':1,'copies':proofs,'answer_request_id':request_id,
                 'answer_sha256':hashlib.sha256(row['answer'].encode()).hexdigest()})
    return str(path)
