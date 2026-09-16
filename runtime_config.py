"""Host configuration overlay, separate from git-managed research sources.

runtime.local.json holds only digest paths and host mailbox definitions.
It is deliberately distinct from sources.local.json (approved RSS feeds).
"""
import copy
import json
import platform
from pathlib import Path


def runtime_path(path):
    path = Path(path)
    if path.name == 'sources-pedagogy.json':
        return path.with_name('runtime-pedagogy.local.json')
    return path.with_name('runtime.local.json')


def save_pedagogy_sources(path, accepted):
    """Persist discovered feeds without writing host paths or tracked config."""
    import fcntl
    import os
    import tempfile
    local = Path(path).with_name('sources-pedagogy.local.json')
    with open(str(local) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(local.read_text()) if local.exists() else {}
        if set(data) - {'rss', 'podcasts'}:
            raise ValueError('pedagogy source overlay may contain only feeds')
        for feed in accepted:
            key, url_key = ('podcasts', 'feed') if feed['type'] == 'podcast' else ('rss', 'rss')
            rows = data.setdefault(key, [])
            if not any(x[url_key].lower() == feed['feed'].lower() for x in rows):
                rows.append({'name': feed['name'], url_key: feed['feed']})
        fd, tmp = tempfile.mkstemp(prefix='.pedagogy-', dir=str(local.parent))
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write('\n'); f.flush(); os.fsync(f.fileno())
            os.replace(tmp, local)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def load_sources(path):
    path = Path(path)
    data = json.loads(path.read_text())
    overlay_path = runtime_path(path)
    if overlay_path.exists():
        overlay = json.loads(overlay_path.read_text())
        if set(overlay) - {'digest', 'email'}:
            raise ValueError('unsupported runtime overlay section')
        data = merge(data, overlay)
    validate(data, require_log=path.name != 'sources-pedagogy.json')
    if path.name == 'sources-pedagogy.json':
        local = path.with_name('sources-pedagogy.local.json')
        if local.exists():
            additions = json.loads(local.read_text())
            if set(additions) - {'rss', 'podcasts'}:
                raise ValueError('pedagogy source overlay may contain only feeds')
            for key, url_key in [('rss', 'rss'), ('podcasts', 'feed')]:
                feeds = data.setdefault(key, [])
                seen = {f[url_key].lower() for f in feeds}
                for feed in additions.get(key, []):
                    if feed[url_key].lower() not in seen:
                        feeds.append(copy.deepcopy(feed))
                        seen.add(feed[url_key].lower())
    return data


def merge(base, overlay):
    result = copy.deepcopy(base)
    paths = overlay.get('digest', {})
    if set(paths) - {'output_dir', 'log_dir'}:
        raise ValueError('runtime digest overlay may contain only paths')
    result.setdefault('digest', {}).update(paths)
    for account in overlay.get('email', {}).get('accounts', []):
        accounts = result.setdefault('email', {}).setdefault('accounts', [])
        label = account.get('label')
        if not label:
            raise ValueError('runtime account requires a label')
        accounts[:] = [a for a in accounts if a.get('label') != label]
        accounts.append(copy.deepcopy(account))
    return result


def validate(data, system=None, require_log=True):
    system = system or platform.system()
    for key in (('output_dir', 'log_dir') if require_log else ('output_dir',)):
        value = data.get('digest', {}).get(key)
        if not value or not Path(value).is_absolute():
            raise ValueError(f'digest.{key} must be absolute')
        if system == 'Linux' and str(value).startswith('/Users/'):
            raise ValueError(f'digest.{key} has a macOS path on Linux')


def check(path, required_account=None):
    data = load_sources(path)
    if required_account and not any(a.get('label') == required_account and
                                   a.get('enabled', True)
                                   for a in data.get('email', {}).get('accounts', [])):
        raise ValueError('required intake account missing or disabled')
    return data


def check_all(config_dir, required_account=None):
    config_dir = Path(config_dir)
    main = check(config_dir / 'sources.json', required_account)
    pedagogy = config_dir / 'sources-pedagogy.json'
    if pedagogy.exists():
        load_sources(pedagogy)
    return main


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--account')
    args = p.parse_args()
    check_all(Path(__file__).resolve().parent / 'config', args.account)
    print('runtime configuration valid')
