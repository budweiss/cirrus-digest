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

import dev_loop

PROJECT_DIR = Path.home() / "projects/cirrus-digest"
WORK_ROOT   = Path.home() / "projects/dev-loop-work"     # worktrees live here
QUEUE_FILE  = PROJECT_DIR / "logs/dev-loop/build-queue.jsonl"
BUILDS_FILE = PROJECT_DIR / "logs/dev-loop/builds.json"

MAX_BUILDS_PER_RUN  = 2          # dry-runs are ~13 min each — cap the night
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

DRYRUN_TIMEOUT = 30 * 60
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


def call_claude_build(system: str, user: str):
    """One-shot Claude API call. Returns raw text. Raises on transport error."""
    import requests
    creds = _creds()
    key = creds.get("anthropic_api_key", "")
    if not key:
        raise RuntimeError("no anthropic_api_key in credentials.json")
    model = creds.get("claude_dev_model", "claude-sonnet-5")
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


def _log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] dev_agent: {msg}"
    print(line)
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
_TEST_CODE_RX = re.compile(r"(def\s+selftest|\bcheck\(|\bassert\s|FAILURES:)")


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


def has_selftest(fp) -> bool:
    """True if the file defines selftest() AND dispatches to it from argv."""
    try:
        text = Path(fp).read_text(errors="ignore")
    except Exception:
        return False
    return "def selftest" in text and "selftest" in text.split("def selftest")[-1]


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


def touches_test_code(patch: dict):
    """(ok, reason) — refuse a REPAIR patch that edits test code.

    The original build may ADD tests; that is good and is not routed here. Only
    repair attempts are checked, and only for weakening what already exists.
    """
    for f in (patch.get("files") or []):
        if _TEST_CODE_RX.search(str(f.get("content", ""))):
            return False, "repair rewrote a file containing test code (%s)" % f.get("path", "?")
    for e in (patch.get("edits") or []):
        found = str((e or {}).get("find", ""))
        repl = str((e or {}).get("replace", ""))
        if _TEST_CODE_RX.search(found) or _TEST_CODE_RX.search(repl):
            return False, "repair edited test code in %s" % (e or {}).get("path", "?")
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
        rc, _out = _run([sys.executable, str(fp), "selftest"],
                        cwd=wt, timeout=SELFTEST_TIMEOUT)
        if rc != 0:
            out.add(m)
    return out


def verify_build(wt, changed, run_dryrun: bool = True, prebroken=()) -> dict:
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
        if not has_selftest(fp):
            continue
        n_self += 1
        rc, out = _run([sys.executable, str(fp), "selftest"],
                       cwd=wt, timeout=SELFTEST_TIMEOUT)
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
            if not has_selftest(fp):
                continue
            n_dep += 1
            rc, out = _run([sys.executable, str(fp), "selftest"],
                           cwd=wt, timeout=SELFTEST_TIMEOUT)
            if rc != 0:
                d = ("%s selftest failed (it imports %s, which this patch "
                     "changed):\n%s" % (dep, module_name(p), out[-1200:]))
                ran.append("dependents(%d)" % n_dep)
                return {"ok": False, "gate": "dependents", "detail": d,
                        "signature": failure_signature("dependents:" + dep, out),
                        "ran": ran, "excused": excused}

    # gate 4 — full daily dry-run when a core digest file changed
    if run_dryrun and (set(changed) & DRYRUN_TRIGGERS):
        ran.append("dryrun")
        rc, out = _run([sys.executable, str(wt / "cirrus_daily.py"), "--dry-run"],
                       cwd=wt, timeout=DRYRUN_TIMEOUT)
        if rc != 0:
            d = "daily --dry-run failed:\n%s" % out[-1200:]
            return {"ok": False, "gate": "dryrun", "detail": d,
                    "signature": failure_signature("dryrun", out),
                    "ran": ran, "excused": excused}

    ran.append("dependents(%d)" % n_dep)
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
        "HARD RULE: do NOT change, weaken, or delete any test code (a "
        "selftest() body, a check(...) line, an assert). If the only way you "
        "can make the gate pass is to change a test, the patch is wrong -- "
        "return CANNOT_BUILD and say so in notes.")
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

            # A REPAIR may not weaken test code. The cheapest way to make a
            # test pass is to delete it, so this is a refusal, and it costs the
            # attempt -- a model that tries it does not get a free retry.
            if attempt > 1:
                okt, whyt = touches_test_code(patch)
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
                blocked = None
                for e in edits:
                    okp, whyp = patch_path_ok((e or {}).get("path", ""))
                    if not okp:
                        blocked = f"{(e or {}).get('path','')}: {whyp}"
                        break
                if blocked is None:
                    ok, why, edited = plan_edits(blobs, edits)
                    if not ok:
                        blocked = why
                if blocked is not None:
                    rec.update(status="blocked", error=f"edits rejected: {blocked}"[:300])
                    _ledger("build", bid, result=f"BLOCKED: {blocked[:60]}")
                    jrn.update(outcome="blocked", refusal=blocked,
                               elapsed_s=round(time.time() - t0, 1))
                    _repair_journal(jrn)
                    _cleanup_worktree(bid)
                    return rec

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
        uid = os.getuid()
        for s in svcs:
            _run(["launchctl", "kickstart", "-k", launchctl_target(s)])
            time.sleep(2)

    # 5. verify: live files compile; restarted services are back
    fail = ""
    for p in changed:
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
        uid = os.getuid()
        for s in svcs:
            _run(["launchctl", "kickstart", "-k", launchctl_target(s)])
        b["status"] = "rolled-back"
        builds_save(builds)
        _ledger("rollback", bid, result=fail[:80])
        _notify(f"↩️ `{bid}` FAILED verify ({fail[:100]}) — auto-reverted and "
                f"restarted. Live tree is back on the previous commit.")
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


