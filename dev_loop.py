#!/usr/bin/env python3
"""
dev_loop.py — CIRRUS Autonomous Dev-Loop, Phase 1 core.

This is the machinery that turns a free-text self-review proposal (the /approve
queue) into something CIRRUS can eventually build, test, and deploy on its own
with minimal interaction. Phase 1 delivers the three foundations that make that
safe; it does NOT yet auto-write code (that is Phase 2). What it provides today:

  1. STRUCTURED PROPOSAL SPEC  — make_spec(): attaches a machine-actionable
     {id, tier, files_to_change, test_plan, rollback, ...} block to each
     proposal so a future dev agent has an unambiguous spec to work against.
  2. RISK CLASSIFIER           — classify_risk(): maps a proposal to a risk tier
     (0 auto / 1 one-tap confirm / 2 design-first / never-auto) and, critically,
     hard-blocks the never-auto categories (credentials, deletion, financial,
     access-control, irreversible) no matter how a proposal is phrased.
  3. SELF-CHANGES LEDGER       — ledger_append(): append-only audit trail
     (JSONL + human-readable markdown) of everything the loop proposes or does,
     so nothing autonomous is ever unaccountable.

Pure logic + local file I/O only — no network, no LLM — so it is unit-testable
anywhere (see the __main__ self-test) and cheap to reason about. cirrus_daily's
--dry-run path is the test harness these specs point at.

See docs/CIRRUS-Autonomous-Dev-Loop.md for the architecture.
"""

import json
import re
from datetime import datetime
from pathlib import Path

# ── Risk tiers ────────────────────────────────────────────────────────────────
TIER_AUTO   = 0   # reversible + additive + non-critical → may auto-apply
TIER_CONFIRM = 1  # code change, dry-run-validatable, git-revertible → one tap
TIER_DESIGN = 2   # behavioural/schema/critical-path change → design first
TIER_NEVER  = -1  # must never be automated — human does it by hand

TIER_NAME = {
    TIER_AUTO: "Tier 0 (auto)",
    TIER_CONFIRM: "Tier 1 (one-tap confirm)",
    TIER_DESIGN: "Tier 2 (design-first)",
    TIER_NEVER: "NEVER-auto (human only)",
}

# Anything matching these is forced to NEVER-auto, regardless of type/phrasing.
# Ordered by concern; the first hit wins and is reported as the reason.
_NEVER_PATTERNS = [
    ("credential/secret",   r'\b(password|passwd|credential|secret|api[\s_-]?key|'
                            r'token|oauth|ssh\s*key|private\s*key|\.env\b)\b'),
    ("deletion/destruction", r'\b(delete|rm\s+-rf|drop\s+table|truncate|wipe|'
                             r'purge|hard[\s-]?delete|empty\s+trash|format)\b'),
    ("financial",           r'\b(payment|invoice|charge|purchase|buy|sell|trade|'
                            r'transfer\s+funds|wire|crypto|bank|credit\s*card)\b'),
    ("access-control",      r'\b(permission|chmod|chown|sudo|access\s*control|'
                            r'sharing|acl|grant\s+access|make\s+public)\b'),
    ("auth/send path",      r'\b(smtp\s+password|login\s+credential|reset\s+password|'
                            r'2fa|mfa|recovery\s+(email|contact))\b'),
]

# Tier 2 (design-first): touches a critical/irreversible-ish path even though it
# isn't a hard NEVER. These are code changes we want a human design pass on.
_DESIGN_PATTERNS = [
    ("email/send path",  r'\b(send_digest|send_email|smtp|outgoing\s+email|'
                         r'email\s+delivery)\b'),
    ("schema/data model", r'\b(schema|migrat|database|sqlite|data\s+model|'
                          r'rewrite|refactor\s+core)\b'),
    ("service lifecycle", r'\b(launchctl|restart\s+service|kickstart|daemon|'
                          r'cron|launchagent|deploy\s+pipeline)\b'),
    ("model swap",       r'\b(replace\s+model|swap\s+model|change\s+the\s+model|'
                         r'default\s+model)\b'),
]

# Tier 0 (auto): additive, reversible, already the established auto-boundary.
_AUTO_PATTERNS = [
    ("add monitoring source", r'\b(add|subscribe|follow|monitor|track)\b[^.]*?'
                              r'\b(rss|feed|podcast|newsletter|blog|substack|source)\b'),
]

# Hardware / environment asks — Buddy provisions these; not a code task at all.
_HARDWARE_RX = re.compile(
    r'\b(gpu|vram|unified\s+memory|\bdgx\b|spark|mac\s+studio|rtx|blackwell|'
    r'\bm5\b|dedicated\s+(server|machine|box|rig)|hardware)\b', re.IGNORECASE)

