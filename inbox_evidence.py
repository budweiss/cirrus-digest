"""Pure local evidence preparation; no inference, network, sends, or persistence."""
import hashlib
import re
from email.header import decode_header, make_header


def decoded_subject(value):
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeError, ValueError) as exc:
        raise ValueError('Invalid encoded subject') from exc


def evidence_spans(source_id, text, provenance, max_chars=700):
    """Keep paragraph/sentence boundaries and immutable offsets; never truncate."""
    if provenance not in {'correspondence', 'reference', 'current_fact', 'request'}:
        raise ValueError('Untrusted evidence provenance')
    out = []
    for paragraph in re.finditer(r'\S(?:.*?\S)?(?=\s*\n[ \t\r]*\n|\s*\Z)', text, re.S):
        start, end = paragraph.span()
        parts = [(start, end)]
        if end-start > max_chars:
            parts = []
            cursor = start
            for boundary in re.finditer(r'(?<=[.!?])\s+(?=[A-Z0-9])', text[start:end]):
                stop = start+boundary.start()
                if stop > cursor:
                    parts.append((cursor, stop))
                cursor = start+boundary.end()
            if cursor < end:
                parts.append((cursor, end))
        for lo, hi in parts:
            if not text[lo:hi].strip():
                continue
            digest = hashlib.sha256((source_id+'\0'+text).encode()).hexdigest()
            key = hashlib.sha256(f'{digest}:{lo}:{hi}'.encode()).hexdigest()[:20]
            out.append(dict(id=key, source_id=source_id, source_sha256=digest,
                            start=lo, end=hi, text=text[lo:hi], provenance=provenance))
    if text.strip() and not out:
        raise ValueError("Nonempty source produced no evidence spans")
    return out


def verify_span(span, source_text):
    digest = hashlib.sha256((span['source_id']+'\0'+source_text).encode()).hexdigest()
    return (span.get('provenance') in {'correspondence','reference','current_fact','request'} and
            span['source_sha256'] == digest and
            0 <= span['start'] < span['end'] <= len(source_text) and
            source_text[span['start']:span['end']] == span['text'])


def exact_quote(source, proposed):
    """No fuzzy matching, normalization, concatenation or silent substitution."""
    return isinstance(proposed, str) and len(proposed.strip()) >= 4 and proposed in source


def delivered_context(rows):
    return [r for r in rows if r.get('state') == 'delivered'
            and r.get('meta', {}).get('delivery_confirmed') is True]


def select_budget(spans, selected_ids, budget=5700):
    """Model selects existing IDs only; unknown IDs fail closed."""
    lookup = {s['id']:s for s in spans}
    result = {};used=0
    for key in dict.fromkeys(selected_ids):
        if key not in lookup:
            raise ValueError('Unknown evidence ID')
        text=lookup[key]['text']
        if used+len(text)>budget:
            raise ValueError('Selected evidence exceeds budget')
        result[key]=text;used+=len(text)
    return result


def canonical_view(raw_text):
    """Normalize layout BEFORE inference; original immutable spans remain retained."""
    paragraphs=re.split(r'\r?\n[ \t]*\r?\n',raw_text)
    return '\n\n'.join(re.sub(r'\s+',' ',p).strip() for p in paragraphs if p.strip())


def trace_canonical_quote(raw_text, quote):
    """Map an exact canonical-view quote back to contiguous raw source, or refuse."""
    if not exact_quote(canonical_view(raw_text),quote):
        raise ValueError('Quote absent from canonical source')
    # Whitespace is the sole transformation. No synonym, spelling or punctuation fix.
    pattern=r'\s+'.join(re.escape(w) for w in quote.split(' '))
    matches=[m for m in re.finditer(pattern,raw_text) if canonical_view(m.group())==quote and not re.search(r'\r?\n[ \t]*\r?\n',m.group())]
    if len(matches)!=1:
        raise ValueError('Quote is not uniquely traceable within one paragraph')
    match=matches[0]
    return match.start(),match.end(),match.group()


def thread_attachments(messages, thread_message_ids):
    """Get extracted attachments from exact thread IDs in an already scoped corpus."""
    unfold = lambda value: re.sub(r'\s+', '', value)
    ids = {unfold(value) for value in thread_message_ids}
    documents = []
    for message in messages:
        message_id = unfold(message['message_id'])
        if message_id not in ids:
            continue
        for index, attachment in enumerate(message.get('attachments', [])):
            text = attachment.get('text', '')
            if not text:
                continue
            key = hashlib.sha256(f'{message_id}:{index}'.encode()).hexdigest()
            documents.append({'source_id': 'thread-attachment-' + key,
                              'text': text, 'name': attachment['name'],
                              'date': message.get('date', '')})
    return documents


def thread_request_timestamp(messages, message_id):
    """Use the exact message's timezone-aware header, never a naive audit timestamp."""
    from email.utils import parsedate_to_datetime
    unfold = lambda value: re.sub(r'\s+', '', value)
    matches = [m for m in messages if unfold(m['message_id']) == unfold(message_id)]
    if len(matches) != 1:
        raise ValueError('Request must match exactly one thread message')
    parsed = parsedate_to_datetime(matches[0]['date'])
    if parsed.tzinfo is None:
        raise ValueError('Request header has no timezone')
    return parsed.timestamp()


def thread_reply_sender(record, messages, allowlist):
    """Resolve legacy envelopes from the exact original header and sender allowlist."""
    from email.utils import parseaddr
    unfold = lambda value: re.sub(r'\s+', '', value)
    matches = [m for m in messages if unfold(m['message_id']) == unfold(record['message_id'])]
    if len(matches) != 1:
        raise ValueError('Request must match exactly one thread message')
    address = parseaddr(matches[0]['sender'])[1].lower()
    entry = allowlist.get(address)
    if not entry or entry['name'].lower() != record['requester'].lower():
        raise ValueError('Original sender does not match the allowlisted requester')
    for key in ('from', 'from_email'):
        if record.get(key) and parseaddr(record[key])[1].lower() != address:
            raise ValueError('Cached sender conflicts with original header')
    return address
