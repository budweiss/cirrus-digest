"""CIRRUS missed-schedule recovery and advance approval/grace warnings.

Light, inference-free monitor. Launches existing jobs, never rewrites approvals,
repeats a completed review, enables a disabled job, or extends grace.
"""
import argparse
import fcntl
import hashlib
import json
import math
import os
import plistlib
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'logs/renewal-recovery.json'
JOBS = ('foundationrenewal', 'foundationrenewalauto')
TZ = ZoneInfo('America/New_York')


def read(path):
    return json.loads(Path(path).read_text())


def save(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n')
    tmp.chmod(0o600)
    os.replace(tmp, path)


def finite(v):
    return type(v) in (int, float) and math.isfinite(v)


def snapshot(root=ROOT, now=None):
    """Allowlisted operational metadata only; no prompts, scopes or answers."""
    import approval_grace as G
    import capability_admission as A
    import capability_registry as C
    now = time.time() if now is None else now
    registry = read(root / 'config/model_capabilities.json')
    grace = G.load(root / 'config/approval-grace.json')
    groups, issues = [], []
    for route, cfg in registry['projects'].items():
        if not route.startswith('foundation:') or not isinstance(cfg, dict) or cfg.get('enabled') is not True:
            continue
        task = route.split(':', 1)[-1]
        if A.expiry_is_advisory(task):
            continue
        try:
            contract = C.contract_digest(cfg, root)
        except (OSError, ValueError):
            issues.append(route)
            continue
        for field in ('evaluations', 'judge_evaluations'):
            records = []
            for e in cfg.get(field, []):
                expiry = e.get('expires_at')
                if (e.get('approved') is not True or not finite(expiry)
                        or e.get('contract_sha256') != contract):
                    continue
                sha = G.record_sha(e)
                entry = grace['entries'].get(sha, {})
                tested = entry.get('tested_at') if isinstance(entry, dict) else None
                end = expiry
                if (sha not in grace.get('revoked', []) and finite(tested)
                        and 0 <= tested <= now):
                    end = max(expiry, min(expiry + G.GRACE_S, tested + G.FRESH_S))
                records.append((end, expiry, sha))
            if records:
                end, expiry, _ = max(records)
                fingerprint = hashlib.sha256(''.join(sorted(x[2] for x in records)).encode()).hexdigest()
                groups.append({'route': route, 'field': field, 'deadline': end,
                               'grace': end > expiry, 'fingerprint': fingerprint})
    packets = []
    for p in (Path.home() / 'model-evaluation').glob('renewal-*-cumulus'):
        match = re.fullmatch(r'renewal-(\d{8}-\d{4})-cumulus', p.name)
        if match and (p / 'decisions.json').is_file():
            stamp = datetime.strptime(match[1], '%Y%m%d-%H%M').replace(tzinfo=TZ).timestamp()
            if stamp <= now:
                packets.append(stamp)
    status_path = root / 'logs/jobs-status.json'
    status = read(status_path).get('foundationrenewalcumulus', {}) if status_path.exists() else {}
    return {'groups': groups, 'issues': issues, 'packet_at': max(packets, default=0),
            'renewal_epoch': status.get('epoch', 0), 'renewal_ok': status.get('ok') is True}


def due_occurrence(now, calendar):
    # Only the existing daily calendars are supported; refuse unexpected cadence.
    if set(calendar) != {'Hour', 'Minute'}:
        raise ValueError('unsupported_renewal_calendar')
    at = datetime.fromtimestamp(now, TZ).replace(hour=calendar['Hour'], minute=calendar['Minute'], second=0, microsecond=0)
    if at.timestamp() > now:
        at -= timedelta(days=1)
    return at.timestamp(), (at + timedelta(days=1)).timestamp()


def launch_state(job):
    out = subprocess.run(['/bin/launchctl', 'print', 'system/com.cirrus.' + job],
                         capture_output=True, text=True, timeout=10)
    return 'unavailable' if out.returncode else ('running' if re.search(r'^\s*pid = \d+', out.stdout, re.M) else 'idle')


def kick(job):
    assert job in JOBS
    out = subprocess.run(['/usr/bin/sudo', '-n', '/bin/launchctl', 'kickstart',
                          'system/com.cirrus.' + job], capture_output=True, timeout=15)
    return out.returncode == 0


def remote_snapshot():
    out = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
        'cumulus1', '/home/buddy/cirrus-digest/.venv/bin/python',
        '/home/buddy/cirrus-digest/renewal_recovery.py', '--snapshot'],
        capture_output=True, text=True, timeout=25)
    if out.returncode:
        raise RuntimeError('cumulus_snapshot_unavailable')
    data = json.loads(out.stdout)
    assert isinstance(data['groups'], list) and finite(data['packet_at'])
    return data