def launchctl_target(label: str) -> str:
    """Which domain holds this job — system or the GUI session?

    S72: the THIRD copy of this fix (after cirrus_api and cirrus_watchdog), and
    the reason it is now linted rather than remembered. dev_agent restarts a
    service after shipping a build; eight com.cirrus.* jobs became system
    LaunchDaemons on 2026-08-21, so a hardcoded gui/<uid> target had already
    stopped resolving for every one of them. Falls back to the GUI domain, so
    nothing changes for jobs still running as agents.
    """
    try:
        if subprocess.run(["launchctl", "print", f"system/{label}"],
                          capture_output=True, timeout=10).returncode == 0:
            return f"system/{label}"
    except Exception:
        pass
    return f"gui/{os.getuid()}/{label}"


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


def report_text(builds=None, path=None) -> str:
    """The morning report. Says what happened AND what it declined to do.

    A report that lists only successes is the T42 shape — a check that reads
    clean because it never looked. Refusals, give-ups and skipped gates are
    first-class lines here, not omissions.
    """
    builds = builds if builds is not None else builds_load()
    today = datetime.now().strftime("%Y-%m-%d")
    mine = [b for b in builds if str(b.get("created", "")).startswith(today)]
    if not mine:
        return "Dev-loop %s: nothing built overnight." % today

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

    for key, label in (("cannot-build", "DECLINED"), ("blocked", "BLOCKED"),
                       ("refused", "REFUSED"), ("build-error", "ERROR")):
        for b in buckets.get(key, []):
            lines.append("%s %s — %s" % (label, b.get("id"), (b.get("error") or "")[:160]))

    lines.append("")
    lines.append("Attempts journalled: %d. `dev_agent.py repairs` to read how."
                 % len(repairs_load(limit=500, path=path)))
    return "\n".join(lines)


def run_report():
    """Send the morning report to Telegram and print it."""
    text = report_text()
    print(text)
    _notify(text[:3500])
    return text


# ── Nightly sweep ─────────────────────────────────────────────────────────────
def run_nightly():
    _log("nightly sweep start")
    todo = find_buildable()
    if not todo:
        evaluate_empty_night()      # S71: is an empty queue the RIGHT outcome?
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


