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
PEEK_FILE  = OUT / "plus_new_peek.json"   # S142: dry-run diff; never the live one
LEADS_FILE = OUT / "plus_leads.json"
XLSX       = HERE / "DE-New-Developments.xlsx"

TO      = "whutchins@knightpropertysvs.com"
CC      = "Buddy.Weiss@outlook.com"
SUBJECT = "New Delaware development leads this week"
QUIET_MIN_DAYS = 6      # S146: a weekly note, guarded weekly -- see the quiet branch


def QUIET_SUBJECT():
    """S146. The quiet note used to go out under SUBJECT -- "New Delaware
    development leads this week" over a body saying there are none, which reads
    as a broken email rather than a reassuring one. It also threaded every
    quiet week into the same Gmail conversation as the real lead emails, so a
    genuine lead would arrive collapsed under a pile of "nothing new".
    Dated, because that is what makes it scannable in a thread list."""
    from datetime import datetime
    return f"Delaware development leads - nothing new this week ({datetime.now():%b %d})"

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


def quiet_note(swept, sent=True, suppressed=""):
    """The ledger note a quiet week records. ONE definition, three callers.

    S146. It used to be written inline as "quiet week sent (...)" while the
    dry-run printed "no new leads (...)" and completeness.py's selftest asserted
    against a third, hand-typed copy. All three drifted apart the moment the live
    one changed, and the drift was invisible: the rule scored the LIVE note
    (1, True) -- PRODUCTIVE, because produced_phrases contains "sent" -- so a
    quiet week counted as a productive week, zero_runs reset every Monday, and
    max_zero_runs=6 could never be reached. The check S142 retuned was dead on
    arrival and its selftest passed, because the selftest tested the string the
    DRY-RUN prints.

    So: the note must never contain the word "sent", and every caller must come
    through here. runner/trap_lint.sh T91 asserts completeness.py's billnewdev
    rule scores this function's real output as zero.
    """
    n = f"no new leads ({swept})"
    if suppressed:
        n += f" - note suppressed: {suppressed}"
    elif not sent:
        n += " - SEND FAILED"
    return n


def compose_quiet(swept):
    c = _swept_counts()
    if c:
        swept_sentence = (
            f"We checked {c['plus']} built communities, {c['dev']} development "
            f"applications and {c['permit']} building permits across Kent and "
            "Sussex. None of them crossed into the list since the last check.")
    else:
        # Say it plainly rather than quietly dropping the evidence: a note that
        # cannot show what it searched is exactly the ambiguity this email exists
        # to remove, and Bill should see that we know it.
        swept_sentence = ("We ran the usual search, but could not read back the "
                          "counts this time — flagging that rather than leaving "
                          "it out.")
    """The quiet-week note. S144.

    Kept beside compose() and used by BOTH the live path and the dry-run
    preview, so `--dry-run` shows the email that would actually go out. The
    dry-run used to print "Live mode would send NOTHING", which stopped being
    true the moment this branch started sending and would have been a diagnostic
    stating the opposite of the behaviour.
    """
    return "\n".join([
        "Hi Bill,",
        "",
        "Weekly check on new Delaware residential developments "
        "(50+ units, Kent & Sussex counties).",
        "",
        "Nothing new this week.",
        "",
        swept_sentence,
        "",
        "These come along roughly once a month, so a quiet week is normal. "
        "This note is so you can tell a quiet week from a broken one.",
        "",
        "Your workbook from the last update still stands; nothing in it changed.",
        "",
        f"— {NODE} (Buddy's assistant)",
    ])


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


def _swept():
    """The sweep evidence that turns "no new leads" from unreadable into proof.

    S142, 2026-09-10. On a quiet week this job recorded exactly "no new leads"
    and nothing else. That note cannot distinguish the two cases that matter:
    the PLUS/parcel sweep ran and Delaware genuinely permitted no new 50+ unit
    residential development that week, versus the sweep returned an empty set
    because a source changed shape and answered 200 with nothing. Bill's feed
    has already been silently wrong once this exact way -- S78, when the source
    started spelling "Sussex_County" with an underscore and an exact-string test
    dropped the 41 newest rows without a word.

    So the note now carries what was swept. The completeness rule deliberately
    does NOT add these to its arithmetic (see halftimecatalogue: summing them
    would make 0-found-of-546-swept read as productive, which is the precise
    failure worth catching). They are there to be read by whoever gets the
    alert, as the first question: did the sweep see anything at all?

    Fails to "swept unknown" rather than to silence -- a missing artifact is
    itself worth seeing in the note, not a reason to drop the evidence.
    """
    c = _swept_counts()
    if c is None:
        return "swept unknown (plus_leads.json unreadable)"
    return f"swept {c['plus']} plus, {c['dev']} dev-app, {c['permit']} permit"


def _swept_counts():
    """The raw sweep counts, or None if the artifact cannot be read.

    S144: split out because _swept() above is a MONITORING string -- terse,
    jargon, meant for the completeness ledger -- and the quiet-week email needs
    the same numbers in English. Reusing _swept() verbatim in the client note
    produced "Searched: swept 546 plus, 249 dev-app, 33 permit", which is our
    telemetry showing through to Bill. One source of truth for the numbers, two
    renderings for two audiences.
    """
    try:
        d = json.loads((OUT / "plus_leads.json").read_text())
        return {"plus": len(d.get("plus_projects") or []),
                "dev": len(d.get("dev_applications") or []),
                "permit": len(d.get("building_permits") or [])}
    except Exception:
        return None


