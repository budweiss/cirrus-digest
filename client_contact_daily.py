#!/usr/bin/env python3
"""client_contact_daily.py — daily "has each client heard from us?" sweep.

S148. Runs on CIRRUS, not on the Mac, and that choice is the point.

`client-contact` existed as a Mac-side runner command from S145 and ran only
when someone asked — which is the same shape as the problem it solves. It was
built after a session concluded a client had been ignored for five weeks,
emailed him an apology saying so, and was wrong. A check against that only fires
when you already suspect is not a check.

WHY CIRRUS. The Mac is powered down for hours at a time (2026-09-10: seven).
A daily job there would silently skip those nights, and a monitor that silently
does not run is the exact failure class this whole line of work exists to close.
CIRRUS is always up, reaches CUMULUS over the LAN, and is where the daily
cadence already lives.

Reads both mailboxes — each box can only see its own — and REFUSES to report
on one. See client_contact_report.assess().
"""
import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import client_contact_report as R          # noqa: E402

DAYS = 45
CUMULUS = "buddy@192.168.0.204"            # LAN, as cirrus-cumulus-link probes
CUMULUS_DIR = "~/cirrus-digest"
CUMULUS_PY = ".venv/bin/python3"
DRY = "--dry-run" in sys.argv


def _probe_local(out):
    r = subprocess.run([sys.executable, str(HERE / "client_contact_probe.py"), str(DAYS)],
                       capture_output=True, text=True, timeout=180, cwd=str(HERE))
    (out / "CIRRUS.json").write_text(r.stdout or "")
    return r.returncode


def _probe_usage(out, host=None, dirn=None, py=None, tag="CIRRUS"):
    """S152. The self-serve half: when did each client last USE their tool?
    Written into the SAME per-box json the contact probe produced, so assess()
    sees both without a second merge path."""
    if host:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", host,
               f"cd {dirn} && {py} client_usage_probe.py"]
    else:
        cmd = [sys.executable, str(HERE / "client_usage_probe.py")]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                           cwd=None if host else str(HERE))
        u = json.loads(r.stdout).get("usage") or {}
    except Exception:
        return            # unknown, never "unused" -- assess() reports the gap
    f = out / f"{tag}.json"
    try:
        d = json.loads(f.read_text())
        d["usage"] = u
        f.write_text(json.dumps(d))
    except Exception:
        pass


def _probe_cumulus(out):
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", CUMULUS,
         f"cd {CUMULUS_DIR} && {CUMULUS_PY} client_contact_probe.py {DAYS}"],
        capture_output=True, text=True, timeout=240)
    (out / "CUMULUS.json").write_text(r.stdout or "")
    return r.returncode


def _rec(ok, note):
    if DRY:
        return
    try:
        import job_status
        job_status.record("clientcontact", ok, note)
    except Exception:
        pass


def _tg(msg):
    """Reuse model_health.tg -- the same token, the same Markdown, the same
    swallow-on-failure posture every other CIRRUS job already uses. Writing a
    second sender here is how snow/send_bid_email.py became T89."""
    if DRY:
        print("[dry-run] would telegram:\n" + msg)
        return
    try:
        import model_health
        model_health.tg(msg)
    except Exception as e:
        print("telegram failed:", type(e).__name__, e)


def build_note(info):
    """The ledger note for one run. Self-contained (S150)."""
    if info.get("refusal"):
        return f"UNVERIFIABLE: {info['refusal']}"
    bits = info.get("findings") or []
    if bits:
        return "; ".join(bits)
    return f"all clients current ({info.get('n_sends', 0)} sends)"


def note_samples():
    """Every note shape this job writes. Built by CALLING build_note (S150).

    The UNVERIFIABLE shape is the whole reason this job exists and the one the
    ledger will almost never contain: it means the sweep could not read both
    mailboxes, which must never be mistaken for "nobody has gone quiet".
    """
    return [
        ("all clients current",
         build_note({"findings": [], "n_sends": 139}), "productive"),
        ("a client has gone quiet",
         build_note({"findings": ["aggie — nothing at all in 45d"]}), "productive"),
        # S152: the self-serve shape. Never in the ledger until a tool goes
        # unused, which is exactly when it must not be mistaken for healthy.
        ("a client has stopped USING their tool",
         build_note({"findings": ["aggie — tool unused 63d, expected within 30d"]}),
         "productive"),
        ("the sweep could not read both mailboxes",
         build_note({"refusal": "only cirrustask@gmail.com answered"}), "blind"),
    ]