# ── Self-test (offline: no creds, no network, no git remotes) ─────────────────
def _selftest():
    ok = fail = 0

    def check(name, cond):
        nonlocal ok, fail
        ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}")

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
    check("touches_test_code: rewriting a file with check() is refused",
          not touches_test_code({"files": [{"path": "m.py", "content": "check(\"x\", 1)"}]})[0])
    check("touches_test_code: rewriting a file with def selftest is refused",
          not touches_test_code({"files": [{"path": "m.py", "content": "def selftest():\n    pass"}]})[0])
    check("touches_test_code: editing OUT an assert is refused",
          not touches_test_code({"edits": [{"path": "m.py", "find": "assert x == 1", "replace": ""}]})[0])
    check("touches_test_code: sneaking an assert IN is refused too",
          not touches_test_code({"edits": [{"path": "m.py", "find": "return 1", "replace": "assert False"}]})[0])
    check("touches_test_code: an innocent patch is allowed",
          touches_test_code({"edits": [{"path": "m.py", "find": "return 1", "replace": "return 2"}]})[0])

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

        # -- gates, in order --------------------------------------------------
        v = verify_build(wt, ["base.py"])
        check("verify_build: a healthy patch passes", v["ok"])
        check("verify_build: it ran compile, selftest AND dependents",
              v["ran"] == ["compile", "selftest(1/1)", "dependents(1)"])
        # S80 live-run finding: a module with NO selftest must not report a
        # gate that inspected nothing as simply "selftest" (T42 shape).
        check("verify_build: a module with no selftest reports 0 checked",
              "selftest(0/1)" in verify_build(wt, ["lonely.py"])["ran"])
        check("verify_build: and 0-inspected still passes, it does not fail",
              verify_build(wt, ["lonely.py"])["ok"])
        check("verify_build: dependents count reflects suites actually run",
              "dependents(1)" in verify_build(wt, ["base.py"], prebroken={"dep.py"})["ran"]
              or "dependents(0)" in verify_build(wt, ["base.py"], prebroken={"dep.py"})["ran"])
        check("verify_build: it does NOT claim to have run the dry-run",
              "dryrun" not in v["ran"])

        (wt / "broken.py").write_text("def x(:\n")
        v = verify_build(wt, ["broken.py"])
        check("verify_build: a syntax error fails at the compile gate",
              not v["ok"] and v["gate"] == "compile")

        (wt / "failing.py").write_text(
            "def selftest():\n    print('FAIL the thing')\n    return 1\n\n"
            "import sys\n"
            "if __name__ == '__main__':\n    sys.exit(selftest())\n")
        v = verify_build(wt, ["failing.py"])
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
        v = verify_build(wt, ["base.py"])
        check("verify_build: a change that breaks a DEPENDENT's suite is caught",
              not v["ok"] and v["gate"] == "dependents")
        check("verify_build: and it names which dependent, and why",
              "dep.py" in v["detail"] and "imports base" in v["detail"])

        # -- a dependent that was ALREADY red is not this patch's fault -------
        check("failing_selftests: spots a suite that is already red",
              failing_selftests(wt, ["failing.py", "base.py"]) == {"failing.py"})
        check("failing_selftests: ignores a module with no selftest",
              failing_selftests(wt, ["lonely.py"]) == set())
        v = verify_build(wt, ["base.py"], prebroken={"dep.py"})
        check("verify_build: an already-red dependent is excused, not blamed",
              v["ok"] and v["excused"] == ["dep.py"])
        check("verify_build: but the excuse is REPORTED, never silent",
              "dep.py" in report_text(builds=[{"id": "b", "status": "awaiting-confirm",
                  "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  "summary": "s", "attempts": 1, "gates_ran": ["compile"],
                  "excused": v["excused"]}]))
        check("verify_build: a dependent NOT in prebroken still fails the gate",
              not verify_build(wt, ["base.py"], prebroken=set())["ok"])

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
    today = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rpt = report_text(builds=[
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
          "selftest" in rpt.split("GAVE UP")[1])
    check("report_text: it keeps the worktree for inspection",
          "/tmp/wt" in rpt)
    check("report_text: it lists which gates actually ran",
          "dependents" in rpt)
    check("report_text: a quiet night says nothing was built",
          "nothing built" in report_text(builds=[]))

    # -- the repair prompt carries the failure and the hard rule --------------
    item = {"detail": "d", "dev_spec": {"id": "x", "files_to_change": ["a.py"]}}
    sysp, usrp = repair_prompt(item, {"a.py": "print(1)\n"}, "", set(),
                               {"gate": "selftest", "detail": "FAIL the thing"},
                               "diff --git a b", 2)
    check("repair_prompt: tells the model it is repairing its own attempt",
          "REPAIRING YOUR OWN" in sysp)
    check("repair_prompt: forbids changing test code", "do NOT change" in sysp)
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
