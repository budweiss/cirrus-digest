#!/usr/bin/env python3
"""Local, narrowly-scoped Yahoo action executor (v2).

Applies ONLY queue entries Buddy submitted (via the live review page, recorded in
yahoo-actions-queue.json, with due dates). Entries are resolved on the server by
UID and verified by fingerprint (Message-ID + sender + subject + date). A rename,
renumber, or look-alike message can never be moved.

Modes:
  --verify      Read-only location/identity check for pending actionable items.
  --unsub-page  Read-only build of the unsubscribe-links page for subscribed-decision items.
  --apply       Apply due actions (trash-after-7-days / filing-after-chosen-days).
  --report      Aggregate queue summary (offline; no network).
"""
import datetime, json, os, sys, re, imaplib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yahoo_review_lib as lib  # noqa: E402

ALLOWED = {'undecided', 'keep', 'unsubscribe_review', 'file_review', 'trash_after_7_days_review'}
ACTIONABLE = {'trash_after_7_days_review', 'file_review'}
FILE_FOLDER = 'Reviewed'
TZ = lib.TZ

def parse_dt(value):
    try: return datetime.datetime.fromisoformat(value)
    except Exception: return None

def due_tasks(entries, now=None):
    now = now or lib.now_eastern()
    out = []
    for e in entries:
        d = parse_dt(e.get('due_at'))
        if e.get('status') == 'pending' and e.get('decision') in ACTIONABLE and d and d <= now:
            out.append(e)
    return out

def _uid_from_scan_row(row):
    return row['id']

def locate_entries(conn, entries, depth=lib.SCAN_DEPTH, also_trash=True):
    """Resolve entries against the live mailbox by UID and fingerprint.
    Returns (resolved: {queue_index: uid}, unresolved_count, in_trash_set)"""
    resolved = {}
    in_trash = set()
    fps = {str(e.get('fingerprint')): str(e.get('uid')) for e in entries}
    uids = {str(e.get('uid')) for e in entries}
    inbox_rows = lib.scan_headers(conn, 'INBOX', depth)
    for row in inbox_rows:
        fp = lib.fingerprint(row)
        if row['id'] in uids:
            for idx, e in enumerate(entries):
                if str(e.get('uid')) == row['id'] and str(e.get('fingerprint')) == fp:
                    resolved[idx] = row['id']
        elif fp in fps:
            for idx, e in enumerate(entries):
                if str(e.get('uid')) == fps[fp] and str(e.get('fingerprint')) == fp:
                    if idx not in resolved:
                        resolved[idx] = row['id']
    if also_trash:
        trash = lib.trash_folder(conn)
        if trash:
            trash_rows = lib.scan_headers(conn, trash, depth)
            for row in trash_rows:
                fp = lib.fingerprint(row)
                uid = row['id']
                for idx, e in enumerate(entries):
                    if str(e.get('uid')) == uid and str(e.get('fingerprint')) == fp:
                        resolved[idx] = uid; in_trash.add(idx)
                    elif str(e.get('fingerprint')) == fp and not str(e.get('uid')) == uid:
                        if idx not in resolved:
                            resolved[idx] = uid; in_trash.add(idx)
    return resolved, in_trash

def _verify():
    entries = lib.load_queue()
    pending = [e for e in entries if e.get('status') == 'pending' and e.get('decision') in ACTIONABLE]
    if not pending:
        print(json.dumps({'actionable_total': 0, 'mailbox_changes': 0})); return
    conn = None
    try:
        conn = lib.connect(readonly=True)
        resolved, _ = locate_entries(conn, pending, also_trash=False)
    finally:
        if conn:
            try: conn.logout()
            except Exception: pass
    trash = None
    conn = None
    try:
        conn = lib.connect(readonly=True)
        trash = lib.trash_folder(conn)
    finally:
        if conn:
            try: conn.logout()
            except Exception: pass
    print(json.dumps({'actionable_total': len(pending), 'resolved_now': len(resolved),
                      'unresolved': len(pending) - len(resolved),
                      'trash_folder_found': bool(trash), 'mailbox_changes': 0}))

