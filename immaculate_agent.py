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
one outbound channel, notify_buddy (Telegram to Buddy). A web page it reads
therefore cannot talk it into reading or sending anything more.

The gate is a PreToolUse hook (`permitted`), not the permission rules alone.
Found live S253 on the first boundary test: with permission_mode "dontAsk" and
an exact allow-list, `hostname` and `tally && hostname` still RAN -- Claude
Code auto-approves commands it judges read-only, so `cat` on the credentials
file would have too. The hook sees every Bash call before it runs and denies
anything that is not one exact script + subcommand from COMMANDS.

    python3 immaculate_agent.py daily|wednesday [--dry-run]
    python3 immaculate_agent.py selftest
"""
import asyncio
import json
import re
import shlex
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

# script -> the subcommands allowed (None = the script runs with no arguments).
# "reads" are safe in a dry run; "writes" are not. The watch is a write: it saves
# what it has seen, so a dry run must not run it -- it would consume a real
# finding and the scheduled run would never report it.
COMMANDS = {
    "daily": {
        "reads": {"immaculate_check.py": None,
                  "immaculate_store.py": {"show", "tally"},
                  "immaculate_espn.py": {"schedule", "summary"}},
        "writes": {"immaculate_watch.py": None,
                   "immaculate_store.py": {"record"}},
    },
    "wednesday": {
        "reads": {"immaculate_store.py": {"show", "tally"},
                  "immaculate_espn.py": {"schedule", "summary"},
                  "immaculate_weekly_store.py": {"weeks", "detail", "tally"}},
        "writes": {"immaculate_watch.py": None,
                   "immaculate_store.py": {"record", "snapshot-leader"},
                   "immaculate_weekly_store.py": {"resolve"}},
    },
}
# Anything that lets one command become two, redirect, substitute or escape.
# Refused even inside quotes; the prompts tell the model to keep notes free of
# them rather than trying to parse bash's quoting rules correctly here.
BANNED = set(";&|<>$`\\\n\r")


def command_table(mode, dry_run):
    parts = [COMMANDS[mode]["reads"]] + ([] if dry_run else [COMMANDS[mode]["writes"]])
    table = {}
    for part in parts:
        for script, subs in part.items():
            table[script] = None if subs is None else (table.get(script) or set()) | subs
    return table


def permitted(mode, dry_run, command):
    """(ok, reason) for one Bash command. The only gate that counts."""
    if any(ch in BANNED for ch in command):
        return False, "shell metacharacters are not allowed (; & | < > $ ` \\ newline)"
    try:
        argv = shlex.split(command)
    except ValueError:
        return False, "could not parse the command"
    if len(argv) < 2 or argv[0] != PY:
        return False, f"only `{PY} immaculate_*.py ...` commands are allowed"
    table = command_table(mode, dry_run)
    if argv[1] not in table:
        return False, f"{argv[1]} is not available in this run"
    subs = table[argv[1]]
    if subs is None:
        return (len(argv) == 2), f"{argv[1]} takes no arguments"
    if len(argv) >= 3 and argv[2] in subs:
        return True, ""
    return False, f"{argv[1]}: allowed here only as {sorted(subs)}"


def allowed_tools(mode, dry_run):
    """Permission rules -- the second layer. `permitted` is the real gate."""
    tools = [f"Bash({PY} {script})" if subs is None else f"Bash({PY} {script} {sub}:*)"
             for script, subs in command_table(mode, dry_run).items()
             for sub in (subs or [None])]
    tools.append("WebSearch")
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
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, HookMatcher,
                                  ResultMessage, TextBlock, create_sdk_mcp_server,
                                  query, tool)
    creds = _load_creds()
    budget_usd, max_turns, _ = LIMITS[mode]
    sent = []

    @tool("notify_buddy", "Send Buddy a short Telegram message (his phone).", {"message": str})
    async def notify_buddy(args):
        result = send_telegram(f"Immaculate: {args['message']}")
        sent.append((args["message"], result))
        return {"content": [{"type": "text", "text": result}]}

    refused = []

    async def gate(input_data, tool_use_id, context):
        command = (input_data.get("tool_input") or {}).get("command", "")
        ok, why = permitted(mode, dry_run, command)
        if ok:
            return {}
        refused.append((command, why))
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason": why}}

    options = ClaudeAgentOptions(
        hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[gate])]},
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
    return narrative, result, cost, error, sent, refused


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
        narrative, result, cost, error, sent, refused = asyncio.run(
            asyncio.wait_for(run_pass(mode, dry_run), timeout=wall))
    except asyncio.TimeoutError:
        narrative, result, cost, error, sent, refused = (
            [], "", None, f"no result after {wall // 60} min -- stopped", [], [])
    except Exception as e:
        narrative, result, cost, error, sent, refused = (
            [], "", None, f"{type(e).__name__}: {e}", [], [])

    if cost is not None:
        _record_cost(cost, mode, stamp)
    TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPTS / f"{mode}{'-dryrun' if dry_run else ''}-{stamp}.md"
    path.write_text("\n\n".join(
        [f"# Immaculate {mode} run {stamp}", f"Dry run: {dry_run}",
         f"Cost: {'unknown' if cost is None else f'${cost:.4f}'}", f"Error: {error or 'none'}",
         "## Telegram sent", *([f"- {m} -> {r}" for m, r in sent] or ["(none)"]),
         "## Refused by the gate", *([f"- `{c}` -- {w}" for c, w in refused] or ["(none)"]),
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
def selftest():
    """Offline (T32): no SDK call, no Telegram, no job_status."""
    ok = True

    def ck(label, cond):
        nonlocal ok
        print(f"  [{'OK ' if cond else 'FAIL'}] {label}")
        ok = ok and cond

    def allowed(cmd, mode="daily", dry=False):
        return permitted(mode, dry, cmd)[0]

    # the S253 boundary test, as a regression: these RAN before the gate existed
    ck("gate: `hostname` refused", not allowed("hostname"))
    ck("gate: allowed command chained to another is refused",
       not allowed(f"{PY} immaculate_store.py tally && hostname"))
    ck("gate: reading the credentials file is refused",
       not allowed("cat config/credentials.json"))
    ck("gate: command substitution inside an allowed command is refused",
       not allowed(f'{PY} immaculate_store.py record 3 PIT "$(cat config/credentials.json)"'))
    ck("gate: a redirect is refused", not allowed(f"{PY} immaculate_store.py show > x"))
    ck("gate: another python script is refused", not allowed(f"{PY} llm_budget.py"))
    ck("gate: a different interpreter is refused", not allowed("python3 immaculate_store.py show"))
    ck("gate: a subcommand not on the list is refused (seed)",
       not allowed(f"{PY} immaculate_store.py seed"))
    ck("gate: a no-argument script given arguments is refused",
       not allowed(f"{PY} immaculate_watch.py --reset"))
    ck("gate: allowed read passes", allowed(f"{PY} immaculate_store.py tally"))
    ck("gate: quoted note with spaces and parentheses passes",
       allowed(f'{PY} immaculate_store.py record 3 PIT "Final 24-17 (ESPN summary)"'))
    ck("gate: dry run refuses a write", not allowed(f"{PY} immaculate_store.py record 3 PIT x", dry=True))
    ck("gate: dry run refuses the watch", not allowed(f"{PY} immaculate_watch.py", dry=True))
    ck("gate: daily cannot resolve weekly questions",
       not allowed(f"{PY} immaculate_weekly_store.py resolve 2 1 Patriots x"))
    ck("gate: wednesday can", allowed(f"{PY} immaculate_weekly_store.py resolve 2 1 Patriots x",
                                      mode="wednesday"))

    for mode in COMMANDS:
        live, dry = allowed_tools(mode, False), allowed_tools(mode, True)
        ck(f"{mode}: dry-run rules have no write and no notify_buddy",
           not any(w in t for t in dry for w in ("record", "resolve", "snapshot",
                                                 "watch", "notify")))
        ck(f"{mode}: live rules can notify and run the watch",
           "mcp__immaculate__notify_buddy" in live and f"Bash({PY} immaculate_watch.py)" in live)
        # the prompt and the gate drift apart silently otherwise: a command the
        # prompt tells the model to run but the gate refuses is a step that
        # never happens, visible only as a refusal mid-transcript.
        text = (PROMPTS / f"{mode}.md").read_text()
        named = set(re.findall(rf"`({re.escape(PY)} immaculate_\w+\.py[ a-z-]*)`", text))
        table = command_table(mode, False)
        missing = sorted(c for c in named
                         if not (c.split()[1] in table and
                                 (table[c.split()[1]] is None or
                                  (len(c.split()) > 2 and c.split()[2] in table[c.split()[1]]))))
        ck(f"{mode}: every command its prompt names passes the gate"
           + (f" -- missing {missing}" if missing else ""), bool(named) and not missing)
        ck(f"{mode}: hard limits set (budget, turns, wall clock)", all(LIMITS[mode]))
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["selftest"]:
        sys.exit(selftest())
    if a[:1] and a[0] in COMMANDS:
        sys.exit(main(a[0], dry_run="--dry-run" in a))
    print("usage: immaculate_agent.py {daily|wednesday} [--dry-run] | selftest")
    sys.exit(2)
