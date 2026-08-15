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
import urllib.error
import urllib.request
from pathlib import Path

from ledger import TIER_AUTO, TIER_NAME, ledger_append

SECRETS_PATH = Path("/opt/cumulus-supervisor/state/secrets.json")

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
