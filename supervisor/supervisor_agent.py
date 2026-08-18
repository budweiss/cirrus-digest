"""CUMULUS supervisor agent (B1) — v1 run-loop.

Cheap deterministic heartbeat every HEARTBEAT_INTERVAL_SEC (no LLM call, see
heartbeat.py). Once a day (after DAILY_CHECK_HOUR) OR immediately if the
heartbeat finds something off, escalates to a claude-agent-sdk reasoning pass
with the tool registry (tools.py) so it can investigate, act within
TIER_AUTO, and notify. Every reasoning-pass call is gated by budget.py's
$5/day cap (CUMULUS.md sec 3) — when the cap is hit, no LLM call is made and
a deterministic-only Telegram note is sent instead.

Confirmed live against claude-agent-sdk 0.2.137 (S63 smoke test): tool
names are exposed to the model as mcp__<server>__<tool>, permission_mode
"bypassPermissions" is required since there's no human to approve a prompt,
and ResultMessage.total_cost_usd gives the real spend per call directly —
no need to compute it from token counts.

CONTROLS: the actual operating contract (identity, tool allowlist, autonomy
tiers, cost discipline, secrets rule) lives in CLAUDE.md, in this same
directory — NOT inline here. The SDK loads it automatically as project
context via cwd + setting_sources=["project"] (confirmed live, see
https://code.claude.com/docs/en/agent-sdk/modifying-system-prompts — CLAUDE.md
injects into the conversation independently of systemPrompt, so it applies
regardless of the custom string below). Edit CLAUDE.md to change the agent's
actual rules; SYSTEM_PROMPT below is just per-invocation framing, deliberately
thin so there's exactly one place — CLAUDE.md — that's the source of truth
for what this agent is allowed to do. Keep CLAUDE.md in sync with the real
tool set / sudoers allowlist in tools.py — it deliberately does NOT mirror
~/Documents/Cowork/CUMULUS.md verbatim, since that broader design doc
describes future capabilities (local-model routing, general sudo, CIRRUS
access) this v1 skeleton does not have.
"""
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions, ResultMessage, create_sdk_mcp_server, query, tool,
)

import budget
import heartbeat
import opus_approval
import tools
from ledger import ledger_append

APP_DIR = Path("/opt/cumulus-supervisor/app")
SECRETS_PATH = Path("/opt/cumulus-supervisor/state/secrets.json")
STATE_DIR = Path("/opt/cumulus-supervisor/state")
LAST_DAILY_FILE = STATE_DIR / "last-daily-check.txt"
LAST_SKIP_NOTIFY_FILE = STATE_DIR / "last-skip-notify.txt"

HEARTBEAT_INTERVAL_SEC = 60
# S67 BUG FIX: was 8, commented "after the 05:30-06:00 client jobs" -- but
# cirrus-hoaleads (Bill's HOA research, added S65) runs at 09:20, so the
# once-daily deep check reviewed a day in which the LAST client job had not run
# yet. Even a perfect reasoning pass was looking at the wrong window. Set after
# the latest client job; raise this whenever a later one is added.
DAILY_CHECK_HOUR = 10      # local time, after the 09:20 hoaleads run
SKIP_NOTIFY_MIN_GAP_SEC = 3600  # don't Telegram-spam a budget-cap skip more than hourly

SYSTEM_PROMPT = """You are the CUMULUS supervisor agent (B1, v1 skeleton).
Your operating contract — what you can do, your autonomy tiers, cost
discipline, secrets handling — is in CLAUDE.md, loaded as project context
alongside this message. Follow it. This message only gives you this run's
trigger reason; CLAUDE.md governs everything else.
"""


def _load_secrets() -> dict:
    with open(SECRETS_PATH) as f:
        return json.load(f)


def _build_mcp_tools():
    @tool("check_service_status", "Check a systemd unit's status (read-only)",
          {"unit": str})
    async def _check_service_status(args):
        return {"content": [{"type": "text", "text": tools.check_service_status(args["unit"])}]}

    @tool("check_timers", "List all systemd timers (read-only)", {})
    async def _check_timers(args):
        return {"content": [{"type": "text", "text": tools.check_timers()}]}

    @tool("tail_journal", "Tail recent journal output for a unit (read-only)",
          {"unit": str, "lines": int})
    async def _tail_journal(args):
        return {"content": [{"type": "text",
                              "text": tools.tail_journal(args["unit"], args.get("lines", 40))}]}

    @tool("check_credentials_health",
          "Verify CUMULUS credentials.json currently parses (read-only, never returns values)", {})
    async def _check_credentials_health(args):
        return {"content": [{"type": "text", "text": tools.check_credentials_health()}]}

    @tool("check_cirrus_timemachine",
          "Check CIRRUS's Time Machine backup health over its admin API (read-only, "
          "scoped token — cannot reach anything else on CIRRUS)", {})
    async def _check_cirrus_timemachine(args):
        return {"content": [{"type": "text", "text": tools.check_cirrus_timemachine()}]}

    @tool("restart_service",
          "Restart an allow-listed systemd unit (reversible, TIER_AUTO)", {"unit": str})
    async def _restart_service(args):
        return {"content": [{"type": "text", "text": tools.restart_service(args["unit"])}]}

    @tool("reset_failed",
          "Clear an allow-listed systemd unit's failed state (reversible, TIER_AUTO)", {"unit": str})
    async def _reset_failed(args):
        return {"content": [{"type": "text", "text": tools.reset_failed(args["unit"])}]}

    @tool("send_telegram",
          "Send Buddy a one-way notification summarizing this check (notify-only, no reply expected)",
          {"message": str})
    async def _send_telegram(args):
        return {"content": [{"type": "text", "text": tools.send_telegram(args["message"])}]}

    @tool("request_opus_upgrade",
          "Ask Buddy's permission (via Telegram) to use Opus starting next pass, for a task that "
          "genuinely seems to need deeper reasoning than Sonnet can give it", {"reason": str})
    async def _request_opus_upgrade(args):
        return {"content": [{"type": "text", "text": tools.request_opus_upgrade(args["reason"])}]}

    @tool("request_guidance",
          "Ask Buddy for actual direction (via Telegram) when genuinely stuck: you've tried your "
          "allowed diagnostics/fixes, the problem persists, and no remaining tool can address it. "
          "NOT for routine anomalies you can already report via send_telegram. His free-text reply "
          "is handed to you at the start of your next run.", {"issue": str, "question": str})
    async def _request_guidance(args):
        return {"content": [{"type": "text",
                              "text": tools.request_guidance(args["issue"], args["question"])}]}

    return [_check_service_status, _check_timers, _tail_journal,
            _check_credentials_health, _check_cirrus_timemachine,
            _restart_service, _reset_failed, _send_telegram,
            _request_opus_upgrade, _request_guidance]