# Map the self-review item "type" to a baseline tier before keyword overrides.
_TYPE_BASELINE = {
    "ADD_SOURCE":         TIER_AUTO,
    "PULL_MODEL":         TIER_CONFIRM,   # reversible, but resource-heavy
    "CAPABILITY_REQUEST": TIER_CONFIRM,   # e.g. `pip install x` — unless hardware
    "CIRRUS_NOTE":        TIER_CONFIRM,   # a suggested enhancement
}

_URL_RX = re.compile(r'https?://[^\s`\'")\]]+')


def classify_risk(ptype: str, detail: str, source_line: str = ""):
    """Return (tier:int, reason:str). NEVER patterns always win, then hardware,
    then Tier-2 design patterns, then Tier-0 auto patterns, else the type
    baseline (defaulting to Tier 1 — a human-confirmed code change)."""
    blob = f"{detail} {source_line}".lower()

    for label, rx in _NEVER_PATTERNS:
        if re.search(rx, blob, re.IGNORECASE):
            return TIER_NEVER, f"matches never-auto category: {label}"

    if ptype == "CAPABILITY_REQUEST" and _HARDWARE_RX.search(blob):
        return TIER_NEVER, "hardware/environment provisioning (human only)"

    for label, rx in _DESIGN_PATTERNS:
        if re.search(rx, blob, re.IGNORECASE):
            return TIER_DESIGN, f"touches critical path: {label}"

    for label, rx in _AUTO_PATTERNS:
        if re.search(rx, blob, re.IGNORECASE):
            return TIER_AUTO, f"additive & reversible: {label}"

    baseline = _TYPE_BASELINE.get(ptype, TIER_CONFIRM)
    return baseline, f"type baseline for {ptype or 'unknown'}"


def _guess_files(ptype: str, detail: str):
    """Best-effort guess of which files a proposal would touch — a hint for the
    (future) dev agent, not a contract. Conservative: empty when unsure."""
    b = detail.lower()
    files = []
    if ptype == "ADD_SOURCE" or re.search(r'\b(rss|feed|source)\b', b):
        files.append("config/sources.local.json")
    if re.search(r'\bdigest|summar|dedupe|article\b', b):
        files.append("cirrus_daily.py")
    if re.search(r'\baction\s+item|extract\b', b):
        files.append("extract_actions.py")
    if re.search(r'\bemail|send\b', b):
        files.append("send_digest.py")
    if re.search(r'\btelegram|bot|/approve|command\b', b):
        files.append("cirrus_bot.py")
    if re.search(r'\bmodel|qwen|ollama|pull\b', b):
        files.append("config/sources.json")
    return sorted(set(files))


def make_spec(item: dict, idx: int, date: str = None):
    """Attach a machine-actionable spec to a self-review proposal.

    item: {type, detail, source_line?}  →  returns the dev_spec dict.
    The spec is additive metadata; existing /approve consumers ignore it."""
    date = date or datetime.now().strftime("%Y-%m-%d")
    ptype = item.get("type", "")
    detail = item.get("detail", "")
    source_line = item.get("source_line", "")
    tier, reason = classify_risk(ptype, detail, source_line)

    # Default test plan is the dry-run harness; NEVER/hardware items are not built.
    if tier == TIER_NEVER:
        test_plan = "n/a — human-only; do not automate"
        rollback = "n/a"
    elif tier == TIER_AUTO:
        test_plan = "validate source URL resolves; already reversible (overlay)"
        rollback = "remove entry from config/sources.local.json (outside git)"
    else:
        test_plan = ("py_compile changed files → runner daily-dryrun → review "
                     "DRYRUN-daily-*.md for regressions before live deploy")
        rollback = "git revert <sha> in cirrus-repo → deploy (job none)"

    return {
        "id": f"prop-{date}-{idx}",
        "type": ptype,
        "tier": tier,
        "tier_name": TIER_NAME[tier],
        "tier_reason": reason,
        "files_to_change": _guess_files(ptype, detail),
        "test_plan": test_plan,
        "rollback": rollback,
        "auto_eligible": tier in (TIER_AUTO, TIER_CONFIRM),
        "created": date,
        "status": "proposed",
    }


# ── Self-changes ledger ───────────────────────────────────────────────────────
def _ledger_paths(project_dir):
    base = Path(project_dir) / "logs" / "self-changes"
    base.mkdir(parents=True, exist_ok=True)
    return base / "ledger.jsonl", base / "CHANGES.md"


