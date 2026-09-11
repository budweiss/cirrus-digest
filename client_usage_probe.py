#!/usr/bin/env python3
"""client_usage_probe.py — when did each client last USE their tool?

S152. `client-contact` answers "when did we last email them", and on
2026-09-11 that turned out to be the wrong question for a self-serve client.
It flagged Aggie at 21 days of email silence. The number that mattered was that
she had not generated an offer in 63 days -- six weeks before the web-address
move we had assumed was the cause. Meanwhile Justin is EXEMPT from the contact
check on purpose (his dashboard is the delivery channel), which made him
invisible to it entirely: nothing anywhere would notice if he stopped opening
the page.

So: for a tool the client drives, the health metric is USE, not contact. This
reports what THIS box can see; the aggregator merges both boxes. Reports the
NEWEST use per client and nothing about what they did -- no addresses, no
property details, no client data of any kind beyond a timestamp and a count.

None (not zero, not "never") is UNKNOWN -- a source this box cannot read must
never read as "the client has stopped", which would be the same class of false
alarm the contact check just produced.
"""
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()


def _offer_last_use():
    """Newest generated offer, from the app's own history. CIRRUS + CUMULUS
    both carry a copy; whichever this box has is fine, the merge takes the max."""
    for p in (HOME / "projects/realestate/config/offer_history.json",
              HOME / "cirrus-digest/realestate/config/offer_history.json"):
        try:
            rows = json.loads(p.read_text())
        except Exception:
            continue
        stamps = []
        for r in rows if isinstance(rows, list) else []:
            try:
                stamps.append(datetime.fromisoformat(r["generated"]).timestamp())
            except Exception:
                continue
        if stamps:
            return {"epoch": max(stamps), "count": len(stamps),
                    "source": "offer_history.json"}
        return {"epoch": None, "count": 0, "source": "offer_history.json (empty)"}
    return None


def _halftime_last_use():
    """Newest request the dashboard served. Its own journal is the only record --
    the page keeps no state, which is the point of it."""
    try:
        r = subprocess.run(
            ["sudo", "-n", "journalctl", "-u", "halftime-serve.service",
             "--since", "-60 days", "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return None
    except Exception:
        return None
    stamps = []
    for line in r.stdout.splitlines():
        # Ignore the browser's automatic favicon fetch: it rides along with a
        # real visit and would also fire on a bare probe, so counting it would
        # overstate use.
        if " GET " not in line or "favicon" in line:
            continue
        m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d)", line)
        if not m:
            continue
        try:
            stamps.append(datetime.fromisoformat(m.group(1)).timestamp())
        except Exception:
            continue
    if not stamps:
        return {"epoch": None, "count": 0, "source": "halftime-serve journal (no hits)"}
    return {"epoch": max(stamps), "count": len(stamps),
            "source": "halftime-serve journal"}


# client -> (what the tool is, how to read its last use)
TOOLS = {
    "aggie":  ("the OFFER agreement-of-sale generator", _offer_last_use),
    "justin": ("the Halftime dashboard", _halftime_last_use),
}


def main():
    import socket
    out = {"box": socket.gethostname(), "ok": False, "error": None, "usage": {}}
    try:
        for client, (what, fn) in TOOLS.items():
            try:
                res = fn()
            except Exception as e:
                res = None
                out.setdefault("notes", []).append(
                    f"{client}: {type(e).__name__}")
            if res is None:
                continue          # this box cannot see that tool; say nothing
            out["usage"][client] = {**res, "what": what}
        out["ok"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    print(json.dumps(out))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

# S152 T92 proof: this comment rides along, and the deploy subject must name the file.
