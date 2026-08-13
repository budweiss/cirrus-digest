"""Append-only audit ledger for the CUMULUS supervisor agent (B1).

Mirrors cirrus-repo/dev_loop.py's ledger shape (JSONL + human-readable
CHANGES.md mirror) as a self-contained port rather than a cross-account
import: the cumulus-supervisor OS account cannot read buddy's cirrus-digest
tree (by design — see CUMULUS.md sec 8a), so dev_loop.py isn't importable here.
"""
import json
from datetime import datetime
from pathlib import Path

STATE_DIR = Path("/opt/cumulus-supervisor/state")
LEDGER_JSONL = STATE_DIR / "ledger.jsonl"
LEDGER_MD = STATE_DIR / "CHANGES.md"

TIER_AUTO = 0     # reversible action the supervisor may take on its own
TIER_CONFIRM = 1  # would need a human tap (not used by v1's tool set)
TIER_NEVER = -1   # must never be automated
TIER_NAME = {TIER_AUTO: "auto", TIER_CONFIRM: "confirm", TIER_NEVER: "never"}


def ledger_append(entry: dict) -> Path:
    """Append one event to ledger.jsonl and mirror a row into CHANGES.md.
    Every tool call the supervisor makes — read-only check, reversible
    action, or notification — should leave a row here, success or failure.

    entry should include at least: {event, tool, tier_name?, detail?, result?}.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    row = dict(entry)
    row.setdefault("ts", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    with open(LEDGER_JSONL, "a") as f:
        f.write(json.dumps(row) + "\n")

    if not LEDGER_MD.exists():
        LEDGER_MD.write_text(
            "# CUMULUS supervisor ledger\n\n"
            "Append-only audit trail of every tool call the supervisor makes "
            "(read-only checks, reversible actions, notifications). Newest at "
            "the bottom.\n\n"
            "| when | event | tool | tier | detail | result |\n"
            "|------|-------|------|------|--------|--------|\n"
        )
    detail = str(row.get("detail", ""))[:80].replace("|", "/").replace("\n", " ")
    result = str(row.get("result", ""))[:60].replace("|", "/").replace("\n", " ")
    with open(LEDGER_MD, "a") as f:
        f.write(f"| {row['ts']} | {row.get('event','')} | {row.get('tool','')} "
                f"| {row.get('tier_name','')} | {detail} | {result} |\n")
    return LEDGER_JSONL


def ledger_today(date: str = None):
    """Return today's ledger rows (list of dicts). Empty if none/no ledger."""
    date = date or datetime.now().strftime("%Y-%m-%d")
    if not LEDGER_JSONL.exists():
        return []
    rows = []
    for line in LEDGER_JSONL.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if str(r.get("ts", "")).startswith(date):
            rows.append(r)
    return rows
