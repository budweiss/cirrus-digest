"""Deterministic (no-LLM) heartbeat check for the CUMULUS supervisor (B1).

Runs every ~60s from supervisor_agent.py's loop. Pure systemctl/subprocess
checks, no Claude API call — costs nothing, so it can run tightly. Deliberately
does NOT use tools.py's ledgered wrappers: at a 60s cadence those would fill
the audit ledger with routine "still fine" noise. This module has its own
light, unledgered checks; the reasoning pass (via tools.py) is what actually
gets ledgered, once the heartbeat decides something is worth escalating.
"""
import subprocess

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

    detail_parts = []
    if failed:
        detail_parts.append(f"failed units: {', '.join(failed)}")
    if not creds_ok:
        detail_parts.append(f"credentials unhealthy: {creds_detail}")

    return {
        "ok": not failed and creds_ok,
        "failed_units": failed,
        "credentials_ok": creds_ok,
        "credentials_detail": creds_detail,
        "detail": "; ".join(detail_parts) if detail_parts else "all clear",
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run_heartbeat(), indent=2))