def move_uid(conn, uid, folder):
    # A failed/uncertain MOVE must never become a second COPY or broad EXPUNGE.
    imaplib.Commands['MOVE'] = ('SELECTED',)
    status, _ = conn.uid('MOVE', uid, folder)
    return 'move' if status == 'OK' else None

def _apply():
    with lib.file_lock('queue'):
        return _apply_locked()

def _apply_locked():
    entries = lib.load_queue()
    now = lib.now_eastern()
    tasks = due_tasks(entries, now)
    uncertain = sum(e.get('status') == 'verification_required' for e in entries)
    if not tasks:
        summary = {'when': now.isoformat(), 'due': 0, 'nothing_due': True, 'mailbox_changes': 0,
                   'failed': uncertain, 'fail_categories': {'verification_required': uncertain} if uncertain else {}}
    else:
        moved, failed = 0, uncertain
        failcats = {'verification_required': uncertain} if uncertain else {}
        conn = None
        trash = None
        try:
            conn = lib.connect(readonly=False)
            capability_status, capability_rows = conn.capability()
            # imaplib.capabilities is the pre-login snapshot; Yahoo advertises
            # MOVE after authentication, so ask the authenticated connection.
            if capability_status != 'OK' or b'MOVE' not in b' '.join(capability_rows).split():
                raise RuntimeError('server_MOVE_unavailable')
            status, _ = conn.select('INBOX', readonly=False)
            if status != 'OK': raise RuntimeError('select_failed')
            trash = lib.trash_folder(conn)
            if not trash: raise RuntimeError('trash_folder_missing')
            resolved, _ = locate_entries(conn, tasks, also_trash=False)
            for idx, e in enumerate(tasks):
                uid = resolved.get(idx)
                if not uid:
                    e['attempts'] = e.get('attempts', 0) + 1
                    e['last_error_category'] = 'entry_not_located'
                    failed += 1; failcats['entry_not_located'] = failcats.get('entry_not_located', 0) + 1
                    continue
                uid_b = uid.encode()
                st, data = conn.uid('fetch', uid_b, '(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])')
                parts = [p for p in data if isinstance(p, tuple)] if st == 'OK' else []
                if len(parts) != 1:
                    e['attempts'] = e.get('attempts', 0) + 1
                    e['last_error_category'] = 'recheck_fetch_failed'
                    failed += 1; failcats['recheck_fetch_failed'] = failcats.get('recheck_fetch_failed', 0) + 1
                    continue
                _, raw = parts[0]
                item = lib.headers(raw, uid)
                if lib.fingerprint(item) != str(e.get('fingerprint')):
                    e['attempts'] = e.get('attempts', 0) + 1
                    e['last_error_category'] = 'identity_mismatch'
                    failed += 1; failcats['identity_mismatch'] = failcats.get('identity_mismatch', 0) + 1
                    continue
                target = trash if e['decision'] == 'trash_after_7_days_review' else FILE_FOLDER
                if e['decision'] == 'file_review':
                    cst, _ = conn.select(FILE_FOLDER, readonly=True)
                    if cst != 'OK':
                        cst, _ = conn.create(FILE_FOLDER)
                        if cst != 'OK':
                            e['last_error_category'] = 'create_folder_failed'
                            failed += 1; failcats['create_folder_failed'] = failcats.get('create_folder_failed', 0) + 1
                            continue
                # Header scans leave INBOX selected read-only; reselect for every action.
                if conn.select('INBOX', readonly=False)[0] != 'OK':
                    raise RuntimeError('writable_select_failed')
                e['status'] = 'verification_required'
                lib.save_queue(entries)
                try:
                    how = move_uid(conn, uid_b, target)
                except Exception:
                    e['last_error_category'] = 'move_outcome_unconfirmed'
                    lib.save_queue(entries)
                    raise RuntimeError('move_outcome_unconfirmed') from None
                if how:
                    e['status'] = 'applied'; e['applied_at'] = now.isoformat(); e['how'] = how
                    moved += 1
                else:
                    e['status'] = 'pending'
                    e['attempts'] = e.get('attempts', 0) + 1
                    e['last_error_category'] = 'move_failed'
                    failed += 1; failcats['move_failed'] = failcats.get('move_failed', 0) + 1
                lib.save_queue(entries)
        finally:
            if conn:
                try: conn.logout()
                except Exception: pass
        summary = {'when': now.isoformat(), 'due': len(tasks), 'moved': moved, 'failed': failed,
                   'fail_categories': failcats, 'mailbox_changes': moved}
    lib.LOGS.mkdir(exist_ok=True, parents=True)
    log = lib.LOGS / ('apply-' + now.strftime('%Y%m%d') + '.json')
    log.write_text(json.dumps(summary, indent=1)); os.chmod(log, 0o600)
    print(json.dumps(summary))

