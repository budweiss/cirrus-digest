"""agent.py -- Alopecia etiology-synthesis agent (S177), the run-loop.

Wakes ONCE DAILY (a systemd timer, scheduled after the 05:45 collector),
unlike Skywarden's continuous 60s heartbeat -- nothing here is time-
critical, so a plain scheduled single-shot pass is the right shape, not a
perpetual process.

Runs as buddy, directly in this git checkout (unlike Skywarden, which is
provisioned into an isolated /opt tree under its own OS account) -- see
tools.py's module docstring for why. cwd is THIS directory, so the SDK
loads alopecia_agent/CLAUDE.md as project context via
setting_sources=["project"], exactly the mechanism supervisor_agent.py
documents and uses (see its own docstring / the Agent SDK docs link
there) -- same idea, different directory, no /opt involved.

    python3 agent.py               # a real scheduled run
    python3 agent.py --dry-run     # reasons normally but does not write
                                    # anything -- read CLAUDE.md's dry-run
                                    # note before trusting a real run
"""
import asyncio
import json
import sys
from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions, ResultMessage, create_sdk_mcp_server, query, tool,
)

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

from alopecia_agent import budget, tools           # noqa: E402
from alopecia_agent.ledger import ledger_append    # noqa: E402

CREDS_PATH = PROJECT_DIR / "config" / "credentials.json"
EST_COST_PER_RUN_USD = 2.00

SYSTEM_PROMPT = """You are the Alopecia etiology-synthesis agent (S177, v1).
Your operating contract -- what you can do, the non-medical-advice boundary,
autonomy tiers -- is in CLAUDE.md, loaded as project context alongside this
message. Follow it. This message only gives you this run's trigger reason;
CLAUDE.md governs everything else.
"""


def _load_creds():
    return json.loads(CREDS_PATH.read_text())


def _build_mcp_tools():
    @tool("read_kb", "Query the grounded Alopecia foundation KB (read-only)",
          {"question": str, "top_k": int})
    async def _read_kb(args):
        return {"content": [{"type": "text",
                              "text": tools.read_kb(args["question"],
                                                    args.get("top_k", 3))}]}

    @tool("read_new_etiology_items",
          "New etiology/cause/trigger-band items collected since this "
          "agent's last run (read-only)", {})
    async def _read_new_etiology_items(args):
        return {"content": [{"type": "text",
                              "text": tools.read_new_etiology_items()}]}

    @tool("read_hypothesis_state",
          "Current ranked, evidence-graded hypotheses -- this agent's own "
          "persistent memory (read-only)", {})
    async def _read_hypothesis_state(args):
        return {"content": [{"type": "text",
                              "text": tools.read_hypothesis_state()}]}

    @tool("write_hypothesis",
          "Create or refine ONE hypothesis. evidence_grade must be A "
          "(controlled trial) through E (unclassified). NEVER phrase "
          "statement as treatment advice -- this is a causation-research "
          "finding, not a recommendation.",
          {"hyp_id": str, "statement": str, "evidence_grade": str,
           "supporting": str, "contradicting": str})
    async def _write_hypothesis(args):
        return {"content": [{"type": "text", "text": tools.write_hypothesis(
            args["hyp_id"], args["statement"], args["evidence_grade"],
            args.get("supporting", ""), args.get("contradicting", ""))}]}

    @tool("mark_run_processed",
          "Advance this agent's cursor to today. Call ONCE, at the end of "
          "a run that actually reviewed the new etiology items -- not if "
          "you skipped review entirely.", {})
    async def _mark_run_processed(args):
        return {"content": [{"type": "text", "text": tools.mark_run_processed()}]}

    @tool("call_local",
          "Cheap local model call (vLLM/ollama, cloud fallback) for "
          "ROUTINE sub-steps: clustering similar items, extracting a claim "
          "from an abstract. Do NOT use this for the actual hypothesis "
          "judgment -- that is call_council's job.",
          {"task_class": str, "prompt": str})
    async def _call_local(args):
        return {"content": [{"type": "text",
                              "text": tools.call_local(args["task_class"],
                                                       args["prompt"])}]}

    @tool("call_council",
          "The actual judgment step: ask the full cloud council (Anthropic "
          "at max effort, plus every other keyed provider including Kimi) "
          "to weigh new evidence against existing hypotheses.", {"prompt": str})
    async def _call_council(args):
        return {"content": [{"type": "text", "text": tools.call_council(args["prompt"])}]}

    @tool("append_to_brief_draft",
          "Append a dated section to the STAGING draft file -- NOT the "
          "real weekly brief, NOT a send. Buddy reviews this file before "
          "any of it is folded into the actual brief.", {"section_text": str})
    async def _append_to_brief_draft(args):
        return {"content": [{"type": "text",
                              "text": tools.append_to_brief_draft(args["section_text"])}]}

    @tool("send_telegram_summary",
          "One-way notification to Buddy -- ONLY when a hypothesis ranking "
          "changed meaningfully. Not for routine per-run noise.", {"message": str})
    async def _send_telegram_summary(args):
        return {"content": [{"type": "text",
                              "text": tools.send_telegram_summary(args["message"])}]}

    @tool("request_guidance",
          "Ask Buddy for actual direction (via Telegram) when genuinely "
          "stuck -- you've read what evidence exists and still can't judge "
          "it, or the question is a decision only Buddy can make. NOT for "
          "routine hypothesis updates. His reply is handed back at the "
          "start of your next run.", {"issue": str, "question": str})
    async def _request_guidance(args):
        return {"content": [{"type": "text",
                              "text": tools.request_guidance(args["issue"],
                                                             args["question"])}]}

    return [_read_kb, _read_new_etiology_items, _read_hypothesis_state,
            _write_hypothesis, _mark_run_processed, _call_local, _call_council,
            _append_to_brief_draft, _send_telegram_summary, _request_guidance]


