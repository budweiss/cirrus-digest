"""Read-only Sent Mail proof for delivered contact-list workbooks. Never resends.

S592: list_delivery ledgers a delivery and intake_lifecycle marks the request
sent_unverified, but nothing ever checked Sent Mail, so every list sent to a
client stayed a Skywarden incident until someone closed it by hand. Exactly one
Sent Mail copy (ledgered subject, client To, Buddy Cc, the workbook attached)
is the proof; anything else leaves the request unverified.
"""
import email
import hashlib
import imaplib
import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

from intake_maintenance import atomic


def matches(raw, sender, recipients, cc, subject, filename, after):
    import intake
    msg = email.message_from_bytes(raw)
    addresses = lambda name: {a.lower() for _, a in getaddresses(msg.get_all(name, []))}
    try:
        stamp = parsedate_to_datetime(msg.get('Date', ''))
    except (TypeError, ValueError):
        return False
    if stamp.tzinfo is None:
        if not msg.get('Date', '').endswith('-0000'):
            return False
        stamp = stamp.replace(tzinfo=timezone.utc)
    if stamp.timestamp() < after:
        return False
    return (addresses('From') == {sender.lower()}
            and bool(addresses('To') & {r.lower() for r in recipients})
            and cc.lower() in addresses('Cc')
            and intake.decode_hdr(msg.get('Subject', '')) == subject
            and any(part.get_filename() == filename for part in msg.walk()))


def verify(root, run_id, client, creds, connect=imaplib.IMAP4_SSL):
    import intake
    import list_delivery
    from runtime_config import load_sources
    row = list_delivery.delivered(run_id, Path(root) / 'logs/contact_lists')
    if not row or not row.get('subject') or not row.get('file') or row.get('client') != client:
        return None
    recipients = json.loads((Path(root) / 'config/intake_senders.json').read_text()).get(client, {}).get('emails') or []
    account = intake.find_account(load_sources(intake.CONFIG_PATH))
    if not recipients or not account:
        return None
    sent_at = datetime.fromisoformat(row['ts'])
    since = (sent_at - timedelta(days=1)).strftime('%d-%b-%Y')
    after = sent_at.timestamp() - 600   # ledger is written just after SMTP accepts
    proofs = []
    with connect(account['imap_server'], account.get('imap_port', 993), timeout=15) as mail:
        mail.login(account['address'], creds[account['credential_key']])
        if mail.select('"[Gmail]/Sent Mail"', readonly=True)[0] != 'OK':
            return None
        from client_mail_review import quoted
        uids = []
        for recipient in recipients:
            status, ids = mail.uid('search', None, 'SINCE', since, 'TO', quoted(recipient))
            if status != 'OK' or len(ids[0].split()) > 50:
                return None
            uids += [u for u in ids[0].split() if u not in uids]
        for uid in uids:
            status, info = mail.uid('fetch', uid, '(RFC822.SIZE)')
            size = re.search(rb'RFC822.SIZE (\d+)', b' '.join(x for x in info if isinstance(x, bytes))) if status == 'OK' else None
            if not size or int(size.group(1)) > 10_000_000:
                return None
            status, parts = mail.uid('fetch', uid, '(BODY.PEEK[])')
            raw = b''.join(x[1] for x in parts if isinstance(x, tuple)) if status == 'OK' else b''
            if not raw or len(raw) != int(size.group(1)):
                return None
            if matches(raw, account['address'], recipients, list_delivery.CC_ADDR,
                       row['subject'], row['file'], after):
                proofs.append({'uid': uid.decode(), 'raw_sha256': hashlib.sha256(raw).hexdigest()})
    if len(proofs) != 1:
        return None
    path = Path(root) / 'data/intake-lifecycle' / ('sent-contact-' + run_id + '.json')
    atomic(path, {'verified': True, 'sent_copies': 1, 'copies': proofs, 'contact_list_run': run_id,
                  'subject_sha256': hashlib.sha256(row['subject'].encode()).hexdigest(), 'file': row['file']})
    return str(path)
