#!/usr/bin/env python3
"""immaculate_agent.py -- Project Immaculate's two judgment passes, on CUMULUS (S253).

These were desktop-app scheduled tasks on Buddy's MacBook until 2026-09-22.
Those only fire while the laptop is awake -- lid closed on battery, they are
skipped -- and three runs had already hung on ssh calls back to this box
(T102). Everything they touch lives here, so they now run here, on timers:

    daily      07:15      new-contest watch, season-PDF check, record any
                          finished game's season question
    wednesday  Wed 06:50  resolve last week's season + weekly-contest
                          questions, snapshot Q18-24 leaders -- feeds the
                          09:05 immaculate-wednesday-report email

The instructions are immaculate_prompts/<mode>.md. The model gets NO general
shell and NO permission classifier to fall back on, so it gets an allow-list
instead: Bash only for the exact immaculate_* commands below, WebSearch, and
one outbound channel, notify_buddy (Telegram to Buddy). permission_mode
"dontAsk" refuses anything else, so a web page it reads cannot talk it into
reading or sending anything more.

    python3 immaculate_agent.py daily|wednesday [--dry-run]
    python3 immaculate_agent.py selftest
"""
import asyncio
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

CREDS_PATH = HERE / "config" / "credentials.json"
PROMPTS = HERE / "immaculate_prompts"
TRANSCRIPTS = HERE / "logs" / "immaculate-agent"
PY = "./.venv/bin/python"

# per mode: (budget USD, max turns, wall-clock seconds). The unit's
# TimeoutStartSec sits above the wall clock as the hard backstop -- a hang
# ends the run instead of blocking every run after it (the T102 failure).
LIMITS = {"daily": (1.50, 40, 15 * 60), "wednesday": (4.00, 80, 20 * 60)}

READS = {
    "daily": [f"{PY} immaculate_check.py",
              f"{PY} immaculate_store.py show", f"{PY} immaculate_store.py tally",
              f"{PY} immaculate_espn.py schedule", f"{PY} immaculate_espn.py summary:*"],
    "wednesday": [f"{PY} immaculate_store.py show", f"{PY} immaculate_store.py tally",
                  f"{PY} immaculate_espn.py schedule", f"{PY} immaculate_espn.py summary:*",
                  f"{PY} immaculate_weekly_store.py weeks",
                  f"{PY} immaculate_weekly_store.py detail:*",
                  f"{PY} immaculate_weekly_store.py tally:*"],
}
WRITES = {
    # the watch saves what it has seen, so a dry run must not run it: it would
    # consume a real finding and the scheduled run would never report it.
    "daily": [f"{PY} immaculate_watch.py", f"{PY} immaculate_store.py record:*"],
    "wednesday": [f"{PY} immaculate_watch.py", f"{PY} immaculate_store.py record:*",
                  f"{PY} immaculate_store.py snapshot-leader:*",
                  f"{PY} immaculate_weekly_store.py resolve:*"],
}


def allowed_tools(mode, dry_run):
    cmds = READS[mode] + ([] if dry_run else WRITES[mode])
    tools = [f"Bash({c})" for c in cmds] + ["WebSearch"]
    if not dry_run:
        tools.append("mcp__immaculate__notify_buddy")
    return tools


def _load_creds():
    return json.loads(CREDS_PATH.read_text())


def send_telegram(message):
    """To Buddy only. Returns "sent" or "FAILED: ..." -- never raises, so a
    failed alert cannot take the run down with it."""
    creds = _load_creds()
    token, chat = creds.get("telegram_bot_token", ""), creds.get("telegram_user_id", "")
    if not token or not chat:
        return "FAILED: telegram creds missing"
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat, "text": message[:4000]}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        return "sent"
    except (urllib.error.URLError, OSError) as e:
        return f"FAILED: {type(e).__name__}"


async def run_pass(mode, dry_run):
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ResultMessage,
                                  TextBlock, create_sdk_mcp_server, query, tool)
    creds = _load_creds()
    budget_usd, max_turns, _ = LIMITS[mode]
    sent = []

    @tool("notify_buddy", "Send Buddy a short Telegram message (his phone).", {"message": str})
    async def notify_buddy(args):
        result = send_telegram(f"Immaculate: {args['message']}")
        sent.append((args["message"], result))
        return {"content": [{"type": "text", "text": result}]}

    options = ClaudeAgentOptions(
        tools=["Bash", "WebSearch"],
        mcp_servers={} if dry_run else {
            "immaculate": create_sdk_mcp_server(name="immaculate", tools=[notify_buddy])},
        strict_mcp_config=True,
        allowed_tools=allowed_tools(mode, dry_run),
        permission_mode="dontAsk",
        system_prompt=(PROMPTS / f"{mode}.md").read_text(),
        model="sonnet",
        max_turns=max_turns,
        max_budget_usd=budget_usd,
        env={"ANTHROPIC_API_KEY": creds["anthropic_api_key"]},
        cwd=str(HERE),
    )
    prompt = f"Scheduled {mode} run. Now: {datetime.now():%A %Y-%m-%d %H:%M} (America/New_York)."
    if dry_run:
        prompt += (" THIS IS A DRY RUN: the watch, every record/resolve/snapshot command and "
                   "notify_buddy are unavailable. Skip the watch; for everything else, do the "
                   "research and write out the exact command or message you WOULD run or send.")

    narrative, result, cost, error = [], "", None, None
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            narrative += [b.text.strip() for b in msg.content
                          if isinstance(b, TextBlock) and b.text.strip()]
        elif isinstance(msg, ResultMessage):
            cost, result = msg.total_cost_usd, msg.result or ""
            if msg.is_error:
                error = f"SDK run ended with {msg.subtype}"
    return narrative, result, cost, error, sent


