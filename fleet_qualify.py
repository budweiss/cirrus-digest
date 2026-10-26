#!/usr/bin/env python3
"""Renew the fleet research qualification automatically, on evidence (S497 P2).

Buddy, 2026-10-26. The articles-infra qualification was a manual 7-day date bump
that could not be renewed early; it lapsed Oct 15 and Oct 22 and learnwatch
lost 2-4 nights each time. Twice a day (CUMULUS user timer), this:

  * does nothing until the qualification is within WINDOW_S (48 h) of expiry;
  * then requires EVIDENCE that the qualified lane still works: in the last
    EVIDENCE_S, at least MIN_SUCCESS real articles-infra jobs succeeded and
    validated, the success rate is at least MIN_RATE, and the worker currently
    reports the qualified model in a fresh controller status;
  * waits (next run) while any fleet job is active -- renewal restarts the
    controller;
  * renews for EXTEND_S from the CURRENT expiry: stop the controller, back up
    the queue, write the state file (fleet_pilot.QUALIFICATION_STATE; never a
    code file -- P8), migrate the queue's stored policy hash, start, and verify
    the contract. A failed verification is rolled back and escalated.

Insufficient evidence renews nothing: the ledger row goes red and the CIRRUS
watchdog raises HUMAN NEEDED under 24 h. `--renew-now --days N` is the manual
operator override (runner fleet-qualify-renew); it records but does not
require the evidence.

usage: fleet_qualify.py --run | --dry-run | --renew-now --days N
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import fleet_pilot
import fleet_queue

STATE = Path('/home/buddy/model-serving/fleet/state-v2')
BACKUP = Path('/home/buddy/model-evaluation/s418-fleet-renewal')
CONTROLLER = 'fleet-controller.service'
PROJECT, WORKER = 'articles-infra', 'cumulus2-qwen'
WINDOW_S, EXTEND_S, EVIDENCE_S = 48 * 3600, 7 * 86400, 7 * 86400
MIN_SUCCESS, MIN_RATE, FRESH_S = 5, 0.8, 180


def stamp(t):
    return datetime.fromtimestamp(t).strftime('%a %m-%d %H:%M')


def evidence(jobs, raw, policy, now):
    """Pure. Did the qualified lane demonstrably work in the last 7 days?"""
    recent = [j for j in jobs if j['project'] == PROJECT and now - j['created'] <= EVIDENCE_S]
    good = sum(j['state'] == 'succeeded' for j in recent)
    # Model failures: validator rejections and reconciled unknowns. An abandoned,
    # never-started request is not evidence about the model, so it is not counted.
    bad = sum(j['state'] == 'failed' or (j['state'] == 'cancelled' and j.get('reason') == 'reconciled')
              or j['state'] == 'unknown' for j in recent)
    rate = good / (good + bad) if good + bad else 0.0
    model = policy['projects'][PROJECT]['model_ids'][WORKER]
    workers = {w.get('worker'): w for w in (raw or {}).get('workers') or [] if isinstance(w, dict)}
    fresh = isinstance((raw or {}).get('observed'), (int, float)) and 0 <= now - raw['observed'] <= FRESH_S
    model_ok = fresh and workers.get(WORKER, {}).get('model') == model
    why = []
    if good < MIN_SUCCESS:
        why.append('%d validated jobs in 7 days (need %d)' % (good, MIN_SUCCESS))
    if good + bad and rate < MIN_RATE:
        why.append('success rate %.0f%% (need %.0f%%)' % (rate * 100, MIN_RATE * 100))
    if not model_ok:
        why.append('worker %s not observed running %s in a fresh status' % (WORKER, model))
    return {'succeeded': good, 'failed': bad, 'rate': round(rate, 3), 'model_ok': model_ok,
            'ok': not why, 'why': '; '.join(why)}


def _meta_hash(db, new=None):
    c = sqlite3.connect(str(db), timeout=10)
    try:
        cols = [r[1] for r in c.execute('PRAGMA table_info(meta)').fetchall()]
        k, v = cols[0], cols[1]                       # live schema introspected (S418 lesson)
        old = c.execute('SELECT %s FROM meta WHERE %s=?' % (v, k), ('policy',)).fetchone()
        if old is None:
            raise RuntimeError('queue has no stored policy record')
        if new is not None:
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE meta SET %s=? WHERE %s=?' % (v, k), (new, 'policy'))
            c.commit()
        return old[0]
    finally:
        c.close()


def _write_state(path, until, note):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps({'qualified_until': until, 'renewed_at': datetime.now().isoformat(timespec='seconds'),
                               'note': note}, indent=1) + '\n')
    os.replace(tmp, path)


def renew(until, note, state=STATE, backup=BACKUP, systemctl=None, check=None, sleep=time.sleep, wait_s=90):
    """Stop, back up, write state, migrate hash, start, verify; roll back on failure."""
    systemctl = systemctl or (lambda *a: subprocess.run(['systemctl', '--user', *a], check=True))
    if check is None:
        import fleet_status
        check = fleet_status.check
    qfile, db = state / 'qualification.json', state / 'queue.db'
    previous = qfile.read_text() if qfile.exists() else None
    systemctl('stop', CONTROLLER)
    try:
        backup.mkdir(parents=True, exist_ok=True)
        tag = datetime.now().strftime('%Y%m%d%H%M%S')
        shutil.copy2(db, backup / ('queue.db.bak.' + tag))
        if previous is not None:
            (backup / ('qualification.json.bak.' + tag)).write_text(previous)
        old_hash = _meta_hash(db)
        _write_state(qfile, until, note)
        _meta_hash(db, fleet_queue.digest(fleet_pilot.policy()))
    except Exception:
        if previous is None:
            qfile.unlink(missing_ok=True)
        else:
            qfile.write_text(previous)
        systemctl('start', CONTROLLER)
        raise
    systemctl('start', CONTROLLER)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        sleep(5)
        if check().get('ok') is True:
            return True
    # Verification failed: put everything back exactly as it was, then escalate.
    systemctl('stop', CONTROLLER)
    if previous is None:
        qfile.unlink(missing_ok=True)
    else:
        qfile.write_text(previous)
    _meta_hash(db, old_hash)
    systemctl('start', CONTROLLER)
    return False


def main(argv, state=STATE, now=None, send=None, record=None, renewer=renew):
    manual = len(argv) == 3 and argv[0] == '--renew-now' and argv[1] == '--days' and argv[2].isdigit() \
        and 1 <= int(argv[2]) <= 30
    if argv not in (['--run'], ['--dry-run']) and not manual:
        print(__doc__.strip().splitlines()[-1])
        return 2
    now = time.time() if now is None else now
    if record is None:
        import job_status
        record = job_status.record
    if send is None:
        def send(msg):
            from vendor_mail_watch import telegram
            return telegram(msg)
    expires = fleet_pilot.qualified_until()
    policy = fleet_pilot.policy()
    q = fleet_queue.Queue(state / 'queue.db', policy)
    s = q.status()
    try:
        raw = json.loads((state / 'status.json').read_text())
    except (OSError, ValueError):
        raw = None
    ev = evidence(s['jobs'], raw, policy, now)
    active = [j['id'][:8] for j in s['jobs'] if j['state'] in fleet_queue.ACTIVE]
    due = expires - now <= WINDOW_S
    if manual:
        until = max(expires, now + int(argv[2]) * 86400)
        ok = renewer(until, 'manual operator renewal; evidence %s' % json.dumps(ev))
        record('fleetqualify', ok, 'manual renewal to %s %s' % (stamp(until), 'verified' if ok else 'FAILED, rolled back'))
        print(json.dumps({'manual': True, 'renewed_to': stamp(until), 'verified': ok, 'evidence': ev}))
        return 0 if ok else 1
    plan = {'expires': stamp(expires), 'due': due, 'evidence': ev, 'active_jobs': active}
    if argv == ['--dry-run']:
        print(json.dumps(plan))
        return 0
    if not due:
        record('fleetqualify', True, 'not due; expires %s' % stamp(expires))
        print(json.dumps(plan))
        return 0
    if not ev['ok']:
        record('fleetqualify', False, 'due (expires %s) but evidence insufficient: %s' % (stamp(expires), ev['why']))
        print(json.dumps(plan))
        return 1
    if active:
        record('fleetqualify', True, 'due (expires %s); deferred while fleet job(s) %s run' % (stamp(expires), ','.join(active)))
        print(json.dumps(plan))
        return 0
    until = max(expires, now) + EXTEND_S
    ok = renewer(until, 'automatic renewal on evidence %s' % json.dumps(ev))
    if ok:
        record('fleetqualify', True, 'renewed to %s on evidence (%d validated, %.0f%%)'
               % (stamp(until), ev['succeeded'], ev['rate'] * 100))
        send('CUMULUS research qualification renewed (FYI, no action needed): %s now runs to %s. '
             'Evidence: %d validated jobs in 7 days, %.0f%% success, qualified model observed.'
             % (PROJECT, stamp(until), ev['succeeded'], ev['rate'] * 100))
    else:
        record('fleetqualify', False, 'renewal to %s FAILED verification; rolled back' % stamp(until))
        send('CUMULUS research qualification renewal FAILED verification and was rolled back -- HUMAN NEEDED: '
             '%s still expires %s. Manual: runner fleet-qualify-renew.' % (PROJECT, stamp(expires)))
    print(json.dumps(dict(plan, renewed_to=stamp(until), verified=ok)))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
