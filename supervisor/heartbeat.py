"""Deterministic (no-LLM) heartbeat check for the CUMULUS supervisor (B1).

Runs every ~60s from supervisor_agent.py's loop. Pure systemctl/subprocess
checks, no Claude API call — costs nothing, so it can run tightly. Deliberately
does NOT use tools.py's ledgered wrappers: at a 60s cadence those would fill
the audit ledger with routine "still fine" noise. This module has its own
light, unledgered checks; the reasoning pass (via tools.py) is what actually
gets ledgered, once the heartbeat decides something is worth escalating.
"""
import subprocess

import completeness
from tools import ALLOWED_UNITS

CREDS_PROBE = ["sudo", "-n", "-u", "buddy", "/usr/local/sbin/cumulus_creds_health.py"]


def _unit_is_failed(unit: str) -> bool:
    r = subprocess.run(["systemctl", "is-failed", unit],
                        capture_output=True, text=True, timeout=10)
    return r.stdout.strip() == "failed"


def _credentials_ok() -> tuple:
    r = subprocess.run(CREDS_PROBE, capture_output=True, text=True, timeout=10)
    out = (r.stdout or r.stderr).strip()
    return r.returncode == 0, out


def run_heartbeat() -> dict:
    """Returns {"ok", "failed_units", "credentials_ok", "credentials_detail", "detail"}.
    ok=False means the run-loop should escalate to a reasoning pass now,
    rather than waiting for the once-daily scheduled one."""
    failed = [u for u in sorted(ALLOWED_UNITS) if _unit_is_failed(u)]
    creds_ok, creds_detail = _credentials_ok()

    # S67 — COMPLETENESS, not just liveness. `is-failed` only answers "did the
    # process crash". On 2026-08-18 Bill's hoaleads run exited 0 having produced
    # nothing (its county source had started 403-ing), and this heartbeat
    # reported "all clear" — correctly by its own definition, uselessly by ours.
    # Deterministic and free, so it belongs on the 60s tick; when it trips,
    # ok=False routes it into the EXISTING escalation path rather than adding a
    # second one. Never allowed to break the heartbeat itself.
    try:
        comp = completeness.check()
    except Exception as e:
        comp = {"ok": True, "detail": f"completeness check unavailable: {e}",
                "stalled": [], "unreadable": [], "unmonitored": []}

    detail_parts = []
    if failed:
        detail_parts.append(f"failed units: {', '.join(failed)}")
    if not creds_ok:
        detail_parts.append(f"credentials unhealthy: {creds_detail}")
    if not comp.get("ok", True):
        detail_parts.append(f"COMPLETENESS: {comp.get('detail','')}")

    return {
        "ok": not failed and creds_ok and comp.get("ok", True),
        "failed_units": failed,
        "credentials_ok": creds_ok,
        "credentials_detail": creds_detail,
        "completeness": comp,
        "detail": "; ".join(detail_parts) if detail_parts else "all clear",
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run_heartbeat(), indent=2))
