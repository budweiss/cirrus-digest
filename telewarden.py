#!/usr/bin/env python3
"""
telewarden.py  (S613, 2026-11-11) -- Telewarden step two, part 2a, on CIRRUS
===============================================================================
Reads every warning Buddy gets on Telegram and keeps one case per distinct
problem, so each one gets explained and then fixed. Plan and approval:
docs/sessions/S609-HANDOFF-telewarden-step-two-plan.md (Buddy approved 11-11).

It never sits between a job and Buddy: nothing he receives passes through it,
so a broken Telewarden cannot stop, delay or reword a warning. It reads copies.

Sources, each with its own cursor (a failed read never advances it):
  cirrus     logs/telegram-outbox.jsonl on this box (step one, S592)
  cumulus1   the same file on cumulus1, read-only over the LAN
  skywarden  notify rows in Skywarden's ledger on cumulus1 (first 80 chars)
  nimbus     rows the MacBook forwarder delivers (--ingest-nimbus)

Each message is sorted as own (Telewarden's), private, routine, info or
warning. A warning's lines become cases by fingerprint (job + text with numbers
stripped), so a repeat adds to a count instead of making a new case. Private
jobs are counted and never stored as text: a cloud session reads these cases.
Explanations are facts (who sent it, how often, what the job's ledger says)
plus the known-case notes from the review. Fixes are made by the scheduled
telewarden-fix session under rule 3a, which records them here with --set.

  python3 telewarden.py                 one pass (the LaunchDaemon, every 5 min)
  python3 telewarden.py --brief         the morning-brief section
  python3 telewarden.py --open [--json] cases waiting for a fix or a decision
  python3 telewarden.py --case ID       one case in full
  python3 telewarden.py --set ID STATUS "note" [--commit C] [--undo U] [--verify-after H]
  python3 telewarden.py --log-change "text"   an improvement for the weekly recap
  python3 telewarden.py --weekly        preview the weekly recap
  python3 telewarden.py --notify "text" one Telegram to Buddy (manual use)
  python3 telewarden.py --ingest-nimbus < rows.jsonl    (the MacBook forwarder)
  python3 telewarden.py --selftest

Messages to Buddy (Buddy, 11-11: "Notify me only if it can't be correct"):
only when a case moves to needs-buddy, plus one weekly recap after Sunday
18:00 of what was corrected or improved. A fix, a verification or a
regression sends nothing; the fixer takes regressed cases first.

Kill switch: create logs/telewarden/disabled. Passes, notices and --set stop;
the step-one copies keep running on their own.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
C1 = "buddy@192.168.0.204"                      # same LAN route as job_status.REMOTE_HOST
C1_OUTBOX = "cirrus-digest/logs/telegram-outbox.jsonl"
C1_LEDGER = "cirrus-digest/logs/jobs-status.json"
SKY = ["sudo", "-n", "-u", "cumulus-supervisor", "/opt/cumulus-supervisor/venv/bin/python3", "-"]
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
SOURCES = ("cirrus", "cumulus1", "skywarden", "nimbus")
HEARTBEATS = None        # logs/heartbeats.json (the MacBook's pings); tests override

STATUSES = ("new", "explained", "fixing", "fixed", "verified", "regressed",
            "needs-buddy", "accepted")
SETTABLE = ("new", "explained", "fixing", "fixed", "needs-buddy", "accepted")
OPEN = ("new", "explained", "regressed")         # waiting for the fixer
VERIFY_H = 48            # a fix with no recurrence for this long is verified
STUCK_H = 72             # target is 24h; three days open means the fixer is stuck
WEEKLY_DAY, WEEKLY_HOUR = 6, 18   # Sunday 18:00: Buddy's "recap at the end of the week"
SOURCE_ALARM_H = 1       # a source failing this long counts against the pass
SILENT_H = 36            # a box that sends daily has stopped copying
NOTIFY_PER_DAY = 6

PRIVATE_JOBS = {"stock_schedule", "personal_schedule", "stockscorecard",
                "stock_scorecard", "holdings_download_watch", "personal_input_sync",
                "strike", "strike_desk", "stock_pickers"}
PRIVATE_RE = re.compile(
    r"(?i)\b(holdings?|portfolio|brokerage|cost basis|shares|positions?|tickers?|"
    r"dividends?|strike|options? (desk|trade|chain)|seeking alpha|net worth|"
    r"account balance|ira|401k|buy|sell)\b")
ROUTINE_JOBS = {"morning_brief", "cumulus_daily_brief", "shore_schedule"}
ROUTINE_RE = re.compile(r"(?i)^(daily (check|review)|status pass|✅)")
WARN_RE = re.compile(
    r"(?i)(fail|error|exception|traceback|❌|⚠|warn|overdue|stale|\bdown\b|"
    r"unreachable|timed? ?out|not completed|missing|cut off|empty repl|refus|"
    r"needs attention|expir|exit code|unavailable|denied|stalled|degraded|"
    r"human needed|could not|cannot)")

# Known cases from the 11-11 review. Matched on (job, line) before anything else.
KNOWN = [
    {"id": "case-1", "job": r"^renewal_recovery$", "text": r"missed-run attempt has not completed",
     "status": "explained",
     "title": "Renewal recovery calls a finished review unavailable (Case 1)",
     "explanation": "False alarm. The 10:00 review finished with records waiting on evidence, "
                    "which ends ok=false; recovery reads that as 'not completed'. Fix at the "
                    "source: say 'completed, N waiting on evidence' for a finished run."},
    {"id": "case-f3", "job": r"^jobs_check$", "text": r"foundationrenewalauto.*fail",
     "status": "explained",
     "title": "jobs_check marks a finished auto-review FAILED (F3)",
     "explanation": "Same root as Case 1: a completed review with records waiting on evidence "
                    "is recorded as failed. Fix with Case 1."},
    # Only the false claim: after the S613 heartbeat fix a failed run reports
    # "(degraded) ... NOT REPAIRED", which is its own case, not this one coming back.
    {"id": "case-2", "job": r"^cirrus_bot$", "text": r"\(repaired\).*last exit code",
     "status": "explained",
     "title": "MacBook heartbeat counts a reload as 'repaired' (Case 2)",
     "explanation": "The heartbeat reloads an agent whose last run failed and reports it "
                    "repaired; the reload clears the exit code, so a job failing every night "
                    "never reaches HUMAN NEEDED. The autoupdate cause was fixed 11-10 (T198). "
                    "Remaining fix: count 'repaired' only after the job's next run succeeds."},
    {"id": "case-f4", "job": r"^skywarden$", "text": r"foundation-renewal\.service",
     "status": "explained",
     "title": "foundation-renewal exits 1 on a gate failure and reads as a crash (F4)",
     "explanation": "The run completes and sends its packet notice, but exits 1 when any gate "
                    "fails, so systemd, Skywarden and jobs_check report a failure. Give it a "
                    "separate 'review needed' result (check supervisor/alert_policy.py)."},
    {"id": "case-vendor", "job": r"^vendor_mail_watch$", "text": r".", "nosample": True,
     "status": "needs-buddy",
     "title": "Vendor-mail items re-alerted for months (by design, never closed)",
     "explanation": "due_for_realert re-sends open items and nobody closes them; several are "
                    "46-83 days old. Closing items for Buddy's own accounts is his call."},
    {"id": "case-aggie", "job": r"^client_contact_daily$", "text": r"tool unused",
     "status": "needs-buddy",
     "title": "Nightly 'aggie - tool unused' line",
     "explanation": "True but unchanged every night. Whether Aggie is still an active client "
                    "is Buddy's call."},
    {"id": "case-r1", "job": r"^foundation_renewal$", "text": r"renewal packet",
     "status": "explained",
     "title": "Foundation approvals need renewal",
     "explanation": "Real. Approvals run on a 7-day review; the daily job asks for "
                    "'review the renewal packet'. A session renews them (S316 procedure)."},
]


# ── storage ──────────────────────────────────────────────────────────────────
def base():
    return Path(os.environ.get("TELEWARDEN_DIR") or HERE / "logs" / "telewarden")


def _load(name, default):
    p = base() / name
    try:
        return json.loads(p.read_text())
    except FileNotFoundError:
        return default
    except ValueError:
        # never let a torn file mean "empty": keep it aside, start clean
        p.replace(p.with_suffix(p.suffix + f".corrupt-{int(time.time())}"))
        return default


def _save(name, data):
    p = base() / name
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(str(tmp), str(p))


def _append(name, rows):
    if not rows:
        return
    p = base() / name
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and p.stat().st_size > 10 * 1024 * 1024:
        p.replace(p.with_suffix(p.suffix + ".1"))
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


class _Lock:
    def __enter__(self):
        import fcntl
        base().mkdir(parents=True, exist_ok=True)
        self.fd = os.open(str(base() / "lock"), os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        os.close(self.fd)


def disabled():
    return (base() / "disabled").exists()


# ── reading sources ──────────────────────────────────────────────────────────
def _epoch(ts):
    """Outbox ts '2026-11-09T19:20:55-0500', Skywarden '2026-11-10 04:55:38'
    (local), or a number. Returns None when it cannot be read."""
    if isinstance(ts, (int, float)):
        return float(ts)
    ts = str(ts or "")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            d = datetime.strptime(ts, fmt)
            return d.timestamp()
        except ValueError:
            continue
    return None


def _rid(line):
    return hashlib.sha1(line.encode("utf-8", "replace")).hexdigest()[:16]


def parse_jsonl(text, source):
    """Outbox-shaped lines -> rows with t (epoch) and rid. Bad lines are skipped."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        t = _epoch(r.get("t", r.get("ts")))
        if t is None:
            continue
        out.append({"t": t, "rid": _rid(line), "source": source,
                    "job": str(r.get("job") or "?"), "ok": r.get("ok"),
                    "kind": r.get("kind"), "text": str(r.get("text") or "")})
    return out


