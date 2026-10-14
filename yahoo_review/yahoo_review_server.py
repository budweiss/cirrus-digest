#!/usr/bin/env python3
"""Local-only live review server (127.0.0.1:8638).

Serves the report page with a real Submit button. On submit it records Buddy's
decisions (with identity fingerprints), prepares the unsubscribe links page, and
regenerates the next 20 to review. Scheduled mailbox actions are applied later
only by the separately authorized apply task. Nothing here mutates the mailbox.
"""
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from collections import Counter
import datetime
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yahoo_review_lib as lib  # noqa: E402
from apply_yahoo_actions import ALLOWED, _unsub_page as build_unsub_page_now  # noqa: E402

PORT = lib.SERVER_PORT
LOCK = threading.Lock()
MAX_BODY = 65536

def _batch_is_stale():
    meta = lib.batch_meta_now()
    if not meta: return True
    if meta.get('mode') != 'server': return True
    tracked = lib.tracked_uids()
    return any(u in tracked for u in meta.get('uids', []))

def regenerate(mode='server'):
    # Every controller call already holds the queue lock.
    result = lib.generate_next_batch(mode=mode, queue_locked=True)
    if result.get('error') not in (None, 'nothing_new'):
        raise RuntimeError(result['error'])
    return result

def batch_entry(uid, decision, fingerprint, submitted_iso, filing_days, trash_after):
    submitted = datetime.datetime.fromisoformat(submitted_iso).astimezone(lib.TZ)
    entry = {'uid': uid, 'decision': decision, 'mailbox': 'INBOX',
             'submitted_at': submitted_iso, 'source': 'live_submit',
             'fingerprint': fingerprint,
             'status': 'recorded' if decision in ('keep', 'undecided') else 'pending',
             'attempts': 0}
    if decision == 'trash_after_7_days_review':
        due = submitted + datetime.timedelta(days=trash_after)
    elif decision == 'file_review':
        due = submitted + datetime.timedelta(days=filing_days)
    else:
        due = None
    if due and entry['status'] == 'pending':
        entry['due_at'] = due.isoformat()
    return entry

