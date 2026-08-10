"""
spend_guard.py — funds-exhausted fail-safe for pay-per-use APIs.  (S58, 2026-08-10)
===============================================================================
Spend model (Buddy): the PROVIDER DASHBOARD is the hard $ ceiling — Buddy sets a
monthly max / auto-reload cap per provider. Code does NOT estimate or pre-budget
spend. Instead, when a provider call returns a funds-exhausted / quota / billing
error, we:
  1. PAUSE that source until the 1st of next month (when caps/credits refresh),
  2. ALERT Buddy once (Telegram), and
  3. AUTO-RESUME automatically once the month rolls over.
So a hit cap degrades gracefully (caller falls back to another source) instead of
erroring — or worse, retry-hammering — every run.

Usage at a call site:
    import spend_guard as SG
    if SG.is_paused("brave"):
        return []                         # skip; caller falls back
    try:
        ...call provider...
    except Exception as e:
        if SG.is_funds_error(e):
            SG.pause("brave", str(e))      # ledger + one-time alert; caller falls back
            return []
        raise

NOTE: bare HTTP 429 is intentionally NOT treated as funds-exhaustion (it is usually
a transient rate-limit). Only clear billing/quota text pauses a provider for a month.

Stdlib only. Never raises to the caller — a guard must not break the job it guards.
"""
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path.home() / "projects/cirrus-digest"
LEDGER = HERE / "logs/spend-pause.json"
CREDS = HERE / "config/credentials.json"

# Funds / credits / quota / billing exhaustion — NOT transient auth/network/rate
# errors. Mirrors model_health.BILLING_ERR (deliberately excludes bare 429).
BILLING_ERR = re.compile(
    r"(insufficient[_ ]?(quota|balance|funds|credit)|no credits|credit balance|"
    r"out of (credits|balance)|billing|payment required|add (funds|credits)|"
    r"purchase|top ?up|402|quota exceeded|exceeded your current quota)", re.I)


def is_funds_error(err) -> bool:
    """True if the error text/status looks like funds/quota/billing exhaustion."""
    try:
        return bool(BILLING_ERR.search(str(err)))
    except Exception:
        return False


def _first_of_next_month(now):
    return datetime(now.year + (now.month // 12), (now.month % 12) + 1, 1)


def _load():
    try:
        return json.loads(LEDGER.read_text())
    except Exception:
        return {}


def _save(d):
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        LEDGER.write_text(json.dumps(d, indent=2) + "\n")
    except Exception:
        pass


def is_paused(provider, now=None) -> bool:
    """True if `provider` is currently paused for funds. Auto-resumes (drops the
    entry) once its `until` — the 1st of next month — has passed."""
    now = now or datetime.now()
    d = _load()
    e = d.get(provider)
    if not e:
        return False
    try:
        until = datetime.fromisoformat(e["until"])
    except Exception:
        return False
    if now >= until:
        d.pop(provider, None)                 # auto-resume
        _save(d)
        return False
    return True


def pause(provider, note="", now=None):
    """Record `provider` as funds-exhausted until the 1st of next month, and alert
    Buddy ONCE per pause. Idempotent: re-calling while already paused refreshes the
    note but does NOT re-alert."""
    now = now or datetime.now()
    d = _load()
    already = provider in d
    until = _first_of_next_month(now)
    d[provider] = {
        "since": now.isoformat(timespec="seconds"),
        "until": until.isoformat(timespec="seconds"),
        "note": (note or "")[:200],
    }
    _save(d)
    if not already:
        _alert(f"\U0001F4B3 *{provider}* paused — funds/quota exhausted. Skipping it "
               f"until {until:%b %-d} (auto-resume when funds refresh). Check the "
               f"{provider} billing dashboard.\n`{(note or '')[:160]}`")


def status(now=None) -> dict:
    """Current pause ledger (for the morning brief / cirrus-health), with expired
    entries pruned. Empty dict = nothing paused."""
    now = now or datetime.now()
    d = _load()
    live, changed = {}, False
    for k, e in d.items():
        try:
            if now < datetime.fromisoformat(e["until"]):
                live[k] = e
            else:
                changed = True
        except Exception:
            changed = True
    if changed:
        _save(live)
    return live


def _alert(msg):
    """Best-effort Telegram alert (same pattern as model_health.tg). Never raises."""
    try:
        creds = json.loads(CREDS.read_text())
        tok = creds.get("telegram_bot_token", "")
        uid = str(creds.get("telegram_user_id", "")).strip()
        if not tok or not uid or os.environ.get("SPEND_GUARD_NO_ALERT"):
            return
        data = urllib.parse.urlencode(
            {"chat_id": uid, "text": msg, "parse_mode": "Markdown"}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=15)
    except Exception:
        pass


def selftest():
    """Offline logic check against a temp ledger (no network, no real creds)."""
    import tempfile
    global LEDGER
    LEDGER = Path(tempfile.mkdtemp()) / "spend-pause.json"
    os.environ["SPEND_GUARD_NO_ALERT"] = "1"
    fails = 0

    def ck(label, cond):
        nonlocal fails
        print(f"  [{'OK ' if cond else 'FAIL'}] {label}")
        fails += 0 if cond else 1

    ck("quota-exceeded is funds error", is_funds_error("HTTP 429: exceeded your current quota"))
    ck("insufficient_quota is funds error", is_funds_error("402 insufficient_quota"))
    ck("bare rate-limit is NOT funds error", not is_funds_error("429 Too Many Requests: rate limit"))
    ck("timeout is NOT funds error", not is_funds_error("Read timed out"))
    ck("not paused initially", not is_paused("brave"))
    pause("brave", "insufficient balance", now=datetime(2026, 8, 10))
    ck("paused after pause()", is_paused("brave", now=datetime(2026, 8, 20)))
    ck("still paused later same month", is_paused("brave", now=datetime(2026, 8, 31, 23, 59)))
    ck("auto-resume on the 1st next month", not is_paused("brave", now=datetime(2026, 9, 1)))
    ck("status() empty after resume", status(now=datetime(2026, 9, 1)) == {})
    print("PASS" if not fails else f"{fails} FAILURE(S)")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    if "selftest" in sys.argv:
        sys.exit(selftest())
    print(json.dumps(status(), indent=2))
