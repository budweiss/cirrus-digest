"""Explicit maintenance hold: durable private queue, one notice per thread.
No expiry-based release: operator validates services before setting active=false.
"""
import hashlib
import json
import os
from pathlib import Path

NOTICE = ("Your message has been received and queued. We're upgrading the system "
          "and expect processing to resume Saturday, September 19, 2026. "
          "If validation takes longer, it may resume Sunday, September 20. "
          "Existing dashboards remain available. Thank you for your patience.")

def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        os.chmod(tmp, 0o600)
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def active(root):
    path = Path(root) / 'config/project_maintenance.json'
    if not path.exists():
        return False
    # Invalid configuration stops intake rather than silently releasing work.
    obj = json.loads(path.read_text())
    if type(obj.get('active')) is not bool:
        raise ValueError('maintenance active must be boolean')
    return obj['active']

def key(text):
    return hashlib.sha256(text.encode()).hexdigest()

def defer(root, messages, creds, sender, thread_key):
    directory = Path(root) / 'data/intake-maintenance'
    failed = 0
    for row in messages:
        uid, address, subject, body, mid = row
        target = directory / 'pending' / (key(address + mid) + '.json')
        if not target.exists():
            atomic(target, list(row))
        notice = directory / 'notices' / (key(address + thread_key(subject)) + '.json')
        if notice.exists():
            # A prior uncertain send requires operator review, never blind retry.
            if json.loads(notice.read_text()).get('status') != 'sent':
                failed += 1
            continue
        atomic(notice, {'status': 'sending', 'message_id': mid})
        ok = sender(creds['outlook_email'], creds['outlook_password'], address,
                    subject if subject.lower().startswith('re:') else 'Re: ' + subject,
                    NOTICE, from_name=False, on_error='false', watch_promises=False,
                    auto_submitted=True, log=lambda _: None)
        atomic(notice, {'status': 'sent' if ok else 'failed', 'message_id': mid})
        failed += not ok
    return failed

def pending(root, limit=5):
    directory = Path(root) / 'data/intake-maintenance/pending'
    return [json.loads(p.read_text()) for p in sorted(directory.glob('*.json'),
            key=lambda p: p.stat().st_mtime)[:limit]]

def complete(root, address, mid):
    directory = Path(root) / 'data/intake-maintenance'
    source = directory / 'pending' / (key(address + mid) + '.json')
    if source.exists():
        done = directory / 'completed'
        done.mkdir(exist_ok=True, mode=0o700)
        os.replace(source, done / source.name)