async def run_reasoning_pass(reason: str, dry_run: bool = False) -> float:
    """Runs one claude-agent-sdk reasoning pass. Returns cost in USD.

    dry_run=True still runs a REAL pass (so the reasoning can be reviewed)
    but the prompt instructs it not to call any of the four write-shaped
    tools -- write_hypothesis, mark_run_processed, append_to_brief_draft,
    send_telegram_summary -- and to describe what it WOULD do instead.
    This is a prompt-level instruction, not a Python-level gate (the tools
    themselves are not separately locked in dry-run mode); the build's own
    checklist is for Buddy to review several real dry-run passes by hand
    before trusting a run to actually write.
    """
    creds = _load_creds()
    mcp_tools = _build_mcp_tools()
    server = create_sdk_mcp_server(name="alopecia", tools=mcp_tools)
    allowed = [f"mcp__alopecia__{t.name}" for t in mcp_tools]

    options = ClaudeAgentOptions(
        mcp_servers={"alopecia": server},
        allowed_tools=allowed,
        permission_mode="bypassPermissions",
        system_prompt=SYSTEM_PROMPT,
        model="sonnet",
        max_turns=15,
        max_budget_usd=EST_COST_PER_RUN_USD,
        env={"ANTHROPIC_API_KEY": creds["anthropic_api_key"]},
        cwd=str(APP_DIR),
        setting_sources=["project"],  # loads CLAUDE.md from cwd (APP_DIR)
    )

    prompt = f"Daily run triggered because: {reason}."
    if dry_run:
        prompt += (" THIS IS A DRY RUN: read and reason as normal, but do "
                   "NOT call write_hypothesis, mark_run_processed, "
                   "append_to_brief_draft, or send_telegram_summary this "
                   "pass -- describe what you WOULD do instead, so Buddy "
                   "can review the reasoning before anything is written.")
    guidance = tools.consume_guidance()
    if guidance:
        prompt += (f" NOTE: Buddy replied to your prior request_guidance "
                   f"escalation with: \"{guidance}\" -- act on this before "
                   f"anything else this run.")

    cost = 0.0
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, ResultMessage):
            cost = msg.total_cost_usd or 0.0
    return cost


def main(dry_run=False):
    allowed, spent, why = budget.allow(est_cost_usd=EST_COST_PER_RUN_USD)
    if not allowed:
        result = tools.send_telegram_summary(
            f"Alopecia agent: skipping today's run -- {why}.")
        ledger_append({"event": "reasoning-pass-skipped", "tool": "agent",
                      "detail": why, "result": result})
        return
    reason = "manual dry-run" if dry_run else "scheduled daily run"
    cost = asyncio.run(run_reasoning_pass(reason, dry_run=dry_run))
    ledger_append({"event": "reasoning-pass", "tool": "agent",
                  "detail": reason, "result": f"cost=${cost:.4f}"})


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
