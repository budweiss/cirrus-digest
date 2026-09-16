"""Two-way approval/guidance flow for the CUMULUS supervisor (Skywarden) — S64/S65.

Two request kinds share one pending-request slot. Skywarden ends every run
with exactly one send_telegram call (CLAUDE.md sec 6), so in practice only
one ask is outstanding at a time. An unanswered or unconsumed request is
never overwritten; repeated guidance for the same incident is suppressed:

- "opus_upgrade": yes/no. Buddy's reply must be exactly
  "approve" (case-insensitive). Approved unlocks exactly ONE Opus pass on
  Skywarden's next invocation, then it reverts to Sonnet automatically.
- "guidance": free-text. Buddy's whole reply becomes Skywarden's direction
  on its next invocation. For when Skywarden is genuinely stuck — tried its
  allowed diagnostics/fixes, the problem persists, and it has no remaining
  tool that could address it (CLAUDE.md sec 3) — not for routine anomalies
  it can already report-and-move-on from via send_telegram.

Module polls for Buddy's Telegram reply via short, non-blocking `getUpdates`
calls (offset-tracked) folded into the existing 60s heartbeat tick —
deliberately NOT a persistent long-poll listener like cirrus_bot.py, since
Skywarden's process model is wake/check/sleep, not a standing service.
"""
import json
import hashlib
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

STATE_DIR = Path("/opt/cumulus-supervisor/state")
REQUEST_FILE = STATE_DIR / "pending-request.json"
UPDATE_OFFSET_FILE = STATE_DIR / "telegram-update-offset.txt"

REQUEST_EXPIRY_SEC = 2 * 3600  # one-time model upgrade approval
GUIDANCE_EXPIRY_SEC = 7 * 86400  # actual decisions survive time away

def _expiry(req):
    return GUIDANCE_EXPIRY_SEC if req.get("kind") == "guidance" else REQUEST_EXPIRY_SEC