def _send(body, subject=None, attach=True):
    """Send one email to Bill. Returns True on a confirmed send.

    S144: extracted so the quiet-week note and the leads email go out the SAME
    way -- one sender, one CC, one duplicate stamp. A second hand-rolled send
    path is how a send stops being guarded: send_guard.mark_sent is what stops
    an auto-restart re-mailing a client, and it has to run for BOTH kinds of
    email or the quiet-week note becomes the one that can double-send.

    attach=False for the quiet-week note: nothing changed, so re-attaching the
    same workbook every quiet week would be noise, not service.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        tf.write(body)
        bodyfile = tf.name
    subject = subject or SUBJECT
    args = [sys.executable, str(DIGEST_DIR / "send_bid_email.py"),
            TO, subject, bodyfile]
    if attach:
        args.append(str(XLSX))
    env = dict(os.environ, CC_EMAIL=CC)
    # run from DIGEST_DIR so send_bid_email's `from send_digest import ...` resolves
    r = subprocess.run(args, cwd=str(DIGEST_DIR), capture_output=True,
                       text=True, env=env)
    print((r.stdout or "") + (r.stderr or ""))
    print("send exit:", r.returncode)
    if r.returncode == 0:
        # Stamp FIRST, then record -- see billsnow for why the order matters.
        import send_guard
        if not send_guard.mark_sent("billnewdev", subject):
            print("WARNING: send stamp not written — a restart could re-send.")
    return r.returncode == 0


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
    force = "--force-send" in sys.argv
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] bill_newdev_weekly ({'dry-run' if dry else 'live'})")

    # S81: refuse a SECOND send on the same day -- same reasoning as billsnow.
    # This unit is auto-restartable and a restart re-runs main() from the top.
    # Checked before plus_pull/enrich/build, which are the expensive part.
    # Fails OPEN by design -- see send_guard's module docstring.
    if not dry and not force:
        import send_guard
        stamp = send_guard.already_sent_today("billnewdev")
        if stamp:
            print(send_guard.blocked_message("billnewdev", stamp))
            _rec(dry, True, "already sent today — duplicate send suppressed")
            return

    # S142: a dry-run must not advance the seen-baseline. Without --peek, running
    # this to LOOK at the job consumed the week's new leads and Monday's live run
    # emailed Bill nothing about them. See plus_pull.py's --peek block.
    if not _run("plus_pull.py", *(["--peek"] if dry else [])):
        print("plus_pull failed — aborting, nothing sent.")
        _rec(dry, False, "plus_pull failed")
        return

    # Under --peek plus_new.json is deliberately NOT rewritten -- it still holds
    # the last LIVE run's diff -- so a dry-run reading it would show last week's
    # leads as this week's. Read the peek file instead.
    src = PEEK_FILE if dry else NEW_FILE
    new = json.loads(src.read_text()) if src.exists() else []
    print(f"NEW leads this run: {len(new)}")

    # S142: show the quiet-week note the LIVE path would record, even on a week
    # that has leads. Without this the note format is only ever exercised on a
    # quiet week -- i.e. it is proved against a fixture and never against the
    # real plus_leads.json, which is the S81 mistake exactly.
    if dry:
        print(f'quiet-week note would be: "{quiet_note(_swept())}"')

    if not new and not dry:
        # S144 (Buddy, 2026-09-10). This used to return silently, and the cost of
        # that showed up from the client's chair: Bill's last development-leads
        # email was 2026-08-03 -- the one that wrongly listed 504 leads because
        # the source had renumbered itself -- and then five weeks of nothing. A
        # weekly product that goes quiet after a visibly broken send does not
        # read as "working, nothing to report". It reads as abandoned.
        #
        # Qualifying 50+ unit developments genuinely arrive about once a month,
        # so most weeks ARE empty. The fix is not to manufacture leads; it is to
        # make a quiet week legible AS a quiet week, by saying what was searched.
        # Same principle as the completeness note: 0 found out of 546 swept is a
        # different statement from 0 found.
        swept = _swept()

        # S146. The duplicate guard above is PER DAY, which is right for the
        # leads email -- a lead is news whenever it arrives. It is wrong for
        # this one. cirrus-billnewdev is a oneshot on Skywarden's restart
        # allowlist, so any restart on a day other than Monday clears the daily
        # guard and mails Bill a second "nothing new this week". A weekly
        # product may say "nothing new" once a week, not once a day.
        #
        # Fails OPEN, like the rest of send_guard: None (no stamp on record)
        # sends. A feed silenced indefinitely by one unreadable file is a worse
        # failure than one duplicate note.
        import send_guard
        since = send_guard.days_since_last_send("billnewdev")
        if since is not None and since < QUIET_MIN_DAYS:
            reason = f"last note {since}d ago, minimum {QUIET_MIN_DAYS}d"
            print(f"Quiet week, but NOT sending: {reason}.")
            _rec(dry, True, quiet_note(swept, suppressed=reason))
            return

        print(f"No new leads this week — sending the quiet-week note ({swept}).")
        ok = _send(compose_quiet(swept), subject=QUIET_SUBJECT(), attach=False)
        _rec(dry, ok, quiet_note(swept, sent=ok))
        return

    # Build the attachment (needed whenever we would send, and useful to verify in dry-run)
    _run("plus_enrich.py")
    _run("build_report.py")

    if new:
        body = compose(new)
    else:
        body = compose_quiet(_swept())   # exactly what live would send

    print("=" * 70)
    print("SUBJECT:", SUBJECT)
    print(body)
    print("=" * 70)
    print("workbook:", XLSX, "| exists:", XLSX.exists())

    if dry:
        print("DRY RUN — nothing sent.")
        return

    ok = _send(body, attach=True)
    _rec(dry, ok, "sent" if ok else "send failed")


if __name__ == "__main__":
    main()
