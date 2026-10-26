#!/usr/bin/env python3
"""Clear stuck fleet jobs automatically -- no-delivery projects only (S497).

Buddy, 2026-10-26: a single rejected analysis (state `unknown`) blocked the
learnwatch lane for days three times (S366 Sep 28, S406 Oct 11, S497 Oct 25)
while it waited for an operator. This applies the SAME guarded steps as the
runner `fleet-reconcile` / `fleet-abandon` commands, deterministically, and
only when ALL of these hold:

  * the job's project policy says delivery == 'none';
  * a stuck job (unknown / suspected_stall / cancelling) has had no worker
    heartbeat for SILENT_S on this boot (or its boot changed), AND its worker
    reads idle in a controller status no older than FRESH_S -- the same proof
    of termination the manual command requires;
  * or a queued request never started and is older than 30 minutes (the
    queue's own abandon_queued() guard enforces the details).

At most CAP clears per project per 24 h. Beyond that the job is LEFT for a
human, and the CIRRUS watchdog raises HUMAN NEEDED: repeated failures are a
defect to fix, not to keep clearing. Work is never reassigned. Every clear is
logged and sent to Buddy as an FYI (no action needed).

usage: fleet_autoreconcile.py --run | --dry-run
"""
import json
import sys
import time
from pathlib import Path

from fleet_queue import Queue, Refused

ROOT = Path(__file__).resolve().parent
STATE = Path('/home/buddy/model-serving/fleet/state-v2')
LOG = ROOT / 'logs/fleet-autoreconcile.jsonl'
STUCK = ('unknown', 'suspected_stall', 'cancelling')
SILENT_S, FRESH_S, CAP = 600, 180, 3


def recent_clears(log, now):
    """Clears per project in the last 24 h, from our own log."""
    counts = {}
    try:
        lines = log.read_text().splitlines()
    except OSError:
        return counts
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get('action') in ('reconciled', 'abandoned') and now - row.get('ts', 0) < 86400:
            counts[row['project']] = counts.get(row['project'], 0) + 1
    return counts


def plan(q, raw, now, counts):
    """Pure decision. Returns ([(action, row)], [(job_id, why_left)]). No writes."""
    if not isinstance(raw, dict) or not isinstance(raw.get('observed'), (int, float)) \
            or not 0 <= now - raw['observed'] <= FRESH_S:
        return [], [('*', 'controller status stale or unreadable')]
    workers = {w.get('worker'): w for w in raw.get('workers') or [] if isinstance(w, dict)}
    counts = dict(counts)
    acts, left = [], []
    for row in q.status()['jobs']:
        if row['state'] not in STUCK + ('queued',):
            continue
        if q.policy['projects'].get(row['project'], {}).get('delivery') != 'none':
            left.append((row['id'], 'project delivers; human only'))
            continue
        if row['state'] == 'queued':
            if not (row['attempt'] == 0 and row['worker'] is None and now - row['created'] > 1800):
                continue          # ordinary waiting work, not orphaned
            action = 'abandon'
        else:
            same_boot = row.get('epoch') == q.epoch and row.get('heartbeat') is not None
            if same_boot and q.monotonic() - row['heartbeat'] < SILENT_S:
                left.append((row['id'], 'stuck under 10 min; wait'))
                continue
            w = workers.get(row['worker'])
            if not w or w.get('busy') is not False:
                left.append((row['id'], 'worker not verified idle; human only'))
                continue
            action = 'reconcile'
        if counts.get(row['project'], 0) >= CAP:
            left.append((row['id'], 'daily cap of %d reached; human needed' % CAP))
            continue
        counts[row['project']] = counts.get(row['project'], 0) + 1
        acts.append((action, row))
    return acts, left


def apply(q, acts, log, now):
    """Run the guarded queue steps. Refusals are recorded, never forced."""
    done = []
    for action, row in acts:
        entry = {'ts': now, 'job': row['id'], 'project': row['project'], 'state': row['state'],
                 'reason': (row.get('reason') or '')[:200]}
        try:
            if action == 'abandon':
                q.abandon_queued(row['id'], q.hash)
                entry['action'] = 'abandoned'
            else:
                if row['state'] != 'cancelling':
                    q.cancel(row['id'], row['fence'], q.hash, confirmed_stall=True)
                q.stopped(row['id'], row['fence'], confirmed=True, backend_idle=True, delivery='none')
                entry['action'] = 'reconciled'
        except Refused as exc:
            entry.update(action='refused', refusal=str(exc)[:200])
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('a') as fh:
            fh.write(json.dumps(entry) + '\n')
        done.append(entry)
    return done


def notice(done):
    cleared = [d for d in done if d['action'] in ('reconciled', 'abandoned')]
    refused = [d for d in done if d['action'] == 'refused']
    if not cleared and not refused:
        return None
    lines = ['%s %s job %s (%s)%s' % ('cleared' if d['action'] != 'refused' else 'COULD NOT clear',
                                      d['project'], d['job'][:8], d['state'],
                                      ': ' + d['reason'] if d['reason'] else '')
             for d in cleared + refused]
    head = 'CUMULUS fleet auto-clear (FYI, no action needed)' if not refused else \
           'CUMULUS fleet auto-clear: a guarded step was REFUSED -- HUMAN NEEDED'
    return head + ':\n' + '\n'.join('- ' + x for x in lines)


def summary(done, left):
    """(ok, note) for the job ledger. A stale controller or a refused step is not ok."""
    cleared = sum(d['action'] in ('reconciled', 'abandoned') for d in done)
    refused = sum(d['action'] == 'refused' for d in done)
    stale = ('*', 'controller status stale or unreadable') in left
    note = '%d cleared, %d refused, %d left for a human%s' % (
        cleared, refused, sum(j != '*' for j, _ in left), '; controller status stale' if stale else '')
    return not (stale or refused), note


def main(argv, state=STATE, log=LOG, send=None):
    if argv not in (['--run'], ['--dry-run']):
        print(__doc__.strip().splitlines()[-1])
        return 2
    from fleet_pilot import policy
    now = time.time()
    q = Queue(state / 'queue.db', policy())
    try:
        raw = json.loads((state / 'status.json').read_text())
    except (OSError, ValueError):
        raw = None
    acts, left = plan(q, raw, now, recent_clears(log, now))
    if argv == ['--dry-run']:
        print(json.dumps({'would': [(a, r['id'], r['project'], r['state']) for a, r in acts], 'left': left}))
        return 0
    done = apply(q, acts, log, now)
    ok, note = summary(done, left)
    import job_status
    job_status.record('fleetautoreconcile', ok, note)
    message = notice(done)
    if message:
        if send is None:
            from vendor_mail_watch import telegram as send
        send(message)
    print(json.dumps({'done': done, 'left': left}))
    return 1 if any(d['action'] == 'refused' for d in done) else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
