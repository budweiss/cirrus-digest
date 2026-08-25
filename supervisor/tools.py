"""Tool registry for the CUMULUS supervisor agent (B1) — v1 skeleton.

Every function here is a tool the claude-agent-sdk reasoning pass can call.
Design rules (CUMULUS.md sec 8a, sec 4):
  - No generic get_secret()-style tool is exposed to the agent's own
    reasoning context. Secrets are read here, in plain Python, and used
    server-side (an HTTP call, a subprocess arg) — never returned as a
    tool result the LLM would see.
  - Reversible actions (restart_service/reset_failed) are allow-listed
    TWICE: once in /etc/sudoers.d/cumulus-supervisor (the real gate) and
    again here in Python (defense in depth — a bug in this file alone
    can't reach an unlisted unit, since sudo would still refuse it).
  - Every call — success or failure — is ledgered.
"""
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from ledger import TIER_AUTO, TIER_NAME, ledger_append

SECRETS_PATH = Path("/opt/cumulus-supervisor/state/secrets.json")
# The app checkout Skywarden READS from (never writes to). Only used by
# check_open_client_promises, which imports the promise ledger module.
APP_DIR = Path("/home/buddy/cirrus-digest")

# Must match /etc/sudoers.d/cumulus-supervisor's Cmnd_Alias lists exactly.
ALLOWED_UNITS = {
    "cirrus-api.service",
    "cirrus-bot.service",
    "cirrus-billnewdev.service",
    "cirrus-billsnow.service",
    "cirrus-hoaleads.service",
    "cirrus-modelhealth.service",
    "cirrus-pedagogy.service",
    "cumulus-creds-materialize.service",
    "cumulus-intake.service",
}


def _normalize_unit(unit: str) -> str:
    unit = (unit or "").strip()
    if unit and not unit.endswith(".service"):
        unit += ".service"
    return unit


def _load_secrets() -> dict:
    with open(SECRETS_PATH) as f:
        return json.load(f)