def _record_cost(cost, mode, stamp):
    try:
        import llm_budget
        llm_budget.record_sdk_cost(_load_creds(), cost, task=f"immaculate-agent:{mode}",
                                   run_id=f"immaculate-{mode}:{stamp}", app_dir=str(HERE))
    except Exception as e:
        print(f"llm_budget.record_sdk_cost failed: {e}")


def _job_status(mode, ok, note):
    # daily is already watched through immaculatecheck, which
    # immaculate_check.py records; only wednesday needs its own row.
    if mode != "wednesday":
        return
    try:
        import job_status
        job_status.record("immaculatewednesdayresolve", ok, note[:200])
    except Exception as e:
        print(f"job_status.record failed: {e}")


def main(mode, dry_run=False):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    wall = LIMITS[mode][2]
    try:
        narrative, result, cost, error, sent = asyncio.run(
            asyncio.wait_for(run_pass(mode, dry_run), timeout=wall))
    except asyncio.TimeoutError:
        narrative, result, cost, error, sent = [], "", None, f"no result after {wall // 60} min -- stopped", []
    except Exception as e:
        narrative, result, cost, error, sent = [], "", None, f"{type(e).__name__}: {e}", []

    if cost is not None:
        _record_cost(cost, mode, stamp)
    TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPTS / f"{mode}{'-dryrun' if dry_run else ''}-{stamp}.md"
    path.write_text("\n\n".join(
        [f"# Immaculate {mode} run {stamp}", f"Dry run: {dry_run}",
         f"Cost: {'unknown' if cost is None else f'${cost:.4f}'}", f"Error: {error or 'none'}",
         "## Telegram sent", *([f"- {m} -> {r}" for m, r in sent] or ["(none)"]),
         "## Narrative", *(narrative or ["(none)"]),
         "## Final summary", result or "(none)"]) + "\n")
    print(f"transcript: {path}")

    if error:
        print(f"FAILED: {error}")
        if not dry_run:
            print("alert:", send_telegram(
                f"Immaculate {mode} run FAILED on cumulus1: {error}. Transcript: {path.name}"))
            _job_status(mode, False, error)
        return 1
    if not dry_run:
        _job_status(mode, True, f"ran, cost=${cost or 0:.2f}")
    return 0


# ── selftest ──────────────────────────────────────────────────────────────────
def _matches(cmd, rule):
    return cmd.startswith(rule[:-2]) if rule.endswith(":*") else cmd == rule


def selftest():
    """Offline (T32): no SDK call, no Telegram, no job_status."""
    ok = True

    def ck(label, cond):
        nonlocal ok
        print(f"  [{'OK ' if cond else 'FAIL'}] {label}")
        ok = ok and cond

    for mode in LIMITS:
        live, dry = allowed_tools(mode, False), allowed_tools(mode, True)
        ck(f"{mode}: dry run has no write command and no notify_buddy",
           not any(w in t for t in dry for w in ("record", "resolve", "snapshot",
                                                 "watch", "notify")))
        ck(f"{mode}: live run can notify and run the watch",
           "mcp__immaculate__notify_buddy" in live and f"Bash({PY} immaculate_watch.py)" in live)
        ck(f"{mode}: every Bash rule is an immaculate_* script under the venv python",
           all(re.fullmatch(rf"Bash\({re.escape(PY)} immaculate_\w+\.py[ \w:*-]*\)", t)
               for t in live if t.startswith("Bash(")))
        # the prompt and the allow-list drift apart silently otherwise: a
        # command the prompt tells the model to run but the list refuses is a
        # step that never happens, reported only as a refusal mid-transcript.
        text = (PROMPTS / f"{mode}.md").read_text()
        cmds = set(re.findall(rf"{re.escape(PY)} immaculate_\w+\.py[ a-z-]*", text))
        rules = READS[mode] + WRITES[mode]
        missing = sorted(c.strip() for c in cmds
                         if not any(_matches(c.strip(), r) for r in rules))
        ck(f"{mode}: every command its prompt names is on its allow-list"
           + (f" -- missing {missing}" if missing else ""), cmds and not missing)
        ck(f"{mode}: hard limits set (budget, turns, wall clock)", all(LIMITS[mode]))
    ck("_matches: prefix rule does not match a different subcommand",
       not _matches(f"{PY} immaculate_store.py seed", f"{PY} immaculate_store.py record:*"))
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["selftest"]:
        sys.exit(selftest())
    if a[:1] and a[0] in LIMITS:
        sys.exit(main(a[0], dry_run="--dry-run" in a))
    print("usage: immaculate_agent.py {daily|wednesday} [--dry-run] | selftest")
    sys.exit(2)
