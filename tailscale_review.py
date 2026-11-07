#!/usr/bin/env python3
"""S580: one-time, deterministic server-side shadow-week review.

CUMULUS1 owns the timer and Telegram receipt. --check is read-only/no-send.
--snapshot and --enable-repair run locally, including through C2's forced key.
Only repair mode is promoted; no logout, key minting or disruptive simulation.
"""
import argparse
import json
import socket
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import tailscale_heal as heal

ZONE = ZoneInfo('America/New_York')
START = datetime(2026, 11, 7, 15, tzinfo=ZONE).timestamp()
DUE = datetime(2026, 11, 14, 9, tzinfo=ZONE).timestamp()
REPORT = heal.LOGS / 'tailscale-review.json'


def history(text, now):
    rows = []
    for line in text.splitlines():
        try:
            stamp = datetime.strptime(line[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=ZONE).timestamp()
            if START <= stamp <= now:
                rows.append((stamp, line[20:].startswith('ok OK ')))
        except ValueError:
            continue
    rows.sort()
    times = [START] + [r[0] for r in rows] + [now]
    return {'runs': len(rows), 'bad_runs': sum(not r[1] for r in rows),
            'coverage': len(rows) / max(1, (now - START) / 300),
            'max_gap_seconds': max(b-a for a, b in zip(times, times[1:]))}


def gates(s, now):
    reasons = []
    if now < DUE:
        reasons.append('observation period has not finished')
    if s.get('host') not in heal.EXPECT or s.get('state') != 'OK':
        reasons.append('local Tailscale health is not OK')
    if not 0 <= s.get('receipt_age', -1) < 900:
        reasons.append('monitor receipt is missing or stale')
    if s.get('hold') or s.get('episodes'):
        reasons.append('hold or unresolved monitoring episode')
    if s.get('hygiene') or not s.get('server_identity_ok'):
        reasons.append('server name, tag, expiry or duplicate problem')
    if not s.get('pings_ok'):
        reasons.append('peer ping failed')
    h = s.get('history', {})
    if h.get('coverage', 0) < .90 or h.get('max_gap_seconds', 99999) > 1800 or h.get('bad_runs', 1):
        reasons.append('observation history has failures or insufficient coverage')
    if not s.get('selftest_ok'):
        reasons.append('repair selftest failed')
    if s.get('mode') not in ('shadow', 'repair'):
        reasons.append('mode changed outside this rollout')
    return reasons


def snapshot():
    now = time.time()
    me = socket.gethostname().split('.')[0].lower()
    cfg = heal.HOSTS[me]
    p = heal.probe(cfg)
    state, _ = heal.classify(p)
    f = p['facts']
    st = heal.load_state(heal.LOGS / 'tailscale-heal-state.json')
    result = {'host': me, 'state': state, 'mode': heal._mode(),
              'receipt_age': now-st.get('t', 0), 'hold': heal.held(heal.LOGS / 'tailscale-heal.hold'),
              'episodes': len(st.get('eps', {})), 'server_identity_ok': False,
              'hygiene': [], 'pings_ok': False}
    if f:
        n = f['self']
        result['server_identity_ok'] = n['dns'] == me and heal.TAG in n['tags'] and not n['keyexp']
        result['hygiene'] = [k for k, _ in heal.hygiene(f, me)]
        checks = []
        for peer in heal.EXPECT:
            if peer == me:
                continue
            node = heal.pick(f, peer)
            checks.append(bool(node and node['online'] and heal.run(
                [cfg['ts'], 'ping', '--c', '1', '--timeout', '5s', node['ip']], 10)[0] == 0))
        result['pings_ok'] = all(checks)
    try:
        result['history'] = history((heal.LOGS / 'tailscale-heal.log').read_text(), now)
    except OSError:
        result['history'] = {}
    rc, _, _ = heal.run(['/usr/bin/python3', str(heal.HERE / 'tailscale_heal.py'), '--selftest'], 60)
    result['selftest_ok'] = rc == 0
    result['reasons'] = gates(result, now)
    return result


def enable_repair():
    # Every host rechecks its own gates; a stale coordinator decision cannot arm it.
    s = snapshot()
    if not s['reasons']:
        p = heal.LOGS / 'tailscale-heal.mode'
        tmp = p.with_suffix('.review-tmp')
        tmp.write_text('repair\n')
        tmp.replace(p)
        s['mode'] = heal._mode()
    return s


def remote(host, action):
    if host == 'cumulus1':
        return enable_repair() if action == 'enable-repair' else snapshot()
    if host == 'cumulus2':
        return heal.c2('review-enable-repair' if action == 'enable-repair' else 'review', timeout=120)
    cmd = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
           '-o', 'ConnectTimeout=10', 'buddy@192.168.0.202',
           '/usr/bin/python3 /Users/buddy/projects/cirrus-digest/tailscale_review.py --' + action]
    rc, out, _ = heal.run(cmd, 120)
    try:
        return json.loads(out) if rc == 0 else {'error': 'CIRRUS review route failed'}
    except ValueError:
        return {'error': 'invalid review response'}


