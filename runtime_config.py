"""Host configuration overlay, separate from git-managed research sources.

runtime.local.json holds only digest paths and host mailbox definitions.
It is deliberately distinct from sources.local.json (approved RSS feeds).
"""
import copy
import json
import platform
from pathlib import Path


def load_sources(path):
    path = Path(path)
    data = json.loads(path.read_text())
    overlay_path = path.with_name('runtime.local.json')
    if overlay_path.exists():
        overlay = json.loads(overlay_path.read_text())
        if set(overlay) - {'digest', 'email'}:
            raise ValueError('unsupported runtime overlay section')
        data = merge(data, overlay)
    validate(data)
    return data


def merge(base, overlay):
    result = copy.deepcopy(base)
    paths = overlay.get('digest', {})
    if set(paths) - {'output_dir', 'log_dir'}:
        raise ValueError('runtime digest overlay may contain only paths')
    result.setdefault('digest', {}).update(paths)
    accounts = result.setdefault('email', {}).setdefault('accounts', [])
    for account in overlay.get('email', {}).get('accounts', []):
        label = account.get('label')
        if not label:
            raise ValueError('runtime account requires a label')
        accounts[:] = [a for a in accounts if a.get('label') != label]
        accounts.append(copy.deepcopy(account))
    return result


def validate(data, system=None):
    system = system or platform.system()
    for key in ('output_dir', 'log_dir'):
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


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--account')
    args = p.parse_args()
    check(Path(__file__).resolve().parent / 'config/sources.json', args.account)
    print('runtime configuration valid')