class Handler(BaseHTTPRequestHandler):
    server_version = 'YahooLocalReview/2.0'

    def log_message(self, fmt, *args):
        pass

    def local_request(self, mutation=False):
        host = self.headers.get('Host', '')
        try:
            parsed = urlsplit('http://' + host)
            if parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.username or parsed.password:
                return False
            if parsed.path or parsed.query or parsed.fragment:
                return False
            if mutation:
                return (self.headers.get('Origin') == 'http://' + host and
                        self.headers.get('Content-Type', '').split(';')[0] == 'application/json')
            return True
        except ValueError:
            return False

    def _send(self, status, body, ctype='text/html; charset=utf-8'):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.local_request():
            return self._send(403, b'Local review only')
        path = self.path.split('?', 1)[0]
        os.umask(0o077)
        if path in ('/', '/report'):
            failed = False
            with LOCK, lib.file_lock('queue'):
                try:
                    if _batch_is_stale() or not lib.LATEST.exists():
                        regenerate(mode='server')
                except Exception:
                    failed = True
            if not failed and lib.LATEST.exists():
                return self._send(200, lib.LATEST.read_bytes())
            return self._send(503, '<!doctype html><html><body style="font:16px system-ui;padding:40px">'
                      '<h1>Yahoo local review</h1><p>The current report could not be prepared on CUMULUS. '
                      'Previously submitted choices are saved. Refresh this page in a moment.</p></body></html>'.encode('utf-8'))
        if path == '/unsubscribe':
            if not lib.UNSUB_PAGE.exists():
                try:
                    with LOCK, lib.file_lock('queue'): build_unsub_page_now()
                except Exception:
                    pass
            if lib.UNSUB_PAGE.exists():
                return self._send(200, lib.UNSUB_PAGE.read_bytes())
            return self._send(404, '<!doctype html><html><body style="font:16px system-ui;padding:40px">'
                      '<h1>No unsubscribe links yet</h1><p>Mark messages as "Review unsubscribe" and press Submit; '
                      'this page will then list each sender\'s own unsubscribe link.</p></body></html>'.encode('utf-8'))
        if path == '/health':
            count = 0
            try: count = len(lib.load_queue())
            except Exception: pass
            return self._send(200, json.dumps({'ok': True, 'tracked': count}).encode(), 'application/json')
        return self._send(404, b'Not found')

    def do_POST(self):
        if not self.local_request(mutation=True):
            return self._send(403, b'Local review only')
        path = self.path.split('?', 1)[0]
        if path != '/submit':
            return self._send(404, b'Not found')
        os.umask(0o077)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > MAX_BODY: raise ValueError('bad_body')
            data = json.loads(self.rfile.read(length).decode())
        except Exception:
            return self._send(400, json.dumps({'error': 'bad_request'}).encode(), 'application/json')
        items = data.get('items') if isinstance(data, dict) else None
        if not isinstance(items, list) or not items or len(items) > 40:
            return self._send(400, json.dumps({'error': 'no_decisions'}).encode(), 'application/json')
        decisions = {}
        for it in items:
            if not isinstance(it, dict):
                return self._send(400, b'{"error":"bad_decision_entry"}', 'application/json')
            uid = str(it.get('uid', ''))
            dec = it.get('decision')
            if not uid.isdigit() or len(uid) > 16 or dec not in ALLOWED:
                return self._send(400, json.dumps({'error': 'bad_decision_entry'}).encode(), 'application/json')
            if uid in decisions:
                return self._send(400, json.dumps({'error': 'duplicate_uid'}).encode(), 'application/json')
            decisions[uid] = dec
        days = data.get('proposed_filing_days')
        if not isinstance(days, int) or not 1 <= days <= 3650:
            return self._send(400, json.dumps({'error': 'bad_filing_days'}).encode(), 'application/json')
        # The offered action is exactly seven days, measured by the server.
        trash_after = 7
        with LOCK, lib.file_lock('queue'):
            meta = lib.batch_meta_now()
            if not meta:
                try: regenerate(mode='server')
                except Exception as error:
                    return self._send(503, json.dumps({'error': 'report_unavailable'}).encode(), 'application/json')
                meta = lib.batch_meta_now()
                if not meta: return self._send(503, json.dumps({'error': 'report_unavailable'}).encode(), 'application/json')
            if data.get('review_id') != meta.get('review_id') or meta.get('fingerprint_version') != 2:
                return self._send(409, json.dumps({'error': 'batch_changed'}).encode(), 'application/json')
            fps = {str(x.get('uid')): str(x.get('fingerprint')) for x in meta.get('entries', [])}
            valid_uids = set(fps)
            if not set(decisions) <= valid_uids:
                return self._send(409, json.dumps({'error': 'batch_changed'}).encode(), 'application/json')
            entries = lib.load_queue()
            existing = {str(e.get('uid')) for e in entries}
            saved = skipped = 0
            submitted_iso = lib.now_eastern().isoformat()
            for uid, dec in decisions.items():
                if dec == 'undecided':
                    continue
                if uid in existing:
                    skipped += 1; continue
                entries.append(batch_entry(uid, dec, fps.get(uid, ''), submitted_iso, days, trash_after))
                saved += 1
            lib.save_queue(entries)
            new_unsub = [uid for uid, dec in decisions.items() if dec == 'unsubscribe_review' and uid not in existing]
            unsub_ready = False
            if new_unsub:
                try:
                    build_unsub_page_now()
                    unsub_ready = lib.UNSUB_PAGE.exists()
                except Exception:
                    unsub_ready = False
            next_batch_error = None
            try:
                regenerate(mode='server')
            except Exception as error:
                next_batch_error = type(error).__name__
            try:
                queue_now = lib.load_queue()
            except Exception:
                queue_now = []
            open_counts = dict(Counter(str(e.get('decision')) for e in queue_now if e.get('status') in ('pending', 'link_ready')))
        response = {'ok': True, 'saved': saved, 'skipped_duplicates': skipped,
                    'unsub_page_ready': unsub_ready, 'next_batch_ready': next_batch_error is None,
                    'next_batch_error': next_batch_error, 'queue_by_decision': open_counts}
        return self._send(200, json.dumps(response).encode(), 'application/json')

def main():
    os.umask(0o077)
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    server.daemon_threads = True
    print(json.dumps({'listening': '127.0.0.1:%d' % PORT}), flush=True)
    server.serve_forever()

if __name__ == '__main__':
    main()
