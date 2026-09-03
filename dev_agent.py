#!/usr/bin/env python3
"""
dev_agent.py — CIRRUS Autonomous Dev-Loop, Phase 3: Tier-1 assisted builds.

The CIRRUS-hosted headless developer agent (host decision: Buddy, 2026-07-14).
Takes an APPROVED Tier-1 proposal (queued by cirrus_bot.execute_action into
logs/dev-loop/build-queue.jsonl), and runs the pipeline:

    build   — git worktree + branch, Claude API writes the patch (safety-gated)
    verify  — four ordered gates (S80): py_compile every changed .py; the
              changed module's own selftest; the selftest of every module that
              IMPORTS it; and a full daily --dry-run when a core digest file is
              touched (paths in cirrus_daily are absolute, so a worktree run
              reads live config but can only write DRYRUN-* files)
    repair  — on failure, show the model how its patch broke and try again, up
              to MAX_REPAIR_ATTEMPTS. Attempts 1-2 Claude alone; attempt 3
              escalates to the keyed panel. A repair may NOT edit test code,
              and two identical failures stop the loop. Every attempt is
              journalled to logs/dev-loop/repairs.jsonl.
    confirm — Telegram one-tap: diff stat + test result + rollback plan;
              Buddy replies `ship N` or `discard N`
    deploy  — config snapshot → rebase on origin/main → push → live
              `git pull --ff-only` → restart service if needed
    verify  — py_compile the live files (+ service check); auto-revert on fail
    ledger  — every step appends to the dev_loop self-changes ledger

Safety model (defense in depth, mirrors dev_loop.may_auto_apply):
  • may_build() re-classifies risk at build time — only Tier-1 items build.
  • patch_path_ok() hard-blocks: paths outside the repo, credentials/cookies/
    secrets/state files, config/* (except sources.json + email_omit.txt),
    non-{.py,.md,.txt,.json} extensions, and .plist/launchd files.
  • Max 4 files per patch; nothing ships without Buddy's explicit `ship N`.
  • The live tree is only ever changed by `git pull --ff-only` from GitHub —
    the agent never edits live files directly.

Usage:
    python3 dev_agent.py nightly      # sweep the queue, build+test+repair, notify
    python3 dev_agent.py report       # morning report (06:30): what happened overnight
    python3 dev_agent.py repairs [N]  # read back HOW the last N fixes were approached
    python3 dev_agent.py list         # show builds awaiting confirm
    python3 dev_agent.py ship N       # deploy build N (bot calls this)
    python3 dev_agent.py discard N    # drop build N
    python3 dev_agent.py selftest     # offline unit tests (no network/creds)

See docs/CIRRUS-Autonomous-Dev-Loop.md (Phase 3).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# S84 (T51): one definition, shared with cirrus_api and cirrus_watchdog.
from launchd_util import launchctl_target, kickstart_cmd

import dev_loop

PROJECT_DIR = Path.home() / "projects/cirrus-digest"
WORK_ROOT   = Path.home() / "projects/dev-loop-work"     # worktrees live here
QUEUE_FILE  = PROJECT_DIR / "logs/dev-loop/build-queue.jsonl"
BUILDS_FILE = PROJECT_DIR / "logs/dev-loop/builds.json"

MAX_BUILDS_PER_RUN  = 2          # dry-runs are ~25 min each (measured S81, was
                                 # documented as 13) — cap the night
MAX_FILES_PER_PATCH = 4
MAX_FILE_CONTEXT    = 45_000     # WHOLE-FILE rewrite ceiling. NOT an input limit —
                                 # Sonnet takes far more. It tracks the OUTPUT budget:
                                 # a whole-file rewrite must be returned COMPLETE and
                                 # max_tokens=16384 is ~65k chars. Files above this are
                                 # not refused any more — they go to EDIT mode (S71).
MAX_EDIT_FILE       = 200_000    # a file we will SHOW for edit mode. Input-only: edits
                                 # emit just the changed hunks, so the output budget
                                 # stops being the binding constraint. This is what
                                 # lets the loop touch cirrus_daily.py (74,827
                                 # chars) and cirrus_bot.py (92,576) at all —
                                 # neither had ever been buildable.
MAX_EDITS_PER_PATCH = 12
MAX_TOTAL_CONTEXT   = 120_000    # chars of all files sent to the model

# Changed files that force a full daily --dry-run before confirm.
DRYRUN_TRIGGERS = {"cirrus_daily.py", "cirrus_digest.py", "extract_actions.py",
                   "self_review.py", "dev_loop.py", "send_digest.py"}
# Changed files that require a service restart after deploy.
RESTART_MAP = {"cirrus_bot.py": "com.cirrus.bot", "cirrus_api.py": "com.cirrus.api"}

# S81: MEASURED, not assumed. A full dry-run on 2026-08-27 took **25m03s**
# (11:08:04 -> 11:33:07, 178 items, 2259-line digest, exit 0). Every note in
# this tree said "~13 min", including docs/COWORK-CONVENTIONS.md, and the 30
# minute timeout was calibrated against that stale figure -- leaving FIVE
# minutes of headroom on a job whose length scales with how much news broke
# that day.
#
# A timeout here does not read as "the gate ran out of time". It returns
# rc != 0 and the build is recorded as FAILED AT THE DRYRUN GATE, which looks
# exactly like the patch being bad. That is the expensive kind of wrong: it
# would discard a good build and teach us to distrust the loop.
#
# 45 minutes is ~1.8x the one real measurement. Widening gate 4's triggers
# (dryrun_reachable) makes this matter more, not less, so the two shipped
# together.
DRYRUN_TIMEOUT = 45 * 60
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"

# ── Patch safety (pure, unit-tested) ─────────────────────────────────────────
_FORBIDDEN_NAME_RX = re.compile(
    r'(credential|cookie|secret|password|token|\.env|email_state|'
    r'pending_approvals|local\.json|\.plist)', re.IGNORECASE)
_ALLOWED_EXTS = {".py", ".md", ".txt", ".json"}
_ALLOWED_CONFIG = {"config/sources.json", "config/email_omit.txt"}


def patch_path_ok(path: str):
    """Return (ok, reason). Relative to repo root; conservative by design."""
    p = path.strip().replace("\\", "/")
    if not p:
        return False, "empty path"
    if p.startswith("/") or p.startswith("~") or ".." in p.split("/"):
        return False, "path escapes the repo"
    if _FORBIDDEN_NAME_RX.search(p):
        return False, "touches a protected file (credentials/secrets/state)"
    if Path(p).suffix.lower() not in _ALLOWED_EXTS:
        return False, f"extension not allowed: {Path(p).suffix or '(none)'}"
    if p.startswith("config/") and p not in _ALLOWED_CONFIG:
        return False, "config/ is protected (only sources.json / email_omit.txt)"
    if p.startswith(("logs/", "digests/", ".git")):
        return False, "runtime/output dirs are not patch targets"
    return True, "ok"


def validate_patch(files: list):
    """Validate the model's proposed file list. Returns (ok, reason)."""
    if not files:
        return False, "model returned no files"
    if len(files) > MAX_FILES_PER_PATCH:
        return False, f"too many files ({len(files)} > {MAX_FILES_PER_PATCH})"
    for f in files:
        path, content = f.get("path", ""), f.get("content", "")
        ok, why = patch_path_ok(path)
        if not ok:
            return False, f"{path}: {why}"
        if not content or not content.strip():
            return False, f"{path}: empty content (deletions are never automated)"
    return True, "ok"


def plan_edits(current: dict, edits: list):
    """Apply search/replace edits in memory. Pure -> (ok, reason, changed).

    Deliberately NOT a unified diff. A unified diff carries line numbers and
    context counts, and a model that miscounts them produces a patch `git apply`
    either rejects or — worse — applies at the wrong offset. Exact-match
    search/replace has no line numbers to get wrong: it either finds the text or
    says so.

    Two rules do the safety work:
      * the `find` text must appear AT LEAST once — a model quoting something
        that is not in the file is hallucinating, and we say so rather than
        silently skipping the edit;
      * it must appear AT MOST once — an ambiguous match could land the change
        in the wrong place, which is exactly the failure a diff offset produces.

    ATOMIC: every edit is applied to an in-memory copy and nothing is returned
    unless all of them succeed, so a patch can never be half-written to disk.
    """
    if not edits:
        return False, "no edits", {}
    if len(edits) > MAX_EDITS_PER_PATCH:
        return False, f"too many edits ({len(edits)} > {MAX_EDITS_PER_PATCH})", {}
    out = dict(current)
    for i, e in enumerate(edits, 1):
        e = e or {}
        path, find, repl = e.get("path", ""), e.get("find", ""), e.get("replace", "")
        if not isinstance(find, str) or not isinstance(repl, str):
            return False, f"edit {i}: 'find'/'replace' must be strings", {}
        if path not in out:
            return False, (f"edit {i}: {path or '(no path)'} was not provided as "
                           f"context, so its current text is unknown"), {}
        n = out[path].count(find) if find else 0
        if not find:
            return False, f"edit {i} ({path}): empty 'find' — refusing to guess where", {}
        if n == 0:
            return False, (f"edit {i} ({path}): the 'find' text does not appear in the "
                           f"file — it was not copied from the content shown"), {}
        if n > 1:
            return False, (f"edit {i} ({path}): the 'find' text appears {n} times — "
                           f"ambiguous; it needs more surrounding lines to be unique"), {}
        if find == repl:
            return False, f"edit {i} ({path}): find and replace are identical (no-op)", {}
        out[path] = out[path].replace(find, repl, 1)
    changed = {p: t for p, t in out.items() if t != current.get(p)}
    if not changed:
        return False, "edits produced no change", {}
    for p, t in changed.items():
        if not t.strip():
            return False, f"{p}: edits emptied the file (deletions are never automated)", {}
    return True, "ok", changed


def parse_model_json(text: str):
    """Parse the model's JSON reply, tolerating markdown fences / prose edges."""
    t = text.strip()
    m = re.search(r'```(?:json)?\s*(\{.*\})\s*```', t, re.DOTALL)
    if m:
        t = m.group(1)
    else:
        # trim to the outermost object
        i, j = t.find("{"), t.rfind("}")
        if i == -1 or j == -1:
            raise ValueError("no JSON object in model reply")
        t = t[i:j + 1]
    return json.loads(t)


def may_build(item: dict) -> bool:
    """Defense-in-depth: re-classify at build time; only Tier-1 builds."""
    tier, _ = dev_loop.classify_risk(item.get("type", ""), item.get("detail", ""),
                                     item.get("source_line", ""))
    return tier == dev_loop.TIER_CONFIRM


# ── Queue + builds state ──────────────────────────────────────────────────────
def queue_append(item: dict, project_dir=None):
    """Called by cirrus_bot.execute_action when Buddy approves a Tier-1 item.
    Durable (its own file) — survives however pending_approvals is persisted."""
    qf = (Path(project_dir) if project_dir else PROJECT_DIR) / "logs/dev-loop/build-queue.jsonl"
    qf.parent.mkdir(parents=True, exist_ok=True)
    row = {"queued": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "item": item}
    with open(qf, "a") as f:
        f.write(json.dumps(row) + "\n")
    return qf


def queue_load(project_dir=None):
    qf = (Path(project_dir) if project_dir else PROJECT_DIR) / "logs/dev-loop/build-queue.jsonl"
    if not qf.exists():
        return []
    rows, seen = [], set()
    for line in qf.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        sid = ((r.get("item") or {}).get("dev_spec") or {}).get("id", "")
        if sid and sid in seen:
            continue
        seen.add(sid)
        rows.append(r)
    return rows


def builds_load(project_dir=None):
    bf = (Path(project_dir) if project_dir else PROJECT_DIR) / "logs/dev-loop/builds.json"
    try:
        return json.loads(bf.read_text())
    except Exception:
        return []


def builds_save(builds: list, project_dir=None):
    bf = (Path(project_dir) if project_dir else PROJECT_DIR) / "logs/dev-loop/builds.json"
    bf.parent.mkdir(parents=True, exist_ok=True)
    bf.write_text(json.dumps(builds, indent=2) + "\n")


def find_buildable(project_dir=None):
    """Queue entries that are Tier-1 and have no build record yet."""
    # Ids with a build record are skipped — EXCEPT a transient "build-error"
    # (truncated/flaky model reply). Those retry now that build_model_patch
    # adds an in-call retry; run_nightly replaces the old record so re-attempts
    # don't pile up. Terminal/in-flight states (shipped/discarded/cannot-build/
    # blocked/test-failed/awaiting-confirm/refused/building) stay skipped.
    done_ids = {b.get("id") for b in builds_load(project_dir)
                if b.get("status") != "build-error"}
    out = []
    for r in queue_load(project_dir):
        item = r.get("item") or {}
        spec = item.get("dev_spec") or {}
        if not spec or spec.get("tier") != dev_loop.TIER_CONFIRM:
            continue
        if spec.get("id") in done_ids:
            continue
        if not may_build(item):
            continue
        out.append(item)
    return out


# ── Model call ────────────────────────────────────────────────────────────────
def _creds():
    try:
        return json.loads((PROJECT_DIR / "config/credentials.json").read_text())
    except Exception:
        return {}


def build_prompt(item: dict, file_blobs: dict, conventions: str = "",
                 edit_only: set = None):
    """Return (system, user) for the patch-writing model call. Pure.

    edit_only: paths too large to return whole (S71). They are shown in full but
    may only be changed through search/replace edits.
    """
    spec = item.get("dev_spec") or {}
    edit_only = edit_only or set()
    system = (
        "You are the CIRRUS Dev-Loop build agent. You write a minimal, surgical "
        "patch for the cirrus-digest Python project to implement ONE approved "
        "proposal. Hard rules:\n"
        "1. Reply with a single JSON object, no markdown fences, shaped exactly:\n"
        '   {"summary": "<one line>",\n'
        '    "files": [{"path": "<repo-relative>", "content": "<complete new file content>"}],\n'
        '    "edits": [{"path": "<repo-relative>", "find": "<exact existing text>", '
        '"replace": "<new text>"}],\n'
        '    "notes": "<risks/assumptions>"}\n'
        "   Use \"files\" to rewrite a small file whole; use \"edits\" for surgical "
        "changes. Either list may be empty, but not both.\n"
        "2. Files marked [EDIT-ONLY] below are too large to return whole. You MUST "
        "change them via \"edits\" and MUST NOT list them in \"files\". Any other "
        "file may use either form.\n"
        "2b. Every \"find\" must be copied EXACTLY from the content shown, and must "
        "appear EXACTLY ONCE in that file — include enough surrounding lines to be "
        "unique. Never use line numbers, and never abbreviate with \"...\". If you "
        "cannot quote it exactly, do not guess: return CANNOT_BUILD.\n"
        "3. Change as few files and as few lines as possible. Preserve existing "
        "style, comments, and behavior everywhere you are not explicitly changing.\n"
        "4. Never touch credentials, cookies, tokens, config files (except "
        "config/sources.json), launchd plists, or anything under logs/ or digests/.\n"
        "5. Python 3.9 stdlib + requests only; no new dependencies.\n"
        "6. If the proposal cannot be implemented safely within these rules, reply "
        '{"summary": "CANNOT_BUILD", "files": [], "edits": [], "notes": "<why>"}.'
        + ("\n\nProject conventions:\n" + conventions[:4000] if conventions else "")
    )
    parts = [
        "Implement this approved proposal:",
        f"PROPOSAL: {item.get('detail', '')}",
        f"CONTEXT LINE: {item.get('source_line', '')}",
        f"DEV SPEC: {json.dumps({k: spec[k] for k in ('id', 'type', 'tier_name', 'files_to_change', 'test_plan') if k in spec})}",
        "",
        "Current file contents:",
    ]
    for path, blob in file_blobs.items():
        tag = "  [EDIT-ONLY — too large to return whole; use \"edits\"]" if path in edit_only else ""
        parts.append(f"\n===== {path} ====={tag}\n{blob}")
    return system, "\n".join(parts)


BUILDER_MODEL_DEFAULT = "claude-sonnet-5"


def _builder_model(creds: dict) -> str:
    """Which Claude model the dev-loop builds with.

    S92: this was `creds.get("claude_dev_model", "claude-sonnet-5")`. A dict
    .get returns the DEFAULT only when the key is ABSENT — when the key exists
    holding an empty string it returns that empty string, and the default never
    applies. CUMULUS's credentials.json has exactly that: claude_dev_model = "".
    So a dev-loop run there would have posted model="" to the Anthropic API.
    CIRRUS is unaffected only by luck — its key is genuinely absent.

    `or` treats empty as unset, which is what every reader of this field already
    assumes and what ensemble.py:125 already does.

    DELIBERATELY NOT falling through to claude_model, the way
    llm_providers._anthropic does. claude_model is haiku-4-5; the builder is the
    one place we want the heavier model, and quietly inheriting the cheap one
    would downgrade the autonomous dev-loop without anyone choosing that.
    """
    return (creds.get("claude_dev_model") or "").strip() or BUILDER_MODEL_DEFAULT


def call_claude_build(system: str, user: str):
    """One-shot Claude API call. Returns raw text. Raises on transport error."""
    import requests
    creds = _creds()
    key = creds.get("anthropic_api_key", "")
    if not key:
        raise RuntimeError("no anthropic_api_key in credentials.json")
    model = _builder_model(creds)
    resp = requests.post(
        CLAUDE_API_URL,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": model, "max_tokens": 16384, "system": system,
              "messages": [{"role": "user", "content": user}]},
        timeout=300)
    resp.raise_for_status()
    data = resp.json()
    for block in data.get("content", []):
        if block.get("type") == "text":
            return block["text"]
    raise RuntimeError(f"no text in model reply (stop_reason={data.get('stop_reason')})")


