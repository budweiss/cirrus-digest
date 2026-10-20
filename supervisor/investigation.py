"""Read-only, deterministic shadow investigation. Never sends or repairs.

Only controlled codes/counters enter receipts; free-text job notes and private
reports never leave memory. Separate SQLite receipts cannot alter alert state.
"""
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
import time

VERSION = 1
MAX_JOBS = 256
MAX_BYTES = 2 * 1024 * 1024
REASONS = {'unsupported_quote', 'invalid_findings', 'invalid_source_index',
           'invalid_verdict', 'summary_length', 'obsolete_purchase_instruction',
           'parse_or_transport', 'media request exceeds context budget'}


def read_json(path):
    with Path(path).open('rb') as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('input_limit')
    return json.loads(data)


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def local_evidence(root, jobs):
    """Fixed metadata paths only. No credentials, report bodies or model calls."""
    root = Path(root)
    evidence = {}
    row = jobs.get('environmentscan', {})
    path = root / 'private/environment-scan/review-rejection.json'
    try:
        # A prior rejection is not evidence for this run. Scheduled status is
        # written after review; allow only a rejection within that run window.
        stamp = path.stat().st_mtime
        epoch = row.get('epoch')
        if number(epoch) and epoch - 600 <= stamp <= epoch + 5:
            rejection = read_json(path)
            reason = rejection.get('controlled_reason')
            if rejection.get('stage') == 'schema' and reason in REASONS:
                evidence['environment_reason'] = reason
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    try:
        # Empty physical queue is corroboration, not the run note alone.
        path = root / 'logs/dev-loop/build-queue.jsonl'
        with path.open('rb') as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) <= MAX_BYTES:
            evidence['queue_empty'] = not data.strip()
    except OSError:
        pass
    return evidence


def diagnose(job, row, now, cadence=None, evidence=None):
    evidence = evidence or {}
    result = {'classification': 'unknown', 'reason': 'unclassified_failure',
              'next_step': 'operator_investigation', 'recovered': False}
    if not isinstance(row, dict) or type(row.get('ok')) is not bool or not number(row.get('epoch')):
        return dict(result, reason='invalid_run_metadata')
    epoch = row['epoch']
    age = now - epoch
    if age < -60:
        return dict(result, reason='future_run_timestamp')
    if number(cadence) and cadence > 0 and age > cadence * 3600:
        return dict(result, reason='stale_run_metadata')
    if row['ok']:
        if job == 'devloop' and age <= 26 * 3600 and row.get('note') == '0 queued, nothing to build':
            if evidence.get('queue_empty') is True:
                return dict(result, classification='waiting', reason='empty_approved_queue',
                            next_step='await_approved_work')
            return dict(result, reason='queue_not_corroborated')
        return dict(result, classification='run_succeeded', reason='recorded_success',
                    next_step='none')
    note = row.get('note') if isinstance(row.get('note'), str) else ''
    if job == 'ytwatch' and 'temporary caption failure: RequestBlocked' in note:
        return dict(result, classification='upstream_block', reason='caption_request_blocked',
                    next_step='preserve_unseen_work_and_check_next_scheduled_run')
    if job == 'environmentscan':
        reason = evidence.get('environment_reason')
        if reason in REASONS:
            return dict(result, classification='review_hold', reason=reason,
                        next_step='synthetic_reproduction_keep_evidence_gate')
    if job == 'foundationrenewalauto':
        match = re.fullmatch(r'reviewed=(\d+) renewed=(\d+) held=(\d+) evidence_pending=(\d+) packets=(\d+)', note)
        if match:
            reviewed, renewed, held, pending, packets = map(int, match.groups())
            if pending and not held and reviewed == renewed + pending and packets:
                return dict(result, classification='review_hold', reason='evidence_review_pending',
                            pending=pending, next_step='independent_evidence_review_keep_hold')
    return result


def investigate(jobs, host, destination, now=None, cadence=None, evidence=None):
    """At most 256 jobs; one transaction, 100ms DB lock ceiling, no network.

    Deduplicate identical observations across processes/restarts while retaining
    every distinct run and changed diagnosis. A successful run is never labeled
    a verified repair. Missing input produces an explicit unavailable receipt.
    """
    now = time.time() if now is None else now
    if host not in ('CIRRUS', 'CUMULUS') or not number(now):
        raise ValueError('invalid_observer')
    valid = isinstance(jobs, dict) and 0 < len(jobs) <= MAX_JOBS
    if not valid:
        jobs = {'monitor_input': None}
    rows = []
    for job, row in jobs.items():
        if not isinstance(job, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', job):
            raise ValueError('invalid_job_name')
        diagnosis = diagnose(job, row, now, (cadence or {}).get(job), evidence)
        epoch = row.get('epoch') if isinstance(row, dict) and number(row.get('epoch')) else None
        receipt = dict(diagnosis, job=job, host=host, run_epoch=epoch,
                       version=VERSION, mode='shadow', actions_taken=0,
                       alerts_changed=False, inference_calls=0)
        encoded = json.dumps(receipt, sort_keys=True)
        key = hashlib.sha256(encoded.encode()).hexdigest()
        rows.append((key, now, encoded))
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Restrict the file from creation, not only after the first transaction.
    path.touch(mode=0o600, exist_ok=True)
    with closing(sqlite3.connect(str(path), timeout=0.1)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, observed REAL NOT NULL, receipt TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS scan (host TEXT PRIMARY KEY, checked REAL NOT NULL, input_ok INTEGER NOT NULL, jobs INTEGER NOT NULL)')
        before = db.total_changes
        db.executemany('INSERT OR IGNORE INTO receipts VALUES (?,?,?)', rows)
        added = db.total_changes - before
        db.execute('INSERT OR REPLACE INTO scan VALUES (?,?,?,?)', (host, now, int(valid), len(rows)))
    return {'mode': 'shadow', 'host': host, 'input_ok': valid, 'jobs': len(rows),
            'new_receipts': added, 'actions_taken': 0, 'alerts_changed': False}


def observe_local(root, host):
    root = Path(root)
    try:
        jobs = read_json(root / 'logs/jobs-status.json')
    except (OSError, ValueError):
        jobs = None
    evidence = local_evidence(root, jobs) if isinstance(jobs, dict) else {}
    # Imported only on the app host; the supervisor service account instead
    # supplies its existing restricted status feed and cadence table.
    import job_status
    return investigate(jobs, host, root / 'logs/shadow-investigation.sqlite',
                       cadence=job_status.CADENCE_H, evidence=evidence)


def safe_observe(callback):
    """Shadow failures never propagate into warning generation or delivery."""
    try:
        return callback()
    except Exception:
        print('Skywarden shadow investigation unavailable; existing alerts unchanged', file=sys.stderr)
        return {'mode': 'shadow', 'input_ok': False, 'observer_failed': True,
                'actions_taken': 0, 'alerts_changed': False}
