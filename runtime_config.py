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


def selftest():
    """Exercise decision-making functions with explicit inputs/expected outputs.

    Returns True on success, raises AssertionError on failure.
    """
    # runtime_path: pedagogy sources get a distinct overlay filename
    assert runtime_path(Path('/x/sources-pedagogy.json')).name == 'runtime-pedagogy.local.json'
    assert runtime_path(Path('/x/sources.json')).name == 'runtime.local.json'

    # merge: digest overlay only allows output_dir/log_dir
    base = {'digest': {'output_dir': '/a', 'log_dir': '/b'}, 'email': {'accounts': [{'label': 'x', 'enabled': True}]}}
    overlay = {'digest': {'output_dir': '/c'}}
    merged = merge(base, overlay)
    assert merged['digest']['output_dir'] == '/c'
    assert merged['digest']['log_dir'] == '/b'
    assert base['digest']['output_dir'] == '/a', 'merge must not mutate base'

    try:
        merge(base, {'digest': {'bogus_key': '/z'}})
        raise AssertionError('merge should reject unsupported digest overlay keys')
    except ValueError:
        pass

    # merge: email accounts overlay replaces by label, requires a label
    overlay2 = {'email': {'accounts': [{'label': 'x', 'enabled': False}, {'label': 'y', 'enabled': True}]}}
    merged2 = merge(base, overlay2)
    labels = {a['label']: a for a in merged2['email']['accounts']}
    assert labels['x']['enabled'] is False
    assert labels['y']['enabled'] is True
    assert len(merged2['email']['accounts']) == 2

    try:
        merge(base, {'email': {'accounts': [{'enabled': True}]}})
        raise AssertionError('merge should reject accounts missing a label')
    except ValueError:
        pass

    # validate: requires absolute paths
    try:
        validate({'digest': {'output_dir': 'relative/path', 'log_dir': '/b'}}, system='Darwin')
        raise AssertionError('validate should reject relative output_dir')
    except ValueError:
        pass

    validate({'digest': {'output_dir': '/a', 'log_dir': '/b'}}, system='Darwin')

    # validate: require_log=False skips log_dir check
    validate({'digest': {'output_dir': '/a'}}, system='Darwin', require_log=False)

    # validate: rejects macOS paths on Linux
    try:
        validate({'digest': {'output_dir': '/Users/me/x', 'log_dir': '/b'}}, system='Linux')
        raise AssertionError('validate should reject /Users/ paths on Linux')
    except ValueError:
        pass

    # same macOS path is fine on Darwin
    validate({'digest': {'output_dir': '/Users/me/x', 'log_dir': '/b'}}, system='Darwin')

    return True


if __name__ == '__main__':
    import argparse
    import sys
    p = argparse.ArgumentParser()
    p.add_argument('--account')
    p.add_argument('--selftest', action='store_true')
    args = p.parse_args()
    if args.selftest:
        try:
            selftest()
        except AssertionError as e:
            print(f'selftest failed: {e}')
            sys.exit(1)
        print('selftest ok')
        sys.exit(0)
    check_all(Path(__file__).resolve().parent / 'config', args.account)
    print('runtime configuration valid')
