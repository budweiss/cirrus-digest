#!/usr/bin/env python3
"""tailscale_heal_cumulus.py -- the cumulus1 entry point for tailscale_heal.py (S579).

Same job, its own job_status name, so job_status's S254 scan maps this file's
unit to a REMOTE_JOBS row (the auto_update_cumulus.py pattern).
"""
import sys

import tailscale_heal

if __name__ == "__main__":
    sys.exit(tailscale_heal.main(sys.argv, record=lambda ok, note: tailscale_heal._record(
        "tailscalehealcumulus", ok, note)))