def recovery_decision(job, now, due, next_due, ledger, attempts, running,
                      local_due, packet_at, cloud_checked=False):
    if ledger.get(job, {}).get('epoch', 0) >= due:
        return 'already_ran'  # failure is a completed attempt too; never auto-repeat
    if running == 'unavailable':
        return 'unavailable'
    if running == 'running':
        return 'running'
    if next_due - now <= 900 or now - due < 300:
        return 'scheduled_window'
    if attempts.get(job, {}).get('due') == due:
        return 'attempted'
    if job == 'foundationrenewalauto':
        if ledger.get('foundationrenewal', {}).get('epoch', 0) < local_due:
            return 'waiting_local'
        if not cloud_checked and (not packet_at or not 0 <= now - packet_at < 20 * 3600):
            return 'waiting_cloud_packet'
    return 'launch'


def alerts_for(host, groups, now):
    alerts = []
    for g in groups:
        remaining = g['deadline'] - now
        if remaining > 24 * 3600:
            continue
        stage = 'ended' if remaining <= 0 else '6h' if remaining <= 6 * 3600 else '24h'
        key = ':'.join((host, g['route'], g['field'], g['fingerprint'], stage))
        at = datetime.fromtimestamp(g['deadline'], TZ).strftime('%b %d %H:%M EST' if datetime.fromtimestamp(g['deadline'], TZ).dst() == timedelta(0) else '%b %d %H:%M EDT')
        kind = 'grace/evidence freshness' if g['grace'] else 'approval'
        alerts.append((key, '%s %s %s: %s coverage %s %s Eastern. Renewal gates remain enforced.' % (
            host, g['route'], g['field'], kind, 'ended' if stage == 'ended' else 'ends', at)))
    return alerts


