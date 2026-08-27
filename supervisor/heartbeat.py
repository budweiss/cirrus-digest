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
    # S81: now only reached on the degraded fallback path. Wrapped because an
    # exception here used to propagate out of run_heartbeat and kill the 60s
    # tick -- the monitor taking down the thing it monitors, which this package
    # forbids everywhere else (see completeness._save_state).
    try:
        r = subprocess.run(["systemctl", "is-failed", unit],
                            capture_output=True, text=True, timeout=10)
    except Exception:
        return False
    return r.stdout.strip() == "failed"


def _list_failed_units() -> list:
    """Every failed unit on the box, asked of systemd rather than of a list.

    S81. This used to iterate ALLOWED_UNITS -- nine names, hand-maintained,
    last extended when the ninth was added. Since then opportunity-scout,
    halftime-catalogue, halftime-routing, halftime-serve, cumulus-daily-brief,
    entity-kb-weekly-digest, cirrus-offer and cloudflared went live on this box
    and none of them were added, so none of them could ever be reported failed.

    On 2026-08-27 that came due. opportunity-scout.service died at 02:00 on a
    transient DNS outage and was still sitting in `failed` at 08:40 with nobody
    told, while cirrus-modelhealth -- which IS in the list -- failed at 05:30,
    was caught, restarted and healed inside sixty seconds. Same box, same
    sixty-second heartbeat, opposite outcomes, and the only difference was
    membership of a list.

    Worse, the gap was a SEAM rather than one module's oversight:
    completeness.check() deliberately skips a run with ok=False ("that is
    heartbeat's problem"), and heartbeat only looked at nine units -- so a
    failed job outside the list was declined by both checks in turn.

    Asking systemd removes the list from the answer, so a job added tomorrow is
    watched the day it is installed and nobody has to remember anything.

    ALLOWED_UNITS deliberately stays as-is: it is the RESTART allowlist, which
    is authority to act, and widening what we can SEE must not widen what we
    may DO to a client-facing service unasked.

    Returns None -- not [] -- when the listing itself could not be taken. An
    empty list means "systemd says nothing is failed"; None means "we do not
    know", and the caller must not render the two the same way. That
    distinction is the whole S74 stall-detector lesson applied here: could not
    check must never print as healthy.
    """
    try:
        r = subprocess.run(
            ["systemctl", "list-units", "--failed", "--all", "--plain",
             "--no-legend", "--no-pager"],
            capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return _parse_failed_units(r.stdout or "")


# Leading status glyph systemd prints per row. --plain is supposed to suppress
# it, but the flag has meant different things across versions and a glyph left
# on the front silently turns every unit name into a non-match -- an empty
# result that reads exactly like "nothing is failed". Strip it explicitly and
# test it, rather than trusting the flag.
_UNIT_GLYPHS = "\u25cf\u00d7\u2718\u2717*\u2022"
_UNIT_SUFFIXES = (".service", ".timer", ".mount", ".socket", ".path",
                  ".target", ".scope", ".slice", ".automount", ".swap")


def _parse_failed_units(out: str) -> list:
    """Unit names from `systemctl list-units --failed` output. Pure, so testable."""
    units = []
    for line in out.splitlines():
        # "\u25cf unit.service loaded failed failed Description..." -- field 0
        # after the optional glyph.
        name = line.strip().lstrip(_UNIT_GLYPHS).strip().split(" ", 1)[0].strip()
        if name.endswith(_UNIT_SUFFIXES):
            units.append(name)
    return sorted(set(units))


def _credentials_ok() -> tuple:
    r = subprocess.run(CREDS_PROBE, capture_output=True, text=True, timeout=10)
    out = (r.stdout or r.stderr).strip()
    return r.returncode == 0, out


def run_heartbeat() -> dict:
    """Returns {"ok", "failed_units", "credentials_ok", "credentials_detail", "detail"}.
    ok=False means the run-loop should escalate to a reasoning pass now,
    rather than waiting for the once-daily scheduled one."""
    # Ask systemd for every failed unit (S81). If that listing could not be
    # taken at all, fall back to probing the units we own one by one and SAY
    # that the sweep was degraded -- an unavailable scan must not render as
    # "all clear", which is the failure mode this whole module keeps paying for.
    failed = _list_failed_units()
    scan_degraded = failed is None
    if scan_degraded:
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
    if scan_degraded:
        detail_parts.append("DEGRADED: systemd failed-unit listing unavailable; "
                            "only the restart allowlist was probed")
    if not creds_ok:
        detail_parts.append(f"credentials unhealthy: {creds_detail}")
    if not comp.get("ok", True):
        detail_parts.append(f"COMPLETENESS: {comp.get('detail','')}")

    return {
        "ok": not failed and not scan_degraded and creds_ok and comp.get("ok", True),
        "failed_units": failed,
        "scan_degraded": scan_degraded,
        "credentials_ok": creds_ok,
        "credentials_detail": creds_detail,
        "completeness": comp,
        "detail": "; ".join(detail_parts) if detail_parts else "all clear",
    }




# ── selftest ───────────────────────────────────────────────────────────────────
def selftest() -> bool:
    """S81. The unit scan had no test at all, which is why a nine-name list
    could go stale for three sessions without anything noticing."""
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))

    # The real shape, taken from `systemctl list-units --failed` on cumulus1
    # on 2026-08-27 -- the run that found opportunity-scout still failed.
    real = ("\u25cf opportunity-scout.service loaded failed failed "
            "Opportunity scout (S77) \u2014 nightly divergent multi-LLM brainstorm\n")
    ck("a failed unit is found in real output",
       _parse_failed_units(real) == ["opportunity-scout.service"])
    ck("the leading glyph does not swallow the unit name",
       "opportunity-scout.service" in _parse_failed_units(real))
    ck("plain (glyph-free) output parses the same",
       _parse_failed_units("opportunity-scout.service loaded failed failed x")
       == ["opportunity-scout.service"])

    # THE bug this file is being changed for, tested END TO END rather than at
    # the parser. Narrowing the scan back to a list would leave every parser
    # check above green, so the guard has to run run_heartbeat() itself with a
    # unit that is deliberately NOT in the restart allowlist.
    ck("the sample unit really is outside the restart allowlist",
       "opportunity-scout.service" not in ALLOWED_UNITS)

    import completeness as _c
    saved = (globals()["_list_failed_units"], globals()["_credentials_ok"],
             _c.check, globals()["_unit_is_failed"])
    try:
        globals()["_list_failed_units"] = lambda: ["opportunity-scout.service"]
        globals()["_credentials_ok"] = lambda: (True, "stub")
        _c.check = lambda *a, **k: {"ok": True, "detail": "stub",
                                    "stalled": [], "unreadable": [],
                                    "unmonitored": []}
        hb = run_heartbeat()
        ck("run_heartbeat REPORTS a failed unit outside the allowlist",
           hb["failed_units"] == ["opportunity-scout.service"])
        ck("...and that flips ok=False so it escalates", hb["ok"] is False)
        ck("...and names it in the detail line",
           "opportunity-scout.service" in hb["detail"])

        # "could not check" must escalate too, not read as all-clear.
        globals()["_list_failed_units"] = lambda: None
        globals()["_unit_is_failed"] = lambda u: False
        hb = run_heartbeat()
        ck("an unavailable scan is marked degraded", hb["scan_degraded"] is True)
        ck("...and does NOT report all clear", hb["ok"] is False)
        ck("...and says so in the detail line", "DEGRADED" in hb["detail"])

        # The healthy path must still be quiet, or the alert becomes noise.
        globals()["_list_failed_units"] = lambda: []
        hb = run_heartbeat()
        ck("a clean box still reports ok", hb["ok"] is True)
        ck("...with no degraded flag", hb["scan_degraded"] is False)
    finally:
        globals()["_list_failed_units"], globals()["_credentials_ok"] = saved[0], saved[1]
        _c.check = saved[2]
        globals()["_unit_is_failed"] = saved[3]

    ck("no failures parses empty", _parse_failed_units("") == [])
    ck("a legend/blank line is not mistaken for a unit",
       _parse_failed_units("\n0 loaded units listed.\n") == [])
    ck("timers are caught too, not just services",
       _parse_failed_units("x.timer loaded failed failed y") == ["x.timer"])
    ck("duplicates collapse",
       _parse_failed_units("a.service l f f d\na.service l f f d") == ["a.service"])

    # "could not check" must not read as "nothing failed".
    ck("an unavailable listing is None, not an empty list",
       _list_failed_units.__doc__ and "None" in _list_failed_units.__doc__)

    bad = 0
    for name, ok in checks:
        print(("  ok   " if ok else "  FAIL ") + name)
        bad += 0 if ok else 1
    print()
    print("all heartbeat selftests passed" if not bad else f"{bad} FAILED")
    return bad == 0


if __name__ == "__main__":
    import json
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(0 if selftest() else 1)
    print(json.dumps(run_heartbeat(), indent=2))
