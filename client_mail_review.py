#!/usr/bin/env python3
"""Full, read-only client delivery evidence; private packets stay on this host.

Unlike a contact/ledger probe this scans all subjects, every configured client
address, and actual Sent Mail. It never sends, marks read, or declares work owed.
CLI stdout is metadata only. Use reconcile() on owned machines after examining
all packet contents; unsupported parts/links make negative findings unknown.
"""
import argparse
import email
from email import policy
from email.utils import getaddresses, parseaddr
import hashlib
import imaplib
import json
import os
from pathlib import Path
import re
import socket
from datetime import datetime, timezone
from workflow_mail_sources import full_body_documents, attachment_text

ROOT = Path(__file__).resolve().parent
EXPECTED_MAILBOXES = {'cirrustask@gmail.com', 'cumulus@cumulustask.com'}
MAX_MESSAGES = 500
# Full delivery audits retain large MIME attachments locally. The answer model
# extractor keeps its smaller bound; unsupported parts still hold absence.
MAX_REVIEW_MESSAGE_BYTES = 50_000_000
MAX_REVIEW_TOTAL_BYTES = 200_000_000


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def inspect_message(raw):
    """Preserve every MIME leaf, including alternatives and unsupported parts."""
    msg = email.message_from_bytes(raw, policy=policy.default)
    result = {'sha256': digest(raw), 'bytes': len(raw),
              'message_id': str(msg.get('Message-ID', '')),
              'date': str(msg.get('Date', '')), 'subject': str(msg.get('Subject', '')),
              'parts': [], 'issues': [], 'links': []}
    if not raw or len(raw) > MAX_REVIEW_MESSAGE_BYTES:
        result['issues'].append('message_size_limit_or_empty')
        return result
    try:
        bodies = full_body_documents(msg)
    except Exception:
        bodies = {}
        result['issues'].append('body_decode_or_mime_error')
    for n, part in enumerate(msg.walk()):
        if part.is_multipart():
            if part.get_content_type() == 'message/rfc822':
                result['issues'].append('embedded_message_requires_review')
            continue
        payload = part.get_payload(decode=True) or b''
        item = {'id': str(n), 'mime': part.get_content_type(),
                'filename': part.get_filename(), 'sha256': digest(payload),
                'bytes': len(payload), 'text': '', 'status': 'unread'}
        try:
            item['text'] = bodies[str(n)] if str(n) in bodies else attachment_text(part)
            if not item['text'].strip():
                raise ValueError('empty extraction')
            item['status'] = 'extracted'
        except Exception:
            result['issues'].append('unread_part:' + str(n))
        # Even text-bearing PDFs/DOCX may contain images. Text extraction is
        # evidence, not proof that every visual has been read.
        if item['filename'] and item['filename'].lower().endswith(('.pdf', '.docx')):
            item['status'] = 'text_extracted_visual_review_required'
            result['issues'].append('document_visual_review:' + str(n))
        result['parts'].append(item)
        result['links'].extend(re.findall(r'https?://[^\s<>"\)]+', item['text']))
    if any(p.defects for p in msg.walk()):
        result['issues'].append('malformed_mime')
    result['links'] = sorted(set(result['links']))
    return result