def _tail_local(path):
    p = Path(path)
    chunks = []
    for q in (Path(str(p) + ".1"), p):          # a rotated copy still holds new rows
        try:
            chunks.append("\n".join(q.read_text(errors="replace").splitlines()[-3000:]))
        except FileNotFoundError:
            pass
    return "\n".join(chunks)


def read_cirrus():
    return parse_jsonl(_tail_local(HERE / "logs" / "telegram-outbox.jsonl"), "cirrus")


def read_cumulus1():
    cmd = f"{{ tail -n 1000 {C1_OUTBOX}.1 2>/dev/null; tail -n 3000 {C1_OUTBOX}; }}"
    r = subprocess.run(SSH + ["-n", C1, cmd], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"ssh rc {r.returncode}")
    return parse_jsonl(r.stdout, "cumulus1")


_SKY_SCRIPT = r'''
import json, sys, time
from datetime import datetime
since = float(sys.argv[1])
for line in open("/opt/cumulus-supervisor/state/ledger.jsonl"):
    try:
        r = json.loads(line)
    except ValueError:
        continue
    if r.get("event") != "notify":
        continue
    try:
        t = datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        continue
    if t >= since:
        print(json.dumps({"t": t, "job": "skywarden", "ok": r.get("result") == "sent",
                          "text": r.get("detail", "")}))
'''


def read_skywarden(since):
    r = subprocess.run(SSH + [C1] + SKY + [str(int(since))], input=_SKY_SCRIPT,
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"ssh rc {r.returncode}")
    return parse_jsonl(r.stdout, "skywarden")


def read_nimbus():
    return parse_jsonl(_tail_local(base() / "nimbus-outbox.jsonl"), "nimbus")


def new_rows(rows, cursor):
    """Rows after the cursor. cursor = {"t": epoch, "ids": [rids at t]}."""
    t0, seen = cursor.get("t", 0.0), set(cursor.get("ids", []))
    fresh = [r for r in rows if r["t"] > t0 or (r["t"] == t0 and r["rid"] not in seen)]
    fresh.sort(key=lambda r: r["t"])
    return fresh


