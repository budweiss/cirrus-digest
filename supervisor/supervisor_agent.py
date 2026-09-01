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
import pathlib
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
# S81: one entry per distinct heartbeat problem -> when it last escalated.
HB_ESCALATION_FILE = STATE_DIR / "last-heartbeat-escalation.json"

HEARTBEAT_INTERVAL_SEC = 60
# S81. A heartbeat problem re-escalates at most this often.
#
# WHY THIS IS NOT OPTIONAL. Until today heartbeat only reported units that were
# ALSO in the restart allowlist, so every failure it saw, it could fix -- and it
# fixed them within one tick (cirrus-modelhealth failed at 05:30 and was healed
# by 05:31). Persistence was impossible by construction, so the loop needed no
# cooldown and had none: `if not hb["ok"]` fires a full reasoning pass EVERY
# 60 SECONDS.
#
# Widening the scan to every failed unit on the box (heartbeat._list_failed_units)
# breaks that assumption on purpose: we now see units we deliberately cannot
# restart, and those STAY failed until a human acts. At ~$0.25 a pass that is
# ~$15/hour, and the $150 monthly cap would be gone in about ten hours -- which
# would leave Skywarden unable to think about anything else for the rest of the
# month. Better detection with no cooldown is not an improvement, it is an
# outage with a different name.
#
# A NEW problem still escalates immediately; only a repeat of the SAME signature
# waits. 6h means an unattended failure still gets four nudges a day.
HB_ESCALATION_COOLDOWN_SEC = 6 * 3600
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

    @tool("check_open_client_promises",
          "Check what we have told a client we would do and not yet delivered "
          "(read-only). Reports overdue promises; you cannot deliver them "
          "yourself — client work is never your call. Report to Buddy, or use "
          "request_guidance if it is unclear what is blocking.", {})
    async def _check_open_client_promises(args):
        return {"content": [{"type": "text",
                              "text": tools.check_open_client_promises()}]}

    @tool("check_duplicate_client_answers",
          "Check whether a client was sent the same answer twice on one thread "
          "(read-only). Reports only — re-answering or correcting a client is a "
          "client send, which is never your call.", {"hours": int})
    async def _check_duplicate_client_answers(args):
        return {"content": [{"type": "text",
                              "text": tools.check_duplicate_client_answers(
                                  args.get("hours", 168))}]}

    @tool("check_thread_stalls",
          "Check for client messages with no substantive reply (an ack does not "
          "count) (read-only). Hits marked 'queued as build/research' are "
          "expected; 'REPLY EXPECTED' is the one that matters. You cannot answer "
          "a client — report it, or use request_guidance if it is unclear what "
          "is blocking.", {"hours": int})
    async def _check_thread_stalls(args):
        return {"content": [{"type": "text",
                              "text": tools.check_thread_stalls(args.get("hours", 48))}]}

    @tool("check_high_value_field_overwrites",
          "Check whether a researched field (board contact, email, management "
          "company) on a warm-or-better client lead was overwritten with a "
          "different value (read-only). ESCALATE, NEVER REVERT — which value is "
          "correct is a judgment call about client data.", {"hours": int})
    async def _check_high_value_field_overwrites(args):
        return {"content": [{"type": "text",
                              "text": tools.check_high_value_field_overwrites(
                                  args.get("hours", 168))}]}

    @tool("check_intake_health",
          "Check whether client intake is actually WORKING on both boxes, not "
          "merely running (read-only). Intake is a KeepAlive loop, so service "
          "status reports healthy even when every iteration is failing — this "
          "reads when it last COMPLETED a poll. A stale intake means client "
          "mail is arriving and being silently ignored.", {"stale_minutes": int})
    async def _check_intake_health(args):
        return {"content": [{"type": "text",
                              "text": tools.check_intake_health(
                                  args.get("stale_minutes", 40))}]}

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

    @tool("file_repair_ticket",
          "File a repair ticket so the dev-loop can write an actual CODE FIX for a unit that a "
          "restart cannot repair. Call this after you have confirmed a failed unit, tried "
          "restart_service, and it failed again — that means the fault is in the code, not in a "
          "dead process. Pass the diagnosis you already gathered (what the unit does, the failing "
          "line from tail_journal, what you tried); that text is what gets built against. Nothing "
          "ships from this: at most it becomes a patch awaiting Buddy's one tap. Send him a short "
          "send_telegram as well, so he knows the repair is queued.",
          {"unit": str, "diagnosis": str})
    async def _file_repair_ticket(args):
        return {"content": [{"type": "text",
                              "text": tools.file_repair_ticket(args["unit"],
                                                               args["diagnosis"])}]}

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
            _check_open_client_promises, _check_duplicate_client_answers,
            _check_thread_stalls, _check_high_value_field_overwrites,
            _check_intake_health,
            _check_credentials_health, _check_cirrus_timemachine,
            _restart_service, _reset_failed, _file_repair_ticket, _send_telegram,
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