async def run_reasoning_pass(reason: str) -> float:
    """Runs one claude-agent-sdk reasoning pass. Returns cost in USD.

    S64: model is explicitly pinned (was previously unset -- silently riding
    the SDK's default rather than a deliberate choice). Defaults to Sonnet;
    upgrades to Opus for exactly ONE pass if opus_approval.consume_approval()
    finds Buddy approved a pending request via Telegram reply -- consuming it
    means the pass after this one reverts to Sonnet automatically, so an
    upgrade never silently persists."""
    secrets = _load_secrets()
    mcp_tools = _build_mcp_tools()
    server = create_sdk_mcp_server(name="supervisor", tools=mcp_tools)
    allowed = [f"mcp__supervisor__{t.name}" for t in mcp_tools]

    upgraded = opus_approval.consume_opus_approval()
    guidance = opus_approval.consume_guidance()
    model = "opus" if upgraded else "sonnet"

    options = ClaudeAgentOptions(
        mcp_servers={"supervisor": server},
        allowed_tools=allowed,
        permission_mode="bypassPermissions",
        system_prompt=SYSTEM_PROMPT,
        model=model,
        max_turns=12,
        max_budget_usd=3.00 if upgraded else 1.00,
        env={"ANTHROPIC_API_KEY": secrets["anthropic_api_key"]},
        cwd=str(APP_DIR),
        setting_sources=["project"],  # loads CLAUDE.md from cwd (APP_DIR) as project context
    )

    prompt = f"Reasoning pass triggered because: {reason}. Do your check now."
    if upgraded:
        prompt += (" NOTE: Buddy approved an Opus upgrade for this pass "
                   "(your prior request). This is a one-time upgrade -- "
                   "you're back on Sonnet next time.")
    if guidance:
        prompt += (f" NOTE: Buddy replied to your prior request_guidance escalation "
                   f"with: \"{guidance}\" -- act on this before anything else this run.")
    cost = 0.0
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, ResultMessage):
            cost = msg.total_cost_usd or 0.0
    return cost


def _daily_check_due() -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    if datetime.now().hour < DAILY_CHECK_HOUR:
        return False
    return not (LAST_DAILY_FILE.exists() and LAST_DAILY_FILE.read_text().strip() == today)


def _mark_daily_done():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LAST_DAILY_FILE.write_text(datetime.now().strftime("%Y-%m-%d"))


def _should_notify_skip() -> bool:
    if not LAST_SKIP_NOTIFY_FILE.exists():
        return True
    try:
        last = float(LAST_SKIP_NOTIFY_FILE.read_text().strip())
    except ValueError:
        return True
    return (time.time() - last) >= SKIP_NOTIFY_MIN_GAP_SEC


def _mark_skip_notified():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LAST_SKIP_NOTIFY_FILE.write_text(str(time.time()))


def _handle_trigger(reason: str, is_daily: bool):
    allowed, spent, why = budget.allow(est_cost_usd=1.00)
    if allowed:
        cost = asyncio.run(run_reasoning_pass(reason))
        budget.record(cost, detail=reason)
        ledger_append({"event": "reasoning-pass", "tool": "supervisor_agent",
                       "tier_name": "n/a", "detail": reason,
                       "result": f"cost=${cost:.4f}"})
    else:
        ledger_append({"event": "reasoning-pass-skipped", "tool": "supervisor_agent",
                       "tier_name": "n/a", "detail": reason, "result": why})
        if _should_notify_skip():
            tools.send_telegram(
                f"CUMULUS supervisor: {reason}. Skipping AI review — this month's "
                f"${budget.MONTHLY_CAP_USD:.2f} cap reached (${spent:.2f} spent).")
            _mark_skip_notified()
    if is_daily:
        _mark_daily_done()


def main_loop():
    print("CUMULUS supervisor starting", flush=True)
    while True:
        hb = heartbeat.run_heartbeat()
        opus_approval.check_for_reply()  # cheap no-op unless a request is pending
        if not hb["ok"]:
            _handle_trigger(f"heartbeat found an issue: {hb['detail']}", is_daily=False)
        elif _daily_check_due():
            _handle_trigger("scheduled daily check", is_daily=True)
        time.sleep(HEARTBEAT_INTERVAL_SEC)


if __name__ == "__main__":
    main_loop()