def _unsub_page():
    entries = lib.load_queue()
    targets = [e for e in entries if e.get('decision') == 'unsubscribe_review' and e.get('status') in ('pending', 'link_ready')]
    if not targets:
        print(json.dumps({'unsub_targets': 0, 'page_written': False, 'mailbox_changes': 0})); return
    conn = None
    placed = missing = 0
    page_ready = None
    try:
        conn = lib.connect(readonly=True)
        resolved, in_trash = locate_entries(conn, targets, also_trash=True)
        if resolved:
            items, matched = [], []
            for idx, uid in resolved.items():
                folder = (lib.trash_folder(conn) if idx in in_trash else 'INBOX')
                if not folder or conn.select(folder, readonly=True)[0] != 'OK':
                    continue
                st, data = conn.uid('FETCH', uid.encode(),
                    '(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID LIST-UNSUBSCRIBE)])')
                parts = [p for p in data if isinstance(p, tuple)] if st == 'OK' else []
                if len(parts) != 1:
                    continue
                raw = parts[0][1]
                item = lib.headers(raw, uid)
                if lib.fingerprint(item) != targets[idx].get('fingerprint'):
                    continue
                message = lib.email.message_from_bytes(raw, policy=lib.email.policy.default)
                item['_raw_list_unsub'] = str(message.get('List-Unsubscribe') or '')
                items.append(item); matched.append(idx)
            page_items = lib.unsub_items_from_scan(items)
            if page_items:
                page_ready = lib.write_unsub_page(page_items)
                for idx in matched:
                    targets[idx]['status'] = 'link_ready'
                    targets[idx]['link_page'] = page_ready
                lib.save_queue(entries)
                placed = len(matched)
        missing = len(targets) - placed
    finally:
        if conn:
            try: conn.logout()
            except Exception: pass
    print(json.dumps({'unsub_targets': len(targets), 'page_written': bool(page_ready),
                      'targets_located': placed, 'targets_missing': missing,
                      'page': page_ready, 'mailbox_changes': 0}))

def _report():
    entries = lib.load_queue()
    by_status = {}; by_decision = {}
    for e in entries:
        by_status[e.get('status', '?')] = by_status.get(e.get('status', '?'), 0) + 1
        by_decision[e.get('decision', '?')] = by_decision.get(e.get('decision', '?'), 0) + 1
    dues = sorted([parse_dt(e['due_at']) for e in entries if e.get('status') == 'pending' and e.get('decision') in ACTIONABLE and parse_dt(e.get('due_at'))])
    print(json.dumps({'tracked_total': len(entries), 'by_status': by_status, 'by_decision': by_decision,
                      'next_due_at': dues[0].isoformat() if dues else None, 'mailbox_changes': 0}))

def main():
    os.umask(0o077)
    argv = sys.argv[1:]
    if '--verify' in argv: _verify()
    elif '--unsub-page' in argv:
        with lib.file_lock('queue'):
            _unsub_page()
    elif '--apply' in argv: _apply()
    elif '--report' in argv: _report()
    else:
        print(json.dumps({'error': 'no_mode'})); sys.exit(2)

if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception as error:
        print(json.dumps({'error_type': type(error).__name__, 'details': 'Withheld to protect credentials and mail metadata.'}))
        sys.exit(1)
