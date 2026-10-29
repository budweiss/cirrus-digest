"""Bounded read-only owner questions; no model, trading, or configuration tools."""
import json
import re
import socket
import sqlite3
import subprocess
from collections import Counter
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit


def route(rec, subject, body):
    import task_solver as ts
    # requester comes from the server allowlist, never the message body.
    if rec.get('requester', '').lower() != 'buddy' or rec.get('status') == 'refused':
        return False
    if rec.get('kind') in ('resend', 'confirmation', 'deliverable'):
        return False
    own = ts.strip_quoted_reply(body or '')
    question = own if ts.is_reply_subject(subject) else subject + ' ' + own
    if ts.wants_deliverable(subject, own):
        return False
    # Mixed questions/action requests keep their existing action/review path.
    if re.search(r'\b(add|remove|delete|change|install|update|buy|sell|trade|transfer|pay|send|subscribe|enable|disable|build|create|deploy|restart|execute|run)\b', question, re.I):
        return False
    if not re.search(r'\b(what|where|which|why|when|who|how)\b|\b(?:explain|understand|know|tell me|show me)\b', question, re.I):
        return False
    rec.update(kind='answer', tier=0, tier_name='Tier 0 (auto)',
               tier_reason='Owner read-only information question; immediate answer path.',
               privacy='LOCAL_ONLY')
    if re.search(r'\bstock[ _-]?pick(?:er|ing)?s?\b', subject + ' ' + own, re.I):
        rec['projects'] = ['stock-pickers']
        rec['data_classification'] = 'financial'
        if re.search(r'\bsources?\b|\bwhere\b.*\b(?:data|information|research)\b', question, re.I):
            rec['information_capability'] = 'stock-source-inventory'
    if 'dev_spec' in rec:
        rec['dev_spec'].update(tier=0, tier_name=rec['tier_name'],
                               tier_reason=rec['tier_reason'], projects=rec['projects'])
    return True


def source_inventory(root):
    """Read actual local source records without running a portfolio cycle."""
    home = Path(root) / 'private/stock-paper/research'
    path = home / 'research.sqlite3'
    if not path.is_file():
        raise ValueError('source_inventory_unavailable')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        rows = db.execute('SELECT url,kind,first_seen FROM documents').fetchall()
        latest = db.execute('SELECT finished FROM runs WHERE finished IS NOT NULL ORDER BY id DESC LIMIT 1').fetchone()
    domains = Counter(urlsplit(url).hostname or '(unknown)' for url, _, _ in rows)
    registry = home / 'public-sources.json'
    adopted = json.loads(registry.read_text()) if registry.exists() else []
    if not isinstance(adopted, list):
        raise ValueError('source_registry_invalid')
    approved_hosts = sorted({urlsplit(x['url']).hostname for x in adopted if x.get('url') and urlsplit(x['url']).hostname})
    text = ["Here is where Stock Pickers gets its information, checked against the current code and local source records.",
        "", "Core collection:",
        "- SEC filings and Company Facts: reported company financial information.",
        "- Company investor-relations/publication pages: original company material; available coverage varies by company.",
        "- Yahoo Finance public chart data: price observations and benchmark data, with timestamps and freshness checks; not a guaranteed real-time brokerage feed.",
        "- Saved Seeking Alpha newsletters: secondary idea leads, not sufficient evidence for a decision on their own.",
        "", "The research archive currently contains %d collected source documents across %d hostnames." % (len(rows), len(domains)),
        "Most recent finished company-research run: " + (latest[0] if latest else 'none recorded') + '.',
        "Collected source hostnames: " + (', '.join(sorted(domains)) or 'none') + '.',
        "", "Additional public sources that have passed adoption review: " + (', '.join(approved_hosts) or 'none recorded') + '.',
        "Other newsletters, podcasts, YouTube channels, investor disclosures and websites can be research candidates. Being allowed or trialed is not the same as being adopted, successfully collected, or used in a particular decision.",
        "The archive shows what was collected, not proof that each source influenced a trade. Individual reports carry their evidence and coverage limits.",
        "", "This was a read-only information check. It did not place trades, change sources, or wait for the next scheduled portfolio run."]
    return '\n'.join(text)


