"""S649: export only eligible technical holds; never export request contents.

Intake already owns polling, local answering and specialist jobs. Its durable
lifecycle is the source queue while the MacBook is offline. This adapter records
callback receipts separately; receipt is not a claim, answer or delivery.
"""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import time

CLIENTS = {'bill', 'aggie', 'alyssa', 'justin'}


def readonly(path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)


def eligible(root):
    """No free-text fields cross the boundary, including reasons and subjects."""
    root = Path(root)
    path = root / 'data/intake-lifecycle/requests.sqlite3'
    if not path.exists():
        raise FileNotFoundError('intake lifecycle unavailable')
    answer_path = root / 'data/conversations/memory.sqlite3'
    found = []
    with closing(readonly(path)) as db:
        rows = db.execute("SELECT id,state,next_action,evidence FROM requests "
                          "WHERE owner='cowork' AND state IN ('blocked_no_worker','blocked_on_evidence') ORDER BY created,id")
        for rid, state, action, raw in rows:
            evidence = json.loads(raw)
            # Safety, reconciliation and uncertain delivery are not technical
            # exhaustion. Existing owner notices remain responsible for them.
            if evidence.get('client', '').lower() not in CLIENTS:
                continue
            if not re.fullmatch('[0-9a-f]{64}', rid):
                continue
            category, calls = None, 0
            if state == 'blocked_no_worker' and action in (
                'request needs a research/file/tool worker; reconcile full mail and assign',
                'build from the attached file needs a session; assign',
                'assign capable worker after full-mail reconciliation',
            ):
                category = 'capability_unavailable'
            elif state == 'blocked_on_evidence' and 'safety hold' not in action.lower():
                aid = evidence.get('answer_request_id')
                if not isinstance(aid, str) or not re.fullmatch('[0-9a-f]{20}', aid) or not answer_path.exists():
                    continue
                with closing(readonly(answer_path)) as answers:
                    row = answers.execute('SELECT state,meta FROM turns WHERE id=?', (aid,)).fetchone()
                if not row or row[0] != 'needs_review':
                    continue
                meta = json.loads(row[1])
                if meta.get('active_stage') or meta.get('delivery_confirmed') or meta.get('recovered'):
                    continue
                stages = meta.get('stages', {})
                budget = meta.get('attempt_budget', {})
                calls = budget.get('calls', 0)
                # Only the actual answer ladder's terminal technical reasons.
                # Never infer exhaustion from the human-readable held label.
                if (meta.get('reason') in (
                    'Local attempts exhausted; private work stays local.',
                    'Bounded attempts exhausted; review the saved evidence before further work.',
                ) and isinstance(calls, int) and 1 <= calls <= 6 and stages
                        and all(isinstance(v, dict) for v in stages.values())):
                    category = 'answer_attempts_exhausted'
            if category:
                found.append({'id': rid, 'category': category, 'calls': calls})
    return found


def receipts(root):
    directory = Path(root) / 'data/cowork-callback'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / 'receipts.sqlite3'
    db = sqlite3.connect(path, timeout=15)
    os.chmod(path, 0o600)
    db.execute('CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, received REAL NOT NULL)')
    return db


def pending(root):
    candidates = eligible(root)
    with closing(receipts(root)) as db:
        received = {r[0] for r in db.execute('SELECT id FROM receipts')}
    return [r for r in candidates if r['id'] not in received]


def acknowledge(root, rid):
    if not re.fullmatch('[0-9a-f]{64}', rid):
        raise ValueError('invalid request id')
    with closing(receipts(root)) as db:
        if db.execute('SELECT 1 FROM receipts WHERE id=?', (rid,)).fetchone():
            return True
        if rid not in {r['id'] for r in eligible(root)}:
            return False
        with db:
            db.execute('INSERT INTO receipts VALUES (?,?)', (rid, time.time()))
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['list', 'ack'])
    parser.add_argument('--id')
    parser.add_argument('--test-root', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    if args.test_root:
        root = args.test_root.resolve()
        if not (root / '.callback-test-root').is_file():
            parser.error('test root requires the synthetic fixture marker')
    if args.command == 'ack':
        if not args.id:
            parser.error('ack requires --id')
        print(json.dumps({'received': acknowledge(root, args.id)}))
    else:
        print(json.dumps({'version': 1, 'requests': pending(root)}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error_type': type(exc).__name__}))
        raise SystemExit(1)
