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


def selftest():
    # decoded_subject
    assert decoded_subject('Hello world') == 'Hello world'
    assert decoded_subject('=?utf-8?b?SGVsbG8=?= there') == 'Hello there'
    try:
        decoded_subject('=?bogus-charset?b?AAAA?=')
        raise AssertionError('decoded_subject accepted invalid charset')
    except ValueError:
        pass

    # evidence_spans
    text = 'First paragraph here.\n\nSecond paragraph is longer. It has two sentences.'
    spans = evidence_spans('src1', text, 'correspondence')
    assert len(spans) == 2, spans
    assert spans[0]['text'] == 'First paragraph here.'
    assert spans[0]['start'] == 0 and spans[0]['end'] == len('First paragraph here.')
    assert spans[1]['provenance'] == 'correspondence'
    digest = hashlib.sha256(('src1' + '\0' + text).encode()).hexdigest()
    assert all(s['source_sha256'] == digest for s in spans)
    try:
        evidence_spans('src1', text, 'bogus')
        raise AssertionError('evidence_spans accepted bad provenance')
    except ValueError:
        pass
    assert evidence_spans('src1', '', 'reference') == []
    long_text = 'Sentence one starts here. Second sentence follows on. Third one ends it.'
    chopped = evidence_spans('src1', long_text, 'request', max_chars=30)
    assert len(chopped) > 1
    assert ''.join(s['text'] + ' ' for s in chopped).strip() == long_text

    # verify_span
    span = spans[0]
    assert verify_span(span, text) is True
    bad = dict(span, start=1)
    assert verify_span(bad, text) is False
    bad = dict(span, provenance='bogus')
    assert verify_span(bad, text) is False

    # exact_quote
    assert exact_quote('the quick brown fox', 'quick brown') is True
    assert exact_quote('the quick brown fox', 'quick  brown') is False
    assert exact_quote('abc', 'abc') is False  # under min length
    assert exact_quote('abc', 12) is False

    # delivered_context
    rows = [{'state': 'delivered', 'meta': {'delivery_confirmed': True}},
            {'state': 'delivered', 'meta': {'delivery_confirmed': False}},
            {'state': 'queued', 'meta': {'delivery_confirmed': True}},
            {'state': 'delivered', 'meta': {}}]
    assert delivered_context(rows) == [rows[0]]

    # select_budget
    picked = select_budget(spans, [spans[0]['id'], spans[0]['id'], spans[1]['id']])
    assert list(picked.keys()) == [spans[0]['id'], spans[1]['id']]
    try:
        select_budget(spans, ['unknown-id'])
        raise AssertionError('select_budget accepted unknown ID')
    except ValueError:
        pass
    try:
        select_budget(spans, [s['id'] for s in spans], budget=10)
        raise AssertionError('select_budget ignored budget')
    except ValueError:
        pass

    # canonical_view / trace_canonical_quote
    raw = 'Alpha  one\ttwo.\n\nBeta   three four.'
    assert canonical_view(raw) == 'Alpha one two.\n\nBeta three four.'
    start, end, matched = trace_canonical_quote(raw, 'Beta three four.')
    assert raw[start:end] == matched
    assert canonical_view(matched) == 'Beta three four.'
    try:
        trace_canonical_quote(raw, 'not in source')
        raise AssertionError('trace_canonical_quote accepted absent quote')
    except ValueError:
        pass
    dup = 'Same words here.\n\nSame words here.'
    try:
        trace_canonical_quote(dup, 'Same words here.')
        raise AssertionError('trace_canonical_quote accepted ambiguous quote')
    except ValueError:
        pass

    # thread_attachments
    messages = [{'message_id': '<m1\n@x>', 'date': 'd1',
                 'attachments': [{'name': 'a.txt', 'text': 'body'},
                                 {'name': 'b.txt', 'text': ''}]},
                {'message_id': '<m2@x>', 'date': 'd2',
                 'attachments': [{'name': 'c.txt', 'text': 'other'}]}]
    docs = thread_attachments(messages, ['<m1@x>'])
    assert len(docs) == 1 and docs[0]['text'] == 'body' and docs[0]['name'] == 'a.txt'
    assert docs[0]['source_id'].startswith('thread-attachment-')
    assert thread_attachments(messages, ['<none@x>']) == []

    # thread_request_timestamp
    msgs = [{'message_id': '<r@x>', 'date': 'Mon, 27 Oct 2026 12:00:00 +0000'}]
    ts = thread_request_timestamp(msgs, '<r@x>')
    assert isinstance(ts, float)
    try:
        thread_request_timestamp(msgs, '<r@x>' if len(msgs) == 0 else '<missing@x>')
        raise AssertionError('thread_request_timestamp accepted missing id')
    except ValueError:
        pass
    naive = [{'message_id': '<n@x>', 'date': 'Mon, 27 Oct 2026 12:00:00 -0000'}]
    try:
        thread_request_timestamp(naive, '<n@x>')
        raise AssertionError('thread_request_timestamp accepted naive date')
    except ValueError:
        pass

    # thread_reply_sender
    allow = {'alice@example.com': {'name': 'Alice'}}
    thread = [{'message_id': '<t@x>', 'sender': 'Alice <alice@example.com>'}]
    record = {'message_id': '<t@x>', 'requester': 'alice',
              'from': 'Alice <alice@example.com>'}
    assert thread_reply_sender(record, thread, allow) == 'alice@example.com'
    try:
        thread_reply_sender({'message_id': '<t@x>', 'requester': 'Mallory'}, thread, allow)
        raise AssertionError('thread_reply_sender accepted wrong requester')
    except ValueError:
        pass
    try:
        thread_reply_sender({'message_id': '<t@x>', 'requester': 'Alice',
                             'from_email': 'bob@example.com'}, thread, allow)
        raise AssertionError('thread_reply_sender accepted conflicting cache')
    except ValueError:
        pass
    print('inbox_evidence selftest: OK')


if __name__ == '__main__':
    import sys
    if len(sys.argv) == 2 and sys.argv[1] == '--selftest':
        selftest()
    else:
        sys.stderr.write('usage: inbox_evidence.py --selftest\n')
        sys.exit(2)
