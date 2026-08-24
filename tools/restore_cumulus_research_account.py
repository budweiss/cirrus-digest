#!/usr/bin/env python3
"""S76 repair: put the 'cumulus-research' email account back into
config/sources.json on cumulus1.

The account entry is CUMULUS-local (never in git). The S75 pull-restoration
clobbered sources.json before skip-worktree protected it; the localize repair
restored the digest paths but not this entry, so cumulus-intake failed every
cycle from 12:57 with the cumulustask inbox unwatched.

Copies the entry from sources.json.bak-localize (the last pre-clobber copy).
Idempotent; backs up first; prints labels only, never values.
Run on cumulus1 from /home/buddy/cirrus-digest.
"""
import json
import shutil
import sys
from datetime import date

CUR = "config/sources.json"
BAK = "config/sources.json.bak-localize"
LABEL = "cumulus-research"

cur = json.load(open(CUR))
accts = cur.setdefault("email", {}).setdefault("accounts", [])
if any(a.get("label") == LABEL for a in accts):
    print(f"'{LABEL}' already present — nothing to do")
    print("labels:", [a.get("label") for a in accts])
    sys.exit(0)

bak = json.load(open(BAK))
found = [a for a in bak.get("email", {}).get("accounts", []) if a.get("label") == LABEL]
if not found:
    # S76: the bak-localize copy turned out to predate the entry, so carry the
    # reconstruction here. Fields per CUMULUS.md S63 (address + credential_key,
    # confirmed by live IMAP login then) and END-USER-DIRECT-INTAKE.md (Google
    # Workspace account -> Gmail IMAP endpoint); shape mirrors gmail-research.
    print(f"'{LABEL}' not in {BAK} — using the reconstructed S63 entry")
    found = [{
        "label": LABEL,
        "address": "cumulus@cumulustask.com",
        "imap_server": "imap.gmail.com",
        "imap_port": 993,
        "credential_key": "outlook_password",
        "enabled": True,
        "_note": "CUMULUS end-user direct intake (S63) - watched by "
                 "cumulus-intake.service. LOCAL-ONLY, never in git; restore "
                 "via tools/restore_cumulus_research_account.py (S76).",
    }]

shutil.copy(CUR, f"{CUR}.bak-{date.today().isoformat()}")
accts.append(found[0])
with open(CUR, "w") as f:
    f.write(json.dumps(cur, indent=2) + "\n")
print("restored. labels now:", [a.get("label") for a in accts])