def run(plan=False):
    now = time.time()
    state = read(STATE) if STATE.exists() else {'attempts': {}, 'sent': {}}
    ledger = read(ROOT / 'logs/jobs-status.json')
    local = snapshot()
    remote_error = False
    try:
        remote = remote_snapshot()
        state.pop('remote_missing_since', None)
    except Exception:
        remote = {'groups': [], 'packet_at': 0}
        remote_error = True
        state.setdefault('remote_missing_since', now)
    calendars, disabled = {}, set()
    for job in JOBS:
        p = read_plist(job)
        calendars[job] = due_occurrence(now, p['StartCalendarInterval'])
        if p.get('Disabled'):
            disabled.add(job)
    results, notices = {}, alerts_for('CIRRUS', local['groups'], now) + alerts_for('CUMULUS1', remote['groups'], now)
    for host, snap in [('CIRRUS', local), ('CUMULUS1', remote)]:
        for route in snap.get('issues', []):
            notices.append((host + ':contract:' + route, host + ' ' + route + ': contract mismatch prevents approval coverage verification.'))
    if remote_error and now - state['remote_missing_since'] >= 3600:
        notices.append(('remote_missing', 'Renewal recovery: CUMULUS1 approval status unavailable for over one hour; cloud renewal catch-up is deferred.'))
    if not remote_error:
        state['sent'].pop('remote_missing', None)
    for job in JOBS:
        due, next_due = calendars[job]
        receipt_path = ROOT / ('logs/renewal-recovery-job-' + job + '.json')
        if receipt_path.exists():
            receipt = read(receipt_path)
            if receipt.get('due') == due:
                state['attempts'][job] = receipt
        decision = recovery_decision(job, now, due, next_due, ledger, state['attempts'],
            'unavailable' if job in disabled else launch_state(job), calendars['foundationrenewal'][0], remote['packet_at'],
            cloud_checked=remote.get('renewal_ok', False) and
            0 <= now - remote.get('renewal_epoch', 0) < 20 * 3600)
        results[job] = decision
        if decision == 'launch' and not plan:
            # Persist BEFORE launch, including uncertain start failures: never replay
            # the same missed occurrence after a crash or repeated startup.
            state['attempts'][job] = {'due': due, 'at': now, 'status': 'attempted'}
            save(STATE, state)
            ok = kick(job)
            state['attempts'][job]['status'] = 'started' if ok else 'start_failed'
            results[job] = state['attempts'][job]['status']
            save(STATE, state)
        waiting = state.setdefault('waiting', {})
        wait_key = job + ':' + str(due)
        if decision in ('waiting_local', 'waiting_cloud_packet'):
            waiting.setdefault(wait_key, now)
            if now - waiting[wait_key] >= 3600:
                notices.append((wait_key + ':dependency', 'Renewal recovery: %s has waited over one hour for fresh prerequisite evidence; no model retry or approval extension was made.' % job))
        else:
            waiting.pop(wait_key, None)
        attempt = state['attempts'].get(job, {})
        if decision == 'unavailable' or (decision != 'already_ran' and attempt.get('due') == due and (
                attempt.get('status') in ('start_failed', 'failed') or (
                decision == 'attempted' and now - attempt['at'] > 7200))):
            notices.append((job + ':' + str(due), 'Renewal recovery: %s is unavailable or its missed-run attempt has not completed. No repeat model run was started.' % job))
    failed_sends = 0
    for key, message in notices:
        if key in state['sent'] or plan:
            continue
        from foundation_renewal import notify
        if notify(message) == 'sent':
            state['sent'][key] = now
        else:
            failed_sends += 1
    # Bounded history; failed delivery is never acknowledged and retries next tick.
    state['sent'] = {k: v for k, v in state['sent'].items() if now - v < 30 * 86400}
    state['checked_at'] = now
    summary = {'plan': plan, 'jobs': results, 'warning_count': len(notices),
               'failed_sends': failed_sends, 'remote_available': not remote_error}
    if not plan:
        save(STATE, state)
        import job_status
        job_status.record('foundationrenewalrecovery', not failed_sends and not notices and not remote_error,
                          json.dumps(summary, sort_keys=True))
    return summary


def read_plist(job):
    assert job in JOBS
    p = Path('/Library/LaunchDaemons/com.cirrus.' + job + '.plist')
    data = plistlib.loads(p.read_bytes())
    if data.get('Label') != 'com.cirrus.' + job:
        raise ValueError('renewal_job_disabled_or_mismatched')
    return data


def run_job(job):
    """Shared scheduled/catch-up wrapper: durable once-per-occurrence receipt."""
    assert job in JOBS
    plist = read_plist(job)
    if plist.get('Disabled'):
        return 1
    due, _ = due_occurrence(time.time(), plist['StartCalendarInterval'])
    path = ROOT / ('logs/renewal-recovery-job-' + job + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        old = read(path) if path.exists() else {}
        ledger = read(ROOT / 'logs/jobs-status.json')
        if old.get('due') == due or ledger.get(job, {}).get('epoch', 0) >= due:
            print(json.dumps({'job': job, 'state': 'already_attempted'}))
            return 0
        receipt = {'due': due, 'at': time.time(), 'status': 'started'}
        save(path, receipt)
        script = 'foundation_renewal_cirrus.py' if job == JOBS[0] else 'foundation_renewal_auto.py'
        try:
            code = subprocess.call([sys.executable, str(ROOT / script)])
        except Exception:
            code = 1
        receipt.update(status='complete' if code == 0 else 'failed', finished=time.time())
        save(path, receipt)
        return code


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot', action='store_true')
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--job', choices=JOBS)
    args = parser.parse_args()
    if args.snapshot:
        print(json.dumps(snapshot()))
        return
    if not Path('/Library/LaunchDaemons').is_dir():
        raise RuntimeError('recovery_is_cirrus_only')
    if args.job:
        raise SystemExit(run_job(args.job))
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'state': 'already_running'}))
            return
        print(json.dumps(run(plan=args.plan), sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # No exception text: paths, subprocess output or packets may be private.
        print(json.dumps({'state': 'attention', 'reason': 'recovery_check_failed'}))
        raise SystemExit(1)
