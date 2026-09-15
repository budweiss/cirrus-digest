"""Append-only audit ledger for the Alopecia etiology-synthesis agent (S177).

Same shape as supervisor/ledger.py (JSONL + human-readable CHANGES.md
mirror) -- reused here as a plain import rather than a second copy, since
this agent runs as buddy in the same checkout and has no cross-account
import barrier to work around (unlike Skywarden, which ported its own copy
specifically because cumulus-supervisor cannot read buddy's tree).
"""
import json
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_DIR / "logs" / "alopecia-agent"
LEDGER_JSONL = STATE_DIR / "ledger.jsonl"
LEDGER_MD = STATE_DIR / "CHANGES.md"

TIER_AUTO = 0     # reversible action the agent may take on its own (read,
                  # synthesize, write to its own state, draft a brief section)
TIER_CONFIRM = 1  # not used by v1's tool set -- everything auto or escalated
TIER_NEVER = -1   # must never be automated (contact anyone but Buddy)
TIER_NAME = {TIER_AUTO: "auto", TIER_CONFIRM: "confirm", TIER_NEVER: "never"}


def ledger_append(entry: dict, state_dir=None) -> Path:
    """Append one event to ledger.jsonl and mirror a row into CHANGES.md.
    Every tool call the agent makes should leave a row here, success or
    failure -- entry should include at least {event, tool, detail?, result?}.

    state_dir=None resolves the module-level STATE_DIR at call time, not as
    a default-argument value -- see hypothesis_store.load()'s docstring for
    why. This is what lets tools.py's callers (which never pass state_dir
    explicitly) be redirected by a test that monkeypatches STATE_DIR.
    """
    state_dir = Path(state_dir or STATE_DIR)
    state_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = state_dir / "ledger.jsonl"
    md_path = state_dir / "CHANGES.md"

    row = dict(entry)
    row.setdefault("ts", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    with open(jsonl_path, "a") as f:
        f.write(json.dumps(row) + "\n")

    if not md_path.exists():
        md_path.write_text(
            "# Alopecia agent ledger\n\n"
            "Append-only audit trail of every tool call the etiology-"
            "synthesis agent makes. Newest at the bottom.\n\n"
            "| when | event | tool | tier | detail | result |\n"
            "|------|-------|------|------|--------|--------|\n"
        )
    detail = str(row.get("detail", ""))[:80].replace("|", "/").replace("\n", " ")
    result = str(row.get("result", ""))[:60].replace("|", "/").replace("\n", " ")
    with open(md_path, "a") as f:
        f.write(f"| {row['ts']} | {row.get('event','')} | {row.get('tool','')} "
                f"| {row.get('tier_name','')} | {detail} | {result} |\n")
    return jsonl_path


def ledger_today(date: str = None, state_dir=None):
    date = date or datetime.now().strftime("%Y-%m-%d")
    jsonl_path = Path(state_dir or STATE_DIR) / "ledger.jsonl"
    if not jsonl_path.exists():
        return []
    rows = []
    for line in jsonl_path.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if str(r.get("ts", "")).startswith(date):
            rows.append(r)
    return rows


def selftest():
    import tempfile
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        ok = ok and cond

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        ledger_append({"event": "action", "tool": "call_local",
                      "tier_name": "auto", "detail": "clustered 4 items",
                      "result": "ok"}, state_dir=d)
        rows = ledger_today(datetime.now().strftime("%Y-%m-%d"), state_dir=d)
        check("ledger_append: writes a row readable back via ledger_today",
              len(rows) == 1 and rows[0]["tool"] == "call_local")
        check("ledger_append: mirrors into CHANGES.md too",
              (d / "CHANGES.md").exists()
              and "call_local" in (d / "CHANGES.md").read_text())
        check("ledger_today: a date with nothing logged returns empty, "
              "not an error", ledger_today("1999-01-01", state_dir=d) == [])

        lines_before = len((d / "CHANGES.md").read_text().splitlines())
        ledger_append({"event": "action", "tool": "x", "detail": "a|pipe\nand a newline",
                      "result": "y|z"}, state_dir=d)
        md = (d / "CHANGES.md").read_text()
        lines_after = len(md.splitlines())
        check("ledger_append: an embedded newline in detail adds exactly "
              "ONE table row, not two (it would break the table otherwise)",
              lines_after == lines_before + 1)
        check("ledger_append: pipes in detail/result are escaped so they "
              "can't be mistaken for a column boundary",
              "a/pipe and a newline" in md and "y/z" in md
              and "a|pipe" not in md and "y|z" not in md)

    print("PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