def quoted(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def write_private(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # O_EXCL also prevents accidentally replacing a previous audit snapshot.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)


def collect(mail, addresses, mailbox, since, directory, client):
    addresses = {a.lower() for a in addresses}
    packet = {'version': 1, 'client': client, 'mailbox': mailbox.lower(),
              'machine': socket.gethostname(), 'since': since,
              'collected_at': datetime.now(timezone.utc).isoformat(),
              'search_complete': False, 'errors': [], 'messages': []}
    seen = set()
    total_bytes = 0
    try:
        for folder, direction, fields in (
            ('"[Gmail]/All Mail"', 'incoming', ('FROM',)),
            ('"[Gmail]/Sent Mail"', 'outgoing', ('TO', 'CC')),
        ):
            if mail.select(folder, readonly=True)[0] != 'OK':
                raise ValueError('folder_unavailable')
            ids = set()
            for address in sorted(addresses):
                for field in fields:
                    status, data = mail.uid('search', None, 'SINCE', since, field, quoted(address))
                    if status != 'OK' or not data or not isinstance(data[0], bytes):
                        raise ValueError('search_failed')
                    ids.update(data[0].split())
            if len(ids) > MAX_MESSAGES:
                raise ValueError('message_count_limit')
            for uid in sorted(ids, key=int):
                status, data = mail.uid('fetch', uid, '(RFC822.SIZE)')
                meta = b' '.join(p for p in data or [] if isinstance(p, bytes))
                size = re.search(rb'RFC822.SIZE (\d+)', meta)
                if status != 'OK' or not size:
                    raise ValueError('size_fetch_failed')
                size = int(size.group(1))
                if size > MAX_REVIEW_MESSAGE_BYTES:
                    packet['errors'].append('message_size_limit')
                    continue
                total_bytes += size
                if total_bytes > MAX_REVIEW_TOTAL_BYTES:
                    raise ValueError('total_size_limit')
                status, data = mail.uid('fetch', uid, '(BODY.PEEK[])')
                raw = b''.join(p[1] for p in data or [] if isinstance(p, tuple))
                if status != 'OK' or not raw or len(raw) != size:
                    raise ValueError('incomplete_message_fetch')
                msg = email.message_from_bytes(raw, policy=policy.default)
                sender = parseaddr(str(msg.get('From', '')))[1].lower()
                dests = {a.lower() for _, a in getaddresses(
                    [str(msg.get('To', '')), str(msg.get('Cc', ''))])}
                # IMAP address searches are substring matches; verify exact
                # identities and actual mailbox sender before accepting evidence.
                if direction == 'incoming' and sender not in addresses:
                    continue
                if direction == 'outgoing' and not (dests & addresses and sender == mailbox.lower()):
                    continue
                key = (direction, digest(raw))
                if key in seen:
                    continue
                seen.add(key)
                row = inspect_message(raw)
                row.update(direction=direction, folder=folder, uid=uid.decode())
                write_private(directory / (row['sha256'] + '.eml'), raw)
                packet['messages'].append(row)
        packet['search_complete'] = not packet['errors']
    except Exception as exc:
        # Exception messages from IMAP/config are deliberately not echoed.
        packet['errors'].append(type(exc).__name__)
    write_private(directory / 'packet.json', json.dumps(packet, indent=2).encode())
    return packet


def summary(packet):
    rows = packet['messages']
    return {'client': packet['client'], 'machine': packet['machine'],
            'mailbox': packet['mailbox'], 'since': packet['since'],
            'search_complete': packet['search_complete'],
            'collection_errors': packet['errors'],
            'messages': len(rows),
            'incoming': sum(r['direction'] == 'incoming' for r in rows),
            'outgoing': sum(r['direction'] == 'outgoing' for r in rows),
            'parts_requiring_review': sum(len(r['issues']) for r in rows),
            'messages_with_links': sum(bool(r['links']) for r in rows),
            'status': 'review_required', 'work_owed': None,
            'automatic_resend_allowed': False}


def reconcile(packets, request_sha256, reviewed_sha256, matches=()):
    """Gate a negative finding after a full local review, never authorize a send.

    A delivery match is (outgoing raw SHA256, MIME part id, exact quote). For
    absence, every message must be read and every part extracted without gaps.
    Unsupported visual/link evidence remains unknown, not silently excluded.
    """
    unknown = {'status': 'unknown', 'work_owed': None, 'automatic_resend_allowed': False}
    if (len(packets) != 2 or {p['mailbox'] for p in packets} != EXPECTED_MAILBOXES
            or len({p['client'] for p in packets}) != 1
            or len({p['since'] for p in packets}) != 1
            or not all(p['search_complete'] and not p['errors'] for p in packets)):
        return unknown
    rows = [r for p in packets for r in p['messages']]
    if not any(r['sha256'] == request_sha256 and r['direction'] == 'incoming' for r in rows):
        return unknown
    if set(reviewed_sha256) != {r['sha256'] for r in rows}:
        return unknown
    if matches:
        for sha, part_id, quote in matches:
            if not quote.strip() or not any(
                r['sha256'] == sha and r['direction'] == 'outgoing'
                and any(p['id'] == part_id and quote in p['text'] for p in r['parts'])
                for r in rows):
                return unknown
        return dict(unknown, status='delivery_evidence_found')
    if any(r['issues'] or r['links'] for r in rows):
        return unknown
    # This is bounded absence, not a semantic conclusion that work is owed.
    return dict(unknown, status='not_found_in_reviewed_scope')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('client')
    ap.add_argument('--since', required=True, help='YYYY-MM-DD, on/before original request')
    args = ap.parse_args()
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,29}', args.client):
        ap.error('invalid client')
    since = datetime.strptime(args.since, '%Y-%m-%d').strftime('%d-%b-%Y')
    creds = json.loads((ROOT / 'config/credentials.json').read_text())
    clients = json.loads((ROOT / 'config/intake_senders.json').read_text())
    addresses = clients[args.client]['emails']
    if not addresses:
        raise ValueError('No configured client addresses')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    directory = ROOT / 'data/client-mail-review' / (args.client + '-' + stamp)
    with imaplib.IMAP4_SSL('imap.gmail.com', 993, timeout=60) as mail:
        mail.login(creds['outlook_email'], creds['outlook_password'])
        packet = collect(mail, addresses, creds['outlook_email'], since, directory, args.client)
    out = summary(packet)
    out['private_packet'] = str(directory / 'packet.json')
    print(json.dumps(out))
    return 0 if packet['search_complete'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'status': 'unknown', 'error': type(exc).__name__, 'work_owed': None}))
        raise SystemExit(1)