def advance(cursor, fresh):
    if not fresh:
        return cursor
    t = fresh[-1]["t"]
    ids = [r["rid"] for r in fresh if r["t"] == t]
    if t == cursor.get("t"):
        ids = list(set(cursor.get("ids", [])) | set(ids))
    return {"t": t, "ids": ids}


# ── sorting ──────────────────────────────────────────────────────────────────
def norm(s):
    s = s.lower()
    s = re.sub(r"https?://\S+", "<url>", s)
    s = re.sub(r"[*_`]", "", s)
    s = re.sub(r"\b[0-9a-f]{7,}\b", "<id>", s)
    s = re.sub(r"\d+", "#", s)
    s = re.sub(r"[^\w#<>%.:/+ -]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()[:160]


# S623: "clean, 0 overdue" in a Skywarden all-clear made a case; a zero count is not a warning.
ZERO_RE = re.compile(r"(?i)\b0 (?:[\w-]+ )?(?:overdue|fail\w*|errors?|warn\w*|missing|stale|expired)(?:\(s\))?")


def _warns(s):
    return WARN_RE.search(ZERO_RE.sub("", s))


def warning_items(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) <= 2:
        whole = " ".join(lines)
        return [whole] if _warns(whole) else []
    items = [l for l in lines if _warns(l)]
    if len(items) > 1 and items[0] == lines[0]:   # a digest's title line is not a problem
        items = items[1:]
    return items[:20]


def is_private(row):
    return row["job"] in PRIVATE_JOBS or bool(PRIVATE_RE.search(row["text"]))


def classify(row):
    """-> (kind, [(case_id, item)]). Private items carry no text."""
    job, text = row["job"], row["text"]
    # S621: the CIRRUS worker's own notices are recorded as job telewarden_worker;
    # reading them back made a new case for each needs-buddy message.
    if job in ("telewarden", "telewarden_worker") or text.startswith("🛡 Telewarden:"):
        return "own", []
    if row.get("kind") == "off":                 # a paused Nimbus heartbeat, not a failure (F6)
        return "off", []
    if job in ROUTINE_JOBS or ROUTINE_RE.search(text.strip()):
        return "routine", []
    items = warning_items(text)
    if is_private(row):
        if not items:
            return "private", []
        cid = "tw-" + hashlib.sha1(f"private|{row['source']}|{job}".encode()).hexdigest()[:10]
        return "private-warning", [(cid, "")]
    if not items:
        return "info", []
    out = []
    for item in items:
        cid = None
        for k in KNOWN:
            if re.search(k["job"], job) and re.search(k["text"], item, re.I):
                cid = k["id"]
                break
        if cid is None:
            cid = "tw-" + hashlib.sha1(
                f"{row['source']}|{job}|{norm(item)}".encode()).hexdigest()[:10]
        if cid not in [c for c, _ in out]:
            out.append((cid, item))
    return "warning", out


# ── explanations (facts, no model) ───────────────────────────────────────────
def _ledger(source, cache):
    if source in cache:
        return cache[source]
    data = {}
    try:
        if source == "cirrus":
            data = json.loads((HERE / "logs" / "jobs-status.json").read_text())
        elif source == "cumulus1":
            r = subprocess.run(SSH + ["-n", C1, f"cat {C1_LEDGER}"],
                               capture_output=True, text=True, timeout=30)
            data = json.loads(r.stdout) if r.returncode == 0 else {}
    except Exception:
        data = {}
    cache[source] = data
    return data


def explain(case, cache):
    when = time.strftime("%m-%d %H:%M", time.localtime(case["first"]))
    bits = [f"Sent by {case['job']} via {case['source']}; first seen {when}."]
    led = _ledger(case["source"], cache)
    for name in (case["job"], case["job"].replace("_", "")):
        row = led.get(name) if isinstance(led, dict) else None
        if isinstance(row, dict):
            res = "ok" if row.get("ok") else "failed"
            bits.append(f"Its ledger ({name}): last run {res} at "
                        f"{str(row.get('last_run') or '?')[:16]}"
                        + (f" - {str(row.get('note'))[:120]}" if row.get("note") else "") + ".")
            break
    if case["private"]:
        bits.append("Private job: counted only. Diagnose on an owned host with a local model.")
    elif case["source"] == "cirrus" and (HERE / f"{case['job']}.py").exists():
        bits.append(f"Source: {case['job']}.py on CIRRUS.")
    return " ".join(bits)


# ── cases ────────────────────────────────────────────────────────────────────
def _day(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


# jobs_check's own line format: "⚠️ name (HOST): 2026-11-07T09:30 FAILED — ...".
STAMP_RE = re.compile(r"^\W*[\w.-]+(?: \([^)]*\))?: (20\d\d-\d\d-\d\dT\d\d:\d\d)\b")


def _item_time(item, job):
    """The run time a jobs_check line reports (host-local). None for anything
    else: a date elsewhere in a warning is not its failure time."""
    m = STAMP_RE.match(item or "") if job == "jobs_check" else None
    try:
        return time.mktime(time.strptime(m[1], "%Y-%m-%dT%H:%M")) if m else None
    except ValueError:
        return None


def touch_case(cases, cid, row, item, now, cache, notices):
    k = next((k for k in KNOWN if k["id"] == cid), None)
    c = cases.get(cid)
    if c is None:
        private = item == "" and cid.startswith("tw-") and k is None
        c = cases[cid] = {
            "id": cid, "source": row["source"], "job": row["job"], "private": private,
            "title": (k["title"] if k else
                      f"{row['job']}: warning (text kept on the host)" if private else
                      item[:120]),
            "sample": "" if private or (k and k.get("nosample")) else item[:500],
            "first": row["t"], "last": row["t"], "count": 0, "days": {},
            "status": k["status"] if k else "new", "history": [],
            "explanation": k["explanation"] if k else "", "fixed_at": None}
        if not k:
            c["explanation"] = explain(c, cache)
        c["history"].append({"t": now, "status": c["status"], "note": "first seen"})
    c["count"] += 1
    c["last"] = max(c["last"], row["t"])
    d = _day(row["t"])
    c["days"][d] = c["days"].get(d, 0) + 1
    c["days"] = dict(sorted(c["days"].items())[-14:])
    stamp = _item_time(item, row["job"])
    if (c["status"] in ("fixed", "verified") and c.get("fixed_at") and row["t"] > c["fixed_at"]
            and (stamp is None or stamp > c["fixed_at"])):
        # S621: jobs_check repeats a job's last failure until its next success, so an
        # item stamped before the fix is the old failure again, not a regression.
        # Buddy (11-11): no message for something we can still correct. The fixer
        # takes regressed cases first; only a needs-buddy case messages him.
        c["status"] = "regressed"
        c["history"].append({"t": now, "status": "regressed",
                             "note": f"came back after the fix ({c.get('commit') or 'no commit'})"})
    return c


def verify(cases, now):
    for c in cases.values():
        if (c["status"] == "fixed" and c.get("fixed_at")
                and now - c["fixed_at"] >= c.get("verify_after_h", VERIFY_H) * 3600):
            c["status"] = "verified"
            c["history"].append({"t": now, "status": "verified",
                                 "note": "no recurrence since the fix"})


# ── one pass ─────────────────────────────────────────────────────────────────
def run_pass(readers=None, now=None, send=None, record=None):
    """readers: {source: fn(cursor) -> rows}. Returns (ok, summary)."""
    now = time.time() if now is None else now
    if disabled():
        return True, "disabled"
    readers = readers or {"cirrus": lambda c: read_cirrus(),
                          "cumulus1": lambda c: read_cumulus1(),
                          "skywarden": lambda c: read_skywarden(c.get("t") or now - 7 * 86400),
                          "nimbus": lambda c: read_nimbus()}
    with _Lock():
        state = _load("state.json", {})
        cases = _load("cases.json", {})
        cursors = state.setdefault("cursors", {})
        health = state.setdefault("sources", {})
        cache, notices, events, counts = {}, [], [], {}
        for src in SOURCES:
            if src not in readers:
                continue
            cur = cursors.get(src) or ({"t": now - 7 * 86400} if src == "skywarden" else {})
            h = health.setdefault(src, {})
            try:
                fresh = new_rows(readers[src](cur), cur)
            except Exception as e:  # noqa: BLE001 -- recorded, cursor kept
                h["err"] = type(e).__name__ + (f": {e}"[:80] if str(e) else "")
                if not h.get("err_since"):       # a cleared source holds None, not no key
                    h["err_since"] = now
                continue
            h.update({"ok_at": now, "err": None, "err_since": None})
            for row in fresh:
                kind, items = classify(row)
                counts[kind] = counts.get(kind, 0) + 1
                for cid, item in items:
                    touch_case(cases, cid, row, item, now, cache, notices)
                ev = {"t": row["t"], "src": src, "job": row["job"], "kind": kind,
                      "cases": [cid for cid, _ in items]}
                if kind == "off":                # the forwarder sends one row per batch
                    m = re.search(r"paused (\d+)x", row["text"])
                    ev["n"] = int(m.group(1)) if m else 1
                events.append(ev)
                if row["source"] in ("cirrus", "cumulus1") and row["job"] != "telewarden":
                    h["last_row"] = max(h.get("last_row") or 0, row["t"])
            cursors[src] = advance(cur, fresh)
        verify(cases, now)
        _append("events.jsonl", events)
        _save("cases.json", cases)
        state["last_pass"] = now
        _save("state.json", state)
    for n in notices:
        notify(n, send=send, now=now)
    maybe_weekly(now, send=send)
    bad = [s for s, h in health.items()
           if h.get("err_since") and now - h["err_since"] > SOURCE_ALARM_H * 3600]
    summary = (f"{sum(counts.values())} new row(s) "
               + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
               + (f"; source down >{SOURCE_ALARM_H}h: {', '.join(bad)}" if bad else ""))
    if record:
        record(not bad, summary[:200])
    return not bad, summary


# ── outputs ──────────────────────────────────────────────────────────────────
def _created(c):
    """When Telewarden opened the case (rows may be older than Telewarden)."""
    return c["history"][0]["t"] if c.get("history") else c["first"]


def log_change(text, now=None):
    """An improvement not tied to one case, for the weekly recap."""
    now = time.time() if now is None else now
    with _Lock():
        _append("changes.jsonl", [{"t": now, "text": str(text)[:300]}])


def _last_boundary(now):
    d = datetime.fromtimestamp(now)
    b = (d.replace(hour=WEEKLY_HOUR, minute=0, second=0, microsecond=0)
         - timedelta(days=(d.weekday() - WEEKLY_DAY) % 7))
    if b > d:
        b -= timedelta(days=7)
    return b.timestamp()


def weekly_text(since, now):
    cases = _load("cases.json", {})
    fixed, accepted = [], []
    for c in cases.values():
        for h in c.get("history", []):
            if since <= h["t"] <= now and h["status"] == "fixed":
                fixed.append((c, h))
            elif since <= h["t"] <= now and h["status"] == "accepted":
                accepted.append((c, h))
    changes = []
    try:
        for line in (base() / "changes.jsonl").read_text().splitlines():
            e = json.loads(line)
            if since <= e.get("t", 0) <= now:
                changes.append(e)
    except (FileNotFoundError, ValueError):
        pass
    needs = [c for c in cases.values() if c["status"] == "needs-buddy"]
    busy = [c for c in cases.values() if c["status"] in OPEN + ("fixing",)]
    span = (f"{time.strftime('%b %d', time.localtime(since))} - "
            f"{time.strftime('%b %d', time.localtime(now))}")
    out = [f"Weekly recap, {span}"]
    state_word = {"verified": "confirmed", "fixed": "waiting for its next run",
                  "regressed": "came back, fixer on it"}
    if fixed:
        out.append(f"\nCorrected ({len(fixed)}):")
        for c, h in fixed:
            out.append(f"- {c['title'][:80]}: {h.get('note', '')[:160]}"
                       f" ({state_word.get(c['status'], c['status'])})")
    if changes:
        out.append(f"\nImproved ({len(changes)}):")
        out += [f"- {e['text'][:200]}" for e in changes]
    if accepted:
        out.append(f"\nClosed as by design ({len(accepted)}):")
        out += [f"- {c['title'][:80]}: {h.get('note', '')[:120]}" for c, h in accepted]
    if needs:
        out.append(f"\nStill needs you ({len(needs)}):")
        out += [f"- {c['title'][:80]}" for c in needs]
    if not (fixed or changes or accepted):
        out.append("\nQuiet week: nothing needed correcting.")
    out.append(f"\n{len(busy)} case(s) still being worked on.")
    return "\n".join(out)


def maybe_weekly(now, send=None):
    """Send the recap once per week, after Sunday 18:00. A failed send retries
    on the next pass; the first ever pass only sets the starting point."""
    with _Lock():
        state = _load("state.json", {})
        last = state.get("weekly_sent")
        if last is None:
            state["weekly_sent"] = now
            _save("state.json", state)
            return False
        if last >= _last_boundary(now):
            return False
    text = weekly_text(last, now)
    if not notify(text, send=send, now=now, limit=3800):
        return False
    with _Lock():
        state = _load("state.json", {})
        state["weekly_sent"] = now
        _save("state.json", state)
    return True


def notify(text, send=None, now=None, limit=1500):
    """One Telegram to Buddy, capped per day. Returns True if sent."""
    now = time.time() if now is None else now
    if disabled():
        return False
    with _Lock():
        state = _load("state.json", {})
        sent = state.setdefault("notified", {})
        day = _day(now)
        if sent.get(day, 0) >= NOTIFY_PER_DAY:
            return False
        sent[day] = sent.get(day, 0) + 1
        state["notified"] = dict(sorted(sent.items())[-7:])
        _save("state.json", state)
    if send is None:
        import vendor_mail_watch
        send = vendor_mail_watch.telegram
    if len(text) > limit:                # T40: say so when a message is cut short
        text = text[:limit - 30] + "\n… (truncated; full text on CIRRUS)"
    return bool(send("🛡 Telewarden: " + text))


def brief_lines(now=None):
    """(lines, attention) for morning_brief.py. Never raises on missing state."""
    now = time.time() if now is None else now
    state = _load("state.json", {})
    cases = _load("cases.json", {})
    lines, att = [], []
    if disabled():
        lines.append("• ⏸ Telewarden is switched off (logs/telewarden/disabled)")
        return lines, att
    last = state.get("last_pass")
    if not last or now - last > 3600:
        msg = ("never ran" if not last else
               f"last pass {int((now - last) // 3600)}h ago")
        lines.append(f"• ⚠️ Telewarden {msg}")
        att.append(f"Telewarden {msg}")
    for src, h in sorted(state.get("sources", {}).items()):
        if h.get("err_since") and now - h["err_since"] > SOURCE_ALARM_H * 3600:
            lines.append(f"• ⚠️ cannot read {src} since "
                         f"{time.strftime('%m-%d %H:%M', time.localtime(h['err_since']))}: {h.get('err')}")
            att.append(f"Telewarden cannot read {src}")
        if src in ("cirrus", "cumulus1") and h.get("last_row") and now - h["last_row"] > SILENT_H * 3600:
            lines.append(f"• ⚠️ no Telegram copies from {src} in {SILENT_H}h (copy stopped?)")
            att.append(f"no Telegram copies from {src} in {SILENT_H}h")
    # Buddy (11-11): "Notify me only if it can't be correct." Corrections are not
    # news; they go in Sunday's weekly recap. The brief shows what needs him, and
    # a case open far past the fixer's 24-hour target (the fixer is stuck, which
    # it cannot report itself).
    def names(sel, n=4):
        return "; ".join(f"{c['title'][:70]} [{c['id']}]" for c in sel[:n]) + (
            f" (+{len(sel) - n} more)" if len(sel) > n else "")

    vals = list(cases.values())
    needs = sorted((c for c in vals if c["status"] == "needs-buddy"), key=lambda c: -c["last"])
    if needs:
        lines.append(f"• Needs you ({len(needs)}): {names(needs)}")
        att.append(f"Telewarden: {len(needs)} decision(s) need you")
    busy = [c for c in vals if c["status"] in OPEN + ("fixing",)]
    stuck = sorted((c for c in busy if now - _created(c) > STUCK_H * 3600), key=_created)
    if stuck:
        lines.append(f"• ⚠️ {len(stuck)} case(s) open over {STUCK_H // 24} days, so the fixer "
                     f"may be stuck: {names(stuck)}")
        att.append(f"Telewarden: {len(stuck)} case(s) open over {STUCK_H // 24} days")
    lines.append(f"• {len(busy)} case(s) being handled automatically; corrections go in "
                 f"Sunday's weekly recap" if busy else
                 "• Nothing open; corrections go in Sunday's weekly recap")
    # A sleeping Mac is silent by design; a Mac whose heartbeat is pinging CIRRUS
    # while the forwarder is not means the forwarder itself has stopped.
    nim = state.get("nimbus_alive")
    try:
        mac = json.loads(Path(HEARTBEATS or HERE / "logs" / "heartbeats.json").read_text())["macbook"]
        mac_t = _epoch(mac.get("ts"))
        if (mac.get("status") == "ok" and mac_t and now - mac_t < 3600
                and (not nim or now - nim > 2 * 3600)):
            lines.append("• ⚠️ Nimbus forwarder silent while the MacBook is up")
            att.append("Nimbus forwarder silent while the MacBook is up")
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return lines, att


def open_cases():
    cases = _load("cases.json", {})
    return sorted((c for c in cases.values() if c["status"] in OPEN + ("fixing",)),
                  key=lambda c: (c["status"] != "regressed", -c["last"]))


def set_status(cid, status, note, commit=None, undo=None, verify_after=None, now=None,
               send=None, owner=None):
    """Moving a case to needs-buddy is the one thing that messages Buddy
    (Buddy, 11-11): it means the fixer cannot correct it."""
    now = time.time() if now is None else now
    if disabled():
        raise SystemExit("telewarden is switched off")
    if status not in SETTABLE:
        raise SystemExit(f"status must be one of {', '.join(SETTABLE)}")
    if status == "fixed" and not (commit and undo):
        raise SystemExit("a fix needs --commit (or 'none') and --undo")
    with _Lock():
        cases = _load("cases.json", {})
        c = cases.get(cid)
        if c is None:
            raise SystemExit(f"no case {cid}")
        lease = c.get('worker_lease', {})
        if lease.get('owner') and lease.get('owner') != owner and lease.get('until', 0) > now:
            raise SystemExit('case is owned by the CIRRUS repair worker')
        if status == 'fixing' and owner is None and (base() / 'server-worker-enabled').exists():
            raise SystemExit('repair ownership moved to CIRRUS; desktop claim refused')
        was = c["status"]
        c["status"] = status
        if owner and lease.get('owner') == owner:
            c.pop('worker_lease', None)
        if status == "fixed":
            c.update({"fixed_at": now, "commit": commit, "undo": undo})
            if verify_after:
                c["verify_after_h"] = float(verify_after)
        c["history"].append({"t": now, "status": status, "note": note[:500],
                             **({"commit": commit} if commit else {})})
        _save("cases.json", cases)
    if status == "needs-buddy" and was != "needs-buddy":
        notify(f"Needs you, could not be corrected: {c['title'][:120]} [{cid}]. {note[:600]}",
               send=send, now=now)
    return c


def ingest_nimbus(text, now=None):
    """Rows from the MacBook forwarder, deduplicated by their id."""
    now = time.time() if now is None else now
    rows, alive = [], None
    with _Lock():
        state = _load("state.json", {})
        seen = state.get("nimbus_ids", [])
        known = set(seen)
        for line in text.splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            rid = str(r.get("id") or "")
            if not rid or rid in known:
                continue
            known.add(rid)
            seen.append(rid)
            if r.get("kind") == "alive":
                alive = max(alive or 0, float(r.get("t") or now))
                continue
            rows.append({"t": float(r.get("t") or now), "host": "macbook",
                         "job": str(r.get("label") or r.get("kind") or "nimbus")[:60],
                         "kind": r.get("kind"), "ok": False,
                         "text": str(r.get("text") or "")[:600]})
        _append("nimbus-outbox.jsonl", rows)
        state["nimbus_ids"] = seen[-5000:]
        if alive:
            state["nimbus_alive"] = max(alive, state.get("nimbus_alive") or 0)
        _save("state.json", state)
    return len(rows)


# ── selftest ─────────────────────────────────────────────────────────────────
def selftest():
    import tempfile
    ok = True

    def check(name, cond):
        nonlocal ok
        ok &= bool(cond)
        print(("PASS " if cond else "FAIL ") + name)

    old = os.environ.get("TELEWARDEN_DIR")
    with tempfile.TemporaryDirectory() as td:
        os.environ["TELEWARDEN_DIR"] = td
        try:
            T = 1_800_000_000.0
            def row(job, text, t, src="cirrus"):
                line = json.dumps({"ts": t, "job": job, "text": text})
                return {"t": t, "rid": _rid(line + src), "source": src, "job": job,
                        "ok": True, "kind": None, "text": text}
            feed = {"cirrus": [], "cumulus1": [], "skywarden": [], "nimbus": []}
            readers = {s: (lambda c, s=s: list(feed[s])) for s in feed}
            sent = []
            send = lambda m: sent.append(m) or True
            recs = []
            record = lambda ok_, note: recs.append(ok_)

            feed["cirrus"] = [
                row("renewal_recovery", "Renewal recovery: x is unavailable or its missed-run attempt has not completed.", T),
                row("jobs_check", "Jobs\n⚠️ stockscorecard: FAILED 11-07 ValueError\n✅ billsnow ok\n⚠️ stratusreview failed, handled", T + 1),
                row("morning_brief", "⚠️ Needs a look: everything failed", T + 2),
                row("stock_schedule", "Sell 10 shares of X", T + 3),
                row("stock_schedule", "stock job FAILED to fetch holdings", T + 4),
                row("telewarden", "🛡 Telewarden: fixed", T + 5),
                row("telewarden_worker", "🛡 Telewarden: Needs you, could not be corrected: ⚠️ x FAILED", T + 5.5),
                row("environment_scan", "New model id seen", T + 6)]
            okp, summ = run_pass(readers, now=T + 10, send=send, record=record)
            cases = _load("cases.json", {})
            check("pass ok and recorded", okp and recs == [True])
            check("known Case 1 matched", "case-1" in cases and cases["case-1"]["status"] == "explained")
            jc = [c for c in cases.values() if c["job"] == "jobs_check"]
            check("digest split into line cases (2 warning lines, ok line ignored)", len(jc) == 2)
            check("brief, own and info rows make no case",
                  not [c for c in cases.values() if c["job"] in ("morning_brief", "telewarden", "telewarden_worker", "environment_scan")])
            pv = [c for c in cases.values() if c["job"] == "stock_schedule"]
            check("private warning: one case, no text stored",
                  len(pv) == 1 and pv[0]["sample"] == "" and pv[0]["private"]
                  and "holdings" not in json.dumps(pv[0]).lower())
            check("private routine row makes no case", pv[0]["count"] == 1)

            okp, _ = run_pass(readers, now=T + 20, send=send, record=record)
            check("same rows again: no double count", _load("cases.json", {})["case-1"]["count"] == 1)

            feed["cirrus"].append(row("jobs_check", "Jobs\n⚠️ stockscorecard: FAILED 11-08 ValueError\n✅ a\n✅ b", T + 30))
            run_pass(readers, now=T + 40, send=send, record=record)
            sc = [c for c in _load("cases.json", {}).values() if "stockscorecard" in c["title"]]
            check("repeat with a new date joins the same case", len(sc) == 1 and sc[0]["count"] == 2)

            feed["cumulus1"] = [row("foundation_renewal",
                                    "Approvals expire Thu 04:47: review the renewal packet", T + 50, "cumulus1")]
            def boom(c):
                raise RuntimeError("down")
            r2 = dict(readers, skywarden=boom)
            before = _load("state.json", {})["cursors"].get("skywarden")
            run_pass(r2, now=T + 60, send=send, record=record)
            st = _load("state.json", {})
            check("failed source keeps its cursor and is recorded",
                  st["sources"]["skywarden"]["err"] and st["cursors"].get("skywarden") == before)
            check("the other sources still advance",
                  "case-r1" in _load("cases.json", {}))
            okp, _ = run_pass(r2, now=T + 60 + 2 * 3600, send=send, record=record)
            check("source down >1h fails the pass", okp is False and recs[-1] is False)
            check("brief flags the down source",
                  any("skywarden" in x for x in brief_lines(now=T + 60 + 2 * 3600)[1]))
            readers["nimbus"] = lambda c: read_nimbus()

            cid = sc[0]["id"]
            try:
                set_status(cid, "fixed", "patched", now=T + 100)
                check("fixed without commit refused", False)
            except SystemExit:
                check("fixed without commit refused", True)
            set_status(cid, "fixed", "patched", commit="abc1234", undo="git revert abc1234", now=T + 100)
            run_pass(readers, now=T + 100 + 49 * 3600, send=send, record=record)
            check("fix with no recurrence becomes verified",
                  _load("cases.json", {})[cid]["status"] == "verified")
            feed["cirrus"].append(row("jobs_check", "Jobs\n⚠️ stockscorecard: FAILED 11-12 ValueError\n✅ a\n✅ b", T + 100 + 50 * 3600))
            run_pass(readers, now=T + 100 + 51 * 3600, send=send, record=record)
            check("recurrence after a fix: regressed, and no message (the fixer takes it)",
                  _load("cases.json", {})[cid]["status"] == "regressed" and sent == [])

            # S621: jobs_check repeats a weekly job's last failure until it next succeeds
            # (11-13: stockscorecard's 11-07 failure read as a regression of its fix).
            stale = time.strftime("%Y-%m-%dT%H:%M", time.localtime(T - 86400))
            fresh = time.strftime("%Y-%m-%dT%H:%M", time.localtime(T + 100 + 60 * 3600))
            wk_row = lambda stamp, t: row("jobs_check", f"Jobs\n⚠️ weeklyjob: {stamp} FAILED — ValueError\n✅ a\n✅ b", t)
            feed["cirrus"].append(wk_row(stale, T + 100 + 52 * 3600))
            run_pass(readers, now=T + 100 + 52 * 3600 + 1, send=send, record=record)
            wk = [c["id"] for c in _load("cases.json", {}).values() if "weeklyjob" in c["title"]]
            set_status(wk[0], "fixed", "patched", commit="abc1234", undo="git revert abc1234", now=T + 100 + 53 * 3600)
            feed["cirrus"].append(wk_row(stale, T + 100 + 54 * 3600))
            run_pass(readers, now=T + 100 + 54 * 3600 + 1, send=send, record=record)
            check("the old failure re-reported after a fix is not a regression",
                  len(wk) == 1 and _load("cases.json", {})[wk[0]]["status"] == "fixed")
            feed["cirrus"].append(wk_row(fresh, T + 100 + 61 * 3600))
            run_pass(readers, now=T + 100 + 61 * 3600 + 1, send=send, record=record)
            check("a failure stamped after the fix is a regression",
                  _load("cases.json", {})[wk[0]]["status"] == "regressed" and sent == [])
            check("only jobs_check's own run stamp counts, not a date elsewhere in the text",
                  _item_time(f"⚠️ x (CUMULUS): {stale} REVIEW NEEDED", "jobs_check") is not None
                  and _item_time(f"⚠️ x: failed reading {stale} data", "jobs_check") is None
                  and _item_time(f"⚠️ x: {stale} FAILED", "intake") is None)
            check("a zero count is not a warning, a real one beside it still is",
                  warning_items("Checked: open promises (clean, 0 overdue), intake health (UP)") == []
                  and warning_items("3 fixtures, 0 gate failure(s)") == []
                  and len(warning_items("0 overdue, 2 failed")) == 1
                  and len(warning_items("10 overdue")) == 1)

            n = ingest_nimbus("\n".join(json.dumps(r) for r in [
                {"id": "a1", "t": T + 200, "kind": "task_run", "label": "skill-review",
                 "text": "skill-review: failed - Agent run failed"},
                {"id": "a2", "t": T + 201, "kind": "off", "label": "heartbeat-main", "text": "paused 3x"},
                {"id": "a3", "t": T + 202, "kind": "alive"}]), now=T + 203)
            n2 = ingest_nimbus(json.dumps({"id": "a1", "t": T + 200, "kind": "task_run"}), now=T + 204)
            check("nimbus ingest dedupes by id", n == 2 and n2 == 0)
            run_pass(readers, now=T + 100 + 52 * 3600, send=send, record=record)
            cs = _load("cases.json", {})
            check("nimbus failure becomes a case; paused heartbeat does not",
                  any(c["source"] == "nimbus" and c["job"] == "skill-review" for c in cs.values())
                  and not any(c["job"] == "heartbeat-main" for c in cs.values()))

            lines, att = brief_lines(now=T + 100 + 52 * 3600 + 60)
            text = "\n".join(lines)
            check("brief lists no corrections, only what is being handled",
                  "being handled automatically" in text and "Came back" not in text
                  and "stockscorecard" not in text
                  and not any("Telewarden:" in a for a in att))
            check("brief never quotes private text",
                  "Sell 10 shares" not in text and "holdings" not in text.lower())
            lines, att = brief_lines(now=T + 80 * 3600)
            check("a case open over 3 days says the fixer may be stuck",
                  any("may be stuck" in l for l in lines) and any("3 days" in a for a in att))
            offs = [json.loads(l) for l in (Path(td) / "events.jsonl").read_text().splitlines()
                    if '"off"' in l]
            check("paused heartbeats counted from the forwarder's batch",
                  len(offs) == 1 and offs[0].get("n") == 3)

            nb = next(c["id"] for c in _load("cases.json", {}).values()
                      if c["job"] == "skill-review")
            set_status(nb, "needs-buddy", "Is the C2 model meant to be off?", send=send,
                       now=T + 100 + 52 * 3600 + 90)
            set_status(nb, "needs-buddy", "asked again", send=send, now=T + 100 + 52 * 3600 + 95)
            check("needs-buddy (cannot be corrected) messages Buddy once",
                  len(sent) == 1 and "could not be corrected" in sent[0]
                  and "Is the C2 model" in sent[0])
            lines, att = brief_lines(now=T + 100 + 52 * 3600 + 120)
            check("brief shows what needs him",
                  any("Needs you (1)" in l for l in lines) and att)

            log_change("Telewarden built: one case per warning", now=T + 50)
            wk = T + 3 * 86400                     # past the first Sunday 18:00
            check("weekly recap sends once after Sunday 18:00",
                  maybe_weekly(wk, send=send) is True and maybe_weekly(wk + 60, send=send) is False)
            rec = sent[-1]
            check("weekly recap lists corrections, improvements and open decisions",
                  "Corrected (2)" in rec and "stockscorecard" in rec and "came back" in rec
                  and "weeklyjob" in rec
                  and "Improved (1)" in rec and "Telewarden built" in rec
                  and "Still needs you (1)" in rec)
            check("weekly recap never quotes private text",
                  "Sell 10 shares" not in rec and "holdings" not in rec.lower())
            wk2 = T + 17 * 86400
            check("a failed weekly send is retried, not lost",
                  maybe_weekly(wk2, send=lambda m: False) is False
                  and maybe_weekly(wk2 + 300, send=send) is True and "Quiet week" in sent[-1])
            global HEARTBEATS
            hb = Path(td) / "heartbeats.json"
            HEARTBEATS = str(hb)
            tnow = T + 100 + 52 * 3600 + 60
            hb.write_text(json.dumps({"macbook": {"status": "ok", "ts": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(tnow - 600))}}))
            st = _load("state.json", {})
            st["nimbus_alive"] = tnow - 3 * 3600
            _save("state.json", st)
            check("forwarder silent while the Mac is up is flagged",
                  any("forwarder silent" in a for a in brief_lines(now=tnow)[1]))
            hb.write_text(json.dumps({"macbook": {"status": "sleeping", "ts": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(tnow - 600))}}))
            check("a sleeping Mac's silent forwarder is not flagged",
                  not any("forwarder silent" in a for a in brief_lines(now=tnow)[1]))
            HEARTBEATS = None

            before = len(sent)
            for i in range(NOTIFY_PER_DAY + 2):
                notify(f"n{i}", send=send, now=T + 10 * 86400)
            check("notices capped per day", len(sent) - before == NOTIFY_PER_DAY)

            (Path(td) / "disabled").write_text("")
            check("kill switch stops the pass", run_pass(readers, now=T + 999999)[1] == "disabled")
            check("kill switch stops notices", notify("x", send=send) is False)
            check("kill switch shows in the brief", "switched off" in brief_lines()[0][0])

            cur = advance({}, [{"t": 5.0, "rid": "a"}, {"t": 5.0, "rid": "b"}])
            check("cursor keeps every id at the last second",
                  set(cur["ids"]) == {"a", "b"} and not new_rows([{"t": 5.0, "rid": "a"}], cur))
            check("epoch parses outbox and Skywarden times",
                  _epoch("2026-11-09T19:20:55-0500") and _epoch("2026-11-10 04:55:38"))
        finally:
            if old is None:
                os.environ.pop("TELEWARDEN_DIR", None)
            else:
                os.environ["TELEWARDEN_DIR"] = old
    print("selftest", "OK" if ok else "FAILED")
    return 0 if ok else 1