def _run(cmd: list, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# ── Read-only checks ─────────────────────────────────────────────────────────

def check_service_status(unit: str) -> str:
    """Report a systemd unit's current load/active/sub state. Read-only, no sudo."""
    unit = _normalize_unit(unit)
    r = _run(["systemctl", "status", unit, "--no-pager", "-l"])
    out = (r.stdout or r.stderr).strip()
    ledger_append({"event": "check", "tool": "check_service_status",
                   "tier_name": "read-only", "detail": unit,
                   "result": out[:200]})
    return out


def check_open_client_promises() -> str:
    """What have we told a client we would do, and not done? Read-only.

    S78. Skywarden watches services, and every service was healthy on the day
    CUMULUS offered Bill a 224-row workbook, took his "yes", and never built it.
    The machine was fine; the CONVERSATION broke, and nothing was looking at
    conversations. This is that check.

    It reports, it does not fix. Delivering a client deliverable -- and any
    client send at all -- is firmly in the NEVER tier of the operating contract
    and stays there. The right action on a hit is send_telegram, or
    request_guidance if it is unclear what is blocking.
    """
    # S78 — two things were wrong here and both are fixed. It used to `import
    # client_promises` off APP_DIR, which could never work (this account cannot
    # traverse /home/buddy), so it shipped reporting UNREADABLE on every run.
    # And it only ever looked at CUMULUS, while CIRRUS answers a different
    # mailbox of its own. Both boxes now, through their scoped read paths.
    late, notes = _both_boxes(48, "promises_overdue")
    counts = []
    for box, fn in (("CUMULUS", _client_digest), ("CIRRUS", _cirrus_digest)):
        try:
            d = fn()
            counts.append(f"{box}: {d.get('promises_open', 0)} offered, "
                          f"{d.get('promises_confirmed', 0)} confirmed")
        except Exception:
            pass          # already reported by _both_boxes as a note

    if not late:
        out = "OK — nothing overdue. " + "; ".join(counts)
    else:
        lines = [f"{len(late)} client promise(s) OVERDUE:"]
        for pr in late:
            lines.append(
                f"  • [{pr.get('_box')}] {pr.get('client')} — "
                f"\"{str(pr.get('promise'))[:110]}\" "
                f"[{pr.get('state')}, {pr.get('age_hours')}h old, "
                f"SLA {pr.get('sla_hours')}h] on thread "
                f"\"{str(pr.get('subject'))[:70]}\"")
        lines.append("You cannot deliver these yourself — client work is "
                     "not your call. Report them to Buddy.")
        out = "\n".join(lines)
    out = _verdict(out, late, notes, out)

    ledger_append({"event": "check", "tool": "check_open_client_promises",
                   "tier_name": "read-only", "detail": "",
                   "result": out[:200]})
    return out


CLIENT_WATCH_PROBE = "/usr/local/sbin/cumulus_client_watch.py"


def _client_digest(hours: int = 168) -> dict:
    """Read the client-conversation digest via the root-owned probe, or raise.

    NOT a direct import, and that is the whole point. This account cannot
    traverse /home/buddy (mode 750), so importing the app modules fails no
    matter what the file modes below that directory are -- which is exactly how
    all four of these checks shipped unable to run while looking installed.
    The sudoers file has documented that constraint since S63 for credentials;
    it was simply not applied here.

    The probe is root-owned, mode 755, outside anything this account can write,
    and returns a fixed JSON digest of counts and labels. Chosen over widening
    filesystem access: the agent gets an answer, never a filesystem.
    """
    r = _run(["sudo", "-n", "-u", "buddy", CLIENT_WATCH_PROBE, str(int(hours))],
             timeout=60)
    body = (r.stdout or "").strip()
    if not body:
        raise RuntimeError(
            f"probe produced no output (rc={r.returncode}): "
            f"{(r.stderr or '').strip()[:200]}")
    try:
        data = json.loads(body)
    except Exception as e:
        raise RuntimeError(f"probe output was not JSON ({e}): {body[:200]}")
    if data.get("error"):
        raise RuntimeError(data["error"])
    return data


CIRRUS_WATCH_URL = "https://cirrus.cirrustask.com/admin/client-watch"


def _cirrus_digest(hours: int = 168) -> dict:
    """The same digest, from CIRRUS. Raises if it cannot be read.

    S78. Every client-conversation check below used to answer for CUMULUS only,
    while com.cirrus.intake runs LIVE on CIRRUS against a DIFFERENT mailbox --
    so CIRRUS can answer a client entirely on its own and nothing was watching
    it. A check that silently covers one box of two is the same failure this
    project keeps paying for: a guard on one path and not its twin, which reads
    as covered.

    Uses `cirrus_watch_token`, scoped to this ONE route -- not the Time Machine
    token and not the main admin token. It cannot reach deploys or approvals.
    """
    import urllib.error
    import urllib.request

    token = _load_secrets()["cirrus_watch_token"]
    # Explicit User-Agent: Cloudflare in front of cirrus.cirrustask.com blocks
    # urllib's default, same as check_cirrus_timemachine already handles.
    req = urllib.request.Request(
        f"{CIRRUS_WATCH_URL}?hours={int(hours)}",
        headers={"User-Agent": "CUMULUS-supervisor/1.0", "X-API-Token": token})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode())
    if data.get("error"):
        raise RuntimeError(f"CIRRUS reported: {data['error']}")
    return data


def _both_boxes(hours: int, key: str):
    """(rows, notes) for one check across BOTH boxes, each row tagged.

    A box that cannot be read contributes a NOTE, never silence. If CIRRUS is
    unreachable the check says so in its own output rather than returning a
    confident CUMULUS-only "OK" -- the caller must be able to tell "both boxes
    are clean" from "one box is clean and the other did not answer".
    """
    rows, notes = [], []
    for box, fn in (("CUMULUS", _client_digest), ("CIRRUS", _cirrus_digest)):
        try:
            for r in (fn(hours).get(key) or []):
                rows.append({**r, "_box": box})
        except Exception as e:
            notes.append(f"{box} UNREADABLE ({type(e).__name__}: {e})")
    return rows, notes


def _verdict(label: str, rows: list, notes: list, clean: str) -> str:
    """Shared shape: findings first, then any box that could not be read."""
    out = clean if not rows else label
    if notes:
        out += ("\n*** NOT A COMPLETE ANSWER — " + "; ".join(notes)
                + ". Treat these boxes as unchecked, not clean.")
    return out


def _unreadable(tool: str, e: Exception) -> str:
    """One phrasing for "this check did not run", used by all three folds.

    Kept separate from a clean result ON PURPOSE. Every silent outage this
    project has paid for looked like a check that could not run and said
    nothing -- indistinguishable, in a Telegram summary, from a check that ran
    and found nothing. These words are deliberately not reassuring.
    """
    out = (f"UNREADABLE: {tool} could not run ({e}). This is NOT a clean "
           f"result -- treat it as a check that did not happen.")
    ledger_append({"event": "check", "tool": tool, "tier_name": "read-only",
                   "detail": "", "result": out[:200]})
    return out


