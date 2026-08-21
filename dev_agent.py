#!/usr/bin/env python3
"""
dev_agent.py — CIRRUS Autonomous Dev-Loop, Phase 3: Tier-1 assisted builds.

The CIRRUS-hosted headless developer agent (host decision: Buddy, 2026-07-14).
Takes an APPROVED Tier-1 proposal (queued by cirrus_bot.execute_action into
logs/dev-loop/build-queue.jsonl), and runs the pipeline:

    build   — git worktree + branch, Claude API writes the patch (safety-gated)
    test    — py_compile every changed .py; full daily --dry-run when a core
              digest file is touched (paths in cirrus_daily are absolute, so a
              worktree run reads live config but can only write DRYRUN-* files)
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
    python3 dev_agent.py nightly      # sweep the queue, build+test, notify
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
MAX_FILE_CONTEXT    = 45_000     # chars of one file sent to the model
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


def build_prompt(item: dict, file_blobs: dict, conventions: str = ""):
    """Return (system, user) for the patch-writing model call. Pure."""
    spec = item.get("dev_spec") or {}
    system = (
        "You are the CIRRUS Dev-Loop build agent. You write a minimal, surgical "
        "patch for the cirrus-digest Python project to implement ONE approved "
        "proposal. Hard rules:\n"
        "1. Reply with a single JSON object, no markdown fences, shaped exactly:\n"
        '   {"summary": "<one line>", "files": [{"path": "<repo-relative>", '
        '"content": "<complete new file content>"}], "notes": "<risks/assumptions>"}\n'
        "2. Return the COMPLETE new content of every file you change (not a diff).\n"
        "3. Change as few files and as few lines as possible. Preserve existing "
        "style, comments, and behavior everywhere you are not explicitly changing.\n"
        "4. Never touch credentials, cookies, tokens, config files (except "
        "config/sources.json), launchd plists, or anything under logs/ or digests/.\n"
        "5. Python 3.9 stdlib + requests only; no new dependencies.\n"
        "6. If the proposal cannot be implemented safely within these rules, reply "
        '{"summary": "CANNOT_BUILD", "files": [], "notes": "<why>"}.'
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
        parts.append(f"\n===== {path} =====\n{blob}")
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

        # Context: the spec's guessed files that actually exist (fallback: daily)
        want = (spec.get("files_to_change") or ["cirrus_daily.py"])
        blobs, total = {}, 0
        for p in want:
            fp = wt / p
            if fp.exists() and total < MAX_TOTAL_CONTEXT:
                blob = fp.read_text()[:MAX_FILE_CONTEXT]
                blobs[p] = blob
                total += len(blob)
        conventions = ""
        conv = wt / "CIRRUS-CONVENTIONS.md"
        if conv.exists():
            conventions = conv.read_text()

        system, user = build_prompt(item, blobs, conventions)
        patch = build_model_patch(system, user)

        if patch.get("summary") == "CANNOT_BUILD" or not patch.get("files"):
            rec.update(status="cannot-build",
                       error=str(patch.get("notes", ""))[:300])
            _ledger("build", bid, result=f"CANNOT_BUILD: {patch.get('notes','')[:60]}")
            _cleanup_worktree(bid)
            return rec

        ok, why = validate_patch(patch["files"])
        if not ok:
            rec.update(status="blocked", error=f"patch rejected: {why}")
            _ledger("build", bid, result=f"BLOCKED: {why}")
            _cleanup_worktree(bid)
            return rec

        changed = []
        for f in patch["files"]:
            dest = wt / f["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(f["content"])
            changed.append(f["path"])
        rec["files"] = changed
        rec["summary"] = str(patch.get("summary", ""))[:200]
        rec["notes"] = str(patch.get("notes", ""))[:300]

        # ── test: compile every changed .py ──────────────────────────────────
        for p in changed:
            if p.endswith(".py"):
                rc, out = _run([sys.executable, "-m", "py_compile", str(wt / p)])
                if rc != 0:
                    rec.update(status="test-failed", error=f"py_compile {p}: {out[:300]}")
                    _ledger("test", bid, result=f"FAIL compile {p}")
                    _cleanup_worktree(bid)
                    return rec
        rec["test_compile"] = "ok"

        # commit before the (long) dry-run so the work is never lost
        _git(["add", "-A"], cwd=wt)
        rc, out = _git(["commit", "-m", f"dev-loop {bid}: {rec['summary'][:60]}"], cwd=wt)
        if rc != 0:
            raise RuntimeError(f"commit failed: {out[:200]}")
        rc, stat = _git(["diff", "--stat", "HEAD~1..HEAD"], cwd=wt)
        rec["diff_stat"] = stat[-500:]

        # ── test: full dry-run if a core digest file changed ─────────────────
        if set(changed) & DRYRUN_TRIGGERS:
            _log(f"{bid}: core file changed — running daily --dry-run (long)")
            rc, out = _run([sys.executable, str(wt / "cirrus_daily.py"), "--dry-run"],
                           cwd=wt, timeout=DRYRUN_TIMEOUT)
            rec["test_dryrun"] = "ok" if rc == 0 else "FAIL"
            rec["dryrun_tail"] = out[-600:]
            if rc != 0:
                rec.update(status="test-failed", error="dry-run failed")
                _ledger("test", bid, result="FAIL dry-run")
                # keep worktree for inspection; do not confirm
                return rec
        else:
            rec["test_dryrun"] = "skipped (no core digest file changed)"

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
            _run(["launchctl", "kickstart", "-k", f"gui/{uid}/{s}"])
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
            _run(["launchctl", "kickstart", "-k", f"gui/{uid}/{s}"])
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
    else:
        print(__doc__)
