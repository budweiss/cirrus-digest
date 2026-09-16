"""Persistent incident-based heartbeat admission; no LLM or notification calls."""
import json
import os
from pathlib import Path
import tempfile

RETRY_SECONDS = 6 * 3600
RECOVERY_SECONDS = 120


def issues(hb):
    keys = {'unit:' + x for x in hb.get('failed_units', [])}
    if not hb.get('credentials_ok', True):
        keys.add('credentials')
    if hb.get('scan_degraded'):
        keys.add('scan-degraded')
    comp = hb.get('completeness') or {}
    for field in ('stalled', 'overdue'):
        keys.update(field + ':' + x['job'] for x in comp.get(field, []))
    for field in ('unreadable', 'failed_runs'):
        keys.update(field + ':' + x for x in comp.get(field, []))
    keys.update('blind:' + x.split(' (', 1)[0] for x in comp.get('blind', []))
    if comp.get('ok') is False and not any(comp.get(x) for x in ('stalled', 'overdue', 'unreadable', 'failed_runs', 'blind')):
        keys.add('completeness-unavailable')
    if hb.get('reply_id'):
        keys.add('reply:' + hb['reply_id'])
    return keys


class IncidentPolicy:
    def __init__(self, path):
        self.path = Path(path)
        self.persistence_error = False
        try:
            self.active = json.loads(self.path.read_text())['incidents']
            if not isinstance(self.active, dict) or any(not isinstance(v, dict) for v in self.active.values()):
                raise ValueError('invalid incidents')
        except FileNotFoundError:
            self.active = {}
        except (OSError, ValueError, KeyError, TypeError):
            self.active = {}
            self.persistence_error = True
        self.present = set()

    def save(self):
        # Keep in-memory suppression if disk fails, so a storage problem does
        # not create one paid call per minute. Expose the failure as an incident.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix='.incidents-', dir=str(self.path.parent))
            try:
                with os.fdopen(fd, 'w') as f:
                    json.dump({'incidents': self.active}, f)
                    f.flush(); os.fsync(f.fileno())
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            self.persistence_error = False
        except OSError:
            self.persistence_error = True

    def observe(self, hb, now):
        present = issues(hb)
        if self.persistence_error:
            present.add('incident-state-unavailable')
        # A broken probe cannot establish recovery of the things it watches.
        if hb.get('scan_degraded'):
            present.update(k for k in self.active if k.startswith('unit:'))
        if 'completeness-unavailable' in present:
            present.update(k for k in self.active if k.split(':')[0] in
                           {'stalled', 'overdue', 'unreadable', 'failed_runs', 'blind'})
        for key in list(self.active):
            row = self.active[key]
            if key in present:
                row.pop('missing_since', None)
            else:
                row.setdefault('missing_since', now)
                if now - row['missing_since'] >= RECOVERY_SECONDS:
                    del self.active[key]
        for key in present:
            self.active.setdefault(key, {'first_seen': now, 'reviewed': False, 'next_attempt': 0})
            self.active[key]['last_seen'] = now
        self.present = present
        due = [k for k in sorted(present) if not self.active[k].get('reviewed')
               and now >= self.active[k].get('next_attempt', 0)]
        # Reserve before the paid call: crashes/restarts don't cause a storm.
        for key in due:
            self.active[key]['next_attempt'] = now + RETRY_SECONDS
        self.save()
        return due

    def complete(self, success, now):
        for key in self.present:
            if success:
                self.active[key]['reviewed'] = True
                self.active[key]['last_review'] = now
            else:
                self.active[key]['next_attempt'] = now + RETRY_SECONDS
        self.save()

    def context(self):
        return '|'.join(f'{k}@{self.active[k]["first_seen"]}' for k in sorted(self.present))