def check_duplicate_client_answers(hours: int = 168) -> str:
    """Did we send a client the same answer twice on one thread? Read-only.

    S78. On 2026-08-25 Bill asked a follow-up and was sent back the recap he
    already had, two seconds later. The subject-line defect that caused it is
    fixed; this watches the SYMPTOM, because a duplicate answer is what several
    different answer-path faults all look like from the client's chair.

    Reports only. Re-answering a client is a client send, and client sends are
    in your NEVER tier.
    """
    hits, notes = _both_boxes(hours, "duplicate_answers")
    if not hits:
        out = f"OK — no repeated answers on any client thread (both boxes, {hours}h)."
    else:
        lines = [f"{len(hits)} repeated client answer(s):"]
        for h in hits:
            lines.append(
                f"  • [{h.get('_box')}] {h['requester']} — {h['count']}x "
                f"{h['kind']} on \"{str(h.get('title') or h['thread'])[:70]}\" "
                f"({h['gap_hours']}h apart, last {h['last']})")
        lines.append("Do NOT re-answer or correct this yourself — report it.")
        out = "\n".join(lines)
    out = _verdict(out, hits, notes, out)

    ledger_append({"event": "check", "tool": "check_duplicate_client_answers",
                   "tier_name": "read-only", "detail": f"{hours}h",
                   "result": out[:200]})
    return out


def check_thread_stalls(hours: int = 48) -> str:
    """Has a client written and had nothing substantive back? Read-only.

    An ack does not count, which is the whole point: Bill's go-ahead was
    acknowledged within seconds and the work it authorised was never done.

    Some hits are expected and are marked so — a build or research request is
    SUPPOSED to become queued work rather than an instant reply. A hit where a
    reply was expected is the one that matters.
    """
    stalls, notes = _both_boxes(hours, "stalled_threads")
    if not stalls:
        out = (f"OK — no client message older than {hours}h is still "
               f"unanswered, on either box.")
    else:
        waiting = [x for x in stalls if x["expected_reply"]]
        lines = [f"{len(stalls)} client thread(s) with no substantive reply "
                 f"({len(waiting)} where a reply was expected):"]
        for x in stalls:
            flag = "REPLY EXPECTED" if x["expected_reply"] else f"queued as {x['kind']}"
            lines.append(
                f"  • [{x.get('_box')}] {x['requester']} — "
                f"\"{str(x.get('title') or x['thread'])[:70]}\" "
                f"[{x['age_hours']}h, {flag}]")
        lines.append("You cannot answer a client. Report it; use "
                     "request_guidance if you cannot tell what is blocking.")
        out = "\n".join(lines)
    out = _verdict(out, stalls, notes, out)

    ledger_append({"event": "check", "tool": "check_thread_stalls",
                   "tier_name": "read-only", "detail": f"{hours}h",
                   "result": out[:200]})
    return out


def check_high_value_field_overwrites(hours: int = 168) -> str:
    """Was a researched fact on a lead the client is working overwritten?

    S78. A bulk directory job replaced the researched board president of a
    warm, tier-A lead with a same-named association's officer from another
    county, and the dry run reported "0 ambiguous". Mailing that board would
    have reached a stranger.

    ESCALATE, NEVER REVERT. Which of two values is correct is a judgment call
    about client data, and that is not yours to make -- report the old and new
    values so a human can decide.
    """
    hits, notes = _both_boxes(hours, "high_value_overwrites")
    if not hits:
        out = (f"OK — no researched field on a warm-or-better lead was "
               f"overwritten on either box in the last {hours}h.")
    else:
        lines = [f"{len(hits)} overwrite(s) of a researched field on a "
                 f"warm-or-better lead:"]
        for h in hits:
            lines.append(f"  • [{h.get('_box')}] {h['name']} [{h['lead_state']}] "
                         f"{h['field']}: \"{h['old']}\" -> \"{h['new']}\" "
                         f"({h['occurred_at']})")
        lines.append("Report only. Do NOT revert — which value is right is a "
                     "judgment call about client data, not your call.")
        out = "\n".join(lines)
    out = _verdict(out, hits, notes, out)

    ledger_append({"event": "check", "tool": "check_high_value_field_overwrites",
                   "tier_name": "read-only", "detail": f"{hours}h",
                   "result": out[:200]})
    return out


