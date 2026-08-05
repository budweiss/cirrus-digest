"""
node_info.py — one place to answer "which node am I?" (S56).
===============================================================================
Returns the running node's display name (CIRRUS / CUMULUS / STRATUS) from
config/node_profiles.json keyed by the TARGET_ENV env var (dev/beta/prod).
Used so client emails sign as the box that actually sent them — on cutover to
CUMULUS the sign-offs read "CUMULUS" automatically, with zero per-box edits.

Stdlib only; never raises (falls back to "CIRRUS").
"""
import json
import os
from pathlib import Path

_PROFILES = Path(__file__).resolve().parent / "config" / "node_profiles.json"


def node_name(default="CIRRUS"):
    try:
        env = os.environ.get("TARGET_ENV", "dev")
        prof = json.loads(_PROFILES.read_text())
        return prof.get(env, {}).get("node", default) or default
    except Exception:
        return default
