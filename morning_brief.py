#!/usr/bin/env python3
"""
CIRRUS Morning Brief  (S49, 2026-08-01)
===============================================================================
Server-side replacement for the MacBook-tied Cowork scheduled task
'cirrus-morning-review'. Runs ON CIRRUS at 07:30 via com.cirrus.morningbrief,
i.e. AFTER the 07:00 daily digest. It composes a short health + digest +
action-items + pending-decisions brief PURELY from local files on CIRRUS (no
web fetch, no external LLM required) and delivers it two ways:

  * email  — reuses send_digest.send_email() (Gmail SMTP → Buddy.Weiss@outlook.com)
  * Telegram — sendMessage to the owner chat id

Because it reads CIRRUS's own files, it does not depend on the MacBook being
awake — which was the whole point of the migration (see
docs/CIRRUS-Scheduled-Task-Migration-Plan.md).

Usage:
  python3 morning_brief.py            # compose + SEND (email + telegram)
  python3 morning_brief.py --dry-run  # compose + PRINT to stdout, send nothing
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

REPO_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Config / paths (mirror send_digest.py) ─────────────────────────────────────
PROJECT_DIR  = Path.home() / "projects/cirrus-digest"
CONFIG_PATH  = PROJECT_DIR / "config/sources.json"
CREDS_PATH   = PROJECT_DIR / "config/credentials.json"
PENDING_FILE = PROJECT_DIR / "config/pending_approvals.json"
BUILDS_FILE = PROJECT_DIR / "logs/dev-loop/builds.json"

# Make sibling modules importable when launched by full path from launchd.
sys.path.insert(0, str(PROJECT_DIR))

CONFIG = json.load(open(CONFIG_PATH))
CREDS  = json.load(open(CREDS_PATH))

DIGEST_CFG  = CONFIG["digest"]
OUTPUT_DIR  = Path(DIGEST_CFG["output_dir"])
LOG_DIR     = Path(DIGEST_CFG["log_dir"])
ACTIONS_DIR = OUTPUT_DIR / "actions"

TG_TOKEN = CREDS.get("telegram_bot_token", "")
TG_USER  = str(CREDS.get("telegram_user_id", "")).strip()

TODAY     = datetime.now().strftime("%Y-%m-%d")
DAY_NAME  = datetime.now().strftime("%A, %B %d")

# ── Small helpers ──────────────────────────────────────────────────────────────
def _read(p):
    try:
        return Path(p).read_text()
    except Exception:
        return ""

def _find_latest(pattern):
    try:
        fs = sorted(OUTPUT_DIR.glob(pattern), reverse=True)
        return fs[0] if fs else None
    except Exception:
        return None

def _find_latest_action(prefix):
    try:
        fs = sorted(ACTIONS_DIR.glob(f"{prefix}-*.md"), reverse=True)
        return fs[0] if fs else None
    except Exception:
        return None

def _bullets_under(md_text, header):
    """Return the '- ...' bullet lines under a '## HEADER' section."""
    out, grab = [], False
    for line in md_text.splitlines():
        s = line.strip()
        if s.startswith("## "):
            grab = header.lower() in s.lower()
            continue
        if grab and s.startswith(("-", "*", "•")):
            out.append("- " + s.lstrip("-*• ").strip())
    return out

# ── Gatherers (each is defensive; a failure degrades one line, not the brief) ──
def gather_digest():
    f = _find_latest("daily-*.md")
    if not f:
        return {"ok": False, "line": "⚠️ No daily digest file found in output dir.",
                "notable": [], "dated_today": False}
    txt = f.read_text(errors="ignore")
    dated_today = TODAY in f.name
    m = re.search(r"Items processed:\s*([0-9]+)", txt)
    count = m.group(1) if m else "?"
    # best-effort "notable" article titles: h2/h3 headings, minus section labels
    skip = ("run stats", "links visited", "access needed", "action item",
            "disk status", "cirrus", "today", "digest", "improvement",
            "recommendation", "follow-up", "interesting tools")
    raw = [t.strip() for t in re.findall(r"^#{2,3}\s+(.+)$", txt, flags=re.MULTILINE)]
    # digest headings often join several headlines with " / " — split + flatten
    titles, seen = [], set()
    for t in raw:
        for part in t.split(" / "):
            p = part.strip()
            key = p.lower()
            if p and key not in seen and not any(s in key for s in skip):
                seen.add(key)
                titles.append(p if len(p) <= 90 else p[:87] + "…")
    kw = ("claude", "anthropic", "llama", "qwen", "deepseek", "local", "ollama",
          "open-weight", "open weight", "gpt", "mistral", "gemma", "model")
    fav = [t for t in titles if any(k in t.lower() for k in kw)]
    notable = (fav or titles)[:3]
    line = (f"{'✅' if dated_today else '⚠️'} Digest {f.name} — {count} items"
            + ("" if dated_today else " (NOT dated today!)"))
    return {"ok": dated_today, "line": line, "notable": notable, "dated_today": dated_today}

def gather_actions():
    f = _find_latest_action("daily-actions")
    if not f:
        return {"actions": [], "notes": [], "file": None}
    txt = f.read_text(errors="ignore")
    return {
        "actions": _bullets_under(txt, "ACTION ITEMS")[:5],
        "notes":   _bullets_under(txt, "CIRRUS IMPROVEMENT NOTES")[:5],
        "file":    f.name,
    }

def gather_pending():
    try:
        items = json.loads(PENDING_FILE.read_text())
    except Exception:
        return []
    pend = [i for i in items if i.get("status") == "pending"]
    out = []
    for it in pend:
        det = (it.get("detail") or "").strip().replace("\n", " ")
        out.append(f"{it.get('type','?')}: {det[:90]}")
    return out

def gather_awaiting_builds():
    """Dev-Loop builds that were built + council-approved and are waiting for a
    ship/discard decision. The /accept queue does NOT surface these, so without
    this a built-but-unconfirmed item can sit unseen for days (happened S57->S60)."""
    try:
        builds = json.loads(BUILDS_FILE.read_text())
    except Exception:
        return []
    out = []
    for b in builds:
        if b.get("status") == "awaiting-confirm":
            summ = (b.get("summary") or b.get("detail") or "").strip().replace("\n", " ")
            out.append(f"{b.get('id','?')}: {summ[:90]}")
    return out

def _domain(url: str) -> str:
    try:
        d = urllib.parse.urlparse(url).netloc
        return d[4:] if d.startswith("www.") else d
    except Exception:
        return url

def _parse_paywall_entries(text):
    """paywalls.log entries are 3 lines each:
    '[ts] PAYWALL | URL: ...' / '          Sender: ...' / '          Subject: ...'
    Returns [{"date", "url", "subject"}, ...] oldest-first."""
    out = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"\[(\d{4}-\d{2}-\d{2}) [\d:]+\] PAYWALL \| URL: (.+)", lines[i])
        if m:
            subject = ""
            if i + 2 < len(lines):
                sm = re.match(r"\s*Subject:\s*(.*)", lines[i + 2])
                if sm:
                    subject = sm.group(1).strip()
                    if subject.startswith("[ref] "):
                        subject = subject[6:]
            out.append({"date": m.group(1), "url": m.group(2).strip(), "subject": subject})
            i += 3
        else:
            i += 1
    return out

def gather_attention():
    flags = []
    # bot.log: real errors, ignoring the benign getUpdates long-poll timeouts
    bl = _read(LOG_DIR / "bot.log")
    if bl:
        tail = bl.splitlines()[-600:]
        # Only flag errors logged TODAY — stale lines shouldn't keep the
        # verdict red forever. bot.log lines are stamped "[YYYY-MM-DD ...]".
        errs = [l for l in tail
                if TODAY in l
                and ("error" in l.lower() or "traceback" in l.lower())
                and "getupdates" not in l.lower()]
        if errs:
            flags.append(f"bot.log: {len(errs)} error line(s) today — e.g. {errs[-1][-120:].strip()}")
    # paywalls: any hits logged today — name the SOURCE + article, plus how
    # often that domain has come up blocked all-time, so Buddy can judge
    # whether a subscription there is actually worth setting up (S66 ask).
    pw = _read(LOG_DIR / "paywalls.log")
    if pw:
        entries = _parse_paywall_entries(pw)
        today_entries = [e for e in entries if e["date"] == TODAY]
        if today_entries:
            domain_counts = {}
            for e in entries:
                d = _domain(e["url"])
                domain_counts[d] = domain_counts.get(d, 0) + 1
            for e in today_entries:
                d = _domain(e["url"])
                title = e["subject"] or "(no title)"
                n = domain_counts.get(d, 1)
                recur = f" — blocked {n}x all-time" if n > 1 else " — first time seen"
                flags.append(f"paywall: {d}{recur} — \"{title[:80]}\"")
    return flags

# ── Compose ────────────────────────────────────────────────────────────────────
def timemachine_verdict(name, mounted, result, age_days, last_txt):
    """(line, ok) from the four facts. PURE, so it can be tested (S141).

    Each of the four can be wrong while the others look fine, which is the
    whole reason this reports three separate facts rather than one. The case
    that matters most is S72's: a RECENT backup and a FAILING attempt means it
    just started failing, and freshness alone called that healthy.
    """
    problems = []
    if not mounted:
        problems.append(f"destination '{name}' NOT MOUNTED")
    if result not in (0, None):
        problems.append(f"last attempt FAILED (RESULT={result})")
    if age_days is None:
        problems.append("no completed backup recorded")
    elif age_days >= 2:
        problems.append(f"last backup was {age_days} days ago")

    if problems:
        return ("- ❌ Time Machine: " + "; ".join(problems)
                + f" (last completed: {last_txt})"), False
    return f"- ✅ Time Machine: last backup {last_txt}, destination mounted", True


def gather_timemachine():
    """Is CIRRUS actually being backed up? -> (line, ok)

    S73 (Buddy): "whatever we need to set to make sure we get daily backups in
    TM is how we should have this set."

    Configuration alone cannot answer that. AutoBackup=1 and a 24h interval were
    BOTH already true this morning while the backup volume sat FileVault-locked
    and unmountable, because CIRRUS now runs with nobody logged in and the
    unlock key lives in the login keychain. Time Machine had silently stopped,
    and nothing anywhere would have said so — the brief did not mention it at
    all. Buddy found out because he asked an unrelated question.

    So the brief now reports three separate facts, because each can be wrong
    while the others look fine:
      * is the destination MOUNTED right now (a locked volume vanishes entirely)
      * how old is the last COMPLETED backup
      * did the last ATTEMPT succeed (RESULT)
    A recent backup plus a failing attempt means it JUST started failing — that
    is the shape S72 saw, and freshness alone called it healthy.
    """
    import plistlib
    from datetime import datetime, timezone
    try:
        raw = subprocess.run(
            ["defaults", "export", "/Library/Preferences/com.apple.TimeMachine.plist", "-"],
            capture_output=True, timeout=20).stdout
        d = plistlib.loads(raw)
        dest = (d.get("Destinations") or [{}])[0]
    except Exception as e:
        return f"- ❌ cannot read Time Machine state ({e}) — treat as UNVERIFIED", False

    name = dest.get("LastKnownVolumeName", "?")
    mounted = os.path.isdir(f"/Volumes/{name}") if name != "?" else False
    result = dest.get("RESULT")
    snaps = dest.get("SnapshotDates") or []

    age_days, last_txt = None, "never"
    if snaps:
        dt = snaps[-1]
        if not isinstance(dt, datetime):
            try:
                dt = datetime.strptime(str(dt)[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                dt = None
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - dt).days
            last_txt = dt.astimezone().strftime("%a %d %b %H:%M")

    return timemachine_verdict(name, mounted, result, age_days, last_txt)


def gather_stalls():
    """S74: what should be moving and isn't. -> (lines, ok)

    Deliberately NOT folded into the verdict. A stall is usually a slow-burn
    finding — "no outcomes recorded" is true for weeks and turning the brief red
    every morning for it would train Buddy to skim past the header, which is the
    one thing that must stay meaningful. It reports; it does not shout.
    """
    try:
        r = subprocess.run(
            [sys.executable, os.path.join(REPO_DIR, "stall_check.py"), "--brief"],
            capture_output=True, text=True, timeout=90)
        lines = [l for l in (r.stdout or "").splitlines() if l.strip().startswith("-")]
        if not lines:
            # S97. This branch used to substitute "- ✅ nothing stalled", so a
            # stall_check.py that died on import printed GREEN into the brief --
            # and the ok flag saying otherwise is discarded by the caller
            # (`_st_ok`). That is the exact S96 failure class (a check that
            # passes because it cannot see) living inside the check built to
            # catch it. A checker that produced no findings has not said "all
            # clear"; it has said nothing, and nothing is not green.
            #
            # The clean path is unaffected: --brief always prints at least one
            # "- ..." line, including "- ✅ nothing stalled (N signals checked)".
            #
            # stderr is deliberately NOT quoted into this line. The brief is an
            # EMAIL, and piping arbitrary subprocess stderr into it is the S67
            # hazard exactly (a read-only diagnostic whose author had no reason
            # to think about tokens). The traceback stays in
            # morningbrief-launchd.log, which is where this line sends you.
            return ([f"- ⚠️ stall check produced no findings (exit {r.returncode})"
                     f" — it did not run to completion; see morningbrief-launchd.log"],
                    False)
        return lines, (r.returncode == 0)
    except Exception as e:
        # A checker that cannot run must say so, not vanish. That silence is
        # exactly the failure this whole check exists to catch.
        return [f"- ⚠️ stall check could not run ({e})"], False


# ── the two decisions this brief actually makes (S141) ───────────────────────
# Both were buried inside compose(), which calls six gather_*() functions that
# each hit disk or the network -- so neither could be tested, and the mutation
# probe found 73 surviving mutations in this file: every branch here could be
# inverted and the suite stayed green.
#
# They are pulled out as PURE functions for that reason alone. compose() is
# unchanged in behaviour; it now delegates the two judgements that matter.

def health_verdict(dated_today, attention, awaiting, tm_ok):
    """(healthy, verdict line). PURE.

    A box with no backup coverage is NOT healthy, however green everything else
    looks -- the fact that was missing entirely until S73. All four conditions
    are required, and each one alone can sink the verdict.
    """
    healthy = bool(dated_today) and not attention and not awaiting and bool(tm_ok)
    return healthy, ("✅ CIRRUS healthy" if healthy else "⚠️ Needs a look")


def next_action(dated_today, tm_ok, awaiting, pending, attention):
    """The single suggested next action. PURE.

    THE ORDER IS THE POINT, and it has been wrong once. S73: `not tm_ok` sat
    BELOW `pending`, so a locked backup volume rendered as "Review the 1
    pending /accept item(s)" -- the most important fact on the page demoted
    below a one-tap chore. Caught then by running the failing case; pinned now
    so it cannot silently reorder again.
    """
    if not dated_today:
        return "Investigate the 7am digest — today's file is missing or misdated."
    if not tm_ok:
        return "Time Machine is not protecting this box — see Backup above."
    if awaiting:
        return f"Ship or discard {len(awaiting)} built Dev-Loop item(s) — reply /builds."
    if pending:
        return f"Review the {len(pending)} pending /accept item(s)."
    if attention:
        return "Check the attention flag(s) above."
    return "Nothing needs you this morning."


def compose():
    dig = gather_digest()
    act = gather_actions()
    pend = gather_pending()
    awaiting = gather_awaiting_builds()
    att = gather_attention()
    tm_line, tm_ok = gather_timemachine()

    healthy, verdict = health_verdict(dig["dated_today"], att, awaiting, tm_ok)

    lines = [f"# ☀️ CIRRUS Morning Brief — {DAY_NAME}", "", f"**{verdict}**", "",
             dig["line"]]
    if dig["notable"]:
        lines.append("Notable: " + "; ".join(dig["notable"]))
    lines.append("")

    lines.append(f"**Pending decisions ({len(pend)})**")
    if pend:
        lines += [f"- {p}" for p in pend[:6]]
        if len(pend) > 6:
            lines.append(f"- …and {len(pend) - 6} more")
    else:
        lines.append("- None — /accept queue is clear")
    lines.append("")

    if awaiting:
        lines.append(f"**⚠️ Awaiting your ship/discard ({len(awaiting)})** — built + council-approved; reply /builds or run dev-ship")
        lines += [f"- {a}" for a in awaiting[:6]]
        lines.append("")

    if act["actions"]:
        lines.append("**Today's action items**")
        lines += act["actions"]
        lines.append("")
    if act["notes"]:
        lines.append("**CIRRUS improvement notes**")
        lines += act["notes"]
        lines.append("")

    lines.append("**Attention**")
    lines += ([f"- {a}" for a in att] if att else ["- Nothing flagged"])
    lines.append("")

    st_lines, _st_ok = gather_stalls()
    lines.append("**Stalled?**")
    lines += st_lines
    lines.append("")

    lines.append("**Backup**")
    lines.append(tm_line)
    lines.append("")

    # scheduled-jobs status (did the CIRRUS jobs run & succeed) — from job_status ledger
    try:
        import job_status
        jlines, _jok = job_status.summarize()
    except Exception:
        jlines = []
    if jlines:
        lines.append("**Scheduled jobs**")
        lines += jlines
        lines.append("")

    lines.append(f"**Next:** {next_action(dig['dated_today'], tm_ok, awaiting, pend, att)}")
    lines.append("")
    lines.append("*Composed by CIRRUS on-box (morning_brief.py) — no MacBook required.*")

    subject = f"☀️ CIRRUS Morning Brief — {DAY_NAME}  ({verdict})"
    return subject, "\n".join(lines)

# ── Delivery ───────────────────────────────────────────────────────────────────
def send_telegram(text):
    if not TG_TOKEN or not TG_USER:
        return "telegram: no token/user configured — skipped"
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    # keep well under Telegram's 4096 hard cap
    chunk = text if len(text) <= 3900 else text[:3900] + "\n…(truncated)"
    data = urllib.parse.urlencode({"chat_id": TG_USER, "text": chunk,
                                   "parse_mode": "Markdown"}).encode()

    # S66 fix: Telegram returns HTTP 400 (not a 200-with-ok:false) for
    # malformed Markdown entities. urlopen() RAISES on a non-2xx status, so
    # this used to jump straight to the outer except and skip the plain-text
    # retry below entirely -- it only ever ran for the rarer 200-but-ok:false
    # case. Catch the markdown attempt's own exception so the plain-text
    # retry actually runs on both failure shapes. Same bug, same fix as
    # cumulus_daily_brief.py's send_telegram (found live, S66).
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
            ok = json.loads(r.read()).get("ok")
        if ok:
            return "telegram: sent"
    except Exception:
        pass

    try:
        data = urllib.parse.urlencode({"chat_id": TG_USER, "text": chunk}).encode()
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
            ok = json.loads(r.read()).get("ok")
        return "telegram: sent (plain)" if ok else "telegram: failed"
    except Exception as e:
        return f"telegram: error {e}"

def send_all(subject, body):
    results = []
    # email (reuse the tested digest sender)
    try:
        from send_digest import send_email
        send_email(subject, body)
        results.append("email: sent")
    except Exception as e:
        results.append(f"email: error {e}")
    # telegram
    results.append(send_telegram(body))
    return results

# ── Selftest ───────────────────────────────────────────────────────────────────
def selftest():
    """S97. Pins gather_stalls by FAKING the subprocess it shells out to.

    This file had no selftest at all, which is how the crash-reads-as-green bug
    above survived: nothing here had ever been asserted in the failing
    direction. Every case pins the direction that matters -- a checker that did
    not report must never render as a clear-all.

    Case 1 is the actual S97 bug and is PROVEN to fail against the old code
    (`lines or ["- ✅ nothing stalled"]`): that version returns a green line for
    a crashed checker. A test never seen red is not known to detect anything
    (T42).
    """
    ok = fail = 0

    def ck(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  PASS {name}")
        else:
            fail += 1
            print(f"  FAIL {name}")

    # ── S141: the two judgements this brief makes ───────────────────────────
    # The mutation probe found 73 survivors in this file -- every branch below
    # could be inverted with the suite still green. These are the ones whose
    # breakage costs something: the verdict Buddy reads, and the one action the
    # brief tells him to take.
    def _verdict_checks(ck):
        h, v = health_verdict(True, [], [], True)
        ck("health: all four conditions good -> healthy", h is True and "✅" in v)
        # each condition ALONE must be able to sink it
        for label, args in (
                ("a missing/misdated digest",  (False, [], [], True)),
                ("an attention flag",          (True, ["x"], [], True)),
                ("an unshipped build",         (True, [], ["b"], True)),
                ("NO BACKUP COVERAGE",         (True, [], [], False))):
            h, v = health_verdict(*args)
            ck(f"health: {label} alone makes it NOT healthy",
               h is False and "⚠️" in v)

    def _next_checks(ck):
        # the ladder, top to bottom
        ck("next: a missing digest outranks everything",
           next_action(False, False, ["b"], ["p"], ["a"]).startswith("Investigate"))
        ck("next: backup outranks builds, approvals and flags",
           "Time Machine" in next_action(True, False, ["b"], ["p"], ["a"]))
        ck("next: builds outrank approvals",
           "Ship or discard" in next_action(True, True, ["b"], ["p"], ["a"]))
        ck("next: approvals outrank attention flags",
           "pending /accept" in next_action(True, True, [], ["p"], ["a"]))
        ck("next: attention flags are last before all-clear",
           "attention flag" in next_action(True, True, [], [], ["a"]))
        ck("next: nothing wrong -> nothing needed",
           next_action(True, True, [], [], []) == "Nothing needs you this morning.")
        # THE S73 REGRESSION, pinned. A locked backup volume must never render
        # as an approval chore just because an approval also happens to be
        # waiting. This is the exact bug that shipped once.
        ck("next: S73 — a locked backup beats a waiting approval, not the other "
           "way round",
           "Time Machine" in next_action(True, False, [], ["p"], []))
        ck("next: ...and the counts it quotes are the real ones",
           next_action(True, True, [], ["a", "b", "c"], []).startswith("Review the 3"))

    def _tm_checks(ck):
        FRESH = ("Vol", True, 0, 0, "Wed 09 Sep 03:00")
        line, ok = timemachine_verdict(*FRESH)
        ck("tm: mounted, recent, last attempt clean -> OK", ok is True and "✅" in line)
        # each fact ALONE must sink it -- they can each be wrong while the
        # others look fine, which is why three are reported and not one.
        line, ok = timemachine_verdict("Vol", False, 0, 0, "x")
        ck("tm: an UNMOUNTED destination alone fails (the S73 case: a "
           "FileVault-locked volume simply vanishes)",
           ok is False and "NOT MOUNTED" in line)
        line, ok = timemachine_verdict("Vol", True, 0, None, "never")
        ck("tm: no completed backup at all fails",
           ok is False and "no completed backup" in line)
        line, ok = timemachine_verdict("Vol", True, 0, 2, "x")
        ck("tm: a backup 2 days old fails", ok is False and "2 days ago" in line)
        line, ok = timemachine_verdict("Vol", True, 0, 1, "x")
        ck("tm: ...but 1 day old is still fine — the threshold is not off by one",
           ok is True)
        # THE S72 REGRESSION, pinned by name.
        line, ok = timemachine_verdict("Vol", True, 1, 0, "x")
        ck("tm: S72 — a FRESH backup with a FAILING last attempt is NOT ok; "
           "freshness alone once called this healthy",
           ok is False and "FAILED" in line)
        line, ok = timemachine_verdict("Vol", True, None, 0, "x")
        ck("tm: RESULT=None means 'no attempt recorded', not a failure", ok is True)
        line, ok = timemachine_verdict("Vol", False, 1, None, "never")
        ck("tm: several problems are ALL reported, not just the first",
           "NOT MOUNTED" in line and "FAILED" in line and "no completed" in line)

    def _attention_checks(ck):
        """gather_attention decides what gets FLAGGED, and its output feeds
        health_verdict -- so if it silently returns [] when something is wrong,
        the brief says healthy. That is the exact failure class this whole file
        exists to avoid, and none of it was tested."""
        ck("paywall: a well-formed 3-line entry parses",
           _parse_paywall_entries(
               f"[{TODAY} 07:00:00] PAYWALL | URL: https://www.ft.com/x\n"
               "          Sender: a@b\n"
               "          Subject: A Headline")
           == [{"date": TODAY, "url": "https://www.ft.com/x",
                "subject": "A Headline"}])
        ck("paywall: a '[ref] ' prefix is stripped from the subject",
           _parse_paywall_entries(
               f"[{TODAY} 07:00:00] PAYWALL | URL: https://x.com/a\n"
               "          Sender: a@b\n"
               "          Subject: [ref] Real Title")[0]["subject"] == "Real Title")
        ck("paywall: unrelated log noise yields NOTHING, not a phantom entry",
           _parse_paywall_entries("just some line\nand another") == [])
        ck("domain: www. is stripped so counts do not split in two",
           _domain("https://www.ft.com/a") == "ft.com"
           and _domain("https://ft.com/b") == "ft.com")

        g = globals()
        saved = g["_read"]
        try:
            # bot.log: only TODAY's errors, and never the benign long-poll
            g["_read"] = lambda p: (f"[{TODAY} 08:00] ERROR boom" if "bot" in str(p) else "")
            ck("attention: an error logged TODAY is flagged",
               any("bot.log" in f for f in gather_attention()))
            g["_read"] = lambda p: ("[2020-01-01 08:00] ERROR ancient" if "bot" in str(p) else "")
            ck("attention: a STALE error does not keep the verdict red forever",
               gather_attention() == [])
            g["_read"] = lambda p: (f"[{TODAY} 08:00] error getUpdates timed out" if "bot" in str(p) else "")
            ck("attention: a benign getUpdates timeout is NOT an error",
               gather_attention() == [])
            g["_read"] = lambda p: ""
            ck("attention: empty logs flag nothing", gather_attention() == [])

            # paywalls: today flags; the recurrence count is ALL-TIME
            pw = (f"[2020-01-01 07:00:00] PAYWALL | URL: https://www.ft.com/old\n"
                  "          Sender: a@b\n"
                  "          Subject: Old One\n"
                  f"[{TODAY} 07:00:00] PAYWALL | URL: https://www.ft.com/new\n"
                  "          Sender: a@b\n"
                  "          Subject: New One")
            g["_read"] = lambda p: (pw if "paywall" in str(p) else "")
            fl = gather_attention()
            ck("attention: a paywall hit TODAY is flagged with its source",
               len(fl) == 1 and "ft.com" in fl[0] and "New One" in fl[0])
            ck("attention: ...and the recurrence count is ALL-TIME, not today "
               "(2x), which is the number that decides a subscription",
               "blocked 2x all-time" in fl[0])
            g["_read"] = lambda p: (pw.split("\n")[0:3] and
                                    "\n".join(pw.split("\n")[0:3]) if "paywall" in str(p) else "")
            ck("attention: an OLD paywall hit alone flags nothing today",
               gather_attention() == [])
        finally:
            g["_read"] = saved

    _verdict_checks(ck)
    _next_checks(ck)
    _tm_checks(ck)
    _attention_checks(ck)

    class R:
        def __init__(self, stdout="", stderr="", rc=0):
            self.stdout, self.stderr, self.returncode = stdout, stderr, rc

    real_run = subprocess.run
    try:
        def fake(res):
            def f(cmd, **kw):
                return res
            return f

        # 1. THE BUG: checker crashed -- no stdout, non-zero exit.
        subprocess.run = fake(R("", "Traceback...\nAttributeError\n", 1))
        lines, okflag = gather_stalls()
        joined = " ".join(lines)
        ck("crash does not render as green", "nothing stalled" not in joined)
        ck("crash is flagged loudly", "⚠️" in joined and "did not run to completion" in joined)
        ck("crash reports ok=False", okflag is False)
        ck("crash names where the traceback is", "morningbrief-launchd.log" in joined)
        ck("crash does not leak subprocess stderr", "Traceback" not in joined
                                                    and "AttributeError" not in joined)

        # 2. Clean run: --brief prints its own green line, which must pass through.
        subprocess.run = fake(R("- ✅ nothing stalled (11 signals checked)\n", "", 0))
        lines, okflag = gather_stalls()
        ck("clean run passes its line through", lines == ["- ✅ nothing stalled (11 signals checked)"])
        ck("clean run reports ok=True", okflag is True)

        # 3. Real findings: exit 1 is stall_check's NORMAL "there are unknowns"
        #    code, not a crash. Findings must survive it.
        subprocess.run = fake(R("- ❌ STALLED devloop: no build in 9d\n"
                                "- ⚠️ UNCHECKED prompt cache: too few calls\n", "", 1))
        lines, okflag = gather_stalls()
        ck("findings survive exit 1", len(lines) == 2 and "STALLED devloop" in lines[0])
        ck("findings report ok=False", okflag is False)

        # 4. Silent success is still silence -- exit 0 with nothing printed is
        #    not a clear-all. (Missing output reading as clean is T8.)
        subprocess.run = fake(R("", "", 0))
        lines, okflag = gather_stalls()
        ck("silent exit-0 is not a clear-all", "nothing stalled" not in " ".join(lines))

        # 5. Non-finding chatter must not be mistaken for a finding.
        subprocess.run = fake(R("warming up\nchecking 11 signals\n", "", 0))
        lines, okflag = gather_stalls()
        ck("chatter without '-' lines is not a clear-all",
           "nothing stalled" not in " ".join(lines) and "⚠️" in " ".join(lines))

        # 6. The subprocess itself blowing up (timeout, missing interpreter)
        #    already had the right behaviour -- pin it so it stays.
        def boom(cmd, **kw):
            raise OSError("no such file")
        subprocess.run = boom
        lines, okflag = gather_stalls()
        ck("exception path stays loud", "could not run" in " ".join(lines) and okflag is False)
    finally:
        subprocess.run = real_run

    print(f"\n  {ok} passed, {fail} failed")
    return 1 if fail else 0


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    # T57: dispatch the subcommand BEFORE doing any work. A selftest that runs
    # after compose() is a selftest that sends mail on the way to being run.
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    dry = "--dry-run" in sys.argv or "--dry" in sys.argv
    subject, body = compose()
    if dry:
        print("=== DRY RUN — nothing sent ===")
        print("SUBJECT:", subject)
        print("-" * 70)
        print(body)
        return
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] sending morning brief…")
    for r in send_all(subject, body):
        print("  ", r)
    try:
        import job_status
        job_status.record("morningbrief", True)
    except Exception:
        pass
    print("done.")

if __name__ == "__main__":
    main()