def _arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def main():
    a = sys.argv[1:]
    if "--selftest" in a:
        return selftest()
    if "--brief" in a:
        print("\n".join(brief_lines()[0]))
        return 0
    if "--open" in a:
        rows = open_cases()
        if "--json" in a:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        else:
            for c in rows:
                print(f"{c['id']}  {c['status']:<10} x{c['count']:<3} {c['source']}/{c['job']}: {c['title'][:90]}")
        return 0
    if "--case" in a:
        print(json.dumps(_load("cases.json", {}).get(_arg("--case")), ensure_ascii=False, indent=1))
        return 0
    if "--set" in a:
        i = a.index("--set")
        c = set_status(a[i + 1], a[i + 2], a[i + 3], commit=_arg("--commit"),
                       undo=_arg("--undo"), verify_after=_arg("--verify-after"))
        print(f"{c['id']} -> {c['status']}")
        return 0
    if "--notify" in a:
        return 0 if notify(_arg("--notify")) else 1
    if "--log-change" in a:
        log_change(_arg("--log-change"))
        print("logged for the weekly recap")
        return 0
    if "--weekly" in a:                  # preview only; the pass sends it Sunday 18:00
        st = _load("state.json", {})
        now = time.time()
        print(weekly_text(st.get("weekly_sent") or now - 7 * 86400, now))
        return 0
    if "--ingest-nimbus" in a:
        print(f"ingested {ingest_nimbus(sys.stdin.read())}")
        return 0
    def record(ok, note):
        try:
            import job_status
            job_status.record("telewarden", ok=ok, note=note)
        except Exception:  # noqa: BLE001 -- the ledger is best-effort here
            pass
    ok, summary = run_pass(record=record)
    print(time.strftime("[%Y-%m-%d %H:%M:%S] ") + summary)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
