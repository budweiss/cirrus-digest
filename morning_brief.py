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


def compose():
    dig = gather_digest()
    act = gather_actions()
    pend = gather_pending()
    awaiting = gather_awaiting_builds()
    att = gather_attention()
    tm_line, tm_ok = gather_timemachine()

    # A box with no backup coverage is NOT healthy, however green everything
    # else looks. This is the fact that was missing entirely until S73.
    healthy = dig["dated_today"] and not att and not awaiting and tm_ok
    verdict = "✅ CIRRUS healthy" if healthy else "⚠️ Needs a look"

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

    # one suggested next action
    # S73 ordering: losing backup coverage outranks an approval tap. The first
    # version put `not tm_ok` after `pend`, so a locked backup volume rendered
    # as "Review the 1 pending /accept item(s)" — the single most important
    # fact on the page, demoted below a one-tap chore. Caught by running the
    # FAILING case, not the passing one.
    if not dig["dated_today"]:
        nxt = "Investigate the 7am digest — today's file is missing or misdated."
    elif not tm_ok:
        nxt = "Time Machine is not protecting this box — see Backup above."
    elif awaiting:
        nxt = f"Ship or discard {len(awaiting)} built Dev-Loop item(s) — reply /builds."
    elif pend:
        nxt = f"Review the {len(pend)} pending /accept item(s)."
    elif att:
        nxt = "Check the attention flag(s) above."
    else:
        nxt = "Nothing needs you this morning."
    lines.append(f"**Next:** {nxt}")
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

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
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
