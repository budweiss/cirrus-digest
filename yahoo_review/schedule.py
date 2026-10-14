#!/usr/bin/env python3
"""CIRRUS scheduler and aggregate-only notifications for the weekly Yahoo flow."""
import argparse
import fcntl
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import job_status
from media_pipeline import atomic_json
from vendor_mail_watch import telegram


def run(action):
    folder = ROOT / 'private/yahoo-schedule'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / (action + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        script = ('yahoo_review/review_yahoo_locally.py' if action == 'collect' else
                  'yahoo_review/apply_yahoo_actions.py --apply')
        result = {'error_type': 'remote_run_unconfirmed'}
        try:
            proc = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
                '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=6', 'buddy@192.168.0.204',
                'cd /home/buddy/cirrus-digest && .venv/bin/python -u ' + script],
                capture_output=True, text=True, timeout=1800)
            lines = [line for line in proc.stdout.splitlines() if line.startswith('{')]
            remote = json.loads(lines[-1]) if lines else {}
            allowed = {'headers_fetched', 'tracked_excluded', 'counts', 'classification_failures',
                       'batch_fingerprint', 'mailbox_changes', 'message_bodies_fetched', 'mailbox_mode',
                       'error_type', 'due', 'nothing_due', 'moved', 'failed', 'fail_categories'}
            result = {k: v for k, v in remote.items() if k in allowed}
            if proc.returncode or not result:
                raise RuntimeError('remote_failed')
            if action == 'collect' and not result.get('error_type'):
                if (result.get('mailbox_mode') != 'readonly' or result.get('mailbox_changes') != 0 or
                        result.get('message_bodies_fetched') != 0 or not 0 <= result.get('headers_fetched', -1) <= 20):
                    raise ValueError('readonly_contract_failed')
        except Exception as exc:
            result = {'error_type': type(exc).__name__}
        ok = (result.get('error_type') in (None, 'nothing_new') and
              not result.get('failed') and not result.get('classification_failures'))
        previous_path = folder / (action + '.json')
        previous = json.loads(previous_path.read_text()) if previous_path.exists() else {}
        signature = {k: result.get(k) for k in ('error_type', 'batch_fingerprint', 'failed', 'classification_failures')}
        notify = signature != previous.get('notified_signature') and (
            not ok or (action == 'collect' and result.get('headers_fetched', 0) > 0))
        if action == 'apply' and result.get('moved', 0):
            notify = True
        if notify:
            if not ok:
                message = 'Yahoo weekly review needs attention on the servers; private mail details remain there. No unsubmitted choices were authorized.'
            elif action == 'collect':
                message = ('Your weekly Yahoo review is ready: %d newest untracked Inbox headers, reviewed locally. '
                           'Open the usual Yahoo review page on your Mac to make choices. No mailbox changes.' % result['headers_fetched'])
            else:
                message = 'Yahoo completed %d previously submitted mailbox actions.' % result['moved']
            result['telegram_confirmed'] = telegram(message)
            if result['telegram_confirmed']:
                previous['notified_signature'] = signature
            else:
                ok = False
        result['notified_signature'] = previous.get('notified_signature')
        atomic_json(previous_path, result)
        job_status.record('yahooreview' if action == 'collect' else 'yahooapply', ok, json.dumps(result))
        print(json.dumps(result))
        return 0 if ok else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('collect', 'apply'))
    raise SystemExit(run(parser.parse_args().action))
