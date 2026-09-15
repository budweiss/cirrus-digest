"""Spend cap for the Alopecia agent's own LLM calls (S177).

Unlike Skywarden's budget.py (a private ledger, because cumulus-supervisor
cannot read buddy's files), this agent shares llm_budget.py's ledger with
every other job in this repo -- call_local()/call_council() already tag
themselves task="alopecia-agent", and record_call() uses task as the
session_id when none is given separately, so llm_budget.session_spent()
already isolates this agent's spend from everything else in the same file
with no new plumbing.

MONTHLY_CAP_USD is a placeholder -- Buddy sets the real number before this
agent is armed to run unattended (see alopecia_agent/CLAUDE.md and the
build's own dry-run step).
"""
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))
import llm_budget  # noqa: E402

CREDS_PATH = PROJECT_DIR / "config" / "credentials.json"
TASK = "alopecia-agent"

MONTHLY_CAP_USD = 30.00  # S177: reasoned, not confirmed -- agent.py caps each
                        # daily pass at $2.00 (EST_COST_PER_RUN_USD), so 30
                        # days at the ceiling would be $60/mo; $30 lands at
                        # half that as a real circuit breaker without being
                        # so tight it stalls on a normal month. Buddy should
                        # still confirm or adjust this before arming -- it's
                        # his money, this is a reasoned default, not his answer.


def _load_creds(path=None):
    path = path or CREDS_PATH
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}


def spent_this_month(creds=None) -> float:
    creds = creds if creds is not None else _load_creds()
    cfg, box, ledger_path = llm_budget.resolve(creds, app_dir=str(PROJECT_DIR))
    if cfg is None:
        return 0.0
    month = datetime.now().strftime("%Y-%m")
    total = 0.0
    for row in llm_budget._read_ledger(ledger_path):
        ts = str(row.get("ts", ""))
        if row.get("session_id") == TASK and ts.startswith(month):
            total += float(row.get("cost", 0.0))
    return total


def allow(est_cost_usd: float = 0.0, creds=None) -> tuple:
    """Return (allowed: bool, spent_this_month: float, reason: str). Never
    raises -- a pricing/ledger read failure is treated as $0 spent (fails
    OPEN on the budget check itself, same as llm_budget.record_call's own
    best-effort philosophy), not as a reason to block the agent entirely."""
    spent = spent_this_month(creds)
    if spent + est_cost_usd > MONTHLY_CAP_USD:
        return False, spent, (f"${spent:.2f} spent this month + ${est_cost_usd:.2f} "
                              f"estimated would exceed ${MONTHLY_CAP_USD:.2f} monthly cap")
    return True, spent, "ok"


def selftest():
    import tempfile
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        ok = ok and cond

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        pricing = d / "pricing.json"
        ledger = d / "sub" / "ledger.jsonl"
        pricing.write_text(json.dumps({"models": {"m": {"in": 1.0, "out": 2.0}}}))
        creds = {"llm_budget": {"pricing_path": str(pricing),
                               "ledger_path": str(ledger), "box": "test"}}

        check("spent_this_month: no ledger yet -> $0, not an error",
              spent_this_month(creds) == 0.0)

        allowed, spent, reason = allow(5.00, creds)
        check("allow: $0 spent + $5 estimated is under the cap",
              allowed and spent == 0.0)

        # Per-row cost scaled OFF the real cap constant (30% of it, well
        # under) rather than a hand-typed dollar figure that could end up
        # accidentally over whatever MONTHLY_CAP_USD is set to later.
        per_row = round(MONTHLY_CAP_USD * 0.10, 2)
        this_month = datetime.now().strftime("%Y-%m")
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger, "a") as f:
            for _ in range(3):
                f.write(json.dumps({
                    "ts": f"{this_month}-05T12:00:00", "session_id": TASK,
                    "cost": per_row,
                }) + "\n")
            # A DIFFERENT job's spend under the same ledger must not count
            # against this agent's cap -- that's the whole point of tagging.
            f.write(json.dumps({
                "ts": f"{this_month}-05T12:00:00", "session_id": "business_idea_scan",
                "cost": 999.00,
            }) + "\n")
            # Last month's spend (any amount) must not count against THIS month.
            f.write(json.dumps({
                "ts": "2020-01-05T12:00:00", "session_id": TASK, "cost": 999.00,
            }) + "\n")

        check("spent_this_month: sums only THIS agent's rows "
              "(session_id==alopecia-agent), ignoring a much larger "
              "unrelated job's spend in the same shared ledger",
              spent_this_month(creds) == round(per_row * 3, 2))

        allowed, spent, reason = allow(0.0, creds)
        check("allow: comfortably under the real cap, no new call yet "
              "-> still allowed", allowed and spent == round(per_row * 3, 2))

    # The over-cap case, against the REAL MONTHLY_CAP_USD, not a
    # hand-typed number that could silently drift from the constant.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        pricing = d / "pricing.json"
        ledger = d / "sub" / "ledger.jsonl"
        pricing.write_text(json.dumps({"models": {"m": {"in": 1.0, "out": 2.0}}}))
        creds = {"llm_budget": {"pricing_path": str(pricing),
                               "ledger_path": str(ledger), "box": "test"}}
        this_month = datetime.now().strftime("%Y-%m")
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps({
            "ts": f"{this_month}-05T12:00:00", "session_id": TASK,
            "cost": MONTHLY_CAP_USD - 0.50,
        }) + "\n")
        allowed, spent, reason = allow(1.00, creds)
        check("allow: spend + estimate crossing the REAL cap is refused, "
              "with a reason naming both numbers",
              not allowed and f"{MONTHLY_CAP_USD:.2f}" in reason)
        allowed, spent, reason = allow(0.10, creds)
        check("allow: staying under the real cap is still allowed",
              allowed)

    check("spent_this_month: an unreadable pricing file -> $0, never raises",
          spent_this_month({"llm_budget": {"pricing_path": "/nonexistent-x/p.json"}}) == 0.0)

    print("PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
