#!/usr/bin/env python3
"""Move host-only settings out of tracked source files. Dry-run unless --apply.

Refuses any migration that changes the effective configuration. Never pulls,
resets a repository, touches credentials, or replays jobs.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime_config import load_sources, merge, runtime_path


def atomic_write(path, data):
    fd, tmp = tempfile.mkstemp(prefix='.runtime-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def migrate(path, apply=False):
    path = Path(path).resolve()
    repo = path.parent.parent
    rel = str(path.relative_to(repo))
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args])
    before_bytes = path.read_bytes()
    base_bytes = git('show', 'HEAD:' + rel)
    base = json.loads(base_bytes)
    current = json.loads(before_bytes)
    overlay_path = runtime_path(path)
    old_overlay = overlay_path.read_bytes() if overlay_path.exists() else None
    overlay = json.loads(old_overlay) if old_overlay is not None else {}
    for key in ('output_dir', 'log_dir'):
        value = current.get('digest', {}).get(key)
        if value != base.get('digest', {}).get(key):
            overlay.setdefault('digest', {})[key] = value
    accounts = base.get('email', {}).get('accounts', [])
    for account in current.get('email', {}).get('accounts', []):
        if account not in accounts:
            overlay = merge(overlay, {'email': {'accounts': [account]}})
    # Exact semantic equality includes feeds, recipients, mailbox definitions,
    # model choices and every other field, not merely valid output paths.
    before = load_sources(path)
    with tempfile.TemporaryDirectory() as td:
        staged = Path(td) / path.name
        staged.write_bytes(base_bytes)
        runtime_path(staged).write_text(json.dumps(overlay))
        additions = path.with_name('sources-pedagogy.local.json')
        if path.name == 'sources-pedagogy.json' and additions.exists():
            staged.with_name(additions.name).write_bytes(additions.read_bytes())
        if load_sources(staged) != before:
            raise ValueError('unrecognized source differences; migration would change runtime behavior')
    flags = git('ls-files', '-v', '--', rel).decode().strip()
    if before_bytes == base_bytes and old_overlay == (json.dumps(overlay, indent=2) + '\n').encode() and flags.startswith('H '):
        return {'file': rel, 'status': 'already migrated'}
    if not apply:
        return {'file': rel, 'status': 'verified dry run; effective configuration unchanged'}
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup = path.parent / 'snapshots' / ('s182-migration-' + stamp)
    backup.mkdir(parents=True, mode=0o700)
    manifest = {'head': git('rev-parse', 'HEAD').decode().strip(), 'index_flag': flags, 'files': {}}
    for source in [path, overlay_path]:
        if source.exists():
            data = source.read_bytes()
            atomic_write(backup / source.name, data)
            manifest['files'][source.name] = hashlib.sha256(data).hexdigest()
    atomic_write(backup / 'manifest.json', (json.dumps(manifest, indent=2)+'\n').encode())
    assert path.read_bytes() == before_bytes, 'config changed during migration'
    assert (overlay_path.read_bytes() if overlay_path.exists() else None) == old_overlay, 'overlay changed during migration'
    try:
        atomic_write(overlay_path, (json.dumps(overlay, indent=2)+'\n').encode())
        git('update-index', '--no-skip-worktree', '--', rel)
        git('update-index', '--no-assume-unchanged', '--', rel)
        atomic_write(path, base_bytes)
        if load_sources(path) != before:
            raise ValueError('post-migration runtime mismatch')
    except Exception:
        atomic_write(path, before_bytes)
        if old_overlay is None:
            overlay_path.unlink(missing_ok=True)
        else:
            atomic_write(overlay_path, old_overlay)
        if flags.startswith('S '):
            git('update-index', '--skip-worktree', '--', rel)
        raise
    return {'file': rel, 'status': 'migrated; effective configuration unchanged', 'backup': str(backup)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    for name in ('sources.json', 'sources-pedagogy.json'):
        path = root / 'config' / name
        if path.exists():
            print(json.dumps(migrate(path, args.apply)))


if __name__ == '__main__':
    main()