def build_model_patch(system: str, user: str, attempts: int = 2):
    """Call the model and parse its JSON patch, retrying once on a truncated
    or empty reply. A single flaky model response (empty text, or an
    unterminated-string JSONDecodeError from parse_model_json) no longer burns
    the nightly build slot — see Session-47 carry-over #8. Raises the last
    error if every attempt fails, so build_item still records a build-error."""
    last = None
    for n in range(1, attempts + 1):
        try:
            reply = call_claude_build(system, user)
            if not (reply and reply.strip()):
                raise ValueError("empty model reply")
            return parse_model_json(reply)
        except (ValueError, RuntimeError) as e:
            # ValueError covers json.JSONDecodeError (truncated/unterminated);
            # RuntimeError covers the "no text in model reply" 0-char case.
            last = e
            _log(f"model patch attempt {n}/{attempts} failed: {e}")
    raise last


# ── git / shell helpers ───────────────────────────────────────────────────────
def _run(args, cwd=None, timeout=120):
    r = subprocess.run(args, cwd=str(cwd) if cwd else None,
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def _git(args, cwd=PROJECT_DIR, timeout=120):
    return _run(["git", "-C", str(cwd)] + args, timeout=timeout)


# S82: the selftest deliberately drives failure paths (a truncated model reply,
# a build error) and those paths call _log. Every run therefore appended lines
# like "model patch attempt 1/2 failed" to the OPERATIONAL log, where the only
# thing reading them is a human deciding whether the nightly loop is healthy.
# Four such lines from a test run were the first thing S82 chased. A test that
# writes fake failures into the log used to diagnose real ones is worse than a
# silent test. Selftest logging stays on stdout, where it belongs.
_IN_SELFTEST = False


def _log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] dev_agent: {msg}"
    print(line)
    if _IN_SELFTEST:
        return
    try:
        with open(PROJECT_DIR / "logs/devloop.log", "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _notify(text: str):
    """Telegram to Buddy; never fatal (nightly run must survive notify errors)."""
    try:
        import cirrus_bot as B
        B.send_message(B.ALLOWED_ID, text)
    except Exception as e:
        _log(f"notify failed: {e}")


def _ledger(event, bid, detail="", result="", tier_name=None):
    try:
        dev_loop.ledger_append(
            {"event": event, "id": bid,
             "tier_name": tier_name or dev_loop.TIER_NAME[dev_loop.TIER_CONFIRM],
             "detail": detail, "result": result,
             "target_env": dev_loop.TARGET_ENV}, PROJECT_DIR)
    except Exception as e:
        _log(f"ledger({event}) failed: {e}")


def _cleanup_worktree(bid):
    wt = WORK_ROOT / bid
    _git(["worktree", "remove", "--force", str(wt)])
    _git(["branch", "-D", f"dev-loop/{bid}"])
    shutil.rmtree(wt, ignore_errors=True)


# ── Self-repair: verification gates + journal (S80) ───────────────────────────
# Buddy, S80: "I like the three times try and fix it" and "keep logs on how we
# approach any fix so we can learn from them too."
#
# The old test section was a straight line: py_compile, maybe a dry-run, stop.
# Two separate things were wrong with it. It captured the error text and then
# THREW IT AWAY, so the model never saw how its own patch broke; and py_compile
# is a weak definition of working -- it proves a file parses, not that it does
# anything right. 32 of the 64 modules here ship a selftest() this loop had
# never once run.
#
# WHAT THIS CANNOT DO, stated here so the morning report is never read as more
# assurance than it is: every gate below is a property of CODE. All of them
# would have been GREEN for every real defect S79 shipped -- a page of 99
# near-identical cards, a ranking that had silently fallen through to
# alphabetical, a roster labelled "available to book" that was not, a false
# credit on a client page. This loop fixes what it BREAKS. It cannot notice it
# built the wrong thing. See docs/DEV-AGENT-SELF-REPAIR.md.
#
# NOTE ON trap_lint: the first draft had it as a blocking gate. It is a Cowork
# tool and this worktree is cirrus-digest -- a different tree it does not scan.
# Dropped rather than faked.

MAX_REPAIR_ATTEMPTS      = 3
REPAIR_MAX_FAILURE_CHARS = 2000
SELFTEST_TIMEOUT         = 300
REPAIRS_FILE             = PROJECT_DIR / "logs/dev-loop/repairs.jsonl"

# A repair may not edit test code. The cheapest way for a model to make a test
# pass is to delete the test, so this is a hard refusal, not a warning.
# One assertion-ish construct. NOTE the absence of a trailing \b after the
# alternation: `check\(` ends on "(", a non-word character, so a closing \b
# could never match and the whole rule would silently never fire. That exact
# mistake is T42, found in test_coverage_check.py earlier the same day.
_TEST_MARK_RX = re.compile(
    r"def\s+selftest|\bcheck\(|\bck\(|\bassert\s|self\.assert")


def module_name(path: str) -> str:
    """Repo-relative .py path -> importable module name ('' if not a module)."""
    if not path.endswith(".py"):
        return ""
    return Path(path).name[:-3]


def importers_of(mod: str, root) -> list:
    """Top-level .py files under root that import `mod`.

    S79 shipped a dashboard test that failed quietly because halftime_routing
    reworded a phrase the dashboard suite pinned, and only the CHANGED module's
    suite was re-run. Changing a file means running the tests of everything that
    imports it. Fan-in here is small enough to just run them: the hottest module
    in the tree has 9 importers.
    """
    if not mod:
        return []
    rx = re.compile(r"^\s*(?:from|import)\s+%s\b" % re.escape(mod), re.M)
    out = []
    try:
        for fp in sorted(Path(root).glob("*.py")):
            if fp.name == mod + ".py":
                continue
            try:
                if rx.search(fp.read_text(errors="ignore")):
                    out.append(fp.name)
            except Exception:
                continue
    except Exception:
        return []
    return out


# S81. How a module lets you ASK for its selftest. The tree uses two spellings
# and gate 2 only ever passed one of them.
_SELFTEST_DEF_RX  = re.compile(r"^\s*def\s+_?selftest\s*\(", re.M)
_DISPATCH_DASH_RX = re.compile(r"[\"']--selftest[\"']")
# A quoted bare "selftest" used as an ARGUMENT test. Anchored to argv (or to an
# `== "selftest"` comparison, for the `cmd = sys.argv[1] ... elif cmd ==` shape)
# rather than matching the word anywhere -- a docstring that merely mentions
# selftest must not make us invoke a module with an argument it ignores, which
# is the false-pass this whole change exists to remove.
# `args` as well as `argv`: the commonest idiom in this tree is
#     args = sys.argv[1:]
#     if "selftest" in args:
# and requiring "argv" on the SAME line missed ten real modules (intake,
# halftime_catalogue, pedagogy_daily and friends) -- which would have made gate
# 2 skip suites it used to run. Tightening a rule can lose coverage as easily as
# loosening it can invent it; both directions were measured against the tree.
_DISPATCH_BARE_RX = re.compile(
    r"arg[sv][^\n]*[\"']selftest[\"']"       # "selftest" in args / argv[1] == "selftest"
    r"|[\"']selftest[\"'][^\n]*arg[sv]"
    r"|==\s*[\"']selftest[\"']")             # elif cmd == "selftest":
# `if __name__ == "__main__":` whose body is nothing but a call to the selftest
# (optionally via sys.exit / raise SystemExit). Anything else in that block and
# we must NOT invoke the file bare -- that would run its production path.
_MAIN_IS_ONLY_SELFTEST_RX = re.compile(
    r"if\s+__name__\s*==\s*[\"']__main__[\"']\s*:\s*\n"
    r"(?:\s*(?:sys\.exit\(|raise\s+SystemExit\()?\s*_?selftest\(\)[^\n]*\n)"
    r"\s*\Z", re.M)


def selftest_argvs(fp):
    """The argument this module actually dispatches its selftest on, or None.

    S81 -- THIS WAS A LIVE FALSE PASS, and it is the worst kind. Gate 2 invoked
    every module as `python3 <file> selftest`, a bare word. Ten modules in this
    tree dispatch only on `--selftest`, so for those the bare word matched
    nothing and python fell through to the module's DEFAULT __main__ action,
    which exited 0 and was recorded as a passing selftest. Measured, not
    theorised:

        $ python3 send_guard.py selftest
        billsnow: clear to send            <- the production entrypoint
        billnewdev: clear to send
        exit=0                             <- gate 2 reads this as PASS

    Zero tests ran and the gate went green -- for send_guard, runtime_window,
    search_usage, placement, cirrus_api, net_sampler, vendor_mail_watch,
    client_watch, nccde_directory and dev_findings. Worse than no gate, because
    it looks like one. It is also the S80 lesson exactly: a gate that reports
    "selftest" while inspecting nothing is how config_snapshot's bug survived.

    Two further points this fixes:
      * `def _selftest` (dev_loop's and dev_agent's own spelling) was not
        recognised at all, so the RISK CLASSIFIER's tests never ran in gate 2.
      * A module that defines a selftest but dispatches on NOTHING is reported
        as having none. That is deliberate and honest: a test nobody can invoke
        is not a gate, and running such a file with no arguments would execute
        its production path.
    """
    try:
        text = Path(fp).read_text(errors="ignore")
    except Exception:
        return []
    if not _SELFTEST_DEF_RX.search(text):
        return []
    # RUN BOTH when a module answers to both, rather than guessing which is
    # "the" suite. The first draft of this preferred the dashed form and would
    # have quietly downgraded THIS file: `dev_agent.py selftest` is the 92-check
    # main suite and `dev_agent.py --selftest` is the 14-check edit-planner one.
    # Choosing the dashed form would have run a seventh of the tests and called
    # the file green -- S80 found and fixed exactly that in dev-agent-selftest,
    # and a heuristic here would have reintroduced it one layer down. Guessing
    # was the bug; not guessing is the fix.
    out = []
    if _DISPATCH_DASH_RX.search(text):
        out.append(["--selftest"])
    if _DISPATCH_BARE_RX.search(text):
        out.append(["selftest"])
    # Third convention, and the one the first draft missed: no argv check at
    # all -- `if __name__ == "__main__": sys.exit(selftest())`. dev_loop.py,
    # task_solver.py and four others are written this way, as are dev_agent's
    # own test fixtures. Invoking with NO arguments is only safe when the
    # __main__ block does nothing else, so that is what is checked; a module
    # whose __main__ also does real work is reported as having no invokable
    # selftest rather than having its production path run by a gate.
    if not out and _MAIN_IS_ONLY_SELFTEST_RX.search(text):
        out.append([])
    return out


def has_selftest(fp) -> bool:
    """True if the file has at least one selftest gate 2 can actually invoke."""
    return bool(selftest_argvs(fp))


def dryrun_reachable(changed, root) -> bool:
    """Is any changed file imported by a DRYRUN_TRIGGER? (S81, one hop.)

    Pure-ish and cheap: importers_of greps the top-level .py files, which is the
    same cost gate 3 already pays.
    """
    for p in changed or ():
        if not str(p).endswith(".py"):
            continue
        if set(importers_of(module_name(p), root)) & DRYRUN_TRIGGERS:
            return True
    return False


def failure_signature(gate: str, detail: str) -> str:
    """Stable fingerprint of a failure, for the no-progress rule.

    Deliberately coarse: gate + the FIRST failing thing named. Two attempts
    that fail the same check the same way are the model circling, and a third
    swing buys the same answer at three times the cost.
    """
    first = ""
    for line in (detail or "").splitlines():
        s = line.strip()
        if s.startswith("FAIL") or s.startswith("check(") or "Error" in s:
            first = s[:120]
            break
    if not first:
        first = (detail or "").strip()[:120]
    return "%s::%s" % (gate, re.sub(r"\s+", " ", first))


def no_progress(history) -> bool:
    """True when the last two attempts failed with the identical signature."""
    sigs = [h.get("signature") for h in (history or []) if h.get("signature")]
    return len(sigs) >= 2 and sigs[-1] == sigs[-2]


def test_weight(text) -> int:
    """How much assertion there is in a file. A count, not a flag."""
    return len(_TEST_MARK_RX.findall(text or ""))


def weakens_tests(final: dict, blobs: dict):
    """(ok, reason) — refuse a REPAIR that REMOVES existing assertions.

    S80, second pass. The first version refused any repair patch that
    *contained* test code, which was wrong in two directions at once:

      * it blocked a repair from ADDING a test -- so a task whose whole purpose
        was writing a selftest could never be repaired, only abandoned;
      * it blocked rewriting any file that merely HAS a selftest, however
        faithfully the rewrite preserved it.

    The thing actually worth forbidding is narrower: the cheapest way to make a
    failing gate go green is to delete the check that failed. So compare the
    FINAL content against the ORIGINAL and refuse only when assertions have
    gone missing. Adding is always fine; keeping is fine; losing is not.

    Takes the resolved file contents, not the patch, so whole-file rewrites and
    surgical edits are judged the same way -- an edit that quietly drops a
    check(...) is the same act as a rewrite that omits it.

    A file absent from `blobs` is new: its old weight is 0, so it can only gain.
    """
    for path, text in (final or {}).items():
        before = test_weight((blobs or {}).get(path, ""))
        after = test_weight(text)
        if after < before:
            return False, ("repair removed %d of %d assertion(s) from %s — the "
                           "gate must be satisfied by fixing the code, not by "
                           "deleting the check" % (before - after, before, path))
        if "def selftest" in (blobs or {}).get(path, "") and "def selftest" not in (text or ""):
            return False, "repair deleted selftest() from %s" % path
    return True, ""


def failing_selftests(wt, mods) -> set:
    """Which of `mods` ALREADY fail their selftest, before we patch anything.

    Without this the loop blames itself for someone else's bug: a dependent
    whose suite was broken on main (or that cannot even import, e.g. a missing
    third-party package on this box) would fail gate 3 on every attempt, and
    the model would burn all three swings trying to fix code its patch never
    touched. Measured once against the pristine worktree, then excused.
    """
    out = set()
    for m in mods:
        fp = Path(wt) / m
        if not fp.exists() or not has_selftest(fp):
            continue
        for argv in selftest_argvs(fp):
            rc, _out = _run([sys.executable, str(fp)] + argv,
                            cwd=wt, timeout=SELFTEST_TIMEOUT)
            if rc != 0:
                out.add(m)
                break
    return out


class _SkipRemote(Exception):
    """Internal: gate 5 was switched off for this call (offline selftest)."""


def verify_build(wt, changed, run_dryrun: bool = True, prebroken=(),
                 run_remote: bool = True) -> dict:
    """Run the gates in order, cheapest first. Stop at the first failure.

    Returns {ok, gate, detail, signature, ran}. `ran` lists the gates that
    actually executed, so a report can never imply a gate passed when it was
    skipped -- that is the T42 shape.

    prebroken: modules already failing before this patch. Excused, and named in
    `excused` so a clean verdict never hides a suite nobody is watching.
    """
    wt = Path(wt)
    ran = []

    # gate 1 — every changed .py parses
    ran.append("compile")
    for p in changed:
        if p.endswith(".py"):
            rc, out = _run([sys.executable, "-m", "py_compile", str(wt / p)])
            if rc != 0:
                d = "py_compile %s: %s" % (p, out[:400])
                return {"ok": False, "gate": "compile", "detail": d,
                        "signature": failure_signature("compile", d),
                        "ran": ran, "excused": []}

    # gate 2 — the changed module's own selftest
    #
    # S80, found on this gate's FIRST live run: config_snapshot.py has no
    # selftest, so this gate inspected nothing and `ran` still said "selftest".
    # That is the T42 shape -- a check that reads clean because it never looked
    # -- written into verify_build the same evening T42 was fixed in
    # test_coverage_check.py. `ran` now records the COUNT actually executed, so
    # "selftest(0/1)" can never be misread as "the tests passed".
    n_self = 0
    cand_self = [p for p in changed if p.endswith(".py")]
    for p in cand_self:
        fp = wt / p
        args = selftest_argvs(fp)   # S81: every spelling THIS module answers to
        if not args:
            continue
        n_self += 1
        rc, out = 0, ""
        for a in args:
            rc, out = _run([sys.executable, str(fp)] + a,
                           cwd=wt, timeout=SELFTEST_TIMEOUT)
            if rc != 0:
                break
        if rc != 0:
            d = "%s selftest failed:\n%s" % (p, out[-1200:])
            ran.append("selftest(%d/%d)" % (n_self, len(cand_self)))
            return {"ok": False, "gate": "selftest", "detail": d,
                    "signature": failure_signature("selftest:" + p, out),
                    "ran": ran, "excused": []}

    ran.append("selftest(%d/%d)" % (n_self, len(cand_self)))

    # gate 3 — selftests of everything that IMPORTS a changed module (S79)
    seen, excused, n_dep = set(), [], 0
    prebroken = set(prebroken or ())
    for p in changed:
        for dep in importers_of(module_name(p), wt):
            if dep in seen or dep in changed:
                continue
            seen.add(dep)
            if dep in prebroken:
                excused.append(dep)          # already broken; not ours to fix
                continue
            fp = wt / dep
            args = selftest_argvs(fp)  # S81: same, for the dependent
            if not args:
                continue
            n_dep += 1
            rc, out = 0, ""
            for a in args:
                rc, out = _run([sys.executable, str(fp)] + a,
                               cwd=wt, timeout=SELFTEST_TIMEOUT)
                if rc != 0:
                    break
            if rc != 0:
                d = ("%s selftest failed (it imports %s, which this patch "
                     "changed):\n%s" % (dep, module_name(p), out[-1200:]))
                ran.append("dependents(%d)" % n_dep)
                return {"ok": False, "gate": "dependents", "detail": d,
                        "signature": failure_signature("dependents:" + dep, out),
                        "ran": ran, "excused": excused}

    # gate 4 — full daily dry-run when a core digest file changed, OR when a
    # changed file is IMPORTED BY one (S81).
    #
    # DRYRUN_TRIGGERS is six literal filenames, and matching on the name alone
    # missed the case that prompted this: `self_review.py` is a trigger and
    # imports `ensemble`, so a change to ensemble.py could break self_review's
    # dry-run while the gate that would catch it never fired. The two most
    # imported modules in the tree were getting THINNER coverage than a leaf
    # file. Same shape as T44 -- a hand-maintained name list standing in for a
    # reachability question -- and answered with `importers_of`, the same
    # function gate 3 already uses.
    #
    # ONE HOP, not transitive, and the limit is deliberate: one hop adds exactly
    # ten modules (measured), while a transitive closure would pull in most of
    # the tree and put a 13-minute dry-run on nearly every build. So this
    # catches `ensemble` but NOT `llm_providers`, which is two hops out. Stated
    # rather than hidden: widening it is a measurement, not a guess.
    if run_dryrun and (set(changed) & DRYRUN_TRIGGERS
                       or dryrun_reachable(changed, wt)):
        ran.append("dryrun")
        rc, out = _run([sys.executable, str(wt / "cirrus_daily.py"), "--dry-run"],
                       cwd=wt, timeout=DRYRUN_TIMEOUT)
        if rc != 0:
            d = "daily --dry-run failed:\n%s" % out[-1200:]
            return {"ok": False, "gate": "dryrun", "detail": d,
                    "signature": failure_signature("dryrun", out),
                    "ran": ran, "excused": excused}

    ran.append("dependents(%d)" % n_dep)

    # gate 5 — does it still work ON CUMULUS? (S97)
    #
    # Gates 1-4 all run here, on a Mac. They prove the patch works on macOS. A
    # fix for a CUMULUS job touches systemd, journald, Linux paths and Linux
    # `ps`, and none of that is exercised — so the builder could ship to the box
    # serving Bill, Alyssa and Justin having never run the code there. That is
    # this session's own theme aimed at the builder: a check that passes because
    # it could not see. `ps -o etimes` is the standing proof.
    #
    # It cannot simply run every changed module on CUMULUS and fail on a red
    # one: most of this tree is CIRRUS-only and would be red there for reasons
    # that have nothing to do with the patch. So remote_verify measures a
    # BASELINE at HEAD first and only judges modules that were green there —
    # the same `prebroken` logic gate 3 uses for dependents, and no
    # hand-maintained list of which file belongs to which box (T44).
    #
    # An unreachable CUMULUS does NOT fail the build. A cross-box gate that
    # hard-fails on a network blip is muted within a week, and dev_agent already
    # treats the CUMULUS ticket read as best-effort. It is always reported,
    # never silently skipped.
    # run_remote=False keeps this file's own selftest OFFLINE. A suite that
    # reaches across the network is slow, flaky, and stops being run (T32 is the
    # same lesson about live files). The selftest exercises the gate's DECISION
    # logic in remote_verify's own suite, where both boxes are faked.
    try:
        if not run_remote:
            raise _SkipRemote()
        import remote_verify
        rv = remote_verify.verify_on_cumulus(wt, changed)
        ran.append(rv.get("ran") or "cumulus(?)")
        if not rv.get("ok"):
            return {"ok": False, "gate": "cumulus", "detail": rv.get("detail", ""),
                    "signature": failure_signature("cumulus", rv.get("detail", "")),
                    "ran": ran, "excused": excused + rv.get("excused", [])}
        excused = excused + rv.get("excused", [])
    except _SkipRemote:
        pass                      # offline by request; nothing claimed either way
    except Exception as e:  # noqa: BLE001
        # Never let a verification helper break a build that passed every gate
        # that could actually run -- but say so rather than implying it passed.
        ran.append("cumulus(error: %s)" % type(e).__name__)

    return {"ok": True, "gate": "", "detail": "", "signature": "", "ran": ran,
            "excused": excused}


def _repair_journal(entry: dict):
    """Append one attempt to the repair journal. Never fatal.

    Buddy's ask: keep a record of HOW a fix was approached, not just whether it
    worked, so the approaches themselves can be reviewed later. Every attempt
    lands here -- including the ones that were refused and the ones that gave
    up. A journal of successes only would teach the wrong lesson.
    """
    try:
        REPAIRS_FILE.parent.mkdir(parents=True, exist_ok=True)
        entry = dict(entry)
        entry.setdefault("ts", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        with open(REPAIRS_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        _log("repair journal write failed: %s" % e)


def repair_prompt(item, blobs, conventions, edit_only, failure, prior, attempt):
    """(system, user) for a REPAIR call: the original task plus how it broke."""
    system, user = build_prompt(item, blobs, conventions, edit_only)
    system = system + (
        "\n\nYOU ARE REPAIRING YOUR OWN PREVIOUS ATTEMPT.\n"
        "The file contents shown below are the ORIGINAL, unpatched files. Your "
        "reply must be a COMPLETE fresh patch against those originals -- not a "
        "patch against your broken attempt.\n"
        "HARD RULE: do NOT REMOVE any existing assertion -- a check(...) line, "
        "an assert, a selftest() body. You MAY add new ones, and you may "
        "rewrite a file that contains tests as long as every existing "
        "assertion survives. If the only way you can make the gate pass is to "
        "delete a check, the patch is wrong -- return CANNOT_BUILD and say so "
        "in notes.")
    parts = [user, "",
             "===== HOW ATTEMPT %d BROKE =====" % (attempt - 1),
             "FAILED GATE: %s" % failure.get("gate", "?"),
             (failure.get("detail") or "")[:REPAIR_MAX_FAILURE_CHARS]]
    if prior:
        parts += ["", "===== THE PATCH THAT BROKE (unified diff) =====",
                  prior[:6000]]
    return system, "\n".join(parts)


def council_repair(system: str, user: str):
    """Attempt 3 escalation: ask the whole keyed panel, not Claude again.

    Buddy, S80. Attempts 1-2 are Claude alone. A third identical swing walks
    straight into the no-progress rule, so the last attempt gets a genuinely
    different brain: ensemble.best_answer runs the keyed providers in parallel
    and has a judge synthesize one answer. dev_agent has always imported this
    module -- it has just never asked it to FIX anything, only to comment on a
    diff after the fact.

    Falls back to Claude alone if the panel is unavailable, and says so.
    """
    try:
        import ensemble
    except Exception as e:
        _log("council_repair: ensemble unavailable (%s) — falling back to Claude" % e)
        return parse_model_json(call_claude_build(system, user)), {"driver": "claude-fallback"}
    try:
        meta, text = ensemble.best_answer(system, user, _creds(),
                                          max_tokens=16384,
                                          task="dev-agent-repair", mode="council")
        return parse_model_json(text), {"driver": "council",
                                        "members": meta.get("members", []),
                                        "judge": meta.get("judge"),
                                        "degraded": bool(meta.get("degraded"))}
    except Exception as e:
        _log("council_repair failed (%s) — falling back to Claude" % e)
        return parse_model_json(call_claude_build(system, user)), {"driver": "claude-fallback"}


# ── Build one item ────────────────────────────────────────────────────────────
# ── Council cross-check of the built patch (S57) ──────────────────────────────
# Buddy: self-improvement should use ALL our LLMs. After Claude writes a patch and
# it passes tests, the full keyed panel REVIEWS the diff and Claude synthesizes one
# verdict — so a single model's mistake gets caught before you ship. ADVISORY only:
# it never blocks a build (you still `ship N`); it just attaches the panel's read to
# the build record + notifications. Fail-open; disable with credentials.json
# "dev_agent_council": false.
_COUNCIL_REVIEW_SYSTEM = (
    "You are one of several AI models on a code-review council for the CIRRUS "
    "Dev-Loop. Another model wrote a MINIMAL patch to implement an approved "
    "proposal. Review the unified diff strictly for: correctness, minimality, "
    "safety (it must NOT touch credentials/tokens/plists or anything under logs/ "
    "or digests/, and among config files only config/sources.json), and whether it "
    "actually implements the proposal. Reply with EXACTLY one line: "
    "VERDICT: approve|concerns|reject | NOTES: <one short sentence>.")


def _review_diff(item: dict, diff: str, creds: dict) -> dict:
    """Panel-review a unified diff → council verdict dict. Never raises."""
    try:
        import ensemble
    except Exception:
        return {"verdict": "n/a", "notes": "ensemble unavailable"}
    if not creds.get("dev_agent_council", True):
        return {"verdict": "off", "notes": "dev_agent_council disabled"}
    try:
        user = (f"PROPOSAL: {item.get('detail','')}\n"
                f"BUILD SUMMARY: {item.get('summary','')}\n"
                f"BUILDER NOTES: {item.get('notes','')}\n\n"
                f"PATCH (unified diff):\n{diff[:12000]}")
        meta, text = ensemble.best_answer(_COUNCIL_REVIEW_SYSTEM, user, creds,
                                          max_tokens=300, task="dev-agent-review",
                                          mode="council")
        m = re.search(r"VERDICT:\s*(approve|concerns|reject)", text or "", re.I)
        n = re.search(r"NOTES:\s*(.+)", text or "")
        return {"verdict": (m.group(1).lower() if m else "unclear"),
                "notes": (n.group(1).strip()[:200] if n else (text or "").strip()[:200]),
                "members": meta.get("members", []), "judge": meta.get("judge"),
                "degraded": bool(meta.get("degraded"))}
    except Exception as e:
        return {"verdict": "n/a", "notes": f"review failed: {e}"[:200]}


def _council_review(item: dict, wt: Path, rec: dict):
    """Cross-check the built patch with the full LLM panel; attach the synthesized
    verdict to rec['council']. Advisory (never blocks a ship). Never raises."""
    creds = _creds()
    try:
        _rc, diff = _git(["diff", "HEAD~1..HEAD"], cwd=wt)
    except Exception as e:
        rec["council"] = {"verdict": "n/a", "notes": f"diff failed: {e}"[:120]}
        return
    rec["council"] = _review_diff(
        {**item, "summary": rec.get("summary", ""), "notes": rec.get("notes", "")},
        diff, creds)
    v = rec["council"].get("verdict")
    if v and v not in ("n/a", "off"):
        _log(f"{rec['id']}: council review = {v} "
             f"({'/'.join(rec['council'].get('members', []))}→{rec['council'].get('judge')})")


def build_item(item: dict):
    """build + test one queued Tier-1 item; returns the build record."""
    spec = item.get("dev_spec") or {}
    bid = spec.get("id") or f"prop-{datetime.now().strftime('%Y-%m-%d')}-x"
    branch = f"dev-loop/{bid}"
    wt = WORK_ROOT / bid
    rec = {"id": bid, "detail": item.get("detail", "")[:120], "branch": branch,
           "worktree": str(wt), "status": "building",
           "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    _ledger("build-start", bid, detail=item.get("detail", ""))

    if not may_build(item):   # defense in depth — never trust the queue alone
        rec.update(status="refused", error="not Tier-1 at build time")
        _ledger("build", bid, result="REFUSED: risk re-check failed")
        return rec

    try:
        WORK_ROOT.mkdir(parents=True, exist_ok=True)
        _cleanup_worktree(bid)   # clear any stale leftovers
        rc, out = _git(["worktree", "add", "-b", branch, str(wt), "HEAD"])
        if rc != 0:
            raise RuntimeError(f"worktree add failed: {out[:200]}")

        # Context: the files the spec says will change (S71 — see below).
        #
        # Two harness bugs used to live in these six lines, and between them they
        # caused HALF of all `cannot-build` results. Both were silent, so the
        # model got blamed for refusing an impossible task:
        #
        #  1. `read_text()[:MAX_FILE_CONTEXT]` truncated at 45,000 chars with NO
        #     marker, while build_prompt tells the model "return the COMPLETE new
        #     content of every changed file". cirrus_daily.py is 77,004 chars and
        #     cirrus_bot.py is 94,759 — the two biggest files in the project were
        #     handed over cut off mid-function. prop-2026-07-19-4 refused, and was
        #     RIGHT to: "the provided content is truncated ... I cannot safely".
        #     A file we cannot show whole is a file we cannot ask to be rewritten
        #     whole, so that is now a build refusal with an honest reason instead
        #     of a wasted ~13-minute model call ending in a confusing error.
        #
        #  2. `or ["cirrus_daily.py"]` silently substituted an unrelated file when
        #     the spec named none. prop-2026-07-29-1 needed config/sources.json,
        #     got a truncated cirrus_daily.py, and reported "config/sources.json
        #     ... its current content was not provided." Guessing wrong is worse
        #     than saying the spec is incomplete — the fix belongs in whatever
        #     produced an empty files_to_change, and it can only be fixed if the
        #     failure names it.
        #
        # A named file that does NOT exist is fine and stays non-fatal: the
        # proposal may legitimately be creating it.
        want = spec.get("files_to_change") or []
        blobs, total, blockers, notes = {}, 0, [], []
        edit_only = set()
        if not want:
            blockers.append(
                "the dev_spec names no files_to_change, so there is nothing to "
                "send the builder (it will not be given an unrelated file to guess from)")
        for p in want:
            fp = wt / p
            if not fp.exists():
                notes.append(f"{p} does not exist yet — treat as a new file")
                continue
            text = fp.read_text()
            if len(text) > MAX_EDIT_FILE:
                blockers.append(
                    f"{p} is {len(text):,} chars, over MAX_EDIT_FILE="
                    f"{MAX_EDIT_FILE:,} — too large even to show for a surgical edit")
                continue
            if total + len(text) > MAX_TOTAL_CONTEXT:
                blockers.append(
                    f"{p} skipped — MAX_TOTAL_CONTEXT={MAX_TOTAL_CONTEXT:,} "
                    f"exhausted; dropping it silently would ask for a rewrite of a "
                    f"file the builder never saw")
                continue
            # Over the whole-file ceiling is no longer fatal (S71). The ceiling
            # tracks the OUTPUT budget — a file that cannot be written back whole
            # can still be changed surgically, because an edit emits only the
            # hunk. This is what lets the loop touch the two biggest files in the
            # project, which it had never once been able to build.
            if len(text) > MAX_FILE_CONTEXT:
                edit_only.add(p)
                _log(f"{bid}: {p} is {len(text):,} chars — EDIT-ONLY mode")
            blobs[p] = text
            total += len(text)

        if blockers or (want and not blobs and not notes):
            reason = "; ".join(blockers) or "no usable file context could be assembled"
            rec.update(status="cannot-build", error=reason[:300])
            _log(f"{bid}: cannot-build before the model call — {reason[:160]}")
            _ledger("build", bid, result=f"CANNOT_BUILD (context): {reason[:60]}")
            _cleanup_worktree(bid)
            return rec
        for n in notes:
            _log(f"{bid}: {n}")
        conventions = ""
        conv = wt / "CIRRUS-CONVENTIONS.md"
        if conv.exists():
            conventions = conv.read_text()

        # ── build → verify → repair, up to MAX_REPAIR_ATTEMPTS (S80) ─────────
        # Attempts 1-2 are Claude alone. Attempt 3 escalates to the keyed panel
        # (council_repair) rather than taking a third identical swing, which
        # would only re-trigger the no-progress rule. Every attempt is
        # journalled -- including refusals and the final give-up.
        rc, base_sha = _git(["rev-parse", "HEAD"], cwd=wt)
        base_sha = base_sha.strip()
        history, verdict, attempt = [], None, 0
        rec["attempts"] = 0

        # Baseline the dependents BEFORE any patch exists, while the worktree is
        # still pristine. A suite that was already red is not this patch's fault
        # and must not eat all three attempts.
        deps = set()
        for _p in want:
            deps.update(importers_of(module_name(_p), wt))
        prebroken = failing_selftests(wt, deps)
        if prebroken:
            rec["prebroken"] = sorted(prebroken)
            _log(f"{bid}: dependent suites already red before this patch — "
                 f"excused: {', '.join(sorted(prebroken))}")

        while attempt < MAX_REPAIR_ATTEMPTS:
            attempt += 1
            rec["attempts"] = attempt
            t0 = time.time()
            driver = {"driver": "claude"}

            # Reset the worktree so each attempt patches the ORIGINAL files.
            # Without this, attempt 2 would be applied on top of attempt 1 and
            # the `find` strings from the untouched blobs would stop matching.
            if attempt > 1:
                _git(["reset", "--hard", base_sha], cwd=wt)
                _git(["clean", "-fd"], cwd=wt)

            if attempt == 1:
                system, user = build_prompt(item, blobs, conventions, edit_only)
                patch = build_model_patch(system, user)
            else:
                system, user = repair_prompt(item, blobs, conventions, edit_only,
                                             verdict, rec.get("diff_stat", ""), attempt)
                if attempt >= MAX_REPAIR_ATTEMPTS:
                    patch, driver = council_repair(system, user)
                else:
                    patch = build_model_patch(system, user)

            jrn = {"build_id": bid, "attempt": attempt, "driver": driver["driver"],
                   "detail": rec["detail"], "fixing_gate": (verdict or {}).get("gate", ""),
                   "fixing_signature": (verdict or {}).get("signature", "")}
            jrn.update({k: v for k, v in driver.items() if k != "driver"})

            whole = patch.get("files") or []
            edits = patch.get("edits") or []
            if patch.get("summary") == "CANNOT_BUILD" or (not whole and not edits):
                why = str(patch.get("notes", ""))[:300]
                rec.update(status="cannot-build", error=why)
                _ledger("build", bid, result=f"CANNOT_BUILD: {why[:60]}")
                jrn.update(outcome="cannot-build", approach=why,
                           elapsed_s=round(time.time() - t0, 1))
                _repair_journal(jrn)
                _cleanup_worktree(bid)
                return rec

            jrn["approach"] = str(patch.get("summary", ""))[:200]
            jrn["model_notes"] = str(patch.get("notes", ""))[:300]

            # A whole-file rewrite of an EDIT-ONLY file is the exact failure this
            # mode exists to prevent: it cannot be returned complete inside
            # max_tokens, so accepting it would write a truncated file to disk.
            ignored = sorted({f.get("path", "") for f in whole} & edit_only)
            if ignored:
                why = (f"returned whole-file content for {', '.join(ignored)}, which is "
                       f"too large to write back whole — those must use \"edits\"")
                rec.update(status="blocked", error=f"patch rejected: {why}"[:300])
                _ledger("build", bid, result=f"BLOCKED: {why[:60]}")
                jrn.update(outcome="blocked", refusal=why,
                           elapsed_s=round(time.time() - t0, 1))
                _repair_journal(jrn)
                _cleanup_worktree(bid)
                return rec

            if whole:
                ok, why = validate_patch(whole)
                if not ok:
                    rec.update(status="blocked", error=f"patch rejected: {why}")
                    _ledger("build", bid, result=f"BLOCKED: {why}")
                    jrn.update(outcome="blocked", refusal=why,
                               elapsed_s=round(time.time() - t0, 1))
                    _repair_journal(jrn)
                    _cleanup_worktree(bid)
                    return rec

            # Edits are planned entirely in memory and only then written, so a patch
            # is never half-applied to the worktree.
            edited = {}
            if edits:
                path_violation = None
                for e in edits:
                    okp, whyp = patch_path_ok((e or {}).get("path", ""))
                    if not okp:
                        path_violation = f"{(e or {}).get('path','')}: {whyp}"
                        break
                rejected = None
                if path_violation is None:
                    ok, why, edited = plan_edits(blobs, edits)
                    if not ok:
                        rejected = why

                # A PATH violation stays TERMINAL, deliberately. Reaching for a
                # file outside the allowed set is a different signal from
                # mis-quoting an anchor: one is a mistake, the other is a model
                # going somewhere it was told not to. Blurring them would hand a
                # patch that reached for config/credentials.json two more swings.
                if path_violation is not None:
                    rec.update(status="blocked",
                               error=f"edits rejected: {path_violation}"[:300])
                    _ledger("build", bid, result=f"BLOCKED: {path_violation[:60]}")
                    jrn.update(outcome="blocked", refusal=path_violation,
                               elapsed_s=round(time.time() - t0, 1))
                    _repair_journal(jrn)
                    _cleanup_worktree(bid)
                    return rec

                # A REJECTED EDIT IS REPAIRABLE (S86). It used to `return` here,
                # which made it TERMINAL on attempt 1 while a failed GATE got
                # three tries -- exactly backwards. plan_edits produces the most
                # actionable error in the system ("the 'find' text does not
                # appear in the file"), and the model fixes it by re-quoting.
                #
                # prop-2026-08-27-649612 (llm_providers.py) died this way with
                # attempts=1 and sat approved-but-dead for 36 hours.
                #
                # Nothing was written -- plan_edits is in-memory and atomic -- so
                # there is no partial state to undo, and the loop already resets
                # the worktree to base_sha before each attempt precisely so the
                # `find` strings still match the originals.
                if rejected is not None:
                    jrn.update(outcome="edits-rejected", refusal=rejected,
                               elapsed_s=round(time.time() - t0, 1))
                    _repair_journal(jrn)
                    if attempt >= MAX_REPAIR_ATTEMPTS:
                        rec.update(status="blocked",
                                   error=f"edits rejected: {rejected}"[:300])
                        _ledger("build", bid, result=f"BLOCKED: {rejected[:60]}")
                        _cleanup_worktree(bid)
                        return rec
                    verdict = {
                        "gate": "edit-application",
                        "signature": "edits-rejected",
                        "detail": (
                            "Your edits were REJECTED before anything was written: "
                            + rejected
                            + "\n\nThe file content shown below is the current, "
                              "unmodified file. Copy any `find` text VERBATIM from "
                              "it -- byte for byte, including indentation and "
                              "blank lines -- and make it long enough to appear "
                              "exactly once."),
                    }
                    _log(f"{bid}: edits rejected ({rejected[:80]}) — repairing "
                         f"(attempt {attempt + 1}/{MAX_REPAIR_ATTEMPTS})")
                    continue

            # A REPAIR may not REMOVE assertions. The cheapest way to make a
            # failing gate go green is to delete the check that failed, so this
            # is a refusal and it COSTS the attempt -- a model that tries it
            # does not get a free retry. Judged on the RESOLVED content, after
            # edits are planned and before anything is written, so a surgical
            # edit that quietly drops a check(...) is caught the same as a
            # rewrite that omits it. Adding tests is always allowed.
            if attempt > 1:
                final = {f["path"]: f["content"] for f in whole}
                final.update(edited)
                okt, whyt = weakens_tests(final, blobs)
                if not okt:
                    _log(f"{bid}: attempt {attempt} REFUSED — {whyt}")
                    _ledger("repair", bid, result=f"REFUSED: {whyt[:60]}")
                    jrn.update(outcome="refused-test-edit", refusal=whyt,
                               elapsed_s=round(time.time() - t0, 1))
                    _repair_journal(jrn)
                    history.append({"signature": "refused::test-edit"})
                    if no_progress(history):
                        break
                    continue

            changed = []
            for f in whole:
                dest = wt / f["path"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(f["content"])
                changed.append(f["path"])
            for path, text in edited.items():
                (wt / path).write_text(text)
                changed.append(path)
            rec["edits_applied"] = len(edits)
            rec["files"] = changed
            rec["summary"] = str(patch.get("summary", ""))[:200]
            rec["notes"] = str(patch.get("notes", ""))[:300]

            # ── the gates ────────────────────────────────────────────────────
            verdict = verify_build(wt, changed, prebroken=prebroken)
            rec["gates_ran"] = verdict["ran"]
            if verdict.get("excused"):
                rec["excused"] = verdict["excused"]
            jrn.update(files=changed, gates_ran=verdict["ran"],
                       elapsed_s=round(time.time() - t0, 1))

            if verdict["ok"]:
                rec["test_compile"] = "ok"
                rec["test_dryrun"] = ("ok" if "dryrun" in verdict["ran"]
                                      else "skipped (no core digest file changed)")
                _git(["add", "-A"], cwd=wt)
                rc, out = _git(["commit", "-m", f"dev-loop {bid}: {rec['summary'][:60]}"], cwd=wt)
                if rc != 0:
                    raise RuntimeError(f"commit failed: {out[:200]}")
                rc, stat = _git(["diff", "--stat", f"{base_sha}..HEAD"], cwd=wt)
                rec["diff_stat"] = stat[-500:]
                jrn.update(outcome=("fixed" if attempt > 1 else "built"),
                           diff_stat=stat[-300:])
                _repair_journal(jrn)
                if attempt > 1:
                    _ledger("repair", bid,
                            result=f"FIXED on attempt {attempt} via {driver['driver']}")
                    _log(f"{bid}: repaired on attempt {attempt} ({driver['driver']})")
                break

            # failed — record how, and decide whether another swing is worth it
            rec.update(status="test-failed", error=f"{verdict['gate']}: {verdict['detail'][:250]}")
            rc, stat = _git(["diff", "--stat"], cwd=wt)
            rec["diff_stat"] = stat[-500:]
            jrn.update(outcome="failed", gate=verdict["gate"],
                       signature=verdict["signature"],
                       failure=verdict["detail"][:REPAIR_MAX_FAILURE_CHARS],
                       diff_stat=stat[-300:])
            _repair_journal(jrn)
            _ledger("test", bid, result=f"FAIL {verdict['gate']} (attempt {attempt})")
            _log(f"{bid}: attempt {attempt} failed at {verdict['gate']}")
            history.append(verdict)

            if no_progress(history):
                _log(f"{bid}: identical failure twice — stopping, not circling")
                _repair_journal({"build_id": bid, "attempt": attempt,
                                 "driver": "-", "outcome": "no-progress",
                                 "signature": verdict["signature"],
                                 "approach": "stopped: same failure twice running"})
                break

        # ── loop over ────────────────────────────────────────────────────────
        if verdict is None or not verdict.get("ok"):
            rec["status"] = "repair-exhausted"
            rec["repair_gate"] = (verdict or {}).get("gate", "")
            _ledger("repair", bid,
                    result=f"EXHAUSTED after {rec['attempts']} attempt(s) at "
                           f"{(verdict or {}).get('gate','?')}")
            _log(f"{bid}: repair exhausted after {rec['attempts']} attempts — "
                 f"worktree kept at {wt}")
            return rec   # worktree KEPT on purpose, for inspection

        # Full-panel cross-check of the diff. A council REJECT auto-HOLDS the build:
        # it stays visible but `ship` refuses it until an explicit `unhold` (S57).
        _council_review(item, wt, rec)
        if (rec.get("council") or {}).get("verdict") == "reject":
            rec["council_hold"] = True

        rec["status"] = "awaiting-confirm"
        _ledger("test", bid, result="PASS")
        _ledger("awaiting-confirm", bid,
                detail=f"{rec['summary']} | council={rec.get('council',{}).get('verdict','n/a')}")

        return rec

    except Exception as e:
        rec.update(status="build-error", error=str(e)[:300])
        _ledger("build", bid, result=f"ERROR: {str(e)[:80]}")
        _cleanup_worktree(bid)
        return rec


# ── Confirm / ship / discard ──────────────────────────────────────────────────
def maybe_autoship(rec):
    """Ship this build unattended if the change is provably test-only. -> str.

    Buddy, 2026-09-03: "I like to have this automated without me at night."

    The measured bottleneck was never capability -- dev_agent logged
    [waiting-on-buddy] on 4 of the last 7 nights while 24 findings sat with room
    for 1. And there was an asymmetry: CLAUDE.md rule 3a lets the INTERACTIVE
    agent fix and deploy without asking, while dev_agent had to wait even for a
    patch that only adds assertions. This closes that gap and only that gap.

    autoship.may_autoship decides MECHANICALLY -- parse before and after, delete
    every test function and the __main__ dispatch from both, require the
    remaining ASTs to be identical. Structural equality on everything that is
    not a test, not "looks like tests". A council reject, an unparseable file
    either side, a new file, a new import, or a selftest gate that did not run
    all disqualify. One production change anywhere blocks the whole build.

    CALLED FROM THE SWEEP, AFTER builds_save -- not from build_item. The first
    version ran inside build_item, where `awaiting()` re-reads builds.json and
    this build is not in it yet, so the lookup always missed and auto-ship would
    have silently NEVER FIRED. A feature that quietly does nothing is the exact
    defect this session has spent the day removing; it was found by asking when
    the record actually becomes durable rather than assuming it already was.
    """
    bid = rec.get("id")
    if rec.get("status") != "awaiting-confirm":
        return "not awaiting-confirm"
    try:
        import autoship
        wt = Path(rec.get("worktree") or "")
        pairs = []
        for rel in (rec.get("files") or []):
            fp = wt / rel
            after = fp.read_text(errors="ignore") if fp.exists() else ""
            # stdout ONLY. _run merges stderr into its output, and a git warning
            # prepended to the original source would make the baseline
            # unparseable -- which fails safe, but for the wrong reason, and
            # "it worked by luck" is not a property to rely on in the code that
            # decides what ships unattended.
            g = subprocess.run(["git", "show", f"origin/main:{rel}"],
                               cwd=str(wt), capture_output=True, text=True,
                               timeout=60)
            pairs.append((rel, g.stdout if g.returncode == 0 else None, after))
        may, why = autoship.may_autoship(rec, pairs)
        rec["autoship_reason"] = why
        if not may:
            _log(f"{bid}: needs Buddy's tap — {why}")
            return why
        idx = next((i for i, b in enumerate(awaiting(), 1)
                    if b.get("id") == bid), None)
        if not idx:
            _log(f"{bid}: auto-ship skipped — not in the awaiting list")
            return "not in the awaiting list"
        _log(f"{bid}: AUTO-SHIP — {why}")
        _ledger("autoship", bid, detail=why)
        rec["autoship_result"] = str(ship(idx))[:300]
        _log(f"{bid}: auto-ship result: {rec['autoship_result'][:160]}")
        return rec["autoship_result"]
    except Exception as e:  # noqa: BLE001
        # A failure to DECIDE must leave the build waiting, never ship it.
        _log(f"{bid}: auto-ship check errored ({type(e).__name__}) — left for Buddy")
        return f"error: {type(e).__name__}"


def awaiting(builds=None):
    return [b for b in (builds if builds is not None else builds_load())
            if b.get("status") == "awaiting-confirm"]


def list_builds_text():
    rows = awaiting()
    if not rows:
        return "No builds awaiting confirmation."
    lines = [f"🔧 *{len(rows)} build(s) awaiting confirm:*", ""]
    for i, b in enumerate(rows, 1):
        lines.append(f"*{i}. {b['id']}* — {b.get('summary', b.get('detail',''))[:80]}")
        lines.append(f"   files: {', '.join(b.get('files', []))}")
        lines.append(f"   tests: compile {b.get('test_compile','?')}, "
                     f"dry-run {b.get('test_dryrun','?')}")
        c = b.get("council") or {}
        if c.get("verdict"):
            mark = {"approve": "🟢", "concerns": "🟡", "reject": "🔴"}.get(c["verdict"], "⚪")
            held = "  🔒 HELD (reply `unhold N` to override)" if b.get("council_hold") else ""
            lines.append(f"   {mark} council: {c['verdict']} — {c.get('notes','')[:90]}{held}")
        lines.append("")
    lines.append("_Reply `ship N` to deploy or `discard N` to drop._")
    return "\n".join(lines)


def ship(n: int):
    """Deploy awaiting-confirm build #n (1-based). Returns a status string."""
    builds = builds_load()
    rows = awaiting(builds)
    if not (1 <= n <= len(rows)):
        return f"Invalid build number. Choose 1-{len(rows)}." if rows else "No builds awaiting confirm."
    b = rows[n - 1]
    if b.get("council_hold"):
        c = b.get("council") or {}
        return (f"🔴 `{b['id']}` was REJECTED by the review council "
                f"({'/'.join(c.get('members', []))}→{c.get('judge','')}): {c.get('notes','')[:140]}\n"
                f"It's HELD. Reply `unhold {n}` to override, then `ship {n}` — or `discard {n}`.")
    bid, wt, changed = b["id"], Path(b["worktree"]), b.get("files", [])
    _ledger("ship", bid, detail=b.get("summary", ""))

    # 1. config snapshot (restorable state before anything moves)
    try:
        from config_snapshot import take_snapshot
        take_snapshot(tag="dev-loop")
    except Exception as e:
        _log(f"snapshot failed (continuing): {e}")

    # 2. rebase on latest origin/main
    rc, out = _git(["fetch", "origin", "main"], cwd=wt)
    if rc != 0:
        return f"❌ fetch failed: {out[:200]}"
    rc, out = _git(["rebase", "origin/main"], cwd=wt)
    if rc != 0:
        _git(["rebase", "--abort"], cwd=wt)
        b["status"] = "rebase-conflict"
        builds_save(builds)
        _ledger("deploy", bid, result="FAIL: rebase conflict")
        return (f"❌ `{bid}` no longer applies cleanly on main (rebase conflict). "
                f"Marked rebase-conflict; rebuild it next nightly run or handle in Cowork.")

    # quick re-compile after rebase
    for p in changed:
        if p.endswith(".py"):
            rc, out = _run([sys.executable, "-m", "py_compile", str(wt / p)])
            if rc != 0:
                b["status"] = "test-failed"
                builds_save(builds)
                return f"❌ post-rebase compile failed on {p}: {out[:200]}"

    # 3. push to GitHub (source of truth), then fast-forward the live tree
    rc, out = _git(["push", "origin", f"HEAD:main"], cwd=wt)
    if rc != 0:
        b["status"] = "staged-no-push"
        builds_save(builds)
        _ledger("deploy", bid, result=f"push failed: {out[:60]}")
        return (f"⚠️ Build is tested + committed on `{b['branch']}` but the push to "
                f"GitHub failed: `{out[:150]}`\n"
                f"Likely no push credential on CIRRUS — deploy via Cowork, or add a "
                f"repo-scoped token for origin.")

    rc, out = _git(["pull", "--ff-only"], cwd=PROJECT_DIR)
    if rc != 0:
        _ledger("deploy", bid, result=f"live pull failed: {out[:60]}")
        return f"❌ pushed to GitHub but live pull failed: {out[:200]} — fix manually."
    rc, sha = _git(["rev-parse", "--short", "HEAD"], cwd=PROJECT_DIR)
    _ledger("deploy", bid, result=f"live at {sha}")

    # 4. restart services if needed (notify FIRST — restarting the bot kills us)
    svcs = sorted({RESTART_MAP[p] for p in changed if p in RESTART_MAP})
    if svcs:
        _notify(f"🚀 `{bid}` deployed ({sha}). Restarting: {', '.join(svcs)} …")
        restart_failed = ""
        for s in svcs:
            # S84 (T51): a system-domain daemon needs root, and this rc was
            # DISCARDED -- so a refused kickstart left the old process running
            # while step 5 below asked `launchctl list`, which still shows it.
            # A failed restart read as a successful deploy.
            rc, out = _run(kickstart_cmd(launchctl_target(s)))
            if rc != 0 and not restart_failed:
                restart_failed = f"restart {s}: {out[:120]}"
            time.sleep(2)

    # 5. verify: live files compile; restarted services are back
    fail = restart_failed if svcs else ""
    for p in changed:
        if fail:
            break
        if p.endswith(".py"):
            rc, out = _run([sys.executable, "-m", "py_compile", str(PROJECT_DIR / p)])
            if rc != 0:
                fail = f"live compile {p}: {out[:150]}"
                break
    if not fail and svcs:
        time.sleep(3)
        rc, out = _run(["launchctl", "list"])
        for s in svcs:
            if s not in out:
                fail = f"service {s} not running after restart"
                break

    if fail:
        _ledger("verify", bid, result=f"FAIL: {fail[:60]}")
        # auto-rollback: revert the deploy commit, push, pull, restart again
        _git(["revert", "--no-edit", "HEAD"], cwd=PROJECT_DIR)
        _git(["push", "origin", "main"], cwd=PROJECT_DIR)
        rb_failed = []
        for s in svcs:
            rc, _out = _run(kickstart_cmd(launchctl_target(s)))
            if rc != 0:
                rb_failed.append(s)
        b["status"] = "rolled-back"
        builds_save(builds)
        _ledger("rollback", bid, result=fail[:80])
        # A rollback whose restart failed has reverted the FILES while the old
        # process keeps running the reverted-away code. Never report that as a
        # clean recovery.
        rb_note = (f" ⚠️ but could NOT restart: {', '.join(rb_failed)} — those "
                   f"services are still running the failed code."
                   if rb_failed else " restarted.")
        _notify(f"↩️ `{bid}` FAILED verify ({fail[:100]}) — auto-reverted and"
                f"{rb_note} Live tree is back on the previous commit.")
        return f"↩️ Verify failed — rolled back. ({fail[:150]})"

    b["status"] = "shipped"
    b["shipped_sha"] = sha
    builds_save(builds)
    _ledger("verify", bid, result="PASS")
    _cleanup_worktree(bid)
    return (f"✅ `{bid}` shipped — live at `{sha}`."
            + (f" Restarted: {', '.join(svcs)}." if svcs else "")
            + f"\nRollback if needed: `git revert {sha}` + deploy.")


def discard(n: int):
    builds = builds_load()
    rows = awaiting(builds)
    if not (1 <= n <= len(rows)):
        return f"Invalid build number. Choose 1-{len(rows)}." if rows else "No builds awaiting confirm."
    b = rows[n - 1]
    b["status"] = "discarded"
    builds_save(builds)
    _cleanup_worktree(b["id"])
    _ledger("discard", b["id"], detail=b.get("summary", ""))
    return f"🗑 Discarded `{b['id']}` — branch and worktree removed."


PATH_VIOLATION_MARK = "edits rejected: "   # see retry(); path violations read
                                           # "edits rejected: <path> is not in
                                           # files_to_change", edit misquotes read
                                           # "edits rejected: edit N (...): the
                                           # 'find' text does not appear..."


def retry(build_id: str):
    """Clear ONE non-terminal `blocked` record so the builder can see it again.

    S87, measured. S86 made a rejected edit repairable *within* a run, and its
    recap said the fix was to "re-queue" prop-2026-08-27-649612. It is not:
    `findings-requeue` re-files the QUEUE row, but find_buildable skips any id
    that has a build record at all, and `blocked` is on its terminal list. So a
    build blocked BEFORE the repair loop existed stays dead forever, and the
    re-queue reports success on the half it can do. Measured on the live box:

        queue rows: 13 -> dropped 1
        files_to_change now: ['llm_providers.py']
        find_buildable sees it: NO          <- the whole point, and it was NO

    A blanket "blocked is retryable" would be wrong and would undo a deliberate
    S86 decision: `blocked` covers BOTH a model mis-quoting an anchor (a mistake,
    worth another swing) AND a patch reaching for a file outside
    files_to_change -- credentials, say -- which is a model going somewhere it
    was told not to. Those must not be blurred. So this refuses the second kind
    by name rather than by trusting the caller to know the difference.
    """
    builds = builds_load()
    row = next((b for b in builds if b.get("id") == build_id), None)
    if row is None:
        return f"No build record for `{build_id}`."

    status = row.get("status")
    if status != "blocked":
        return (f"`{build_id}` is `{status}`, not `blocked` — retry only clears a "
                f"blocked record. Use dev-discard/dev-ship for the others.")

    err = str(row.get("error") or "")
    if "not in files_to_change" in err or "outside" in err.lower():
        return (f"REFUSED: `{build_id}` was blocked for a PATH violation, which is "
                f"terminal by design (S86) — the patch reached for a file it was "
                f"told not to touch. Not a mis-quote, not retryable here.\n{err[:200]}")

    builds = [b for b in builds if b.get("id") != build_id]
    builds_save(builds)
    _cleanup_worktree(build_id)
    _ledger("retry", build_id, detail=f"cleared blocked: {err[:80]}")
    seen = any((i.get("dev_spec") or {}).get("id") == build_id
               for i in find_buildable())
    return (f"♻️ Cleared blocked record for `{build_id}`.\n"
            f"find_buildable sees it: {'YES' if seen else 'NO'}"
            + ("" if seen else "  <- queue row missing; run findings-requeue first."))


def unhold(n: int):
    """Clear a council auto-hold on awaiting build #n so it can be shipped (S57).
    Explicit override of a council REJECT — Buddy's decision, logged."""
    builds = builds_load()
    rows = awaiting(builds)
    if not (1 <= n <= len(rows)):
        return f"Invalid build number. Choose 1-{len(rows)}." if rows else "No builds awaiting confirm."
    b = rows[n - 1]
    if not b.get("council_hold"):
        return f"`{b['id']}` is not held — nothing to override."
    b["council_hold"] = False
    b["council_hold_overridden"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    builds_save(builds)
    _ledger("unhold", b["id"], detail=f"override council reject: {(b.get('council') or {}).get('notes','')[:80]}")
    return f"🔓 Override recorded — `{b['id']}` un-held. Reply `ship {n}` to deploy it anyway."




# ── Goal-loop evaluator (S71) ─────────────────────────────────────────────────
# "nothing queued for build" is a true statement about the QUEUE. It is NOT a
# statement about whether the dev-loop is working, and until S71 nothing asked
# the second question: the sweep logged that line 11 nights running (2026-08-10
# -> 08-20) and correct-and-empty looked exactly like silently-broken.
#
# So on an empty night we now decide whether the empty outcome is EXPLAINED,
# and escalate when it is not. Borrowed from s17_goal_loop in
# shareAI-lab/learn-claude-code (docs/LEARN-CLAUDE-CODE-EVAL.md): an evaluator
# separate from the worker, judging evidence rather than accepting "no error".
STREAK_FILE    = PROJECT_DIR / "logs/dev-loop/empty-streak.json"
EMPTY_ALERT_AT = 3      # first alert after this many consecutive empty nights
EMPTY_REALERT  = 7      # then at most weekly — never nags nightly, never goes silent
RECENT_DAYS    = 7      # window for "is anything still flowing in?"


def _streak_load():
    try:
        return json.loads(STREAK_FILE.read_text())
    except Exception:
        return {"count": 0, "since": None, "last_alert_at": 0}


def _streak_save(d):
    try:
        STREAK_FILE.parent.mkdir(parents=True, exist_ok=True)
        STREAK_FILE.write_text(json.dumps(d, indent=2) + "\n")
    except Exception as e:
        _log(f"empty-streak write failed: {e}")


def _recent(items, days=RECENT_DAYS):
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    return [i for i in items if (i.get("added") or "") >= cutoff]


def explain_empty_queue():
    """Why is there nothing to build? -> (verdict, detail).

    verdict:
      waiting-on-buddy — Tier-1 proposals are sitting in /approve. Healthy.
      gate-starved     — candidates arrived but the relevance gate rejected them.
      no-proposals     — nothing arrived at all. This is the alarming one.
      unknown          — could not read the evidence; treated as alarming.
    """
    try:
        import cirrus_bot as B
        pending = B.load_pending()
    except Exception as e:
        return "unknown", f"could not read pending approvals: {e}"

    t1 = [p for p in pending
          if p.get("status") == "pending"
          and (p.get("dev_spec") or {}).get("tier") == dev_loop.TIER_CONFIRM]
    if t1:
        return ("waiting-on-buddy",
                f"{len(t1)} Tier-1 proposal(s) waiting in /approve — tap to queue a build")

    recent = _recent(pending)
    filt = [p for p in recent if p.get("status") == "filtered"]
    if filt:
        return ("gate-starved",
                f"{len(filt)} candidate(s) in {RECENT_DAYS}d, all rejected by the "
                f"mission-relevance gate — 0 reached /approve "
                f"(logs/self-review-filtered.md)")
    if not recent:
        return ("no-proposals",
                f"0 candidates of ANY kind in {RECENT_DAYS}d — self_review may not be "
                f"running or the digest produced no action items")
    return ("no-proposals",
            f"{len(recent)} candidate(s) in {RECENT_DAYS}d but none became a Tier-1 "
            f"proposal and none were filtered — the proposal step may be broken")


def evaluate_empty_night():
    """Record the empty night, and alert when it stops being explainable."""
    verdict, detail = explain_empty_queue()
    st = _streak_load()
    st["count"] = int(st.get("count") or 0) + 1
    if not st.get("since"):
        st["since"] = datetime.now().strftime("%Y-%m-%d")
    n = st["count"]
    _log(f"nothing queued for build [{verdict}] {detail} (empty night #{n}, "
         f"since {st['since']})")

    last = int(st.get("last_alert_at") or 0)
    alarming = verdict in ("no-proposals", "unknown")
    due = (n >= EMPTY_ALERT_AT and not last) or (last and n - last >= EMPTY_REALERT)
    # A broken pipeline should not wait three nights to speak up.
    if alarming and not last:
        due = True

    if due:
        head = ("⚠️ *Dev-Loop has built nothing for "
                f"{n} night(s)* (since {st['since']})")
        body = {
            "waiting-on-buddy": "This is *not* a fault — it is waiting on you.",
            "gate-starved": ("Not a fault in the builder. The relevance gate is "
                             "rejecting everything upstream — check whether the gate "
                             "is right or the digest input has gone generic."),
            "no-proposals": "⛔ *This looks like a real break* — nothing is reaching the queue.",
            "unknown": "⛔ *Could not verify* why. Treating as a break.",
        }.get(verdict, "")
        _notify("\n".join([head, "", f"*{verdict}* — {detail}", "", body,
                            "", "_(Silence here used to look identical to success — S71.)_"]))
        st["last_alert_at"] = n
    _streak_save(st)


def _streak_reset():
    st = _streak_load()
    if int(st.get("count") or 0):
        _log(f"empty-night streak reset after {st['count']} night(s)")
    _streak_save({"count": 0, "since": None, "last_alert_at": 0})


def selftest() -> bool:
    """Offline unit tests for the edit planner: python3 dev_agent.py --selftest

    plan_edits is where a bad patch is supposed to die. It is pure, so it can be
    exercised with no worktree, no model call and no network — which means there
    is no excuse for it being unverified.
    """
    checks = []

    def ck(name, cond):
        checks.append((name, bool(cond)))

    # ── S92: which model does the builder actually use? ──────────────────────
    # The bug this replaces was invisible: .get(k, default) returns the default
    # only when the key is ABSENT, so a key present-and-empty silently defeated
    # it. CUMULUS's credentials.json holds claude_dev_model = "".
    ck("an explicit builder model is used", _builder_model(
        {"claude_dev_model": "claude-opus-5"}) == "claude-opus-5")
    ck("an ABSENT key falls back to the default",
       _builder_model({}) == BUILDER_MODEL_DEFAULT)
    ck("an EMPTY key falls back too (the CUMULUS case, and the whole bug)",
       _builder_model({"claude_dev_model": ""}) == BUILDER_MODEL_DEFAULT)
    ck("  ...as does whitespace-only",
       _builder_model({"claude_dev_model": "   "}) == BUILDER_MODEL_DEFAULT)
    ck("the builder NEVER inherits claude_model (that would downgrade it)",
       _builder_model({"claude_dev_model": "", "claude_model": "claude-haiku-4-5"})
       == BUILDER_MODEL_DEFAULT)

    # ── S91: repair-ticket promotion. T32 — a tempdir, never the live queue. ──
    import tempfile as _tf
    with _tf.TemporaryDirectory() as _td:
        _pd = Path(_td)
        (_pd / "logs/dev-loop").mkdir(parents=True)
        _tickets = _pd / "logs/dev-loop/ticket-queue.jsonl"

        def _mk(tid, status, tier, detail="cirrus-modelhealth.service exits 1: "
                                          "KeyError 'parts' in llm_providers._gemini"):
            return json.dumps({
                "id": tid, "created": "2026-09-01 05:32:00", "requester": "skywarden",
                "origin": "skywarden-repair",
                "title": "cirrus-modelhealth.service is failing", "detail": detail,
                "tier": tier, "status": status,
                "dev_spec": {"id": f"spec-{tid}", "type": "USER_REQUEST", "tier": tier},
            })

        def _mk_client(tid):
            """An S36 end-user intake ticket — queued, Tier-1, and NOT ours."""
            d = json.loads(_mk(tid, "queued", dev_loop.TIER_CONFIRM))
            d["origin"] = "ticket"
            d["requester"] = "bill"
            d["title"] = "Re: Pennrose PA — Snow-Removal Bid Package research"
            return json.dumps(d)

        _tickets.write_text("\n".join([
            _mk("t-queued-1", "queued", dev_loop.TIER_CONFIRM),
            _mk("t-session", "session", dev_loop.TIER_DESIGN),
            _mk("t-refused", "refused", dev_loop.TIER_NEVER),
            _mk("t-tier0", "queued", dev_loop.TIER_AUTO),
            _mk_client("t-client-email"),
        ]) + "\n")

        # No CUMULUS in the selftest: it must never open a network connection.
        _real_remote = globals()["_read_tickets_cumulus"]
        globals()["_read_tickets_cumulus"] = lambda: None
        try:
            n1 = promote_tickets(_pd)
            ck("a queued Tier-1 ticket is promoted to the build queue", n1 == 1)
            _q = [r for r in queue_load(_pd)]
            ck("  ...and a CLIENT intake ticket in the same queue is NOT built",
               not any((r.get("item") or {}).get("source") == "ticket:t-client-email"
                       for r in queue_load(_pd)))
            ck("  ...and only that one — session/refused/Tier-0 are left alone",
               len(_q) == 1 and
               (_q[0].get("item") or {}).get("source") == "ticket:t-queued-1")
            ck("  ...carrying the fields may_build re-classifies from",
               all(k in (_q[0].get("item") or {})
                   for k in ("type", "detail", "source_line", "dev_spec")))
            n2 = promote_tickets(_pd)
            ck("promotion is IDEMPOTENT — a second sweep re-files nothing", n2 == 0)
            ck("  ...and the queue still holds exactly one row", len(queue_load(_pd)) == 1)
            ck("an unreachable CUMULUS returns None, not an empty list "
               "(silent zero == 'nothing to file')",
               _read_tickets_cumulus() is None)
        finally:
            globals()["_read_tickets_cumulus"] = _real_remote

    F = {"a.py": "import os\ndef one():\n    return 1\ndef two():\n    return 2\n"}

    ok, _, ch = plan_edits(F, [{"path": "a.py", "find": "return 1", "replace": "return 11"}])
    ck("a single edit applies", ok and "return 11" in ch["a.py"])
    ck("  ...and leaves the rest of the file alone",
       ok and "def two():" in ch["a.py"] and "import os" in ch["a.py"])

    ok, _, ch = plan_edits({"a.py": "X\n"}, [{"path": "a.py", "find": "X", "replace": "Y"},
                                             {"path": "a.py", "find": "Y", "replace": "Z"}])
    ck("a later edit sees the earlier edit's result", ok and ch["a.py"] == "Z\n")

    # The two rules that do the safety work.
    ok, why, _ = plan_edits(F, [{"path": "a.py", "find": "return 42", "replace": "x"}])
    ck("text that is not in the file is refused (hallucinated quote)",
       not ok and "does not appear" in why)
    ok, why, _ = plan_edits({"a.py": "dup\ndup\n"}, [{"path": "a.py", "find": "dup", "replace": "x"}])
    ck("an ambiguous match is refused, never applied to the first hit",
       not ok and "appears 2 times" in why)

    # A patch must never be half-written to disk.
    ok, why, ch = plan_edits(F, [{"path": "a.py", "find": "return 1", "replace": "return 11"},
                                 {"path": "a.py", "find": "NOT THERE", "replace": "z"}])
    ck("one bad edit discards the whole patch (atomic)", not ok and ch == {})

    ok, why, _ = plan_edits(F, [{"path": "a.py", "find": "", "replace": "x"}])
    ck("an empty 'find' is refused rather than guessed at", not ok and "empty 'find'" in why)
    ok, why, _ = plan_edits(F, [{"path": "nope.py", "find": "x", "replace": "y"}])
    ck("a path that was never shown is refused", not ok and "not provided as context" in why)
    # ---- the edit-application RETRY (S86) ----------------------------------
    # Exercising this for real needs a worktree and a model call, so it is
    # checked at source level -- the same reason ship()'s restart rule is.
    # prop-2026-08-27-649612 died here with attempts=1: a rejected edit was
    # TERMINAL while a failed gate got three tries, exactly backwards.
    # These are AST checks, not text checks, and that is not decoration: the
    # first version asked `"continue" in <source slice>` and PASSED against a
    # deliberately reverted fix, because some other `continue` further down the
    # slice satisfied it. A check that matches text near the thing it is
    # checking is the S84 docstring failure wearing a different hat.
    import ast as _ast
    _tree = _ast.parse(Path(__file__).read_text())
    _bi = next((n for n in _ast.walk(_tree)
                if isinstance(n, _ast.FunctionDef) and n.name == "build_item"), None)
    ck("build_item is parseable (every check below is vacuous without it)", _bi is not None)

    def _branch(varname):
        """The `if <varname> IS NOT None:` node inside build_item.

        The operator check is load-bearing: without it this matched
        `if path_violation is None:` -- which sits three lines earlier and has
        no Return -- and the terminal-path assertion failed against correct
        code. Naming the variable is not the same as naming the branch.
        """
        for n in _ast.walk(_bi or _ast.Module(body=[], type_ignores=[])):
            if isinstance(n, _ast.If) and isinstance(n.test, _ast.Compare) \
               and isinstance(n.test.left, _ast.Name) and n.test.left.id == varname \
               and len(n.test.ops) == 1 and isinstance(n.test.ops[0], _ast.IsNot):
                return n
        return None

    _rej, _pv = _branch("rejected"), _branch("path_violation")
    ck("the rejected-edit branch exists", _rej is not None)
    ck("a rejected edit CONTINUES to a repair attempt",
       _rej is not None and any(isinstance(x, _ast.Continue) for x in _rej.body))
    ck("a rejected edit does NOT return unconditionally (the S86 defect)",
       _rej is not None and not any(isinstance(x, _ast.Return) for x in _rej.body))
    ck("it still gives up at MAX_REPAIR_ATTEMPTS rather than looping forever",
       _rej is not None and any(
           isinstance(x, _ast.If) and any(isinstance(y, _ast.Return) for y in x.body)
           for x in _rej.body))
    # The asymmetry is deliberate and must not be tidied away: a model reaching
    # for a file outside the allowed set is a different signal from one that
    # mis-quoted an anchor.
    ck("a PATH violation is still TERMINAL (no free retry at credentials.json)",
       _pv is not None and any(isinstance(x, _ast.Return) for x in _pv.body)
       and not any(isinstance(x, _ast.Continue) for x in _pv.body))

    _src = _source_between("def build_item(item: dict)", "\ndef awaiting(")
    ck("the repair verdict names the edit-application gate",
       '"gate": "edit-application"' in _src)
    ck("the retry tells the model to copy the find text VERBATIM", "VERBATIM" in _src)

    ok, why, _ = plan_edits(F, [{"path": "a.py", "find": "return 1", "replace": "return 1"}])
    ck("a no-op edit is refused", not ok and "identical" in why)
    ok, why, _ = plan_edits(F, [{"path": "a.py", "find": "return 1", "replace": None}])
    ck("non-string find/replace is refused", not ok and "must be strings" in why)
    ok, why, _ = plan_edits(F, [{"path": "a.py", "find": "x", "replace": "y"}] * (MAX_EDITS_PER_PATCH + 1))
    ck("too many edits is refused", not ok and "too many edits" in why)

    ok, _, ch = plan_edits(F, [{"path": "a.py", "find": "def two():\n    return 2\n", "replace": ""}])
    ck("removing a block is allowed", ok and "def two()" not in ch["a.py"])
    ok, why, _ = plan_edits({"a.py": "only\n"}, [{"path": "a.py", "find": "only\n", "replace": ""}])
    ck("emptying a file is refused (deletions are never automated)",
       not ok and "emptied the file" in why)

    ok, _, ch = plan_edits({"a.py": "X\n", "b.py": "keep\n"},
                           [{"path": "a.py", "find": "X", "replace": "Y"}])
    ck("untouched files are not reported as changed", ok and set(ch) == {"a.py"})

    bad = 0
    for name, good in checks:
        print(f"  {'ok  ' if good else 'FAIL'}  {name}")
        bad += 0 if good else 1
    print(f"all {len(checks)} dev_agent selftests passed" if not bad else f"{bad} FAILED")
    return bad == 0


# ── Repair journal read-back + morning report (S80) ───────────────────────────
def repairs_load(limit: int = 200, path=None):
    """Newest-last journal entries. Never raises on a bad line."""
    fp = Path(path) if path else REPAIRS_FILE
    if not fp.exists():
        return []
    rows = []
    try:
        for line in fp.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return rows[-limit:]


def repairs_text(limit: int = 20, path=None) -> str:
    """Human read-back of HOW fixes were approached — Buddy's ask, S80.

    Shows the approach line for every attempt, not just the ones that worked.
    A journal of successes only would teach the wrong lesson: the refusals and
    the give-ups are the entries worth reading.
    """
    rows = repairs_load(limit=limit, path=path)
    if not rows:
        return "No repair attempts journalled yet."
    out = ["REPAIR JOURNAL (last %d attempts)" % len(rows), ""]
    for r in rows:
        head = "%s  %s  attempt %s/%s  [%s]" % (
            r.get("ts", "?"), r.get("build_id", "?"), r.get("attempt", "?"),
            MAX_REPAIR_ATTEMPTS, r.get("driver", "?"))
        out.append(head)
        out.append("   outcome : %s" % r.get("outcome", "?"))
        if r.get("fixing_gate"):
            out.append("   fixing  : %s" % r["fixing_gate"])
        if r.get("approach"):
            out.append("   approach: %s" % r["approach"])
        if r.get("model_notes"):
            out.append("   notes   : %s" % r["model_notes"])
        if r.get("refusal"):
            out.append("   REFUSED : %s" % r["refusal"])
        if r.get("gate"):
            out.append("   failed  : %s — %s" % (
                r["gate"], (r.get("failure") or "").splitlines()[0][:110]))
        if r.get("members"):
            out.append("   panel   : %s → %s" % ("/".join(r["members"]), r.get("judge")))
        if r.get("files"):
            out.append("   files   : %s" % ", ".join(r["files"]))
        out.append("")
    return "\n".join(out)


# The report runs at 06:30 and describes the 21:30 run -- which is YESTERDAY'S
# calendar date. The first version filtered on `created.startswith(today)` and
# would therefore have reported "nothing built overnight" EVERY SINGLE DAY,
# from a job that was working perfectly, in a message that looks entirely
# normal. Caught before the first real 06:30 fire, by asking what the filter
# would actually match at the moment the daemon runs. A WINDOW, not a date.
REPORT_WINDOW_HOURS = 18   # 06:30 back to 12:30 yesterday: catches the 21:30
                           # run, excludes the night before it.


def _within_window(created: str, now, hours: int) -> bool:
    """Was this build created inside the reporting window?"""
    try:
        when = datetime.strptime(str(created)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return False
    return when > now - timedelta(hours=hours)


def report_text(builds=None, path=None, now=None,
                hours: int = REPORT_WINDOW_HOURS) -> str:
    """The morning report. Says what happened AND what it declined to do.

    A report that lists only successes is the T42 shape — a check that reads
    clean because it never looked. Refusals, give-ups and skipped gates are
    first-class lines here, not omissions.
    """
    builds = builds if builds is not None else builds_load()
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    mine = [b for b in builds if _within_window(b.get("created", ""), now, hours)]
    if not mine:
        return ("Dev-loop %s: nothing built in the last %dh." % (today, hours))

    lines = ["Dev-loop overnight report — %s" % today, ""]
    buckets = {}
    for b in mine:
        buckets.setdefault(b.get("status", "?"), []).append(b)

    for b in buckets.get("awaiting-confirm", []):
        n = b.get("attempts", 1)
        how = "first try" if n <= 1 else "repaired on attempt %d" % n
        lines.append("READY  %s — %s (%s)" % (b.get("id"), b.get("summary", ""), how))
        lines.append("       gates: %s" % ", ".join(b.get("gates_ran") or ["?"]))
        if b.get("excused"):
            lines.append("       NOT CHECKED (already red before this patch): %s"
                         % ", ".join(b["excused"]))
        if (b.get("council") or {}).get("verdict"):
            lines.append("       council: %s" % b["council"]["verdict"])
        lines.append("       reply `ship N` or `discard N`")

    for b in buckets.get("repair-exhausted", []):
        lines.append("GAVE UP %s — %s" % (b.get("id"), b.get("detail", "")))
        lines.append("       tried %d times, never passed: %s"
                     % (b.get("attempts", 0), b.get("repair_gate") or "?"))
        lines.append("       %s" % (b.get("error") or "")[:160])
        lines.append("       worktree kept: %s" % b.get("worktree", "?"))

    for b in buckets.get("shipped", []):
        n = b.get("attempts", 1)
        how = "first try" if n <= 1 else "repaired on attempt %d" % n
        lines.append("SHIPPED %s — %s (%s)" % (b.get("id"), b.get("summary", ""), how))
        if b.get("shipped_sha"):
            lines.append("       live at %s — rollback: git revert %s"
                         % (b["shipped_sha"], b["shipped_sha"]))

    # A build that shipped and then FAILED verify was auto-reverted. That is the
    # single most important line this report can carry, so it is explicit rather
    # than left to the catch-all.
    for b in buckets.get("rolled-back", []):
        lines.append("ROLLED BACK %s — %s" % (b.get("id"), b.get("summary", "")))
        lines.append("       verify failed after deploy; live tree is back on "
                     "the previous commit")

    for b in buckets.get("discarded", []):
        lines.append("DROPPED %s — %s" % (b.get("id"), b.get("summary", "")))

    for key, label in (("cannot-build", "DECLINED"), ("blocked", "BLOCKED"),
                       ("refused", "REFUSED"), ("build-error", "ERROR")):
        for b in buckets.get(key, []):
            lines.append("%s %s — %s" % (label, b.get("id"), (b.get("error") or "")[:160]))

    # Anything not named above still gets a line. S80: the report was written
    # with a bucket per status and `shipped` was not one of them, so the first
    # real report -- for a build that had just gone live -- said only "Attempts
    # journalled: 1" and named nothing. A report that silently drops a status it
    # was not taught about is the same defect as one that lists only successes;
    # enumerate the leftovers rather than trusting the list to stay complete.
    KNOWN = {"awaiting-confirm", "repair-exhausted", "shipped", "rolled-back",
             "discarded", "cannot-build", "blocked", "refused", "build-error"}
    for status, items in sorted(buckets.items()):
        if status in KNOWN:
            continue
        for b in items:
            lines.append("%s %s — %s" % (status.upper(), b.get("id"),
                                         b.get("summary", "") or b.get("detail", "")))

    lines.append("")
    lines.append("Attempts journalled: %d. `dev_agent.py repairs` to read how."
                 % len(repairs_load(limit=500, path=path)))
    return "\n".join(lines)


def run_report():
    """Send the morning report to Telegram and print it."""
    text = report_text()
    print(text)
    _notify(text[:3500])
    _log_job("devreport", True, f"{len(text.splitlines())} line report sent")
    return text




def _log_job(name, ok, note=""):
    """S81: write this run into the job_status ledger.

    dev_agent is the loop that repairs everything else and nothing watched IT.
    An empty night counts as a healthy RUN -- the job did fire and correctly
    found nothing -- so both exits record, otherwise a quiet week would read
    identically to a dead timer.
    """
    try:
        import job_status
        job_status.record(name, ok, note)
    except Exception as e:
        _log(f"job_status.record failed: {e}")


# ── Nightly sweep ─────────────────────────────────────────────────────────────
# ── Repair tickets -> build queue (S91) ───────────────────────────────────────
# Buddy, 2026-09-01: "we spent 25% of our allotted time working on fixing
# issues." The instructive case was cirrus-modelhealth, which failed every
# morning from 2026-08-31 on a four-line code bug. Skywarden (the CUMULUS
# supervisor) detected it, diagnosed it, tried a restart, saw the restart fail,
# and told Buddy — all correct, all within two minutes, for $0.28. Then it sat
# for two days, because a restart cannot repair a deterministic defect and the
# agent had no way to hand the problem to something that writes code.
#
# dev_loop has had ticket_create since S36, and said so in its own source:
# "dev_agent does not read tickets yet." This is that wiring. A ticket filed by
# the supervisor becomes an ordinary Tier-1 build item and goes through every
# existing gate unchanged — classify, may_build, compile, dry-run, council —
# and lands in awaiting-confirm for Buddy's tap. Nothing here ships anything.
CUMULUS_TICKET_HOST = "buddy@192.168.0.204"
CUMULUS_TICKET_PATH = "~/cirrus-digest/logs/dev-loop/ticket-queue.jsonl"


def _read_tickets_local(project_dir=None):
    tf = dev_loop._ticket_path(Path(project_dir) if project_dir else PROJECT_DIR)
    if not tf.exists():
        return []
    out = []
    for line in tf.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _read_tickets_cumulus():
    """CUMULUS's ticket queue, over the existing CIRRUS->cumulus1 SSH link.

    Best-effort ON PURPOSE: the builder lives here, but the supervisor that
    files most repair tickets lives there. If the link is down we log it and
    build what we can — a cross-box read failing must never stop CIRRUS's own
    nightly sweep. It is reported, not swallowed: a silent zero here would look
    exactly like 'CUMULUS had nothing to file', which is the failure shape this
    whole session kept finding.
    """
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             CUMULUS_TICKET_HOST, f"cat {CUMULUS_TICKET_PATH} 2>/dev/null || true"],
            capture_output=True, text=True, timeout=45)
    except Exception as e:  # noqa: BLE001
        _log(f"tickets: CUMULUS unreadable ({type(e).__name__}) — built CIRRUS's only")
        return None
    if r.returncode != 0:
        _log(f"tickets: CUMULUS unreadable (ssh rc={r.returncode}) — built CIRRUS's only")
        return None
    out = []
    for line in (r.stdout or "").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def promote_tickets(project_dir=None):
    """Promote queued Tier-1 tickets into the build queue. Returns how many.

    Idempotent: a ticket whose dev_spec id is already queued or already has a
    build record is skipped, so running this every night never re-files one.
    """
    known = {(r.get("item") or {}).get("dev_spec", {}).get("id")
             for r in queue_load(project_dir)}
    known |= {b.get("id") for b in builds_load(project_dir)}
    known.discard(None)

    tickets = list(_read_tickets_local(project_dir))
    remote = _read_tickets_cumulus()
    if remote:
        tickets += remote

    n = 0
    for t in tickets:
        # ONLY repair tickets. This filter is the whole safety of this function.
        #
        # The ticket queue is SHARED with the S36 end-user direct-intake path,
        # and it already held ten tickets on the day this shipped — old client
        # email threads (a Pennrose snow-removal bid, Bill's Delaware leads,
        # Justin's halftime research) plus two literal "test question" rows.
        # Eight of them were status=queued and Tier-1, so without this line the
        # first nightly sweep would have handed dev_agent a client email and
        # asked it to write a code patch for it. Measured on CIRRUS before the
        # first unattended run, not guessed at.
        #
        # `origin` is set by the filer, not by the ticket's author, and the
        # privileged script hardcodes it — so this cannot be spoofed from
        # outside. Anything else in this queue keeps whatever behaviour it had.
        if t.get("origin") != "skywarden-repair":
            continue
        if t.get("status") != "queued":
            continue                      # 'session'/'refused' need a human
        spec = t.get("dev_spec") or {}
        if spec.get("tier") != dev_loop.TIER_CONFIRM:
            continue                      # Tier-0/2 are not dev_agent's to build
        if spec.get("id") in known:
            continue
        item = {
            "type": "USER_REQUEST",
            "detail": t.get("detail", ""),
            "source_line": t.get("title", ""),
            "why": f"repair ticket filed by {t.get('requester','?')}",
            "source": f"ticket:{t.get('id','')}",
            "added": str(t.get("created", ""))[:10],
            "status": "approved",
            "dev_spec": spec,
        }
        if not may_build(item):
            # The ticket classified Tier-1 when filed but does not now. Trust
            # the stricter answer and say so — never widen at pickup time.
            _log(f"tickets: {spec.get('id')} no longer classifies Tier-1, skipped")
            continue
        queue_append(item, project_dir)
        known.add(spec.get("id"))
        n += 1
        _log(f"tickets: promoted {spec.get('id')} — {item['source_line'][:60]}")
    if n:
        _log(f"tickets: {n} repair ticket(s) promoted into the build queue")
    return n


def run_nightly():
    _log("nightly sweep start")
    promote_tickets()
    todo = find_buildable()
    if not todo:
        evaluate_empty_night()      # S71: is an empty queue the RIGHT outcome?
        _log_job("devloop", True, "0 queued, nothing to build")
        return
    _streak_reset()
    picked = todo[:MAX_BUILDS_PER_RUN]
    _log(f"{len(todo)} queued, building {len(picked)} (cap {MAX_BUILDS_PER_RUN})")
    builds = builds_load()
    done = []
    for item in picked:
        rec = build_item(item)
        bid = rec.get("id")
        builds = [b for b in builds if b.get("id") != bid]   # replace any prior record (e.g. a retried build-error)
        builds.append(rec)
        builds_save(builds)
        # S97: AFTER the record is durable, because maybe_autoship re-reads
        # builds.json to find this build's index. Ordering is the whole reason
        # this call is here and not inside build_item.
        maybe_autoship(rec)
        builds = [b for b in builds_load() if b.get("id") != bid] + [rec]
        builds_save(builds)
        done.append(rec)

    lines = [f"🌙 *Dev-Loop nightly* — {datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]
    for rec in done:
        if rec["status"] == "awaiting-confirm":
            lines.append(f"✅ *{rec['id']}* built + tested — {rec.get('summary','')[:80]}")
            lines.append(f"   {rec.get('diff_stat','').splitlines()[-1] if rec.get('diff_stat') else ''}")
            lines.append(f"   tests: compile {rec.get('test_compile')}, dry-run {rec.get('test_dryrun','')[:40]}")
            c = rec.get("council") or {}
            if c.get("verdict"):
                mark = {"approve": "🟢", "concerns": "🟡", "reject": "🔴"}.get(c["verdict"], "⚪")
                held = "  🔒 HELD (`unhold N` to override)" if rec.get("council_hold") else ""
                lines.append(f"   {mark} council ({'/'.join(c.get('members', []))}→{c.get('judge','')}): "
                             f"{c['verdict']} — {c.get('notes','')[:80]}{held}")
        else:
            lines.append(f"❌ *{rec['id']}* {rec['status']} — {rec.get('error','')[:100]}")
    n_wait = len(awaiting())
    if n_wait:
        lines += ["", f"_{n_wait} build(s) awaiting your confirm — reply `/builds`, "
                      f"then `ship N` or `discard N`._"]
    if len(todo) > len(picked):
        lines.append(f"_({len(todo)-len(picked)} more queued for tomorrow night.)_")
    _notify("\n".join(lines))
    _log("nightly sweep done")
    built = sum(1 for r in done if r.get("status") == "awaiting-confirm")
    _log_job("devloop", True,
             f"{len(done)} built, {built} awaiting confirm, {len(todo)} queued")


# ── Self-test (offline: no creds, no network, no git remotes) ─────────────────
def _source_between(start_marker, end_marker):
    """Return this file's source between two `def` markers, for source-level
    checks. Used where exercising the real path would mean restarting a live
    service inside a selftest."""
    try:
        src = Path(__file__).read_text()
    except Exception:
        return ""
    i = src.find(start_marker)
    j = src.find(end_marker, i + 1) if i >= 0 else -1
    return src[i:j] if i >= 0 and j > i else (src[i:] if i >= 0 else "")


def _selftest():
    globals()["_IN_SELFTEST"] = True
    ok = fail = 0

    def check(name, cond):
        nonlocal ok, fail
        ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")

    # S84 -- the restart rule. deploy() used to DISCARD the kickstart return
    # code, and step 5 then asked `launchctl list`, which still lists the
    # service because the OLD process never died. So a refused restart (a
    # system LaunchDaemon needs root; the API runs as buddy) was recorded as a
    # SUCCESSFUL DEPLOY, and the auto-rollback path reported "auto-reverted and
    # restarted" while the box kept running the code that had just failed
    # verify. Source-level, because exercising it for real means restarting a
    # live service mid-selftest.
    dep = _source_between("def ship(n: int)", "\ndef discard(n: int)")
    check("deploy() captures the restart return code (not discarded)",
          re.search(r"rc,\s*out\s*=\s*_run\(kickstart_cmd", dep or "") is not None)
    check("a failed restart is carried into `fail`, so verify fails",
          "restart_failed" in (dep or "") and "fail = restart_failed" in (dep or ""))
    check("the compile loop cannot clobber a recorded restart failure",
          re.search(r"for p in changed:\s*\n\s*if fail:\s*\n\s*break", dep or "")
          is not None)
    check("a failed ROLLBACK restart is reported, not called 'restarted'",
          "rb_failed" in (dep or "") and "could NOT restart" in (dep or ""))
    check("restarts go through kickstart_cmd (sudo for system/, T51)",
          "kickstart_cmd(" in (dep or "")
          and '["launchctl", "kickstart"' not in (dep or ""))

    # S87 -- retry(). Source-level for the same reason as deploy(): the real
    # path reads and REWRITES the live builds.json, and a selftest must never
    # touch a real state file (T32). The property worth asserting is ORDER --
    # every refusal has to return BEFORE builds_save, or a path violation gets
    # cleared and the S86 decision to keep it terminal is quietly undone.
    import ast
    ret = _source_between("def retry(build_id: str)", "\ndef unhold(n: int)")
    _tree = ast.parse(ret) if ret.strip() else None
    _fn = _tree.body[0] if _tree and isinstance(_tree.body[0], ast.FunctionDef) else None
    _save_at = next((i for i, n in enumerate(ast.walk(_fn) if _fn else [])
                     if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "builds_save"), None)
    check("retry() parses as one function (the source slice is not truncated)",
          _fn is not None and _fn.name == "retry")
    check("retry() refuses a PATH violation by name, not by caller judgement",
          "not in files_to_change" in ret and "REFUSED" in ret)
    check("retry() refuses anything whose status is not 'blocked'",
          re.search(r'status\s*!=\s*"blocked"', ret) is not None)
    # The mutation this is written to catch: moving builds_save above the
    # guards, or dropping a `return`. Both leave the earlier string checks
    # passing while the function starts clearing path violations.
    _guard_returns = [n for n in ast.walk(_fn or ast.Module(body=[], type_ignores=[]))
                      if isinstance(n, ast.Return)]
    check("every refusal is a real `return`, not a logged warning",
          len(_guard_returns) >= 4)
    check("builds_save happens AFTER the refusals, never before",
          _save_at is not None
          and ret.index("builds_save(") > ret.index("REFUSED"))
    check("retry() reports whether the item is ACTUALLY buildable afterwards",
          "find_buildable()" in ret and "YES" in ret and "NO" in ret)

    # path safety
    cases = [
        ("cirrus_daily.py", True), ("tools/registry.py", True),
        ("docs/notes.md", True), ("config/sources.json", True),
        ("config/email_omit.txt", True),
        ("config/credentials.json", False), ("config/cookies.json", False),
        ("../outside.py", False), ("/etc/passwd", False),
        ("~/x.py", False), ("run.sh", False), ("com.cirrus.bot.plist", False),
        ("logs/devloop.log", False), ("digests/x.md", False),
        ("config/other.json", False), ("email_state.json", False),
        ("secret_helper.py", False),
    ]
    for p, want in cases:
        got, why = patch_path_ok(p)
        check(f"patch_path_ok({p!r}) -> {want} ({why})", got is want)

    # patch validation
    check("validate_patch: empty list rejected", not validate_patch([])[0])
    check("validate_patch: too many files rejected",
          not validate_patch([{"path": f"f{i}.py", "content": "x"} for i in range(5)])[0])
    check("validate_patch: empty content rejected",
          not validate_patch([{"path": "a.py", "content": "  "}])[0])
    check("validate_patch: good patch accepted",
          validate_patch([{"path": "cirrus_daily.py", "content": "# ok"}])[0])

    # model JSON parsing
    j = parse_model_json('```json\n{"summary":"s","files":[],"notes":"n"}\n```')
    check("parse_model_json: fenced", j["summary"] == "s")
    j = parse_model_json('Sure!\n{"summary":"s2","files":[{"path":"a.py","content":"c"}],"notes":""} thanks')
    check("parse_model_json: prose-wrapped", j["files"][0]["path"] == "a.py")

    # build_model_patch: a truncated/empty first reply is retried, not fatal (S47 #8)
    _orig_call = call_claude_build
    _replies = iter(['{"summary":"s","files":[',                    # truncated -> ValueError
                     '{"summary":"ok","files":[],"notes":""}'])     # valid on retry
    globals()["call_claude_build"] = lambda s, u: next(_replies)
    try:
        _jj = build_model_patch("sys", "usr", attempts=2)
        check("build_model_patch: retries then parses", _jj["summary"] == "ok")
    finally:
        globals()["call_claude_build"] = _orig_call

    # ...and that the failure it just logged did NOT land in the operational log
    # (S82). The retry above is the only place the suite exercises _log's error
    # path, so this is measured right where it happens, not asserted in theory.
    _lf = PROJECT_DIR / "logs/devloop.log"
    _before = _lf.stat().st_size if _lf.exists() else -1
    _log("selftest log-isolation probe")
    _after = _lf.stat().st_size if _lf.exists() else -1
    check("_log does not write the live devloop.log during selftest",
          _IN_SELFTEST and _after == _before)

    # tier gate
    check("may_build: Tier-1 dedupe note builds",
          may_build({"type": "CIRRUS_NOTE", "detail": "improve article dedupe in the digest"}))
    check("may_build: Tier-0 source does NOT build",
          not may_build({"type": "ADD_SOURCE", "detail": "subscribe to MLQ.ai rss feed"}))
    check("may_build: NEVER item does NOT build",
          not may_build({"type": "CIRRUS_NOTE", "detail": "rotate the api token"}))
    check("may_build: Tier-2 send-path does NOT build",
          not may_build({"type": "CIRRUS_NOTE", "detail": "refactor send_digest delivery"}))

    # queue + builds round-trip in a temp dir
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        item = {"type": "CIRRUS_NOTE", "detail": "improve article dedupe in the digest",
                "dev_spec": {"id": "prop-t-1", "tier": dev_loop.TIER_CONFIRM}}
        queue_append(item, td)
        queue_append(item, td)   # duplicate — must dedupe on load
        rows = queue_load(td)
        check("queue: append + dedupe by spec id", len(rows) == 1)
        builds_save([{"id": "prop-t-1", "status": "awaiting-confirm"}], td)
        check("builds: round-trip", builds_load(td)[0]["id"] == "prop-t-1")
        # a settled build is not re-attempted...
        check("find_buildable: skips settled id",
              not any(i["dev_spec"]["id"] == "prop-t-1" for i in find_buildable(td)))
        # ...but a transient build-error is retried
        builds_save([{"id": "prop-t-1", "status": "build-error"}], td)
        check("find_buildable: retries build-error id",
              any(i["dev_spec"]["id"] == "prop-t-1" for i in find_buildable(td)))

    # prompt shape
    sys_p, usr_p = build_prompt(
        {"detail": "improve dedupe", "dev_spec": {"id": "p1", "type": "CIRRUS_NOTE",
         "tier_name": "Tier 1 (one-tap confirm)", "files_to_change": ["cirrus_daily.py"],
         "test_plan": "dry-run"}},
        {"cirrus_daily.py": "print('hi')"})
    check("build_prompt: JSON contract in system", '"files"' in sys_p)
    check("build_prompt: file content included", "print('hi')" in usr_p)
    check("build_prompt: CANNOT_BUILD escape hatch", "CANNOT_BUILD" in sys_p)

    # ── self-repair loop (S80) ────────────────────────────────────────────────
    import tempfile

    # -- pure helpers ---------------------------------------------------------
    check("module_name: .py -> module", module_name("halftime_routing.py") == "halftime_routing")
    check("module_name: non-.py -> ''", module_name("notes.md") == "")

    check("failure_signature: same failure -> same sig",
          failure_signature("selftest", "FAIL a sweep that broke")
          == failure_signature("selftest", "FAIL a sweep that broke"))
    check("failure_signature: different failure -> different sig",
          failure_signature("selftest", "FAIL one thing")
          != failure_signature("selftest", "FAIL another thing"))
    check("failure_signature: whitespace-insensitive",
          failure_signature("g", "FAIL  x   y") == failure_signature("g", "FAIL x y"))

    check("no_progress: two identical failures -> stop",
          no_progress([{"signature": "a"}, {"signature": "a"}]))
    check("no_progress: two different failures -> keep going",
          not no_progress([{"signature": "a"}, {"signature": "b"}]))
    check("no_progress: a single failure is not yet circling",
          not no_progress([{"signature": "a"}]))

    # -- a repair may not weaken test code ------------------------------------
    # A repair may not REMOVE assertions. It may add them, and it may rewrite a
    # file that has them so long as they survive (S80 second pass -- the first
    # version refused any patch CONTAINING test code, which made a
    # test-writing task unrepairable and blocked honest rewrites).
    THREE = ("def selftest():\n"
             "    check(\"a\", 1)\n"
             "    check(\"b\", 2)\n"
             "    assert True\n")
    B = {"m.py": THREE}

    check("test_weight counts each assertion", test_weight(THREE) == 4)
    check("test_weight of nothing is 0", test_weight("") == 0 and test_weight(None) == 0)

    check("removing one check is REFUSED",
          not weakens_tests({"m.py": THREE.replace('    check("b", 2)\n', "")}, B)[0])
    check("  ...and the refusal says how many went missing",
          "1 of 4" in weakens_tests({"m.py": THREE.replace('    check("b", 2)\n', "")}, B)[1])
    check("gutting selftest() entirely is REFUSED",
          not weakens_tests({"m.py": "def one():\n    return 1\n"}, B)[0])
    check("deleting only the selftest DEF is REFUSED even if checks remain",
          not weakens_tests({"m.py": '    check("a", 1)\n    check("b", 2)\n    assert True\n'}, B)[0])

    check("keeping every assertion while rewriting the file is ALLOWED",
          weakens_tests({"m.py": THREE + "\ndef extra():\n    return 2\n"}, B)[0])
    check("ADDING a test is ALLOWED — the whole point of the second pass",
          weakens_tests({"m.py": THREE + '    check("c", 3)\n'}, B)[0])
    check("a NEW file that is all tests is ALLOWED (nothing to lose)",
          weakens_tests({"new.py": THREE}, B)[0])
    check("a patch touching no tests at all is ALLOWED",
          weakens_tests({"m.py": THREE.replace("return 1", "return 2")}, B)[0])
    check("an empty patch is ALLOWED", weakens_tests({}, B)[0])

    with tempfile.TemporaryDirectory() as td:
        wt = Path(td)
        (wt / "base.py").write_text(
            "def one():\n    return 1\n\n"
            "def selftest():\n    return 0 if one() == 1 else 1\n\n"
            "import sys\n"
            "if __name__ == '__main__':\n    sys.exit(selftest())\n")
        (wt / "dep.py").write_text(
            "import base\n\n"
            "def selftest():\n    return 0 if base.one() == 1 else 1\n\n"
            "import sys\n"
            "if __name__ == '__main__':\n    sys.exit(selftest())\n")
        (wt / "lonely.py").write_text("def x():\n    return 1\n")

        # -- importer graph (the S79 gate) -----------------------------------
        check("importers_of: finds the module that imports it",
              importers_of("base", wt) == ["dep.py"])
        check("importers_of: does not list the module itself",
              "base.py" not in importers_of("base", wt))
        check("importers_of: nothing imports dep", importers_of("dep", wt) == [])
        check("importers_of: unknown module is empty, not an error",
              importers_of("nosuch", wt) == [])

        check("has_selftest: true when defined AND dispatched",
              has_selftest(wt / "base.py"))
        check("has_selftest: false for a module without one",
              not has_selftest(wt / "lonely.py"))

        # -- S81: WHICH argv does the module answer to? ------------------------
        # Gate 2 used to invoke every file as `python3 <file> selftest`. Thirteen
        # modules in this tree dispatch only on `--selftest`, so the bare word
        # matched nothing, python ran their DEFAULT __main__ path, it exited 0,
        # and the gate recorded a passing selftest having run no tests. Measured
        # on the real tree, not theorised. These checks pin every convention.
        conv = wt / "conv"
        conv.mkdir(exist_ok=True)
        (conv / "dashed.py").write_text(
            "import sys\ndef selftest():\n    return True\n"
            "if __name__ == '__main__':\n"
            "    if '--selftest' in sys.argv:\n        sys.exit(0)\n"
            "    print('PRODUCTION PATH')\n")
        (conv / "bare.py").write_text(
            "import sys\ndef selftest():\n    return True\n"
            "if __name__ == '__main__':\n"
            "    if 'selftest' in sys.argv:\n        sys.exit(0)\n")
        (conv / "both.py").write_text(
            "import sys\ndef selftest():\n    return True\ndef _selftest():\n    return True\n"
            "if __name__ == '__main__':\n"
            "    if '--selftest' in sys.argv:\n        sys.exit(0)\n"
            "    if sys.argv[1:] == ['selftest']:\n        sys.exit(0)\n")
        (conv / "noargv.py").write_text(
            "import sys\ndef _selftest():\n    return True\n"
            "if __name__ == '__main__':\n    sys.exit(_selftest())\n")
        (conv / "busy_main.py").write_text(
            "import sys\ndef selftest():\n    return True\n"
            "if __name__ == '__main__':\n    print('does real work')\n    selftest()\n")

        check("selftest_argvs: a --selftest-only module is invoked with --selftest",
              selftest_argvs(conv / "dashed.py") == [["--selftest"]])
        check("selftest_argvs: a bare-selftest module is invoked with selftest",
              selftest_argvs(conv / "bare.py") == [["selftest"]])
        # THE regression this file nearly shipped: dev_agent itself answers to
        # BOTH, and they are DIFFERENT suites -- `selftest` is the 100-check main
        # one, `--selftest` the 14-check edit planner. Preferring either would
        # run a fraction of the tests and call the file green, which is the exact
        # defect S80 fixed in dev-agent-selftest. So: run both, never guess.
        check("selftest_argvs: a module answering to BOTH gets BOTH run",
              selftest_argvs(conv / "both.py") == [["--selftest"], ["selftest"]])
        check("selftest_argvs: dev_agent itself answers to both",
              len(selftest_argvs(Path(__file__))) == 2)
        # `def _selftest` -- dev_loop's spelling, and the RISK CLASSIFIER was
        # therefore invisible to gate 2 entirely.
        check("selftest_argvs: an argv-less __main__ selftest is invoked bare",
              selftest_argvs(conv / "noargv.py") == [[]])
        check("selftest_argvs: _selftest is recognised, not just selftest",
              has_selftest(conv / "noargv.py"))
        # ...but only when __main__ does NOTHING ELSE. Running a file bare that
        # also does real work would have a GATE execute a production path.
        check("selftest_argvs: a __main__ that also does real work is NOT invoked bare",
              selftest_argvs(conv / "busy_main.py") == [])
        check("selftest_argvs: a file with no selftest at all yields nothing",
              selftest_argvs(wt / "lonely.py") == [])
        # The commonest idiom in this tree, and the one a first tightening of
        # the regex broke: `args = sys.argv[1:]` on one line, `"selftest" in
        # args` on another. Ten real modules are written this way.
        (conv / "argsidiom.py").write_text(
            "import sys\ndef selftest():\n    return True\n"
            "if __name__ == '__main__':\n    args = sys.argv[1:]\n"
            "    if 'selftest' in args:\n        sys.exit(0)\n")
        check("selftest_argvs: the `\"selftest\" in args` idiom is recognised",
              selftest_argvs(conv / "argsidiom.py") == [["selftest"]])
        # A docstring that merely MENTIONS selftest must not make us pass an
        # argument the module ignores -- that is the false pass, rebuilt.
        (conv / "mentions.py").write_text(
            '"""Run me with: python3 mentions.py selftest"""\n'
            "import sys\ndef selftest():\n    return True\n"
            "if __name__ == '__main__':\n    print('real work')\n")
        check("selftest_argvs: a docstring mention is not a dispatch",
              selftest_argvs(conv / "mentions.py") == [])

        # -- S81: gate 4 must follow the IMPORT GRAPH, not just filenames ------
        # self_review.py is a DRYRUN_TRIGGER and imports ensemble, so a change
        # to ensemble.py could break self_review's dry-run with gate 4 never
        # firing. The two most-imported modules in the tree were getting
        # thinner coverage than a leaf file.
        dr = wt / "dr"
        dr.mkdir(exist_ok=True)
        (dr / "self_review.py").write_text("import helper\n")     # a real trigger name
        (dr / "helper.py").write_text("x = 1\n")
        (dr / "loner.py").write_text("y = 2\n")
        (dr / "twohop.py").write_text("z = 3\n")
        (dr / "helper2.py").write_text("import twohop\n")
        check("dryrun_reachable: a file imported by a trigger reaches gate 4",
              dryrun_reachable(["helper.py"], dr))
        check("dryrun_reachable: a file nothing triggers imports does not",
              not dryrun_reachable(["loner.py"], dr))
        check("dryrun_reachable: a TRIGGER itself is left to the name check",
              not dryrun_reachable(["self_review.py"], dr))
        # The stated limit, pinned so nobody assumes it is transitive.
        check("dryrun_reachable: is ONE HOP only (two hops does NOT reach)",
              not dryrun_reachable(["twohop.py"], dr))
        check("dryrun_reachable: non-.py changes are ignored",
              not dryrun_reachable(["config/sources.json"], dr))
        check("dryrun_reachable: empty change list is False, not an error",
              not dryrun_reachable([], dr))

        # RATCHET against the real tree. Tightening this regex loses coverage as
        # easily as loosening it invents it, and both happened while writing it.
        # A count measured against reality catches the direction a fixture cannot.
        real = [f for f in Path(__file__).resolve().parent.glob("*.py")
                if selftest_argvs(f)]
        check("selftest_argvs: at least 30 real modules stay invokable (got %d)"
              % len(real), len(real) >= 30)

        # End to end: the gate must FAIL a dashed module whose tests fail, where
        # before it would have run the production path and passed.
        (conv / "dashed_fails.py").write_text(
            "import sys\ndef selftest():\n    return False\n"
            "if __name__ == '__main__':\n"
            "    if '--selftest' in sys.argv:\n        sys.exit(1)\n"
            "    print('PRODUCTION PATH'); sys.exit(0)\n")
        vconv = verify_build(conv, ["dashed_fails.py"], run_dryrun=False, run_remote=False)
        check("gate 2 now FAILS a --selftest module whose tests fail",
              not vconv["ok"] and vconv["gate"] == "selftest")

        # -- gates, in order --------------------------------------------------
        v = verify_build(wt, ["base.py"], run_remote=False)
        check("verify_build: a healthy patch passes", v["ok"])
        check("verify_build: it ran compile, selftest AND dependents",
              v["ran"] == ["compile", "selftest(1/1)", "dependents(1)"])
        # S80 live-run finding: a module with NO selftest must not report a
        # gate that inspected nothing as simply "selftest" (T42 shape).
        check("verify_build: a module with no selftest reports 0 checked",
              "selftest(0/1)" in verify_build(wt, ["lonely.py"], run_remote=False)["ran"])
        check("verify_build: and 0-inspected still passes, it does not fail",
              verify_build(wt, ["lonely.py"], run_remote=False)["ok"])
        check("verify_build: dependents count reflects suites actually run",
              "dependents(1)" in verify_build(wt, ["base.py"], prebroken={"dep.py"}, run_remote=False)["ran"]
              or "dependents(0)" in verify_build(wt, ["base.py"], prebroken={"dep.py"}, run_remote=False)["ran"])
        check("verify_build: it does NOT claim to have run the dry-run",
              "dryrun" not in v["ran"])

        (wt / "broken.py").write_text("def x(:\n")
        v = verify_build(wt, ["broken.py"], run_remote=False)
        check("verify_build: a syntax error fails at the compile gate",
              not v["ok"] and v["gate"] == "compile")

        (wt / "failing.py").write_text(
            "def selftest():\n    print('FAIL the thing')\n    return 1\n\n"
            "import sys\n"
            "if __name__ == '__main__':\n    sys.exit(selftest())\n")
        v = verify_build(wt, ["failing.py"], run_remote=False)
        check("verify_build: a failing selftest is caught (py_compile never would)",
              not v["ok"] and v["gate"] == "selftest")
        check("verify_build: the failure text reaches the model",
              "FAIL the thing" in v["detail"])

        # THE S79 CASE: the changed module is fine; the module that IMPORTS it
        # is broken by the change. Today's loop misses this entirely.
        (wt / "base.py").write_text(
            "def one():\n    return 99\n\n"
            "def selftest():\n    return 0\n\n"
            "import sys\n"
            "if __name__ == '__main__':\n    sys.exit(selftest())\n")
        v = verify_build(wt, ["base.py"], run_remote=False)
        check("verify_build: a change that breaks a DEPENDENT's suite is caught",
              not v["ok"] and v["gate"] == "dependents")
        check("verify_build: and it names which dependent, and why",
              "dep.py" in v["detail"] and "imports base" in v["detail"])

        # -- a dependent that was ALREADY red is not this patch's fault -------
        check("failing_selftests: spots a suite that is already red",
              failing_selftests(wt, ["failing.py", "base.py"]) == {"failing.py"})
        check("failing_selftests: ignores a module with no selftest",
              failing_selftests(wt, ["lonely.py"]) == set())
        v = verify_build(wt, ["base.py"], prebroken={"dep.py"}, run_remote=False)
        check("verify_build: an already-red dependent is excused, not blamed",
              v["ok"] and v["excused"] == ["dep.py"])
        check("verify_build: but the excuse is REPORTED, never silent",
              "dep.py" in report_text(builds=[{"id": "b", "status": "awaiting-confirm",
                  "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  "summary": "s", "attempts": 1, "gates_ran": ["compile"],
                  "excused": v["excused"]}]))
        check("verify_build: a dependent NOT in prebroken still fails the gate",
              not verify_build(wt, ["base.py"], prebroken=set(), run_remote=False)["ok"])

        # -- gate 5: verify on CUMULUS (S97) ----------------------------------
        # Gates 1-4 all run on this Mac. A CUMULUS fix touches systemd, journald
        # and Linux `ps`, and none of that is exercised here -- the builder could
        # ship to the box serving Bill, Alyssa and Justin having never run the
        # code there. Faked at the module boundary so this suite stays offline;
        # remote_verify's own suite pins the decision logic with both boxes faked.
        import types as _types
        _saved_rv = sys.modules.get("remote_verify")
        try:
            _fake = _types.ModuleType("remote_verify")
            _fake.verify_on_cumulus = lambda w, c: {
                "ok": False, "detail": "m.py passes on CIRRUS and FAILS on CUMULUS",
                "ran": "cumulus(1 checked, 0 excused)", "excused": []}
            sys.modules["remote_verify"] = _fake
            vfail = verify_build(wt, ["base.py"], prebroken={"dep.py"})
            check("gate 5: a patch that breaks on CUMULUS FAILS the build",
                  not vfail["ok"] and vfail["gate"] == "cumulus")
            check("gate 5: ...and the ran list records it",
                  any("cumulus(" in g for g in vfail["ran"]))

            _fake.verify_on_cumulus = lambda w, c: {
                "ok": True, "detail": "", "ran": "cumulus(1 checked, 0 excused)",
                "excused": ["dep.py (rc=1 at HEAD)"]}
            vpass = verify_build(wt, ["base.py"], prebroken={"dep.py"})
            check("gate 5: a patch that survives CUMULUS passes", vpass["ok"])
            check("gate 5: an excused module is carried, not silently dropped",
                  any("dep.py" in e for e in vpass["excused"]))

            # An unreachable box must not fail the build, and must not claim a
            # verification either -- the same rule the rest of today enforces.
            _fake.verify_on_cumulus = lambda w, c: {
                "ok": True, "detail": "ssh down", "ran": "cumulus(unreachable)",
                "excused": []}
            vunr = verify_build(wt, ["base.py"], prebroken={"dep.py"})
            check("gate 5: an unreachable CUMULUS does not fail the build",
                  vunr["ok"])
            check("gate 5: ...and the ran list SAYS it was unreachable",
                  any("unreachable" in g for g in vunr["ran"]))

            # A broken helper must never read as a pass.
            def _boom(w, c):
                raise RuntimeError("helper exploded")
            _fake.verify_on_cumulus = _boom
            verr = verify_build(wt, ["base.py"], prebroken={"dep.py"})
            check("gate 5: a helper that raises is reported, not treated as pass",
                  verr["ok"] and any("cumulus(error" in g for g in verr["ran"]))

            check("gate 5: run_remote=False leaves no cumulus claim at all",
                  not any("cumulus" in g for g in
                          verify_build(wt, ["base.py"], prebroken={"dep.py"}, run_remote=False)["ran"]))
        finally:
            if _saved_rv is None:
                sys.modules.pop("remote_verify", None)
            else:
                sys.modules["remote_verify"] = _saved_rv

        # -- auto-ship (S97) --------------------------------------------------
        # The decision is pinned in autoship.py's own 17 cases. What is pinned
        # HERE is the wiring, because the first version ran inside build_item --
        # before builds_save -- so awaiting() never contained the build and
        # auto-ship would have silently never fired. A feature that quietly does
        # nothing is the defect this session exists to remove.
        _as_src = _source_between("def maybe_autoship(rec)", "\ndef awaiting(")
        check("maybe_autoship refuses a build that is not awaiting-confirm",
              'rec.get("status") != "awaiting-confirm"' in (_as_src or ""))
        check("maybe_autoship reads the ORIGINAL from git, stdout only",
              "g.stdout if g.returncode == 0 else None" in (_as_src or ""))
        check("a failure to DECIDE leaves the build waiting, never ships it",
              "left for Buddy" in (_as_src or ""))
        _sweep = _source_between("picked = todo[:MAX_BUILDS_PER_RUN]", "\n    lines = [")
        check("the sweep calls maybe_autoship AFTER builds_save",
              _sweep is not None
              and _sweep.index("builds_save(builds)") < _sweep.index("maybe_autoship(rec)"))
        check("...and build_item itself does NOT auto-ship",
              "maybe_autoship" not in (_source_between(
                  "def build_item(item: dict)", "\ndef maybe_autoship(") or ""))

        # -- the journal ------------------------------------------------------
        jf = wt / "repairs.jsonl"
        check("repairs_load: a missing journal is empty, not an error",
              repairs_load(path=jf) == [])
        jf.write_text(json.dumps({"build_id": "b1", "attempt": 2, "driver": "council",
                                  "outcome": "fixed", "approach": "widen the regex"})
                      + "\nNOT JSON\n")
        rows = repairs_load(path=jf)
        check("repairs_load: reads good lines and skips a corrupt one", len(rows) == 1)
        txt = repairs_text(path=jf)
        check("repairs_text: shows the approach, not just the outcome",
              "widen the regex" in txt and "fixed" in txt)
        check("repairs_text: shows which driver did it", "council" in txt)
        check("repairs_text: empty journal says so plainly",
              "No repair attempts" in repairs_text(path=wt / "none.jsonl"))

    # -- the morning report is honest about failure ---------------------------
    # THE REAL TIMING: the daemon fires at 06:30 and must describe the 21:30 run
    # from the night before -- a different calendar date. Every case below uses
    # those two clocks, because a test written at "now" passes against a filter
    # that would report nothing at the only moment it actually runs.
    RUN_AT   = datetime(2026, 8, 26, 21, 30, 0)          # the nightly build
    READ_AT  = datetime(2026, 8, 27, 6, 30, 0)           # the morning report
    today = RUN_AT.strftime("%Y-%m-%d %H:%M:%S")

    check("report: a build from 21:30 IS in the 06:30 report (different DATE)",
          "b9" in report_text(now=READ_AT, builds=[
              {"id": "b9", "status": "awaiting-confirm", "created": today,
               "summary": "s", "attempts": 1, "gates_ran": ["compile"]}]))
    check("report: the night BEFORE last is not dragged in",
          "b8" not in report_text(now=READ_AT, builds=[
              {"id": "b8", "status": "shipped", "attempts": 1,
               "created": (RUN_AT - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
               "summary": "old"}]))
    check("report: an unparseable created stamp is excluded, not crashed on",
          "b7" not in report_text(now=READ_AT, builds=[
              {"id": "b7", "status": "shipped", "created": "who knows", "summary": "x"}]))
    check("report: a quiet night names the window it looked at",
          "last 18h" in report_text(now=READ_AT, builds=[]))

    rpt = report_text(now=READ_AT, builds=[
        {"id": "b1", "status": "awaiting-confirm", "created": today,
         "summary": "cache the mission text", "attempts": 2,
         "gates_ran": ["compile", "selftest", "dependents"]},
        {"id": "b2", "status": "repair-exhausted", "created": today,
         "detail": "widen the parser", "attempts": 3, "repair_gate": "selftest",
         "error": "still failing", "worktree": "/tmp/wt"}])
    check("report_text: a repaired build says which attempt fixed it",
          "repaired on attempt 2" in rpt)
    check("report_text: a give-up is reported, not omitted", "GAVE UP" in rpt)
    check("report_text: the give-up names the gate that never went green",
          "selftest" in (rpt.split("GAVE UP")[1] if "GAVE UP" in rpt else ""))
    check("report_text: it keeps the worktree for inspection",
          "/tmp/wt" in rpt)
    check("report_text: it lists which gates actually ran",
          "dependents" in rpt)
    check("report_text: a quiet night says nothing was built",
          "nothing built" in report_text(now=READ_AT, builds=[]))

    # S80, found on the daemon's FIRST real run: `shipped` was not a bucket, so
    # a build that had just gone live produced a report naming nothing at all.
    srep = report_text(now=READ_AT, builds=[
        {"id": "b3", "status": "shipped", "created": today, "summary": "the thing",
         "attempts": 1, "shipped_sha": "d95c4f2"},
        {"id": "b4", "status": "rolled-back", "created": today, "summary": "bad one"},
        {"id": "b5", "status": "some-status-nobody-taught-it", "created": today,
         "summary": "unknown state"}])
    check("report_text: a SHIPPED build is named, not silently dropped",
          "SHIPPED b3" in srep and "the thing" in srep)
    check("report_text: and carries the sha and its rollback",
          "d95c4f2" in srep and "git revert" in srep)
    check("report_text: an auto-rollback is called out explicitly",
          "ROLLED BACK b4" in srep)
    check("report_text: an UNKNOWN status still gets a line (no silent drop)",
          "b5" in srep and "SOME-STATUS-NOBODY-TAUGHT-IT" in srep)

    # -- the repair prompt carries the failure and the hard rule --------------
    item = {"detail": "d", "dev_spec": {"id": "x", "files_to_change": ["a.py"]}}
    sysp, usrp = repair_prompt(item, {"a.py": "print(1)\n"}, "", set(),
                               {"gate": "selftest", "detail": "FAIL the thing"},
                               "diff --git a b", 2)
    check("repair_prompt: tells the model it is repairing its own attempt",
          "REPAIRING YOUR OWN" in sysp)
    check("repair_prompt: forbids REMOVING an assertion", "do NOT REMOVE" in sysp)
    check("repair_prompt: and explicitly PERMITS adding one",
          "You MAY add new ones" in sysp)
    check("repair_prompt: includes how the last attempt broke",
          "FAIL the thing" in usrp and "selftest" in usrp)
    check("repair_prompt: patches against the ORIGINAL files, not the broken ones",
          "ORIGINAL, unpatched" in sysp)

    print(f"\n{ok} passed, {fail} failed")
    return fail == 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "nightly"
    if cmd == "selftest":
        sys.exit(0 if _selftest() else 1)
    elif cmd == "nightly":
        run_nightly()
    elif cmd == "list":
        print(list_builds_text())
    elif cmd == "ship" and len(sys.argv) > 2:
        print(ship(int(sys.argv[2])))
    elif cmd == "discard" and len(sys.argv) > 2:
        print(discard(int(sys.argv[2])))
    elif cmd == "unhold" and len(sys.argv) > 2:
        print(unhold(int(sys.argv[2])))
    elif cmd == "repairs":
        print(repairs_text(int(sys.argv[2]) if len(sys.argv) > 2 else 20))
    elif cmd == "report":
        run_report()
    elif cmd == "review-test":
        # Live check of the council review path on this box (no build/worktree).
        # Feeds a SAFE sample patch and an UNSAFE one; prints the panel's verdict.
        creds = _creds()
        good_item = {"detail": "Cache the mission text so the relevance gate doesn't re-read it per item",
                     "summary": "cache _mission() result", "notes": "pure refactor"}
        good_diff = ("--- a/self_review.py\n+++ b/self_review.py\n"
                     "@@\n-def _mission():\n-    return open('config/mission.txt').read()\n"
                     "+_M=None\n+def _mission():\n+    global _M\n+    if _M is None:\n"
                     "+        _M=open('config/mission.txt').read()\n+    return _M\n")
        bad_item = {"detail": "Improve logging", "summary": "add debug logging",
                    "notes": "also prints the api token for debugging"}
        bad_diff = ("--- a/cirrus_daily.py\n+++ b/cirrus_daily.py\n"
                    "@@\n+import json\n+creds=json.load(open('config/credentials.json'))\n"
                    "+print('DEBUG anthropic key:', creds['anthropic_api_key'])\n")
        print(f"providers: {__import__('ensemble').L.available(creds)}")
        for label, it, df in (("SAFE refactor", good_item, good_diff),
                              ("UNSAFE (prints api key)", bad_item, bad_diff)):
            r = _review_diff(it, df, creds)
            print(f"\n[{label}] verdict={r.get('verdict')} "
                  f"({'/'.join(r.get('members', []))}→{r.get('judge')})\n  {r.get('notes')}")
    elif cmd == "--selftest":
        sys.exit(0 if selftest() else 1)
    else:
        print(__doc__)