def main():
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] client-contact daily "
          f"({'dry-run' if DRY else 'live'})")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        rc_l, rc_c = _probe_local(out), _probe_cumulus(out)
        _probe_usage(out, tag="CIRRUS")
        _probe_usage(out, CUMULUS, CUMULUS_DIR, CUMULUS_PY, tag="CUMULUS")
        v = R.assess(out)

        if not v.get("ok"):
            # UNVERIFIABLE is a finding, not a quiet success. Recorded ok=False so
            # the completeness check sees it; a sweep that could not look must
            # never read the same as a sweep that looked and found nothing.
            note = build_note({"refusal": v.get("refusal")})
            print(note, f"(probe rc: cirrus={rc_l} cumulus={rc_c})")
            _rec(False, note)
            _tg(f"client-contact could not verify today: {v.get('refusal')}\n"
                f"Not reporting a partial answer — one mailbox looks exactly like "
                f"both. Probe exit codes: cirrus={rc_l} cumulus={rc_c}.")
            return 1

        quiet, silent = v["quiet"], v["silent"]
        for c in silent:
            print(f"  {c}: NO SEND IN WINDOW")
        for q in quiet:
            print(f"  {q['client']}: {q['days']}d quiet (limit {q['limit']}d)")
        for u in v.get("stale_use", []):
            print(f"  {u['client']}: tool unused "
                  f"{'ever' if u['never'] else str(round(u['days']))+'d'} "
                  f"(limit {u['limit']}d) — {u['what']}")
        for c in v.get("unknown_use", []):
            print(f"  {c}: tool usage UNKNOWN (not the same as unused)")
        if not quiet and not silent and not v.get("stale_use"):
            print(f"  all clients heard from inside their window "
                  f"({v['n_sends']} sends across both mailboxes)")

        bits = ([f"{c} — nothing at all in {DAYS}d" for c in silent]
                + [f"{q['client']} — {q['days']}d quiet, expected within "
                   f"{q['limit']}d" for q in quiet]
                + [(f"{u['client']} — has NEVER used their tool" if u["never"]
                    else f"{u['client']} — tool unused {u['days']:.0f}d, "
                         f"expected within {u['limit']}d")
                   for u in v.get("stale_use", [])])
        note = build_note({"findings": bits, "n_sends": v["n_sends"]})
        # ok=True even when a client is quiet: the JOB worked. Whether a client
        # is overdue is the finding it is meant to produce, not a fault in it.
        _rec(True, note)
        if bits:
            # S152: the heading must match the finding. It said "has not heard
            # from us" for every case, including a client who has stopped USING
            # their tool -- the opposite statement. A misleading alert header is
            # the same defect as hoaleads' `why` pointing at a healthy source:
            # it sends the reader somewhere the problem is not.
            heads = []
            if silent or quiet:
                heads.append("has not heard from us")
            if v.get("stale_use"):
                heads.append("has stopped using their tool")
            _tg(f"*A client {' / '.join(heads)}:*\n"
                + "\n".join(f"• {b}" for b in bits)
                + "\n\n_Both mailboxes checked (cirrustask + cumulus), and "
                  "self-serve tool use alongside them. Justin is exempt from the "
                  "CONTACT check only — his dashboard is the channel — but his "
                  "use of it is watched._")
        return 0


def selftest() -> int:
    """Offline: no probes, no ssh, no mailboxes.

    S150. PASS 6 flagged this file as having no selftest at all, and it was
    right: the dev-loop's gate 2 runs `<module> --selftest`, so without one a
    broken build_note here is caught only when trap-lint next runs.
    """
    bad = 0

    def ck(name, cond):
        nonlocal bad
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        bad += 0 if cond else 1

    ck("a clean sweep names the send count",
       build_note({"findings": [], "n_sends": 139}) == "all clients current (139 sends)")
    ck("a finding is reported verbatim",
       "aggie" in build_note({"findings": ["aggie — nothing at all in 45d"]}))
    ck("several findings are joined, not truncated to the first",
       build_note({"findings": ["a — x", "b — y"]}) == "a — x; b — y")
    # THE one that matters: a sweep that could not read both mailboxes must
    # never be mistaken for "nobody has gone quiet".
    n = build_note({"refusal": "only cirrustask@gmail.com answered"})
    ck("an unverifiable sweep says UNVERIFIABLE", n.startswith("UNVERIFIABLE:"))
    ck("...and a refusal beats any findings passed alongside it",
       build_note({"refusal": "one mailbox", "findings": ["a — x"]})
       .startswith("UNVERIFIABLE:"))

    shapes = note_samples()
    # NOT a hardcoded count: pinning it to 3 broke the moment a fourth shape was
    # added, which is a test failing for bookkeeping rather than for a defect.
    ck("every declared sample builds", len(shapes) >= 3 and all(s[1] for s in shapes))
    ck("...and one of them is the UNVERIFIABLE path",
       any(e == "blind" for _l, _n, e in shapes))

    print()
    print("all client_contact_daily selftests passed" if not bad else f"{bad} FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main())
