"""Spend cap for the CUMULUS supervisor's own reasoning-pass calls (B1).

Mirrors cirrus-repo/llm_budget.py's allow/record pattern as a self-contained
port — same cross-account-import reason as ledger.py. Keeps its own ledger
(box-tagged "cumulus-supervisor" for parity with the shared ledger's box
convention, even though it's a separate file) rather than sharing
buddy's out/llm-spend-ledger.jsonl, which cumulus-supervisor cannot read.

S64: switched from a daily to a MONTHLY ceiling, per Buddy -- a hard daily
reset wastes unused capacity on quiet days instead of letting it carry a
busier one later in the same month. When the monthly cap is hit, stop
escalating for the rest of the month and fall back to the deterministic-only
heartbeat -- never silently over-spend. See CUMULUS.md sec 3.
"""
import json
from datetime import datetime
from pathlib import Path

STATE_DIR = Path("/opt/cumulus-supervisor/state")
SPEND_LEDGER = STATE_DIR / "spend-ledger.jsonl"
MONTHLY_CAP_USD = 150.00  # S64: was $5/day ($150/mo at the old rate if spent
                          # every single day) -- same effective ceiling,
                          # now usable on the days that actually need it.
                          # Buddy: raise this directly if it's ever too tight.
BOX = "cumulus-supervisor"


def _this_month() -> str:
    return datetime.now().strftime("%Y-%m")


def spent_this_month() -> float:
    if not SPEND_LEDGER.exists():
        return 0.0
    month = _this_month()
    total = 0.0
    for line in SPEND_LEDGER.read_text().splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        if str(row.get("ts", "")).startswith(month):
            total += float(row.get("cost_usd", 0.0))
    return total


def allow(est_cost_usd: float = 0.0) -> tuple:
    """Return (allowed: bool, spent_this_month: float, reason: str)."""
    spent = spent_this_month()
    if spent + est_cost_usd > MONTHLY_CAP_USD:
        return False, spent, (f"${spent:.2f} spent this month + ${est_cost_usd:.2f} "
                               f"estimated would exceed ${MONTHLY_CAP_USD:.2f} monthly cap")
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
