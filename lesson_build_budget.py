"""Durable USD reservations for lesson work. Unknown outcomes retain the ceiling."""
import fcntl
import json
import math
import uuid
from pathlib import Path

TASK = 'pedagogy-lesson-build'


def amount(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('invalid dollar amount')
    return float(value)


def reserve(root, ledger, job, ceiling, qualification=False):
    from lesson_build import durable_json
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    ceiling = amount(ceiling)
    with (root / 'dollar-budget.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / 'dollar-budget.json'
        if path.exists():
            state = json.loads(path.read_text())
        else:
            # Missing ledger is unknown, never a fresh $0 allowance.
            rows = [json.loads(x) for x in Path(ledger).read_text().splitlines() if x.strip()]
            old = [r for r in rows if r.get('task') == TASK]
            if any(r.get('unpriced') or r.get('usage_basis') != 'provider_tokens' for r in old):
                raise ValueError('legacy cost evidence incomplete')
            baseline = sum(amount(r.get('cost')) for r in old)
            state = {'baseline': baseline, 'jobs': {}, 'reservations': {}, 'blocked': False}
        if state['blocked']:
            raise ValueError('accounting reconciliation required')
        baseline = amount(state['baseline'])
        reservations = state['reservations']
        totals = [amount(r['charge']) for r in reservations.values()]
        # Legacy calls lacked module IDs. Charge their entire amount to each
        # qualification module conservatively; never pretend it was all another module.
        if job not in state['jobs']:
            state['jobs'][job] = baseline if qualification else 0.0
        job_total = amount(state['jobs'][job]) + sum(amount(r['charge']) for r in reservations.values() if r['job'] == job)
        if baseline + sum(totals) + ceiling > 15.0 or job_total + ceiling > 5.0:
            raise ValueError('lesson dollar allowance exhausted')
        rid = 'lesson-' + uuid.uuid4().hex
        reservations[rid] = {'job': job, 'charge': ceiling, 'ceiling': ceiling, 'status': 'reserved'}
        durable_json(path, state)
        return rid


def settle(root, ledger, rid):
    from lesson_build import durable_json
    root = Path(root)
    with (root / 'dollar-budget.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / 'dollar-budget.json'
        state = json.loads(path.read_text())
        reservation = state['reservations'][rid]
        rows = [json.loads(x) for x in Path(ledger).read_text().splitlines() if x.strip()]
        hits = [r for r in rows if r.get('session_id') == rid and r.get('task') == TASK]
        if len(hits) != 1 or hits[0].get('unpriced') or hits[0].get('usage_basis') != 'provider_tokens':
            state['blocked'] = True
            durable_json(path, state)
            raise ValueError('missing or ambiguous call accounting; reserved ceiling retained')
        actual = amount(hits[0].get('cost'))
        if actual > amount(reservation['ceiling']):
            reservation.update(charge=actual, status='overrun')
            state['blocked'] = True
            durable_json(path, state)
            raise ValueError('cost exceeded reservation; stop for pricing review')
        reservation.update(charge=actual, status='accounted')
        durable_json(path, state)
