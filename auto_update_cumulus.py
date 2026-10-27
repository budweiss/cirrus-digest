#!/usr/bin/env python3
"""auto_update_cumulus.py -- the CUMULUS entry point for auto_update.py (S516).

Same dry run, its own job_status name. job_status's selftest maps each record()
name to the file a box's unit runs (S254), so a script both boxes run records a
different name from each entry point, or one box goes unwatched: the
foundation_renewal split (S316). cumulus2 runs this file too, but its policy
sets ledger=false, so only cumulus1 records.

Usage: as auto_update.py, whose main() parses every argument:
  auto_update_cumulus.py [--machine NAME] [--out DIR] [--no-record] | --selftest
"""
import sys

import auto_update

if __name__ == "__main__":
    sys.exit(auto_update.main(sys.argv, record=lambda ok, note: auto_update._record(
        "autoupdatecumulus", ok, note)))
