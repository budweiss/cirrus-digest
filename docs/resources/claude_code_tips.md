# Claude Code Usage Tips & Automation Workflows

_Source: Boris Cherny (creator of Claude Code) — public talks/interviews on effective
usage patterns. Curated here as a reference resource for CIRRUS's own automation
work (Buddy reviewing/approving CIRRUS_NOTE items, and general dev-loop hygiene
when working with Claude Code or similar coding agents on this project)._

This file is a **reference resource only** — it does not change any CIRRUS
behavior. It's meant to inform future manual work and future proposals so they
stay grounded in proven patterns rather than generic advice.

## Core workflow habits

- **Keep a `CLAUDE.md` (or equivalent grounding doc) up to date.** This project
  already does this via `CIRRUS Conventions & Ground Truth` — the same
  principle Cherny recommends: give the agent a short, accurate map of the
  codebase's real architecture so it doesn't propose generic/fictional patterns.
- **Use `/clear` (or start a fresh session) between unrelated tasks.** Long,
  meandering context windows degrade output quality — a clean context per
  task produces more focused, surgical changes (the same spirit as this
  agent's "change as few files/lines as possible" rule).
- **Prefer plan-first, then execute.** Ask the agent to outline its intended
  change before writing code, especially for anything touching more than one
  file. Mirrors CIRRUS's own approve → dev-spec → build pipeline.
- **Small, scoped diffs over big rewrites.** Cherny repeatedly emphasizes
  giving the agent a narrow, well-defined slice of work rather than "refactor
  the whole thing" — exactly why CIRRUS proposals are scoped per-tier and
  reviewed before implementation.
- **Use custom slash commands / scripts for repeated workflows.** Rather than
  re-explaining a multi-step task each time, script it once (e.g. CIRRUS's
  `launchctl kickstart` deploy step, or a `--dry-run` test harness like the
  one in `cirrus_daily.py`) and invoke it by name.
- **Headless/non-interactive mode for automation.** For jobs that should run
  unattended (analogous to CIRRUS's `launchd` jobs), drive the agent via a
  scripted, non-interactive invocation rather than an interactive chat loop —
  keeps automation reliable and reviewable.
- **Use git worktrees (or equivalent isolation) for parallel work-in-progress**
  so exploratory changes don't collide with the main working tree — useful
  discipline if CIRRUS ever needs to try a risky change before committing it.

## How this applies to CIRRUS specifically

- Buddy's `/approve` review step already mirrors the "plan before execute"
  habit — dev specs are reviewed before `generate_proposal()` output becomes
  code.
- The `--dry-run` test harness in `cirrus_daily.py` (DRYRUN-daily-*.md output,
  no side effects) is the CIRRUS equivalent of a scoped, isolated trial run
  before touching production state — keep using it for any pipeline change.
- Future CIRRUS proposals that touch code should stay scoped to the smallest
  file set that accomplishes the goal, per this project's existing dev-loop
  build rules — the same minimalism Cherny advocates for coding-agent tasks.

No code changes accompany this note; it's a reference doc only.
