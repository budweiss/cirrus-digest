"""Opus-upgrade approval flow for the CUMULUS supervisor (B1) — S64.

Buddy's ask: Skywarden shouldn't be permanently capped at Sonnet if a task
genuinely needs more — but an upgrade needs Buddy's explicit Telegram
approval each time, not a silent self-escalation.

Skywarden's `request_opus_upgrade` tool (tools.py) writes a pending request
here and sends the ask. This module polls for Buddy's reply — short,
non-blocking `getUpdates` calls (offset-tracked, same technique cirrus_bot.py's
long-poll loop uses, just called in short bursts from the existing 60s
heartbeat cycle instead of run as its own persistent listener; Skywarden's
process model is wake-check-sleep, not a standing service). A reply is
checked once per heartbeat tick, and consumed at most once by the next
reasoning pass, then Skywarden reverts to Sonnet automatically.
"""
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

STATE_DIR = Path("/opt/cumulus-supervisor/state")
REQUEST_FILE = STATE_DIR / "opus-request.json"
UPDATE_OFFSET_FILE = STATE_DIR / "telegram-update-offset.txt"

REQUEST_EXPIRY_SEC = 2 * 3600  # a pending ask goes stale after 2h unanswered


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


def create_request(reason: str) -> str:
    """Called by tools.request_opus_upgrade(). Writes the pending marker and
    returns the Telegram text to send (kept here, not in tools.py, so the
    request/approval state shape lives in one place)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    REQUEST_FILE.write_text(json.dumps({
        "reason": reason, "requested_at": time.time(),
        "status": "pending",
    }))
    return (f"Skywarden is requesting an Opus upgrade for: {reason}\n\n"
            f"Reply \"approve\" within 2 hours to allow ONE upgraded pass. "
            f"No reply = stays on Sonnet, no action needed.")


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
    if time.time() - req.get("requested_at", 0) > REQUEST_EXPIRY_SEC:
        req["status"] = "expired"
        REQUEST_FILE.write_text(json.dumps(req))
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

    approved = False
    max_update_id = offset - 1
    for update in result.get("result", []):
        max_update_id = max(max_update_id, update["update_id"])
        msg = update.get("message", {})
        if str(msg.get("from", {}).get("id", "")) != chat_id:
            continue  # only Buddy's own replies count
        text = (msg.get("text") or "").strip().lower()
        if "approve" in text:
            approved = True

    if max_update_id >= offset:
        UPDATE_OFFSET_FILE.write_text(str(max_update_id + 1))

    if approved:
        req["status"] = "approved"
        REQUEST_FILE.write_text(json.dumps(req))


def consume_approval() -> bool:
    """Called once per reasoning-pass invocation. Returns True (and marks
    the request consumed) exactly once per approval -- the NEXT pass after
    that reverts to Sonnet automatically, matching 'ONE upgraded pass'."""
    if not REQUEST_FILE.exists():
        return False
    try:
        req = json.loads(REQUEST_FILE.read_text())
    except Exception:
        return False
    if req.get("status") != "approved":
        return False
    req["status"] = "consumed"
    REQUEST_FILE.write_text(json.dumps(req))
    return True
