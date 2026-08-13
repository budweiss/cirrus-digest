#!/usr/bin/env python3
"""Fixed, root-owned credential-health probe for the CUMULUS supervisor (B1).

Deployed to /usr/local/sbin/cumulus_creds_health.py, owned root:root mode 755
— NOT synced as part of supervisor/app/ (which cumulus-supervisor itself can
write to). A script cumulus-supervisor could edit and then run via sudo would
be a privilege-escalation hole, so this one lives somewhere it can't touch.

sudoers grants cumulus-supervisor NOPASSWD run of this exact path as buddy
(who owns the credentials file) — see cirrus-repo/supervisor/cumulus-supervisor.sudoers.
Prints only ok/fail + key count. Never prints a credential value.
"""
import json
import sys

CREDS_PATH = "/home/buddy/cirrus-digest/config/credentials.json"

try:
    with open(CREDS_PATH) as f:
        data = json.load(f)
    print(f"OK keys={len(data)}")
    sys.exit(0)
except Exception as e:
    print(f"FAIL {type(e).__name__}: {e}")
    sys.exit(1)
