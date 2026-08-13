# DEPLOYMENT-STATE — what runs where, and who watches it

**Single source of truth for the current promotion state of every job.** This file
is git-tracked in `cirrus-digest`, so an **identical copy ships to every server**
(CIRRUS, CUMULUS, STRATUS) on the next pull — open it on any box to see where each
project is promoted and where it is monitored from. **Update it on every cutover**
(it is part of the cutover, not an afterthought).

- Tiers: **CIRRUS** = dev (`TARGET_ENV=dev`), **CUMULUS** = beta+prod (`beta`),
  **STRATUS** = final prod (`prod`, not yet provisioned).
- "Runs on" = the box whose scheduler actually fires the job today.
- "Monitored from" = where the health of that job is reported (see topology below).
- Full mechanism: `~/Documents/Cowork/docs/PROMOTION-AND-NODE-AWARENESS.md`.

_Last updated: 2026-08-13 (S63) — Boss (CUMULUS supervisor agent) armed live; intake now runs on
BOTH boxes (Tier-0 auto-apply + live LLM-council answers); credential self-heal + nightly backup
extended to cover CUMULUS._

## Client jobs (external email — high-stakes)

| Job | Script | Runs on | Schedule | Ensemble | Monitored from | Notes |
|:--|:--|:--|:--|:--|:--|:--|
| billsnow | `snowbrief/bill_snow_weekly.py` | **CUMULUS** | Mon 08:06 | council | CIRRUS (SSH pull) | S57 cutover; sends to Bill on material change only, cc Buddy |
| billnewdev | `newdev/bill_newdev_weekly.py` | **CUMULUS** | Mon 09:05 | n/a (deterministic) | CIRRUS (SSH pull) | S57 cutover; sends to Bill only if new leads, cc Buddy |
| pedagogy | `pedagogy_daily.py` | **CUMULUS** | daily 06:00 | panel model-brief + council topic briefs | CIRRUS (SSH pull) | S57 cutover; sends to Alyssa (cc Buddy); config localized on CUMULUS |
| hoaleads | `hoa_leads/hoa_monitor.py` | **CUMULUS** | Mon 09:20 | council lead-filter | CIRRUS (SSH pull) | S57 NEW; DE HOA lead monitor (X + web/RFP); emails Bill vetted LINKS cc Buddy only when the council finds genuine leads; we never contact anyone |

Rollback for any client job: `cirrus-job enable <job>` + `cumulus-service disable <unit>.timer`
(CIRRUS plists are left in place — disable-not-delete).

## Internal jobs (no external send)

| Job | Script | Runs on | Schedule | Monitored from | Notes |
|:--|:--|:--|:--|:--|:--|
| research digest | `cirrus_daily.py` | CIRRUS | daily 07:00 | CIRRUS (local) | local-model summarization; dev tier |
| morning brief | `morning_brief.py` | CIRRUS | daily 07:30 | CIRRUS (local) | reads job_status; now pulls CUMULUS jobs too |
| model health | `model_health.py` | **BOTH** | daily 05:30 | each box (local) | self-heals retired models; S57 funding alert |
| intake | `intake.py` | **BOTH** | every 15 min | each box (local) | S63: CUMULUS now also watches `cumulus@cumulustask.com` (`INTAKE_ACCOUNT_LABEL=cumulus-research`), same allowlist (Bill/Aggie/Alyssa/Buddy) as CIRRUS's `cirrustask@gmail.com`. Tier-0 requests (add/subscribe/monitor a source) now **auto-apply** on both boxes via `task_solver.py` — reuses the existing `source-add` validation, writes to `sources.local.json` (git-external overlay, unaffected by the carryback TODO below). New opt-in `request_kind: "answer"` live-answers via the 4-provider council instead of queuing; nobody's opted in yet. |
| dev-loop | `dev_agent.py` | CIRRUS | nightly 21:30 | CIRRUS (local) | Tier-1 self-improvement builder |
| watchdog | `cirrus_watchdog.py` | CIRRUS | every 30 min | CIRRUS (local) | agent-loaded / exit-code watch |
| privacy monitor | `privacy/privacy_monitor.py` | CIRRUS | Sun 07:15 | CIRRUS (local) | own-info exposure scan |
| stratus review | `stratus/stratus_monthly.py` | CIRRUS | 1st 09:00 | CIRRUS (local) | research log |
| **Boss** (supervisor agent) | `supervisor/supervisor_agent.py` | **CUMULUS** | 60s heartbeat (deterministic, no LLM) + once-daily reasoning pass after 08:00, or immediately on an anomaly | CUMULUS (local, `cumulus-supervisor-status`) | S63 NEW, armed live. Watches CUMULUS's own client-job services + its own credential health via a fixed 7-tool registry (read-only checks + restart/reset-failed on 8 allow-listed units + one-way Telegram). Runs under its own low-privilege OS account (`cumulus-supervisor`, narrowly-scoped sudo — not general access). Does **not** touch email/intake — that's a separate system. $5/day spend cap. Full operating contract: `supervisor/CLAUDE.md`. |

