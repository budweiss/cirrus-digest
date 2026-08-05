#!/usr/bin/env python3
"""
bill_newdev_weekly.py  (S49, 2026-08-01)
===============================================================================
CIRRUS-side weekly Delaware new-development lead check for Bill (William
Hutchins, Knight Property Services). Replaces the MacBook-tied Cowork task
'bill-newdev-weekly-check' — runs entirely on CIRRUS (open network for the DE
GIS pulls), so it no longer depends on the laptop being awake on Monday.

Flow (mirrors the old task):
  1. plus_pull.py   — pull DE PLUS residential projects (Kent+Sussex, >=50u) and
                      diff against last week's baseline -> out/plus_new.json.
                      FIRST run on CIRRUS baselines silently (0 new -> no email).
  2. If NO new leads and not --dry-run: log + exit, send nothing.
  3. plus_enrich.py — add owner/builder-of-record + mailing contact.
  4. build_report.py— build DE-New-Developments.xlsx (owner-enriched).
  5. Email Bill (cc Buddy), signed as CIRRUS, with the workbook attached and the
     working-rates pricing note. Consequential matters (contracts/money) are NOT
     handled here — this is a leads heads-up only.

Usage:
  python3 bill_newdev_weekly.py --dry-run   # run pipeline, PRINT the email, send nothing
  python3 bill_newdev_weekly.py             # live: email Bill ONLY if new leads appear
"""
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE       = Path(__file__).resolve().parent      # ~/projects/cirrus-digest/newdev
DIGEST_DIR = HERE.parent                           # ~/projects/cirrus-digest
sys.path.insert(0, str(DIGEST_DIR))                # for job_status
import node_info                                   # S56: sign as the running node
NODE = node_info.node_name()                       # CIRRUS (dev) / CUMULUS (beta)
OUT        = HERE / "out"
NEW_FILE   = OUT / "plus_new.json"
LEADS_FILE = OUT / "plus_leads.json"
XLSX       = HERE / "DE-New-Developments.xlsx"

TO      = "whutchins@knightpropertysvs.com"
CC      = "Buddy.Weiss@outlook.com"
SUBJECT = "New Delaware development leads this week"

# Verbatim working-rates note (matches the retired Cowork task). If Bill sends
# real rates, drop this and switch to his numbers.
PRICING_NOTE = (
    "On the snow estimates — unless you send us updated numbers, we'll price using "
    "our working rates: plowing $0.02/sq ft per push, salt $250/acre per application, "
    "walks $0.60/linear ft per push, and a $2,000 seasonal minimum (~5 events/yr). "
    "Send your real rates anytime and we'll switch to those."
)


def _run(script, *args):
    r = subprocess.run([sys.executable, str(HERE / script), *args],
                       cwd=str(HERE), capture_output=True, text=True)
    print(f"--- {script} (exit {r.returncode}) ---")
    print((r.stdout or "")[-1200:])
    if r.returncode != 0:
        print("STDERR:", (r.stderr or "")[-800:])
    return r.returncode == 0


def _units(n):
    u = n.get("R_UNITS") or n.get("RESIDENTIAL_UNITS") or 0
    try:
        return int(u)
    except Exception:
        return 0


def _tier(u):
    return "A" if u >= 250 else "B" if u >= 150 else "C" if u >= 100 else "D"


def summarize(new):
    lines = []
    cc = Counter((n.get("COUNTY") or "?") for n in new)
    lines.append(f"{len(new)} new lead(s): " + ", ".join(f"{k} {v}" for k, v in cc.items()))
    for n in sorted(new, key=_units, reverse=True)[:6]:
        u = _units(n)
        name = (n.get("NOTES") or n.get("LOCATION") or n.get("RECTYPE") or "").replace("\n", " ").strip()[:60]
        lines.append(f"  • {n.get('kind', 'lead')}: {name or '(unnamed)'} — {u}u (Tier {_tier(u)}), {n.get('COUNTY', '')}")
    return "\n".join(lines)


def compose(new):
    return "\n".join([
        "Hi Bill,",
        "",
        "Quick weekly check on new Delaware residential developments (50+ units, "
        "Kent & Sussex counties). New since last week:",
        "",
        summarize(new),
        "",
        "The attached workbook has the full list with the owner / builder of record "
        "and a mailing contact on each lead. Let me know if any look worth pursuing.",
        "",
        PRICING_NOTE,
        "",
        f"— {NODE} (Buddy's assistant)",
    ])


def _rec(dry, ok, note=""):
    if dry:
        return
    try:
        import job_status
        job_status.record("billnewdev", ok, note)
    except Exception:
        pass


def main():
    dry = "--dry-run" in sys.argv
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] bill_newdev_weekly ({'dry-run' if dry else 'live'})")

    if not _run("plus_pull.py"):
        print("plus_pull failed — aborting, nothing sent.")
        _rec(dry, False, "plus_pull failed")
        return

    new = json.loads(NEW_FILE.read_text()) if NEW_FILE.exists() else []
    print(f"NEW leads this run: {len(new)}")

    if not new and not dry:
        print("No new leads this week — no email sent.")
        _rec(dry, True, "no new leads")
        return

    # Build the attachment (needed whenever we would send, and useful to verify in dry-run)
    _run("plus_enrich.py")
    _run("build_report.py")

    if new:
        body = compose(new)
    else:
        body = "(dry-run: 0 NEW leads — baseline run or no change. Live mode would send NOTHING.)"

    print("=" * 70)
    print("SUBJECT:", SUBJECT)
    print(body)
    print("=" * 70)
    print("workbook:", XLSX, "| exists:", XLSX.exists())

    if dry:
        print("DRY RUN — nothing sent.")
        return

    # Live send via the shared CIRRUS SMTP sender (run from DIGEST_DIR so its
    # `from send_digest import ...` resolves).
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        tf.write(body)
        bodyfile = tf.name
    env = dict(os.environ, CC_EMAIL=CC)
    r = subprocess.run([sys.executable, str(DIGEST_DIR / "send_bid_email.py"),
                        TO, SUBJECT, bodyfile, str(XLSX)],
                       cwd=str(DIGEST_DIR), capture_output=True, text=True, env=env)
    print((r.stdout or "") + (r.stderr or ""))
    print("send exit:", r.returncode)
    _rec(dry, r.returncode == 0, "sent" if r.returncode == 0 else "send failed")


if __name__ == "__main__":
    main()