def ledger_append(entry: dict, project_dir):
    """Append one event to the append-only self-changes ledger (JSONL) and
    mirror a human-readable line into CHANGES.md. Every autonomous action —
    proposal, build, test, deploy, rollback — should leave a row here.

    entry should include at least: {event, id, tier_name?, detail?, result?}.
    Returns the JSONL path written."""
    jsonl, md = _ledger_paths(project_dir)
    row = dict(entry)
    row.setdefault("ts", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    with open(jsonl, "a") as f:
        f.write(json.dumps(row) + "\n")

    if not md.exists():
        md.write_text("# CIRRUS self-changes ledger\n\n"
                      "Append-only audit trail of the Autonomous Dev-Loop. "
                      "Newest at the bottom.\n\n"
                      "| when | event | id | tier | detail | result |\n"
                      "|------|-------|----|------|--------|--------|\n")
    detail = str(row.get("detail", ""))[:80].replace("|", "/").replace("\n", " ")
    result = str(row.get("result", ""))[:40].replace("|", "/").replace("\n", " ")
    with open(md, "a") as f:
        f.write(f"| {row['ts']} | {row.get('event','')} | {row.get('id','')} "
                f"| {row.get('tier_name','')} | {detail} | {result} |\n")
    return jsonl


# ── Self-test (no network, no LLM) ────────────────────────────────────────────
def _selftest():
    import tempfile, os
    cases = [
        ({"type": "CIRRUS_NOTE", "detail": "reset the SMTP password for the sender"}, TIER_NEVER),
        ({"type": "CIRRUS_NOTE", "detail": "delete old digests with rm -rf"}, TIER_NEVER),
        ({"type": "CAPABILITY_REQUEST", "detail": "buy a DGX Spark for testing"}, TIER_NEVER),
        ({"type": "CAPABILITY_REQUEST", "detail": "we need 96GB VRAM GPU"}, TIER_NEVER),
        ({"type": "CIRRUS_NOTE", "detail": "refactor the send_digest email delivery path"}, TIER_DESIGN),
        ({"type": "CIRRUS_NOTE", "detail": "add a sqlite schema migration"}, TIER_DESIGN),
        ({"type": "ADD_SOURCE", "detail": "subscribe to the MLQ.ai RSS feed"}, TIER_AUTO),
        ({"type": "CAPABILITY_REQUEST", "detail": "pip install selenium"}, TIER_CONFIRM),
        ({"type": "CIRRUS_NOTE", "detail": "improve article dedupe in the digest"}, TIER_CONFIRM),
        ({"type": "PULL_MODEL", "detail": "pull deepseek-v4"}, TIER_CONFIRM),
    ]
    ok = 0
    for item, want in cases:
        got, reason = classify_risk(item["type"], item["detail"])
        status = "OK " if got == want else "FAIL"
        if got == want:
            ok += 1
        print(f"  [{status}] want={TIER_NAME[want]:<26} got={TIER_NAME[got]:<26} "
              f":: {item['detail'][:45]}  ({reason})")
    print(f"\nclassify_risk: {ok}/{len(cases)} passed")

    # make_spec shape
    spec = make_spec({"type": "CIRRUS_NOTE", "detail": "improve digest dedupe"}, 1, "2026-07-14")
    assert spec["id"] == "prop-2026-07-14-1"
    assert spec["tier"] == TIER_CONFIRM and spec["auto_eligible"]
    assert "cirrus_daily.py" in spec["files_to_change"]
    assert "daily-dryrun" in spec["test_plan"]
    print("make_spec: shape OK ->", spec["id"], spec["tier_name"], spec["files_to_change"])

    # never-auto spec must not be auto-eligible
    nspec = make_spec({"type": "CIRRUS_NOTE", "detail": "rotate the api token"}, 2, "2026-07-14")
    assert nspec["tier"] == TIER_NEVER and not nspec["auto_eligible"]
    print("make_spec: never-auto guard OK ->", nspec["tier_name"])

    # ledger round-trip in a temp project dir
    with tempfile.TemporaryDirectory() as td:
        ledger_append({"event": "proposal", "id": spec["id"],
                       "tier_name": spec["tier_name"], "detail": "improve digest dedupe",
                       "result": "queued"}, td)
        jsonl, md = _ledger_paths(td)
        rows = [json.loads(l) for l in jsonl.read_text().splitlines()]
        assert rows and rows[0]["id"] == spec["id"]
        assert "self-changes ledger" in md.read_text()
        print("ledger_append: JSONL + markdown OK ->", os.path.basename(str(jsonl)))

    print("\nALL SELF-TESTS PASSED" if ok == len(cases) else "\nSOME CLASSIFIER CASES FAILED")


if __name__ == "__main__":
    _selftest()