def check_timers() -> str:
    """List all systemd timers with next/last-fire times. Read-only, no sudo."""
    r = _run(["systemctl", "list-timers", "--all", "--no-pager"])
    out = (r.stdout or r.stderr).strip()
    ledger_append({"event": "check", "tool": "check_timers",
                   "tier_name": "read-only", "detail": "",
                   "result": f"{len(out.splitlines())} lines"})
    return out


def tail_journal(unit: str, lines: int = 40) -> str:
    """Tail recent journal output for a unit. Read-only — cumulus-supervisor
    is in the systemd-journal group, no sudo needed."""
    unit = _normalize_unit(unit)
    lines = max(1, min(int(lines), 200))
    r = _run(["journalctl", "-u", unit, "-n", str(lines), "--no-pager"])
    out = (r.stdout or r.stderr).strip()
    ledger_append({"event": "check", "tool": "tail_journal",
                   "tier_name": "read-only", "detail": f"{unit} (-n {lines})",
                   "result": f"{len(out.splitlines())} lines"})
    return out


def check_credentials_health() -> str:
    """Verify CUMULUS's credentials.json currently parses. Runs a fixed,
    root-owned probe script AS buddy via sudo (cumulus-supervisor can't
    read buddy's home tree directly) — see
    supervisor/root-scripts/cumulus_creds_health.py. Never sees or returns
    any credential VALUE, only ok/fail + key count."""
    r = _run(["sudo", "-n", "-u", "buddy",
              "/usr/local/sbin/cumulus_creds_health.py"])
    out = (r.stdout or r.stderr).strip()
    ok = r.returncode == 0
    ledger_append({"event": "check", "tool": "check_credentials_health",
                   "tier_name": "read-only", "detail": "",
                   "result": out})
    return out if ok else f"UNHEALTHY: {out}"


CIRRUS_TM_URL = "https://cirrus.cirrustask.com/admin/tm-status"
# Warn if the last successful attempt is older than this. TM's own schedule
# drifts a few minutes later each day (documented, expected) but should
# never go this long without a fresh attempt.
TM_STALE_HOURS = 36


