#!/usr/bin/env python3
"""Mac review-page tunnel using the existing runner credential handoff pattern.

The parent reads local.json and passes two values only in the child environment.
No credential file is copied and no secret is put in arguments or printed.
"""
import json
import os
from pathlib import Path


def main():
    try:
        creds = json.loads((Path.home() / 'Documents/Cowork/cirrus/config/local.json').read_text())
        values = {name: creds[key] for name, key in (
            ('TUNNEL_SERVICE_TOKEN_ID', 'cf_ssh_service_token_id'),
            ('TUNNEL_SERVICE_TOKEN_SECRET', 'cf_ssh_service_token_secret'))}
        if not all(isinstance(v, str) and v for v in values.values()):
            raise ValueError('missing')
    except Exception:
        print('Yahoo review tunnel cannot read its existing access credential.', flush=True)
        return 1
    env = dict(os.environ, **values)
    env['PATH'] = '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'
    os.execve('/usr/bin/ssh', ['/usr/bin/ssh', '-N', '-o', 'BatchMode=yes',
              '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=30',
              '-o', 'ServerAliveCountMax=3', '-L', '127.0.0.1:8638:127.0.0.1:8638',
              'cumulus1'], env)


if __name__ == '__main__':
    raise SystemExit(main())