def live_source_inventory(root):
    if socket.gethostname().lower() == 'cumulus1':
        return source_inventory(root)
    # Only owned-host transport; no private question appears in command args.
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
        'cumulus1', 'cd ~/cirrus-digest && .venv/bin/python intake_information.py --sources'],
        capture_output=True, text=True, timeout=45)
    if result.returncode or not result.stdout.startswith('Here is where Stock Pickers'):
        raise ValueError('source_inventory_remote_failed')
    return result.stdout


def solve_and_answer(rec, creds, to_addr, subject, *, inventory=None, sender=None):
    import answer_workflow as aw
    import task_solver as ts
    from conversation_memory import Memory
    if (rec.get('requester', '').lower() != 'buddy' or rec.get('kind') != 'answer'
            or rec.get('tier') != 0 or rec.get('status') == 'refused'
            or rec.get('projects') != ['stock-pickers']
            or rec.get('information_capability') != 'stock-source-inventory'
            or to_addr != rec.get('from_email')):
        raise ValueError('information_request_not_authorized')
    memory = Memory(ts.PROJECT_DIR)
    row = aw.intake_record(ts.PROJECT_DIR, rec, subject, 'LOCAL_ONLY')
    if row['state'] == 'delivered':
        ts._resolve_fallback(rec, 'source inventory already delivered (request %s)' % row['id'])
        return {'answered': True, 'request_id': row['id'], 'reason': 'Previously delivered; duplicate suppressed.'}
    if row['state'] != 'observed' or not memory.transition(row['id'], 'observed', 'running'):
        return {'answered': False, 'request_id': row['id'], 'reason': 'Existing attempt needs review; no duplicate send.'}
    try:
        answer = (inventory or live_source_inventory)(ts.PROJECT_DIR)
        memory.update(row['id'], state='ready', answer=answer, meta={'method': 'deterministic_source_inventory'})
        if not memory.transition(row['id'], 'ready', 'sending'):
            raise ValueError('delivery_already_claimed')
        send = sender or ts._send_mail
        ok = send(creds.get('outlook_email', ''), creds.get('outlook_password', ''),
                  to_addr, ts.CC_ADDR, subject if subject.lower().startswith('re:') else 'Re: ' + subject, answer)
        memory.update(row['id'], state='delivered' if ok else 'delivery_unknown', meta={'delivery_confirmed': bool(ok)})
        if ok:
            try:
                ts.dev_loop.ledger_append({'event': 'auto-answered', 'requester': rec['requester'],
                    'title': rec.get('title'), 'request_id': row['id'], 'thread': row['thread'],
                    **ts._answer_fingerprint(answer)}, ts.PROJECT_DIR)
            except Exception:
                memory.update(row['id'], meta={'ledger_needs_repair': True})
            ts._resolve_fallback(rec, 'source inventory delivered (request %s)' % row['id'])
        else:
            memory.review_packet(memory.get(row['id']))
        return {'answered': bool(ok), 'request_id': row['id'],
                'reason': 'Source inventory delivered.' if ok else 'Delivery unconfirmed; held for review.'}
    except Exception:
        row = memory.get(row['id'])
        memory.update(row['id'], state='delivery_unknown' if row['state'] == 'sending' else 'needs_review',
                      meta={'reason': 'Information answer failed; review required.'})
        memory.review_packet(memory.get(row['id']))
        return {'answered': False, 'request_id': row['id'], 'reason': 'Information answer failed; review required.'}


if __name__ == '__main__':
    import sys
    if sys.argv[1:] != ['--sources'] or socket.gethostname().lower() != 'cumulus1':
        raise SystemExit('CUMULUS1 source inventory only')
    print(source_inventory(Path(__file__).resolve().parent))
