#!/usr/bin/env python3
"""immaculate_agent.py -- Project Immaculate's post-game resolve pass, on CUMULUS (S253).

After each Steelers game, immaculate_tick.py (09:00 / 21:00) runs this to
record what actually happened: the season question for that game (Q1-Q17),
the week's mini-contest questions, and the current leaders for the
season-long Q18-Q24. The tick then emails Buddy the comparison. This is the
only step that needs judgment -- mapping a box score onto a question's exact
options -- so it is the only one that calls a model.

It began S253 as two Mac desktop-app scheduled tasks (daily + Wednesday),
moved here because those skip whenever the laptop sleeps; the same day Buddy
reshaped the schedule around games instead of weekdays.

The instructions are immaculate_prompts/postgame.md. The model gets NO general
shell and NO permission classifier to fall back on, so it gets an allow-list
instead: Bash only for the exact immaculate_* commands in COMMANDS, and
WebSearch. A web page it reads therefore cannot talk it into reading or
running anything more. It has no outbound channel at all: the tick's email
is the delivery.

The gate is a PreToolUse hook (`permitted`), not the permission rules alone.
Found live S253 on the first boundary test: with permission_mode "dontAsk" and
an exact allow-list, `hostname` and `tally && hostname` still RAN -- Claude
Code auto-approves commands it judges read-only, so `cat` on the credentials
file would have too (TOOLING-TRAPS T105). The hook sees every Bash call before
it runs and denies anything that is not one exact script + subcommand.

S307 added `inactives`: the one-off Week 3 game-day inactives check, moved off
the Mac for the same reason (the desktop app skips tasks while it is closed).
Unlike postgame, its final reply IS the delivery -- main() sends it to Buddy.

    python3 immaculate_agent.py postgame [--dry-run]
    python3 immaculate_agent.py inactives [--dry-run]
    python3 immaculate_agent.py selftest
"""
import asyncio
import json
import re
import shlex
import sys
import time
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

# per mode: (budget USD, max turns, wall-clock seconds). The tick's own
# subprocess timeout and the unit's TimeoutStartSec sit above the wall clock:
# a hang ends the run instead of blocking every run after it (T102).
LIMITS = {"postgame": (4.00, 80, 20 * 60), "inactives": (2.00, 40, 12 * 60)}
WEB_TOOLS = {"postgame": ["WebSearch"], "inactives": ["WebSearch", "WebFetch"]}
# inactives: a reply opening with this means "look again" -- the prompt promises
# a follow-up pass, so the marker and the prompt must agree (selftest checks).
NOT_POSTED = "Immaculate Wk3 INACTIVES NOT POSTED YET"
RECHECK_WAIT = 8 * 60

# script -> the subcommands allowed (None = the script runs with no arguments).
# "reads" are safe in a dry run; "writes" are not.
COMMANDS = {
    "postgame": {
        "reads": {"immaculate_store.py": {"show", "tally"},
                  "immaculate_espn.py": {"schedule", "summary"},
                  "immaculate_weekly_store.py": {"weeks", "detail", "tally"}},
        "writes": {"immaculate_store.py": {"record", "snapshot-leader"},
                   "immaculate_weekly_store.py": {"resolve"}},
    },
    "inactives": {"reads": {"immaculate_espn.py": {"schedule"}}, "writes": {}},
}
# Interpreted by bash even inside double quotes ($ and backtick substitute,
# backslash escapes) or able to end the command line: refused ANYWHERE.
BANNED = set("$`\\\n\r")
# Operators that chain, pipe, redirect or group commands. Refused only OUTSIDE
# quotes: inside "..." they are literal text. S253, found live: banning ";"
# everywhere refused 4 of 7 leader snapshots whose quoted notes contained a
# semicolon -- and the model's own summary said nothing had been refused.
OPERATOR_CHARS = set("();<>|&")


def command_table(mode, dry_run):
    parts = [COMMANDS[mode]["reads"]] + ([] if dry_run else [COMMANDS[mode]["writes"]])
    table = {}
    for part in parts:
        for script, subs in part.items():
            table[script] = None if subs is None else (table.get(script) or set()) | subs
    return table


def _tokens(command):
    """Split like bash would at the top level: quotes respected, and any
    operator OUTSIDE quotes becomes its own token (see OPERATOR_CHARS)."""
    lx = shlex.shlex(command, posix=True, punctuation_chars=True)
    lx.whitespace_split = True
    lx.commenters = ""
    return list(lx)