## Per-box knobs (in each box's `credentials.json`)

| Knob | CIRRUS | CUMULUS | STRATUS |
|:--|:--|:--|:--|
| `TARGET_ENV` | dev | beta | prod |
| `dev_escalation.mode` | failover (baseline) | **council** (all 4 LLMs + local) | tbd |
| local model | qwen2.5:14b | qwen3-coder:30b | qwen3-coder:480b |
| `llm_budget.box` | cirrus | cumulus | stratus |

## Monitoring topology (who watches whom)

```
CIRRUS morning brief / jobs_check
  ├─ local jobs        → read CIRRUS logs/jobs-status.json
  └─ CUMULUS jobs      → SSH read-only pull of cumulus1:~/cirrus-digest/logs/jobs-status.json
                         (billsnow, billnewdev, pedagogy — tagged "(CUMULUS)")
                         unreachable → shown "can't confirm", never a false OVERDUE
```

- The CIRRUS→CUMULUS read link is a dedicated read-only SSH key (`cirrus-cumulus-link-setup`);
  test it any time with `cirrus-cumulus-link`.
- `job_status.REMOTE_JOBS` is the list the CIRRUS brief pulls from CUMULUS — **keep it in
  sync with the "Runs on = CUMULUS" rows above whenever a job is cut over.**

## Change-tier promotion rules (Buddy, S57)

- **Tier 1 & 2 changes ALWAYS start on CIRRUS (dev).** Develop + dry-run on CIRRUS,
  then promote to CUMULUS, then (later) STRATUS. Never edit code directly on a
  prod box.
- **Tier 0 changes may auto-apply on CUMULUS (prod)** — e.g. self_review adding an
  RSS/source. Because CUMULUS's configs are localized + `skip-worktree`, those
  auto-applied changes do **not** flow to git on their own, so **they must be carried
  BACK to CIRRUS/git** or dev will silently drift from prod.
  → **TODO (build next):** a `cumulus-tier0-carryback` step that diffs CUMULUS's
  Tier-0 config changes vs git and merges the source/omit additions back into the
  tracked configs (so CIRRUS and the repo stay in sync). Until it exists, check
  CUMULUS's self-change ledger manually when reconciling.
  → **S63: intake's new Tier-0 auto-apply (`task_solver.py`) is NOT affected by this
  gap** — it writes to `sources.local.json`, which is deliberately gitignored/
  box-local by design (same overlay the manual `source-add` runner command has
  always used), not one of the localized/skip-worktree tracked configs the
  carryback TODO is about. Runs on both boxes independently now, each with its
  own overlay file — nothing to reconcile between them.

## Local models per box (live inventory 2026-08-06)

| Box | Active digest model | Also installed |
|:--|:--|:--|
| CIRRUS | qwen2.5:14b | qwen2.5:72b, qwen2.5-coder:14b, llama3.2:3b (podcast), nomic-embed-text |
| CUMULUS | qwen3-coder:30b | qwen2.5:72b, qwen2.5:14b, nomic-embed-text *(no llama3.2:3b — podcast falls back to main model)* |
| STRATUS | qwen3-coder:480b (planned) | vLLM-served on the 256GB pooled tier; chosen via model-bench v2 before promotion |

## When STRATUS comes online

Add a `prod` column / rows here, extend the monitoring pull to STRATUS (mirror the
`cirrus-cumulus-link` set), and move any promoted job's "Runs on" to STRATUS. Same
disable-not-delete cutover pattern. Update this file as the final step of the promotion.

- 2026-08-11 (S60): GitHub push auth migrated PAT -> GitHub App (App ID 4560828); tokens auto-rotate, no silent expiry. cowork-deploy.sh + cowork-push mint via runner/gh_app_token.py (PAT fallback retained until old PAT revoked).
