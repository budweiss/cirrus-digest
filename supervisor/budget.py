"""Daily spend cap for the CUMULUS supervisor's own reasoning-pass calls (B1).

Mirrors cirrus-repo/llm_budget.py's allow/record pattern as a self-contained
port — same cross-account-import reason as ledger.py. Keeps its own ledger
(box-tagged "cumulus-supervisor" for parity with the shared ledger's box
convention, even though it's a separate file) rather than sharing
buddy's out/llm-spend-ledger.jsonl, which cumulus-supervisor cannot read.

CUMULUS.md sec 3's hard daily ceiling: $5.00/day. When hit, stop escalating
for the rest of the day and fall back to the deterministic-only heartbeat —
never silently over-spend.
"""
import json
from datetime import datetime
from pathlib import Path

STATE_DIR = Path("/opt/cumulus-supervisor/state")
SPEND_LEDGER = STATE_DIR / "spend-ledger.jsonl"
DAILY_CAP_USD = 5.00
BOX = "cumulus-supervisor"


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def spent_today() -> float:
    if not SPEND_LEDGER.exists():
        return 0.0
    today = _today()
    total = 0.0
    for line in SPEND_LEDGER.read_text().splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        if str(row.get("ts", "")).startswith(today):
            total += float(row.get("cost_usd", 0.0))
    return total


def allow(est_cost_usd: float = 0.0) -> tuple:
    """Return (allowed: bool, spent_today: float, reason: str)."""
    spent = spent_today()
    if spent + est_cost_usd > DAILY_CAP_USD:
        return False, spent, (f"${spent:.2f} spent today + ${est_cost_usd:.2f} "
                               f"estimated would exceed ${DAILY_CAP_USD:.2f} cap")
    return True, spent, "ok"


def record(cost_usd: float, detail: str = "") -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "box": BOX,
        "cost_usd": round(float(cost_usd), 6),
        "detail": detail[:120],
    }
    with open(SPEND_LEDGER, "a") as f:
        f.write(json.dumps(row) + "\n")
