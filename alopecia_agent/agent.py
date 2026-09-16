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
from datetime import datetime
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock,
    create_sdk_mcp_server, query, tool,
)

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

from alopecia_agent import budget, tools           # noqa: E402
from alopecia_agent.ledger import ledger_append, STATE_DIR  # noqa: E402

CREDS_PATH = PROJECT_DIR / "config" / "credentials.json"
EST_COST_PER_RUN_USD = 2.00
TRANSCRIPT_DIR = STATE_DIR / "transcripts"

SYSTEM_PROMPT = """You are the Alopecia etiology-synthesis agent (S177, v1).
Your operating contract -- what you can do, the non-medical-advice boundary,
autonomy tiers -- is in CLAUDE.md, loaded as project context alongside this
message. Follow it. This message only gives you this run's trigger reason;
CLAUDE.md governs everything else.
"""


def _load_creds():
    return json.loads(CREDS_PATH.read_text())


def _build_mcp_tools(dry_run=False):
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
          "(controlled trial) through E (unclassified). supporting/"
          "contradicting: put each distinct item (a citation key or a "
          "longer note) on its OWN LINE -- do not separate items with "
          "commas, since a note's own prose may contain commas. NEVER "
          "phrase statement as treatment advice -- this is a "
          "causation-research finding, not a recommendation.",
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

    if dry_run:
        return [_read_kb, _read_new_etiology_items, _read_hypothesis_state, _call_local, _call_council]
    return [_read_kb, _read_new_etiology_items, _read_hypothesis_state,
            _write_hypothesis, _mark_run_processed, _call_local, _call_council,
            _append_to_brief_draft, _send_telegram_summary, _request_guidance]