def permitted(mode, dry_run, command):
    """(ok, reason) for one Bash command. The only gate that counts."""
    if any(ch in BANNED for ch in command):
        return False, "these characters are not allowed anywhere, even in quotes: $ ` \\ newline"
    try:
        argv = _tokens(command)
    except ValueError:
        return False, "could not parse the command (unbalanced quotes?)"
    if any(t and set(t) <= OPERATOR_CHARS for t in argv):
        return False, ("one command only: no ; & | < > ( ) outside quotes "
                       "(inside double quotes they are fine)")
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


def _cut(command):
    """Quote-aware tokens up to the first operator: the command as it was
    meant, minus any `| head` or `2>&1` the model bolted on."""
    try:
        toks = _tokens(command)
    except ValueError:
        toks = command.split()
    out = []
    for t in toks:
        if t and set(t) <= OPERATOR_CHARS:
            break
        out.append(t)
    return out


def _step(command):
    """What a write DOES, ignoring its note: script, subcommand, target
    (question N; week + number for a weekly resolve)."""
    argv = _cut(command)
    n = 5 if len(argv) > 2 and argv[2] == "resolve" else 4
    return tuple(argv[1:n])


def is_write(mode, command):
    """Does this command name one of the mode's WRITES? Only a lost write
    matters: a refused read leaves the stores exactly as they were."""
    argv = _cut(command)
    writes = COMMANDS[mode]["writes"]
    if len(argv) < 2 or argv[0] != PY or argv[1] not in writes:
        return False
    subs = writes[argv[1]]
    return subs is None or (len(argv) >= 3 and argv[2] in subs)


def lost_steps(mode, dry_run, refused, ran):
    """Writes the run needed and never made: refused, and not recovered by a
    permitted retry of the same step. A dry run makes no writes, so loses none.
    S253, found live: counting refused READS here fired a false FAILED alert
    when the model poked at `show 2>&1 | cat -A`, which the gate rightly refused."""
    if dry_run:
        return []
    done = {_step(c) for c in ran}
    return [c for c in refused if is_write(mode, c) and _step(c) not in done]


def allowed_tools(mode, dry_run):
    """Permission rules -- the second layer. `permitted` is the real gate."""
    tools = [f"Bash({PY} {script})" if subs is None else f"Bash({PY} {script} {sub}:*)"
             for script, subs in command_table(mode, dry_run).items()
             for sub in (subs or [None])]
    return tools + WEB_TOOLS[mode]


def _load_creds():
    return json.loads(CREDS_PATH.read_text())


def send_telegram(message):
    """To Buddy only. Returns "sent" or "FAILED: ..." -- never raises, so a
    failed alert cannot take the run down with it. Also used by the tick."""
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


async def run_pass(mode, dry_run, followup=False):
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, HookMatcher,
                                  ResultMessage, TextBlock, query)
    creds = _load_creds()
    budget_usd, max_turns, _ = LIMITS[mode]
    refused, ran = [], []

    async def gate(input_data, tool_use_id, context):
        command = (input_data.get("tool_input") or {}).get("command", "")
        ok, why = permitted(mode, dry_run, command)
        if ok:
            ran.append(command)
            return {}
        refused.append((command, why))
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason": why}}

    options = ClaudeAgentOptions(
        hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[gate])]},
        tools=["Bash", *WEB_TOOLS[mode]],
        mcp_servers={},
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
    kind = "Game-day inactives check" if mode == "inactives" else "Post-game run"
    prompt = f"{kind}. Now: {datetime.now():%A %Y-%m-%d %H:%M} (America/New_York)."
    if followup:
        prompt += (" This is the FOLLOW-UP look: the first one found the inactives not posted "
                   "and Buddy already has that message. Send the final verdict.")
    if dry_run and mode == "inactives":
        prompt += " THIS IS A DRY RUN: your reply is printed, not sent. Write it exactly as you would send it."
    elif dry_run:
        prompt += (" THIS IS A DRY RUN: every record/resolve/snapshot command is unavailable. "
                   "Do the research and write out the exact command you WOULD run.")

    narrative, result, cost, error = [], "", None, None
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            narrative += [b.text.strip() for b in msg.content
                          if isinstance(b, TextBlock) and b.text.strip()]
        elif isinstance(msg, ResultMessage):
            cost, result = msg.total_cost_usd, msg.result or ""
            if msg.is_error:
                error = f"SDK run ended with {msg.subtype}"
    return narrative, result, cost, error, refused, ran