def _write_json(path, data):
    fd, tmp = tempfile.mkstemp(prefix='.request-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _load_secrets() -> dict:
    with open("/opt/cumulus-supervisor/state/secrets.json") as f:
        return json.load(f)


def _api_call(method: str, params: dict, token: str, timeout: int = 10):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(params).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json",
                                  "User-Agent": "CUMULUS-supervisor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _request_slot_busy():
    if not REQUEST_FILE.exists():
        return False
    req = json.loads(REQUEST_FILE.read_text())
    return (req.get('status') in ('answered', 'approved') or
            (req.get('status') == 'pending' and
             time.time() - req.get('requested_at', 0) < _expiry(req)))


def ready_reply_id():
    if not REQUEST_FILE.exists():
        return ''
    try:
        req = json.loads(REQUEST_FILE.read_text())
    except (OSError, ValueError):
        return ''
    if req.get('status') in ('answered', 'approved'):
        return str(req.get('kind')) + ':' + str(req.get('requested_at'))
    return ''


def record_request_delivery(sent):
    req = json.loads(REQUEST_FILE.read_text())
    if not sent:
        req['status'] = 'delivery_failed'
        _write_json(REQUEST_FILE, req)
    elif req.get('kind') == 'guidance':
        path = STATE_DIR / 'guidance-history.json'
        history = json.loads(path.read_text()) if path.exists() else {}
        history[req['dedup_key']] = time.time()
        # Bounded history; incident context changes after confirmed recovery.
        history = dict(sorted(history.items(), key=lambda x: x[1])[-256:])
        _write_json(path, history)


def create_opus_request(reason: str) -> str:
    """Called by tools.request_opus_upgrade(). Writes the pending marker and
    returns the Telegram text to send."""
    if _request_slot_busy():
        return None
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _write_json(REQUEST_FILE, {
        "kind": "opus_upgrade", "reason": reason,
        "requested_at": time.time(), "status": "pending",
    })
    return (f"Skywarden is requesting an Opus upgrade for: {reason}\n\n"
            f"Reply \"approve\" within 2 hours to allow ONE upgraded pass. "
            f"No reply = stays on Sonnet, no action needed.")


def create_guidance_request(issue: str, question: str, context: str = "") -> str:
    """Called by tools.request_guidance(). Writes the pending marker and
    returns the Telegram text to send."""
    if _request_slot_busy():
        return None
    identity = context or ' '.join((issue + ' ' + question).lower().split())
    dedup_key = hashlib.sha256(identity.encode()).hexdigest()
    history_path = STATE_DIR / 'guidance-history.json'
    history = json.loads(history_path.read_text()) if history_path.exists() else {}
    if dedup_key in history:
        return None
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _write_json(REQUEST_FILE, {
        "kind": "guidance", "issue": issue, "question": question,
        "dedup_key": dedup_key,
        "requested_at": time.time(), "status": "pending",
    })
    return (f"Skywarden is stuck and needs direction:\n\n{issue}\n\n"
            f"{question}\n\nReply with what you'd like done (within 7 "
            f"days) — Skywarden will read your reply on its next run. "
            f"No reply = it holds off; it will not repeat this same request.")


def check_for_reply() -> None:
    """Cheap, called every ~60s heartbeat tick. No-ops instantly if there's
    no pending request -- costs nothing on the vast majority of ticks."""
    if not REQUEST_FILE.exists():
        return
    try:
        req = json.loads(REQUEST_FILE.read_text())
    except Exception:
        REQUEST_FILE.unlink(missing_ok=True)
        return
    if req.get("status") != "pending":
        return
    if time.time() - req.get("requested_at", 0) > _expiry(req):
        req["status"] = "expired"
        _write_json(REQUEST_FILE, req)
        return

    try:
        secrets = _load_secrets()
        token, chat_id = secrets["telegram_bot_token"], str(secrets["telegram_user_id"])
    except Exception:
        return

    offset = 0
    if UPDATE_OFFSET_FILE.exists():
        try:
            offset = int(UPDATE_OFFSET_FILE.read_text().strip())
        except ValueError:
            pass

    try:
        # timeout=0: a quick poll, not cirrus_bot.py's 30s long-poll -- this
        # runs inline in the 60s heartbeat loop and must return fast.
        result = _api_call("getUpdates", {"offset": offset, "timeout": 0}, token)
    except (urllib.error.URLError, TimeoutError):
        return

    reply_text = None
    max_update_id = offset - 1
    for update in result.get("result", []):
        max_update_id = max(max_update_id, update["update_id"])
        msg = update.get("message", {})
        if str(msg.get("from", {}).get("id", "")) != chat_id:
            continue  # only Buddy's own replies count
        if msg.get("date", 0) < req.get("requested_at", 0):
            continue  # never consume messages from before this request
        text = (msg.get("text") or "").strip()
        if text:
            reply_text = text  # last non-empty message from Buddy in this batch wins

    if max_update_id >= offset:
        UPDATE_OFFSET_FILE.write_text(str(max_update_id + 1))

    if reply_text is None:
        return

    if req["kind"] == "opus_upgrade":
        if reply_text.strip().lower() == "approve":
            req["status"] = "approved"
            _write_json(REQUEST_FILE, req)
    elif req["kind"] == "guidance":
        req["status"] = "answered"
        req["reply"] = reply_text
        _write_json(REQUEST_FILE, req)


def consume_opus_approval() -> bool:
    """Called once per reasoning-pass invocation. Returns True (and marks
    the request consumed) exactly once per approval -- the NEXT pass after
    that reverts to Sonnet automatically, matching 'ONE upgraded pass'."""
    if not REQUEST_FILE.exists():
        return False
    try:
        req = json.loads(REQUEST_FILE.read_text())
    except Exception:
        return False
    if req.get("kind") != "opus_upgrade" or req.get("status") != "approved":
        return False
    req["status"] = "consumed"
    _write_json(REQUEST_FILE, req)
    return True


def consume_guidance():
    """Called once at the start of a reasoning pass. Returns Buddy's reply
    text if a guidance request was answered since the last run, else None.
    Consumed immediately so the same guidance can't be silently re-applied
    to a later, unrelated issue."""
    if not REQUEST_FILE.exists():
        return None
    try:
        req = json.loads(REQUEST_FILE.read_text())
    except Exception:
        return None
    if req.get("kind") != "guidance" or req.get("status") != "answered":
        return None
    reply = req.get("reply")
    req["status"] = "consumed"
    _write_json(REQUEST_FILE, req)
    return reply