def watch(now=None):
    """Called by C1's existing 5-minute job; no forever-overdue one-shot cadence."""
    now = now or time.time()
    try:
        report = json.loads(REPORT.read_text())
    except (OSError, ValueError):
        report = {}
    if report.get('complete') and report.get('notified'):
        return {}
    if now > DUE + 1800:
        return {'review:overdue': ('Tailscale November 14 review did not finish and deliver its result. Check cirrus-tailscalereview.service on CUMULUS1.', 0, 21600)}
    rc, out, _ = heal.run(['systemctl', 'is-active', 'cirrus-tailscalereview.timer'], 10)
    if rc or out.strip() != 'active':
        return {'review:timer': ('Tailscale November 14 review timer is not active on CUMULUS1.', 0, 21600)}
    return {}


def review(check=False):
    if socket.gethostname().split('.')[0].lower() != 'cumulus1':
        raise RuntimeError('coordinator must run on cumulus1')
    if not check and time.time() < DUE:
        print('Review is not due; no changes or sends.')
        return 0
    if not check and REPORT.exists():
        prior = json.loads(REPORT.read_text())
        if prior.get('complete') and prior.get('notified'):
            return 0
        if prior.get('complete'):
            prior['notified'] = heal.telegram(prior['message'])
            heal.save_state(REPORT, prior)
            return 0 if prior['notified'] else 1
    results = {h: remote(h, 'snapshot') for h in heal.EXPECT}
    ready = all(r.get('host') == h and not r.get('reasons', ['missing evidence']) and not r.get('error') for h,r in results.items())
    if not check and ready:
        for h in ('cumulus2', 'cirrus', 'cumulus1'):
            results[h] = remote(h, 'enable-repair')
            if results[h].get('mode') != 'repair' or results[h].get('reasons', ['missing evidence']):
                break  # partial rollout is explicitly reported; never reset preexisting modes
    modes = ', '.join(h + '=' + str(r.get('mode', 'unknown')) for h,r in results.items())
    issues = [h + ': ' + '; '.join(r.get('reasons', ['review unavailable'])) for h,r in results.items()
              if r.get('reasons') or r.get('error') or r.get('host') != h]
    msg = 'Tailscale November 14 review: ' + ('checks passed. ' if not issues else 'attention needed. ') + modes
    if issues:
        msg += '\n' + '\n'.join(issues)
    msg += '\nAutomatic re-login is not enabled by this review. No logout tests were run.'
    report = {'complete': True, 'time': time.time(), 'results': results, 'message': msg,
              'notified': False, 'checks_passed': not issues}
    if check:
        print(json.dumps(report, indent=2))
        return 0
    heal.save_state(REPORT, report)
    report['notified'] = heal.telegram(msg)
    heal.save_state(REPORT, report)
    import job_status
    job_status.record('tailscalereview', ok=not issues and report['notified'], note=msg[:200])
    print(msg)
    return 0 if report['notified'] else 1


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--snapshot', action='store_true')
    mode.add_argument('--enable-repair', action='store_true')
    args = parser.parse_args()
    if args.snapshot or args.enable_repair:
        print(json.dumps(enable_repair() if args.enable_repair else snapshot()))
        return 0
    return review(args.check)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('tailscale review failed: ' + type(exc).__name__)  # exception URLs may carry secrets
        raise SystemExit(1)