def check_cirrus_timemachine() -> str:
    """Check CIRRUS's Time Machine backup health (S64). Calls CIRRUS's admin
    API over HTTPS with a token scoped to ONLY this one endpoint — never the
    main admin token, so this tool cannot reach deploys/approvals/anything
    else on CIRRUS. Read-only; makes no changes on either box."""
    import urllib.error
    import urllib.request
    from datetime import datetime, timezone

    try:
        token = _load_secrets()["cirrus_tm_token"]
    except Exception as e:
        result = f"FAILED: no cirrus_tm_token in secrets.json ({e})"
        ledger_append({"event": "check", "tool": "check_cirrus_timemachine",
                       "tier_name": "read-only", "detail": "", "result": result})
        return result

    try:
        # Explicit User-Agent required -- Cloudflare's bot protection in
        # front of cirrus.cirrustask.com blocks urllib's default UA (curl's
        # default sails through fine; confirmed live testing this). Same
        # fix already used by intake.py's telegram() and task_solver.py's
        # _fetch_is_feed() for the same reason.
        req = urllib.request.Request(
            f"{CIRRUS_TM_URL}?token={token}",
            headers={"User-Agent": "CUMULUS-supervisor/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        result = f"FAILED: could not reach CIRRUS tm-status ({e})"
        ledger_append({"event": "check", "tool": "check_cirrus_timemachine",
                       "tier_name": "read-only", "detail": "", "result": result})
        return result

    last_attempt = data.get("last_attempt")
    ok = data.get("last_result_ok")
    stale = False
    if last_attempt:
        try:
            dt = datetime.strptime(last_attempt, "%Y-%m-%d %H:%M:%S %z")
            stale = (datetime.now(timezone.utc) - dt).total_seconds() > TM_STALE_HOURS * 3600
        except ValueError:
            pass

    if ok and not stale:
        result = f"OK: last successful backup {last_attempt}"
    elif stale:
        result = f"STALE: last attempt {last_attempt} is over {TM_STALE_HOURS}h old"
    else:
        result = f"UNHEALTHY: last attempt {last_attempt}, result code {data.get('last_result')}"

    ledger_append({"event": "check", "tool": "check_cirrus_timemachine",
                   "tier_name": "read-only", "detail": "", "result": result})
    return result


# ── Reversible actions (TIER_AUTO) ───────────────────────────────────────────

def restart_service(unit: str) -> str:
    """Restart an allow-listed systemd unit via the scoped sudoers grant.
    TIER_AUTO per CUMULUS.md sec 4's reversible-action allowlist."""
    unit = _normalize_unit(unit)
    if unit not in ALLOWED_UNITS:
        result = f"REFUSED: {unit} not in ALLOWED_UNITS"
        ledger_append({"event": "action", "tool": "restart_service",
                       "tier_name": TIER_NAME[TIER_AUTO], "detail": unit,
                       "result": result})
        return result
    r = _run(["sudo", "-n", "systemctl", "restart", unit])
    result = "restarted" if r.returncode == 0 else f"FAILED: {(r.stderr or r.stdout).strip()}"
    ledger_append({"event": "action", "tool": "restart_service",
                   "tier_name": TIER_NAME[TIER_AUTO], "detail": unit,
                   "result": result})
    return result


def reset_failed(unit: str) -> str:
    """Clear a unit's failed state via the scoped sudoers grant.
    TIER_AUTO — same allowlist as restart_service."""
    unit = _normalize_unit(unit)
    if unit not in ALLOWED_UNITS:
        result = f"REFUSED: {unit} not in ALLOWED_UNITS"
        ledger_append({"event": "action", "tool": "reset_failed",
                       "tier_name": TIER_NAME[TIER_AUTO], "detail": unit,
                       "result": result})
        return result
    r = _run(["sudo", "-n", "systemctl", "reset-failed", unit])
    result = "cleared" if r.returncode == 0 else f"FAILED: {(r.stderr or r.stdout).strip()}"
    ledger_append({"event": "action", "tool": "reset_failed",
                   "tier_name": TIER_NAME[TIER_AUTO], "detail": unit,
                   "result": result})
    return result


def request_opus_upgrade(reason: str) -> str:
    """S64: ask Buddy's permission to use Opus for the rest of THIS reasoning
    pass onward — call this if a task genuinely seems to need deeper
    reasoning than you can give it, not as a routine choice. Sends Buddy a
    Telegram; if he replies "approve" within 2 hours, your NEXT invocation
    runs on Opus for exactly one pass, then reverts to Sonnet automatically.
    This pass itself still finishes on Sonnet — note in your summary that
    you're requesting an upgrade and will revisit next time you're woken."""
    import opus_approval
    text = opus_approval.create_opus_request(reason)
    result = send_telegram(text)
    ledger_append({"event": "action", "tool": "request_opus_upgrade",
                   "tier_name": "notify", "detail": reason[:120], "result": result})
    return result


def request_guidance(issue: str, question: str) -> str:
    """S65: ask Buddy for actual direction, not just a yes/no — call this
    ONLY when genuinely stuck: you've tried your allowed diagnostics/fixes
    (restart_service/reset_failed on the allowlist, the read-only checks),
    the problem persists, and you have no remaining tool that could address
    it. NOT for routine anomalies you can already report-and-move-on from
    via send_telegram — this is for when you need a human decision. Sends
    Buddy a Telegram describing the issue and your question; his free-text
    reply (within 2 hours) is read back to you at the START of your NEXT
    invocation, before you begin your checks. This pass itself still
    finishes without an answer — note in your summary that you've escalated
    and will act on Buddy's direction next time you're woken."""
    import opus_approval
    text = opus_approval.create_guidance_request(issue, question)
    result = send_telegram(text)
    ledger_append({"event": "action", "tool": "request_guidance",
                   "tier_name": "notify", "detail": f"{issue[:80]} | {question[:80]}",
                   "result": result})
    return result


# ── Notify ────────────────────────────────────────────────────────────────────

def send_telegram(message: str) -> str:
    """Send a one-way notification to Buddy via the existing shared bot
    token. Token is read here, server-side, and never returned to the
    caller — the LLM only ever sees 'sent'/'FAILED: ...'."""
    secrets = _load_secrets()
    token = secrets.get("telegram_bot_token", "")
    chat_id = secrets.get("telegram_user_id", "")
    if not token or not chat_id:
        result = "FAILED: telegram creds missing from secrets.json"
        ledger_append({"event": "notify", "tool": "send_telegram",
                       "tier_name": "notify", "detail": message[:80],
                       "result": result})
        return result
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": message[:4000]}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        result = "sent"
    except urllib.error.URLError as e:
        result = f"FAILED: {e}"
    ledger_append({"event": "notify", "tool": "send_telegram",
                   "tier_name": "notify", "detail": message[:80],
                   "result": result})
    return result