def _hb_signature(hb: dict) -> str:
    """What is wrong, stably, so a repeat can be recognised.

    Deliberately NOT hb["detail"] -- that carries counts and free text which
    drift between ticks, and a signature that changes every minute is the same
    as no cooldown at all.
    """
    parts = ["units:" + ",".join(sorted(hb.get("failed_units") or []))]
    if not hb.get("credentials_ok", True):
        parts.append("creds")
    if hb.get("scan_degraded"):
        parts.append("scan-degraded")
    comp = hb.get("completeness") or {}
    parts.append("stalled:" + ",".join(sorted(x.get("job", "")
                                              for x in comp.get("stalled") or [])))
    parts.append("unreadable:" + ",".join(sorted(comp.get("unreadable") or [])))
    return "|".join(parts)


def _should_escalate_hb(sig: str, now: float = None) -> bool:
    """First sighting escalates at once; a repeat waits out the cooldown."""
    now = time.time() if now is None else now
    try:
        seen = json.loads(HB_ESCALATION_FILE.read_text())
    except Exception:
        seen = {}
    last = seen.get(sig)
    if last is not None and (now - float(last)) < HB_ESCALATION_COOLDOWN_SEC:
        return False
    seen[sig] = now
    # Forget signatures older than a day so the file cannot grow without bound
    # and a problem that returns next week is treated as new.
    seen = {k: v for k, v in seen.items()
            if (now - float(v)) < max(HB_ESCALATION_COOLDOWN_SEC * 4, 86400)}
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        HB_ESCALATION_FILE.write_text(json.dumps(seen))
    except Exception:
        pass          # a state file we cannot write must not stop the alert
    return True


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
            # S81: same problem, still there -> stay quiet until the cooldown
            # expires. Without this a unit we cannot auto-restart would fire a
            # paid reasoning pass every 60s and exhaust the monthly cap in a
            # single afternoon.
            if _should_escalate_hb(_hb_signature(hb)):
                _handle_trigger(f"heartbeat found an issue: {hb['detail']}",
                                is_daily=False)
            elif _daily_check_due():
                _handle_trigger("scheduled daily check", is_daily=True)
        elif _daily_check_due():
            _handle_trigger("scheduled daily check", is_daily=True)
        time.sleep(HEARTBEAT_INTERVAL_SEC)


# ── selftest ──────────────────────────────────────────────────────────────────
def selftest() -> bool:
    """S81: the escalation cooldown, which is the thing standing between a
    widened failure scan and a burned monthly budget."""
    global HB_ESCALATION_FILE
    import tempfile
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))

    saved = HB_ESCALATION_FILE
    tmp = pathlib.Path(tempfile.mkdtemp()) / "esc.json"
    HB_ESCALATION_FILE = tmp
    try:
        hb_scout = {"failed_units": ["opportunity-scout.service"],
                    "credentials_ok": True, "scan_degraded": False,
                    "completeness": {"stalled": [], "unreadable": []}}
        sig = _hb_signature(hb_scout)

        t0 = 1_000_000.0
        ck("a NEW problem escalates immediately", _should_escalate_hb(sig, t0))
        ck("the same problem one minute later does NOT",
           not _should_escalate_hb(sig, t0 + 60))
        ck("...nor an hour later", not _should_escalate_hb(sig, t0 + 3600))
        ck("...but it does once the cooldown expires",
           _should_escalate_hb(sig, t0 + HB_ESCALATION_COOLDOWN_SEC + 1))

        # THE budget arithmetic this exists for: 60s ticks over 6h would be 360
        # paid passes; the cooldown must allow exactly one.
        fired = sum(1 for i in range(360)
                    if _should_escalate_hb(sig, t0 + 100_000 + i * 60))
        ck(f"6h of 60s ticks on one problem escalates ONCE (got {fired})",
           fired == 1)

        # A DIFFERENT problem must not be silenced by an unrelated cooldown.
        hb_creds = dict(hb_scout, credentials_ok=False)
        ck("a different problem escalates even while the first is cooling",
           _should_escalate_hb(_hb_signature(hb_creds), t0 + 100_000 + 60))
        ck("signatures of different problems differ",
           _hb_signature(hb_creds) != sig)

        # The signature must be stable across ticks, or the cooldown is a no-op.
        ck("the signature ignores free-text detail drift",
           _hb_signature(dict(hb_scout, detail="failed units: x (seen 3x)")) == sig)
        ck("a second failed unit IS a different signature",
           _hb_signature(dict(hb_scout,
                              failed_units=["opportunity-scout.service",
                                            "cirrus-api.service"])) != sig)
        ck("unit order does not change the signature",
           _hb_signature(dict(hb_scout, failed_units=["b.service", "a.service"]))
           == _hb_signature(dict(hb_scout, failed_units=["a.service", "b.service"])))

        # An unwritable state file must not swallow the alert.
        HB_ESCALATION_FILE = pathlib.Path("/nonexistent-dir-s81/esc.json")
        ck("an unwritable state file still lets the alert through",
           _should_escalate_hb("anything", t0))
    finally:
        HB_ESCALATION_FILE = saved

    bad = 0
    for name, ok in checks:
        print(("  ok   " if ok else "  FAIL ") + name)
        bad += 0 if ok else 1
    print()
    print("all supervisor_agent selftests passed" if not bad else f"{bad} FAILED")
    return bad == 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(0 if selftest() else 1)
    main_loop()
