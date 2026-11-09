"""Durable intake handoffs. No inference, sending, or automatic delivery claims.

Owned-host private storage; identity is sender + original Message-ID, never a
subject. An interrupted handler may have sent mail and must not be replayed.
"""
import hashlib
import json
import os
import sqlite3
import time
import tempfile
from contextlib import contextmanager, closing
from pathlib import Path

START_TARGET_SECONDS = 1200


def identity(address, mid):
    return hashlib.sha256((address.lower() + '\0' + mid).encode()).hexdigest()


@contextmanager
def connect(root):
    directory = Path(root) / 'data/intake-lifecycle'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / 'requests.sqlite3'
    db = sqlite3.connect(path, timeout=15)
    os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    try:
        with db:
            db.execute('''CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL,
                owner TEXT NOT NULL, next_action TEXT NOT NULL, created REAL NOT NULL,
                updated REAL NOT NULL, started REAL, evidence TEXT NOT NULL)''')
            yield db
    finally:
        db.close()


def capture(root, row, raw=None):
    key = identity(row[1], row[4])
    with connect(root) as db:
        if raw is not None:
            path = Path(root) / 'data/intake-lifecycle' / (key + '.eml')
            # Immutable original bytes. A duplicate cannot replace evidence.
            fd, temporary = tempfile.mkstemp(prefix='.mime-', dir=path.parent)
            try:
                with os.fdopen(fd, 'wb') as f:
                    f.write(raw); f.flush(); os.fsync(f.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            finally:
                os.unlink(temporary)
        now = time.time()
        db.execute('INSERT OR IGNORE INTO requests VALUES (?,?,?,?,?,?,?,?,?)',
                   (key, json.dumps(list(row)), 'queued', 'intake', 'route request',
                    now, now, None, '{}'))
    return key



def rate_limit_notice(root, row, day):
    key=identity(row[1],row[4])
    with connect(root) as db:
        current=db.execute('SELECT evidence FROM requests WHERE id=?',(key,)).fetchone()
        evidence=json.loads(current['evidence'])
        if evidence.get('rate_limit_notice_day')==day:return False
        evidence['rate_limit_notice_day']=day
        db.execute("UPDATE requests SET evidence=?,next_action='retry after daily rate limit resets' WHERE id=?",(json.dumps(evidence),key))
        return True


def hold_catchup(root, row):
    # A mailbox reset must not overwrite an existing completed/working request.
    with connect(root) as db:
        db.execute("UPDATE requests SET state='blocked_on_evidence',owner='cowork',"
                   "next_action='All Mail catch-up: reconcile delivery before execution' "
                   "WHERE id=? AND state='queued'", (identity(row[1],row[4]),))


def pending(root):
    with connect(root) as db:
        return [json.loads(r['payload']) for r in db.execute(
            "SELECT payload FROM requests WHERE state='queued' ORDER BY created,id")]


def begin(root, row):
    key = capture(root, row)
    with connect(root) as db:
        now = time.time()
        return bool(db.execute("UPDATE requests SET state='working',started=?,updated=? "
                               "WHERE id=? AND state='queued'", (now, now, key)).rowcount)


def update(root, row, state, owner, next_action, evidence=None):
    key = identity(row[1], row[4])
    if state == 'sent_verified' and not (evidence or {}).get('sent_receipt'):
        raise ValueError('Actual Sent Mail receipt required')
    if state == 'ready_for_send' and not (evidence or {}).get('artifact'):
        raise ValueError('Reviewable artifact required')
    with connect(root) as db:
        current = db.execute('SELECT evidence FROM requests WHERE id=?', (key,)).fetchone()
        evidence = dict(json.loads(current['evidence']), **(evidence or {})) if current else (evidence or {})
        if not db.execute('UPDATE requests SET state=?,owner=?,next_action=?,updated=?,evidence=? WHERE id=?',
            (state, owner, next_action, time.time(), json.dumps(evidence or {}), key)).rowcount:
            raise ValueError('Unknown request')


def finish(root, row, rec):
    evidence = {k: rec[k] for k in ('ticket_id', 'contact_list_run') if rec.get(k)}
    evidence['client'] = rec.get('requester', 'unknown')
    solved = rec.get('solved') or {}
    if solved.get('request_id'):
        evidence['answer_request_id'] = solved['request_id']
    if rec.get('sent_receipt'):
        evidence['sent_receipt'] = rec['sent_receipt']
        state, owner, action = 'sent_verified', 'delivery-review', 'completed with actual Sent Mail proof'
    elif solved.get('answered') or solved.get('resent'):
        state, owner, action = 'sent_unverified', 'delivery-review', 'verify actual Sent Mail and artifact'
    elif rec.get('status') == 'refused':
        state, owner, action = 'blocked_on_evidence', 'cowork', 'review request safety hold locally'
    elif (rec.get('kind') == 'confirmation'
          and rec.get('confirmation_scope') == 'existing_reports'
          and not rec.get('confirms') and not rec.get('ticket_id')):
        evidence['confirmation_scope'] = 'existing_reports'
        # This closes the intake task, not a claim that a report was delivered.
        state, owner, action = 'noted', 'intake', 'existing report list confirmed; no delivery work'
    elif rec.get('contact_list_run'):
        state, owner, action = 'working', 'contact_list', 'check job artifact and automatic delivery status; honor explicit holds'
    elif rec.get('kind') == 'research' and rec.get('topic_queued'):
        state, owner, action = 'working', 'project-research', 'verify topic execution and resulting artifact'
    elif rec.get('auto_applied'):
        state, owner, action = 'working', 'project-research', 'verify requested source produces results'
    elif rec.get('kind') in ('answer', 'resend'):
        state, owner, action = 'blocked_on_evidence', 'cowork', 'review saved attempt; preserve budget and send uncertainty'
        if rec.get('held_label'):
            import held_label
            action = held_label.wording(rec['held_label']) + '; ' + action
    elif rec.get('kind') in ('feedback','deliverable','confirmation','build'):
        state, owner, action = 'queued_preparation', 'client_request_prepare', 'check text capability and prepare local draft'
    else:
        state, owner, action = 'blocked_no_worker', 'cowork', 'assign capable worker after full-mail reconciliation'
    update(root, row, state, owner, action, evidence)


def _contact_job_state(root, row, evidence):
    import list_delivery
    import zipfile
    client = evidence["client"]
    jobs = list_delivery.matching_jobs(root, client, row[4])
    if not jobs:
        return None
    if len(jobs) != 1:
        state, owner, action = 'blocked_on_evidence', 'cowork', 'multiple jobs for original message; reconcile without replay'
    else:
        run_id = next(iter(jobs))
        run_dir = Path(root) / 'logs/contact_lists' / run_id
        artifact = run_dir / ('contact-list-' + run_id + '-client.xlsx')
        evidence['contact_list_run'] = run_id
        if list_delivery.delivered(run_id, Path(root) / 'logs/contact_lists'):
            state, owner, action = 'sent_unverified', 'delivery-review', 'verify actual Sent Mail and workbook; never resend'
        elif artifact.is_file() and (run_dir / 'stats.json').is_file() and (run_dir / 'summary.json').is_file():
            with zipfile.ZipFile(artifact) as workbook:
                if 'xl/workbook.xml' not in workbook.namelist() or workbook.testzip():
                    raise ValueError('Incomplete contact workbook')
            evidence['artifact'] = str(artifact)
            evidence['artifact_sha256'] = hashlib.sha256(artifact.read_bytes()).hexdigest()
            state, owner, action = 'ready_for_send', 'cowork', 'check automatic delivery outcome or explicit hold; verify Sent Mail before retry'
        else:
            state, owner, action = 'working', 'contact_list', 'check existing job progress; never launch duplicate'
    return state, owner, action


def reconcile_contact_jobs(root):
    """Refresh existing worker handoffs from exact identity/artifact evidence.

    No launches, sends, budget resets, or inference from a ticket's 'resolved'
    label. A delivered ledger row is still unverified until Sent Mail is checked.
    """
    import zipfile
    changed = 0
    with connect(root) as db:
        rows = list(db.execute("SELECT * FROM requests WHERE state IN "
                               "('blocked_no_worker','blocked_on_evidence','working','ready_for_send')"))
        for r in rows:
            row, evidence = json.loads(r['payload']), json.loads(r['evidence'])
            client = evidence.get('client')
            if not client or not row[4]:
                continue
            try:
                result = _contact_job_state(root, row, evidence)
            except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
                result = ('blocked_on_evidence', 'cowork',
                          'contact job evidence unreadable: ' + type(exc).__name__ + '; never replay')
            if result is None:
                continue
            state, owner, action = result
            if ((r['state'], r['owner'], r['next_action']) == (state, owner, action)
                    and json.loads(r['evidence']) == evidence):
                continue  # polls must not reset the stalled-work clock
            db.execute('UPDATE requests SET state=?,owner=?,next_action=?,updated=?,evidence=? WHERE id=?',
                       (state, owner, action, time.time(), json.dumps(evidence), r['id']))
            changed += 1
    return changed


def verify_contact_sends(root, creds, verify=None):
    """sent_unverified contact deliveries -> sent_verified on one Sent Mail copy.

    Read-only on the mailbox; no resend. A missing or ambiguous copy leaves the
    row unverified (and visible), exactly as before.
    """
    if verify is None:
        import contact_delivery_evidence
        verify = contact_delivery_evidence.verify
    with connect(root) as db:
        rows = list(db.execute("SELECT payload,evidence FROM requests WHERE state='sent_unverified'"))
    done = 0
    for r in rows:
        evidence = json.loads(r['evidence'])
        if not evidence.get('contact_list_run') or not evidence.get('client'):
            continue
        receipt = verify(root, evidence['contact_list_run'], evidence['client'], creds)
        if receipt:
            update(root, json.loads(r['payload']), 'sent_verified', 'delivery-review',
                   'completed with actual Sent Mail proof', {'sent_receipt': receipt})
            done += 1
    return done


def recover(root):
    # Caller holds intake process lock. Only the intake handler is abandoned;
    # detached project workers retain their own lifecycle and must not be reset.
    with connect(root) as db:
        return db.execute("UPDATE requests SET state='blocked_on_evidence',owner='cowork',"
                          "next_action='interrupted handler: reconcile effects before retry',updated=? "
                          "WHERE state='working' AND owner='intake'", (time.time(),)).rowcount


def summary(root, now=None):
    path = Path(root) / 'data/intake-lifecycle/requests.sqlite3'
    if not path.exists():
        return {'coverage': 'not_initialized', 'attention': [], 'counts': {}}
    now = time.time() if now is None else now
    counts, attention = {}, []
    # Read-only: a monitor must not create or repair the database it checks.
    with closing(sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        for r in db.execute('SELECT id,state,owner,next_action,created,updated,started,evidence FROM requests'):
            counts[r['state']] = counts.get(r['state'], 0) + 1
            age = now - r['updated']
            due = (r['state'].startswith('blocked_') or r['state'] in ('ready_for_send','sent_unverified')
                   or r['state'] in ('queued','queued_preparation') and now-r['created'] > START_TARGET_SECONDS
                   or r['state'] in ('working','waiting_on_peer') and age > 3600)
            if due:
                attention.append({'id':r['id'], 'client':json.loads(r['evidence']).get('client','unknown'), 'state':r['state'], 'owner':r['owner'],
                                  'next_action':r['next_action'], 'age_minutes':round(age/60,1)})
    return {'coverage':'captured_since_install', 'counts':counts, 'attention':attention,
            'start_target_seconds':START_TARGET_SECONDS}


def notice(root):
    """Stable, metadata-only notice. Caller persists delivery via intake._notify."""
    rows = summary(root)['attention']
    if not rows:
        return None
    return 'Client request handoffs need attention (not proof of work owed):\n' + '\n'.join(
        f"{r['client']} {r['id'][:12]}: {r['state']}; owner={r['owner']}; {r['next_action']}" for r in rows)


def claim(root, key, owner):
    """CUMULUS arbitrates both mailboxes atomically; hashes only, no mail content."""
    import re
    if not re.fullmatch('[a-f0-9]{64}', key) or owner not in ('cirrus','cumulus1'):
        raise ValueError('Invalid claim')
    with connect(root) as db:
        db.execute('CREATE TABLE IF NOT EXISTS claims (id TEXT PRIMARY KEY, owner TEXT NOT NULL, created REAL NOT NULL)')
        db.execute('INSERT OR IGNORE INTO claims VALUES (?,?,?)',(key,owner,time.time()))
        return db.execute('SELECT owner FROM claims WHERE id=?',(key,)).fetchone()['owner']


def coordinate(root, row):
    """Use only the already verified CIRRUS -> CUMULUS SSH route. Fail closed."""
    import socket
    import subprocess
    host=socket.gethostname().split('.')[0].lower()
    key=identity(row[1],row[4])
    if host=='cumulus1':
        owner=claim(root,key,host)
    elif host=='cirrus':
        result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','cumulus1',
            'cd /home/buddy/cirrus-digest && .venv/bin/python3 intake_lifecycle.py --claim '+key+' --owner cirrus'],
            capture_output=True,text=True,timeout=20)
        if result.returncode or result.stdout.strip() not in ('cirrus','cumulus1'):
            raise RuntimeError('Shared claim unavailable')
        owner=result.stdout.strip()
    else:
        raise RuntimeError('Unqualified intake host')
    if owner!=host:
        update(root,row,'waiting_on_peer',owner,'peer owns same original message; reconcile evidence, never resend')
        return False
    return True


def selftest():
    """Run the existing intake suites through the builder's supported gate."""
    import unittest
    suite = unittest.defaultTestLoader.loadTestsFromNames([
        'test_intake_lifecycle', 'test_held_label', 'test_intake_notice_outbox'])
    if not suite.countTestCases():
        return 1
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__=='__main__':
    import sys
    if '--selftest' in sys.argv:
        if sys.argv[1:] != ['--selftest']:
            raise SystemExit(2)
        raise SystemExit(selftest())
    import argparse
    parser=argparse.ArgumentParser(description='Hash-only cross-mailbox claim; no sends')
    parser.add_argument('--claim',required=True)
    parser.add_argument('--owner',required=True,choices=['cirrus','cumulus1'])
    args=parser.parse_args()
    print(claim(Path(__file__).resolve().parent,args.claim,args.owner))