async def run_reasoning_pass(reason: str, dry_run: bool = False) -> float:
    """Runs one claude-agent-sdk reasoning pass. Returns cost in USD.

    Dry runs expose only read/reasoning tools. Audit, transcript and paid
    usage records are retained; project mutations and sends are unavailable.
    """
    creds = _load_creds()
    mcp_tools = _build_mcp_tools(dry_run=dry_run)
    server = create_sdk_mcp_server(name="alopecia", tools=mcp_tools)
    allowed = [f"mcp__alopecia__{t.name}" for t in mcp_tools]

    options = ClaudeAgentOptions(
        tools=[],  # Only the explicit MCP tools; no inherited shell/file tools.
        strict_mcp_config=True,
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
    guidance = None if dry_run else tools.consume_guidance()
    if guidance:
        prompt += (f" NOTE: Buddy replied to your prior request_guidance "
                   f"escalation with: \"{guidance}\" -- act on this before "
                   f"anything else this run.")

    # S177: found live on the agent's own first dry run -- the ledger only
    # ever logged terse tool-call summaries (by design, see tools.py's
    # _log(), truncated to keep the audit trail scannable), so there was
    # NOTHING durable anywhere showing what the model actually said: no
    # narrative between tool calls, no final summary. Buddy reviewing a
    # dry run needs exactly that, so it's captured here and written to its
    # own transcript file -- ResultMessage.total_cost_usd was the only
    # field this used to read; .result (the final assistant text) and the
    # running AssistantMessage/TextBlock narrative were both being
    # silently discarded.
    import llm_budget
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    run_id = "alopecia-sdk:" + stamp
    cost = 0.0
    final_result = ""
    narrative = []
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    narrative.append(block.text.strip())
        elif isinstance(msg, ResultMessage):
            if msg.total_cost_usd is None:
                raise ValueError("SDK result omitted cost; accounting incomplete")
            cost = msg.total_cost_usd
            llm_budget.record_sdk_cost(creds, cost, task="alopecia-agent:coordinator",
                                       run_id=run_id, app_dir=str(PROJECT_DIR))
            if getattr(msg, "is_error", False):
                raise RuntimeError("SDK reasoning pass failed; its cost was recorded")
            final_result = msg.result or ""

    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / f"{'dryrun' if dry_run else 'run'}-{stamp}.md"
    parts = [f"# Alopecia agent run — {stamp}", "",
            f"Trigger: {reason}", f"Dry run: {dry_run}", f"Cost: ${cost:.4f}", "",
            "## Narrative (between tool calls)", ""]
    parts += (narrative or ["_(none -- the model went straight to tool calls "
                            "with no intervening text)_"])
    parts += ["", "## Final summary", "",
             final_result or "_(ResultMessage carried no .result text)_"]
    transcript_path.write_text("\n\n".join(parts) + "\n")
    return cost, transcript_path


def _job_status_record(ok, note):
    """Best-effort, never raises -- mirrors alopecia_collect.py/
    alopecia_brief.py's own pattern. Only called for REAL scheduled runs
    (dry_run=False): a manual dry-run test completing must not read as
    "the job ran today" and mask a real scheduled failure the same day."""
    try:
        import job_status
        job_status.record("alopeciaagent", ok, note)
    except Exception as e:
        print(f"job_status.record failed: {e}")


def main(dry_run=False):
    allowed, spent, why = budget.allow(est_cost_usd=EST_COST_PER_RUN_USD)
    if not allowed:
        result = "dry-run budget blocked" if dry_run else tools.send_telegram_summary(
            f"Alopecia agent: skipping today's run -- {why}.")
        ledger_append({"event": "reasoning-pass-skipped", "tool": "agent",
                      "detail": why, "result": result})
        # S177: recorded as OK, not a failure -- a budget cap correctly
        # doing its job is not the same as the job being broken, same
        # reasoning alopeciabrief's own send_guard-blocked path uses. The
        # note still says WHY, so a human reading job_status sees the real
        # reason rather than a bare "ok".
        if not dry_run:
            _job_status_record(True, f"skipped (budget): {why}")
        return
    reason = "manual dry-run" if dry_run else "scheduled daily run"
    cost, transcript_path = asyncio.run(run_reasoning_pass(reason, dry_run=dry_run))
    ledger_append({"event": "reasoning-pass", "tool": "agent", "detail": reason,
                  "result": f"cost=${cost:.4f} transcript={transcript_path}"})
    print(f"transcript: {transcript_path}")
    if not dry_run:
        _job_status_record(True, f"ran, cost=${cost:.4f}")


# ── selftest ──────────────────────────────────────────────────────────────────
def selftest():
    """Offline: no network, no live SDK call, no live Telegram/job_status (T32).
    Tests main()'s decision logic and _job_status_record's safety net --
    NOT run_reasoning_pass itself (that drives a real claude_agent_sdk
    query(), same reason supervisor_agent.py's own selftest tests its pure
    decision functions like _hb_signature/_should_escalate_hb but not
    run_reasoning_pass there either)."""
    import types
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
        ok = ok and cond

    g = globals()
    saved_run_pass = g["run_reasoning_pass"]
    saved_ledger_append = g["ledger_append"]
    saved_job_status_mod = sys.modules.get("job_status")
    saved_allow = budget.allow
    saved_send_telegram = tools.send_telegram_summary

    recorded = []
    ledgered = []
    g["ledger_append"] = lambda entry: ledgered.append(entry)
    fake_job_status = types.ModuleType("job_status")
    fake_job_status.record = lambda name, ok_, note: recorded.append((name, ok_, note))

    def fake_job_status_raises(name, ok_, note):
        raise RuntimeError("simulated job_status failure")

    telegram_sent = []

    try:
        sys.modules["job_status"] = fake_job_status
        tools.send_telegram_summary = lambda msg: (telegram_sent.append(msg) or "sent")

        _job_status_record(True, "test note")
        check("_job_status_record: passes through to job_status.record with "
              "the fixed job name 'alopeciaagent'",
              recorded == [("alopeciaagent", True, "test note")])

        fake_job_status.record = fake_job_status_raises
        try:
            _job_status_record(True, "x")
            _raised = False
        except Exception:
            _raised = True
        check("_job_status_record: a job_status failure is swallowed, "
              "never raises (a monitoring write must not break the job "
              "it is monitoring)", not _raised)
        fake_job_status.record = lambda name, ok_, note: recorded.append((name, ok_, note))

        # ── main(): budget-disallowed skip ──
        recorded.clear()
        telegram_sent.clear()
        budget.allow = lambda est_cost_usd: (False, 12.34, "cap reached")
        main(dry_run=False)
        check("main(): budget skip sends a Telegram explaining why",
              len(telegram_sent) == 1 and "cap reached" in telegram_sent[0])
        check("main(): a REAL run's budget skip DOES record to job_status "
              "as ok=True (a working cap is not a failure), with the "
              "reason in the note",
              recorded == [("alopeciaagent", True, "skipped (budget): cap reached")])

        recorded.clear()
        telegram_sent.clear()
        main(dry_run=True)
        check("main(): a DRY-RUN's budget skip does NOT touch job_status "
              "at all -- a manual test must never look like a completed "
              "scheduled run", recorded == [])

        # ── main(): a real (non-dry) reasoning pass ──
        budget.allow = lambda est_cost_usd: (True, 0.0, "ok")

        async def fake_pass(reason, dry_run=False):
            return 1.2345, Path("/fake/transcript.md")
        g["run_reasoning_pass"] = fake_pass

        recorded.clear()
        main(dry_run=False)
        check("main(): a real run records to job_status as ok=True with "
              "the actual cost in the note",
              len(recorded) == 1 and recorded[0][0] == "alopeciaagent"
              and recorded[0][1] is True and "1.2345" in recorded[0][2])

        recorded.clear()
        main(dry_run=True)
        check("main(): a dry run completing does NOT record to job_status "
              "-- only a REAL scheduled run may (T32/S177: a manual test "
              "must not mask a genuine scheduled failure the same day)",
              recorded == [])
    finally:
        g["run_reasoning_pass"] = saved_run_pass
        g["ledger_append"] = saved_ledger_append
        budget.allow = saved_allow
        tools.send_telegram_summary = saved_send_telegram
        if saved_job_status_mod is not None:
            sys.modules["job_status"] = saved_job_status_mod
        else:
            sys.modules.pop("job_status", None)

    print("PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    main(dry_run="--dry-run" in sys.argv)