def _record_cost(cost, mode, stamp):
    try:
        import llm_budget
        llm_budget.record_sdk_cost(_load_creds(), cost, task=f"immaculate-agent:{mode}",
                                   run_id=f"immaculate-{mode}:{stamp}", app_dir=str(HERE))
    except Exception as e:
        print(f"llm_budget.record_sdk_cost failed: {e}")


def _pass(mode, dry_run, followup=False):
    """One agent run: transcript, cost, failure alert. Returns (rc, final reply)."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    wall = LIMITS[mode][2]
    try:
        narrative, result, cost, error, refused, ran = asyncio.run(
            asyncio.wait_for(run_pass(mode, dry_run, followup), timeout=wall))
    except asyncio.TimeoutError:
        narrative, result, cost, error, refused, ran = (
            [], "", None, f"no result after {wall // 60} min -- stopped", [], [])
    except Exception as e:
        narrative, result, cost, error, refused, ran = (
            [], "", None, f"{type(e).__name__}: {e}", [], [])

    if cost is not None:
        _record_cost(cost, mode, stamp)
    # S253: the model's own summary is not evidence. On the first live run it
    # reported "no steps were blocked" while the gate had refused 4 writes.
    lost = lost_steps(mode, dry_run, [c for c, _ in refused], ran)
    if lost and not error:
        error = (f"the gate refused {len(lost)} command(s) the run needed, so those "
                 f"steps did not happen (see 'Refused by the gate')")
    if mode == "inactives" and not result.strip() and not error:
        error = "the run finished with no reply, so there is no verdict to send"
    TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPTS / f"{mode}{'-dryrun' if dry_run else ''}-{stamp}.md"
    path.write_text("\n\n".join(
        [f"# Immaculate {mode} run {stamp}", f"Dry run: {dry_run}",
         f"Cost: {'unknown' if cost is None else f'${cost:.4f}'}", f"Error: {error or 'none'}",
         "## Refused by the gate", *([f"- `{c}` -- {w}" for c, w in refused] or ["(none)"]),
         "## Narrative", *(narrative or ["(none)"]),
         "## Final summary", result or "(none)"]) + "\n")
    print(f"transcript: {path}")

    if error:
        print(f"FAILED: {error}")
        if not dry_run:
            then = ("Check the ESPN game page yourself before 1:00." if mode == "inactives"
                    else "The 9am/9pm check will retry it.")
            print("alert:", send_telegram(
                f"Immaculate {mode} run FAILED on cumulus1: {error}. Transcript: {path.name}. {then}"))
        return 1, result
    return 0, result


def deliver(message, dry_run):
    """inactives only: the reply goes to Buddy by Telegram; email if that fails twice."""
    if dry_run:
        print(f"DRY RUN -- not sent:\n{message}")
        return
    status = send_telegram(message)
    if status != "sent":
        status = send_telegram(message)
    if status != "sent":
        from entity_kb_weekly_digest import _send_mail
        c = _load_creds()
        ok = _send_mail(c.get("outlook_email", ""), c.get("outlook_password", ""),
                        "Buddy.Weiss@outlook.com", "", "Immaculate Wk3 INACTIVES", message)
        status = f"telegram {status}; email {'sent' if ok else 'FAILED'}"
    print("delivery:", status)


def main(mode, dry_run=False):
    rc, result = _pass(mode, dry_run)
    if mode != "inactives" or rc:
        return rc
    deliver(result.strip(), dry_run)
    if result.strip().startswith(NOT_POSTED) and not dry_run:
        time.sleep(RECHECK_WAIT)
        rc, result = _pass(mode, dry_run, followup=True)
        if rc == 0:
            deliver(result.strip(), dry_run)
    return rc


# ── selftest ──────────────────────────────────────────────────────────────────
def selftest():
    """Offline (T32): no SDK call, no Telegram."""
    ok = True

    def ck(label, cond):
        nonlocal ok
        print(f"  [{'OK ' if cond else 'FAIL'}] {label}")
        ok = ok and cond

    def allowed(cmd, dry=False):
        return permitted("postgame", dry, cmd)[0]

    def allowed_in(mode, cmd):
        return permitted(mode, False, cmd)[0]

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
    ck("gate: the contest watch is not this agent's (the tick runs it)",
       not allowed(f"{PY} immaculate_watch.py"))
    ck("gate: allowed read passes", allowed(f"{PY} immaculate_store.py tally"))
    ck("gate: quoted note with spaces and parentheses passes",
       allowed(f'{PY} immaculate_store.py record 3 PIT "Final 24-17 (ESPN summary)"'))
    ck("gate: a semicolon INSIDE a quoted note passes (the S253 live miss)",
       allowed(f'{PY} immaculate_store.py snapshot-leader 18 "Pat" "1 TD" "vs ATL Wk1; DK has 0"'))
    ck("gate: > | & inside quotes pass", allowed(f'{PY} immaculate_store.py record 3 PIT "a > b | c & d"'))
    ck("gate: an unspaced chain is refused", not allowed(f"{PY} immaculate_store.py tally&&hostname"))
    ck("gate: an unquoted semicolon is refused", not allowed(f"{PY} immaculate_store.py tally;hostname"))
    ck("gate: unquoted parentheses are refused", not allowed(f"{PY} immaculate_store.py record 3 PIT (x)"))
    ck("gate: $ inside double quotes is refused (bash still expands it)",
       not allowed(f'{PY} immaculate_store.py record 3 PIT "$HOME"'))
    ck("lost step: a refused but well-named write counts as lost",
       is_write("postgame", f'{PY} immaculate_store.py record 3 PIT "x $y"'))
    probe = f"{PY} immaculate_store.py show 2>&1 | cat -A | head -5"
    ck("lost step: a refused READ is never lost (the S253 false alarm)",
       lost_steps("postgame", False, [probe], []) == [])
    piped = f'{PY} immaculate_store.py record 3 PIT "x" | cat'
    ck("lost step: a piped write retried plainly is recovered",
       lost_steps("postgame", False, [piped], [f'{PY} immaculate_store.py record 3 PIT "x"']) == [])
    ck("lost step: a dry run loses nothing", lost_steps("postgame", True, [piped], []) == [])
    bad = f'{PY} immaculate_store.py snapshot-leader 18 "Pat" "1" "a $x"'
    fixed = f'{PY} immaculate_store.py snapshot-leader 18 "Pat" "1" "a x"'
    ck("lost step: refused and never retried -> lost",
       lost_steps("postgame", False, [bad], []) == [bad])
    ck("lost step: refused, then fixed and run -> not lost",
       lost_steps("postgame", False, [bad], [fixed]) == [])
    ck("lost step: a DIFFERENT question's success does not cover it",
       lost_steps("postgame", False, [bad], [fixed.replace(" 18 ", " 19 ")]) == [bad])
    ck("lost step: a probe (--help, hostname) does not",
       not is_write("postgame", f"{PY} immaculate_store.py --help")
       and not is_write("postgame", "hostname"))
    ck("gate: weekly resolve passes live", allowed(f"{PY} immaculate_weekly_store.py resolve 2 1 Patriots x"))
    ck("gate: dry run refuses a write", not allowed(f"{PY} immaculate_store.py record 3 PIT x", dry=True))

    for mode in COMMANDS:
        dry = allowed_tools(mode, True)
        ck(f"{mode}: dry-run rules have no write",
           not any(w in t for t in dry for w in ("record", "resolve", "snapshot")))
        ck(f"{mode}: no MCP tool -- no outbound channel of its own",
           not any(t.startswith("mcp__") for t in allowed_tools(mode, False)))
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
        ck(f"{mode}: prompt never mentions notify_buddy (it has no such tool)",
           "notify_buddy" not in text)
        ck(f"{mode}: hard limits set (budget, turns, wall clock)", all(LIMITS[mode]))
    # the follow-up pass fires only on this exact opening; if the prompt stops
    # promising it, a not-posted reply would be the last word Buddy gets.
    ck("inactives: prompt carries the exact NOT_POSTED opening main() looks for",
       f"`{NOT_POSTED}:`" in (PROMPTS / "inactives.md").read_text())
    ck("inactives: gate refuses every write (it has none)",
       not allowed_in("inactives", f"{PY} immaculate_store.py record 3 PIT x")
       and not allowed_in("inactives", f"{PY} immaculate_weekly_store.py resolve 3 1 x y"))
    ck("inactives: gate allows its one read", allowed_in("inactives", f"{PY} immaculate_espn.py schedule"))
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["selftest"]:
        sys.exit(selftest())
    if a[:1] and a[0] in COMMANDS:
        sys.exit(main(a[0], dry_run="--dry-run" in a))
    print("usage: immaculate_agent.py postgame|inactives [--dry-run] | selftest")
    sys.exit(2)
