#!/usr/bin/env python3
"""
CIRRUS Flask API
Allows Cowork/Claude to trigger digest runs and make changes remotely.
Runs on port 5001, accessible via Cloudflare Tunnel at https://cirrus.cirrustask.com

Auth: ALL endpoints require the API token, including /status.
  X-API-Token: <credentials.json["api_token"]>
  or ?token=<api_token> query param
"""

from flask import Flask, jsonify, request, abort
import json
import re
import subprocess
import os
from pathlib import Path
from datetime import datetime

app = Flask(__name__)
PROJECT_DIR = Path.home() / "projects/cirrus-digest"

# ── Auth ──────────────────────────────────────────────────────────────────────

with open(PROJECT_DIR / "config/credentials.json") as f:
    _creds = json.load(f)

SECRET_TOKEN = _creds.get("api_token", "")

ALLOWED_SERVICES = {
    "com.cirrus.bot",
    "com.cirrus.daily",
    "com.cirrus.digest",
    "com.cirrus.api",
    "com.cirrus.offer",
}

def require_token():
    """Abort with 403 if the request doesn't include the correct API token.
    Accepts token via X-API-Token header OR ?token= query param (for web_fetch GET calls).
    """
    token = request.headers.get("X-API-Token", "") or request.args.get("token", "")
    if not SECRET_TOKEN or token != SECRET_TOKEN:
        abort(403, description="Invalid or missing API token.")

@app.after_request
def no_cache(response):
    """Tell Cloudflare and all intermediate caches never to cache API responses."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

# ── Health ─────────────────────────────────────────────────────────────────────

@app.route("/status")
def status():
    """Health check — includes stale proposal nudge (pending > 3 days)."""
    require_token()
    result = {"status": "ok", "cirrus": "running", "time": datetime.now().isoformat()}

    # Surface proposals that have been pending review for more than 3 days
    # so Cowork sessions notice them without having to call /admin/proposals.
    try:
        proposals_dir = PROJECT_DIR / "digests/proposals"
        stale = []
        if proposals_dir.exists():
            today = datetime.now().date()
            for f in sorted(proposals_dir.glob("proposal-*.md"), reverse=True):
                content = f.read_text()
                status_match = re.search(r"\*\*Status:\*\*\s*(.+)", content)
                if not status_match or status_match.group(1).strip() != "pending review":
                    continue
                date_match = re.match(r"proposal-(\d{4}-\d{2}-\d{2})", f.name)
                if not date_match:
                    continue
                proposal_date = datetime.strptime(date_match.group(1), "%Y-%m-%d").date()
                days = (today - proposal_date).days
                if days >= 3:
                    title_match = re.search(r"^#\s*Proposal:\s*(.+)", content, re.MULTILINE)
                    stale.append({
                        "name": f.name,
                        "days_pending": days,
                        "title": (title_match.group(1).strip()[:80] if title_match else f.stem)
                    })
        if stale:
            result["stale_proposals"] = stale
    except Exception as e:
        result["stale_proposals_error"] = str(e)

    return jsonify(result)

# ── Approvals ──────────────────────────────────────────────────────────────────

@app.route("/admin/approvals/pending")
def approvals_pending():
    """Return all items in pending_approvals.json with status=pending."""
    require_token()
    pending_file = PROJECT_DIR / "config/pending_approvals.json"
    if not pending_file.exists():
        return jsonify({"pending": [], "total": 0})
    with open(pending_file) as f:
        all_items = json.load(f)
    pending = [i for i in all_items if i.get("status") == "pending"]
    return jsonify({"pending": pending, "total": len(pending)})

@app.route("/admin/approvals/all")
def approvals_all():
    """Return all items in pending_approvals.json regardless of status."""
    require_token()
    pending_file = PROJECT_DIR / "config/pending_approvals.json"
    if not pending_file.exists():
        return jsonify({"items": [], "total": 0})
    with open(pending_file) as f:
        all_items = json.load(f)
    return jsonify({"items": all_items, "total": len(all_items)})

@app.route("/admin/approvals/reject")
def approvals_reject():
    """Mark approval items as rejected.

    GET: /admin/approvals/reject?details=foo,bar&token=<token>
         Comma-separated detail strings to reject by exact match.
    GET: /admin/approvals/reject?all_pending=1&token=<token>
         Reject ALL items currently in pending status.
    """
    require_token()
    pending_file = PROJECT_DIR / "config/pending_approvals.json"
    if not pending_file.exists():
        return jsonify({"rejected": 0, "not_found": []})
    with open(pending_file) as f:
        all_items = json.load(f)

    if request.args.get("all_pending") == "1":
        rejected = []
        for item in all_items:
            if item.get("status") == "pending":
                item["status"] = "rejected"
                rejected.append(item["detail"])
        with open(pending_file, "w") as f:
            json.dump(all_items, f, indent=2)
        return jsonify({"rejected": len(rejected), "items": rejected, "not_found": []})

    raw = request.args.get("details", "")
    targets = {d.strip() for d in raw.split(",") if d.strip()}
    if not targets:
        return jsonify({"error": "use ?details=... or ?all_pending=1"}), 400
    rejected = []
    for item in all_items:
        if item.get("detail", "") in targets and item.get("status") == "pending":
            item["status"] = "rejected"
            rejected.append(item["detail"])
    with open(pending_file, "w") as f:
        json.dump(all_items, f, indent=2)
    not_found = list(targets - set(rejected))
    return jsonify({"rejected": len(rejected), "items": rejected, "not_found": not_found})

# ── Run ────────────────────────────────────────────────────────────────────────

@app.route("/run/daily", methods=["GET", "POST"])
def run_daily():
    """Start a daily digest run. GET support added so Cowork's web_fetch
    (GET-only) can trigger runs. Output goes to logs/daily-manual.log so
    manual runs are inspectable via /read/log/daily-manual."""
    require_token()
    log_path = PROJECT_DIR / "logs" / "daily-manual.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logf = open(log_path, "a")
    logf.write(f"\n=== manual /run/daily @ {datetime.now().isoformat()} ===\n")
    logf.flush()
    proc = subprocess.Popen(
        ["python3", "-u", "cirrus_daily.py"],
        cwd=PROJECT_DIR,
        stdout=logf,
        stderr=subprocess.STDOUT
    )
    return jsonify({"status": "started", "pid": proc.pid, "job": "daily",
                    "log": "daily-manual"})

@app.route("/run/weekly", methods=["POST"])
def run_weekly():
    require_token()
    proc = subprocess.Popen(
        ["python3", "cirrus_digest.py"],
        cwd=PROJECT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    return jsonify({"status": "started", "pid": proc.pid, "job": "weekly"})

# ── Read ───────────────────────────────────────────────────────────────────────

@app.route("/read/log/<logname>")
def read_log(logname):
    require_token()
    allowed = ["daily", "daily-error", "daily-manual", "bot", "bot-error", "digest", "tool_calls", "paywalls"]
    if logname not in allowed:
        return jsonify({"error": "log not found"}), 404
    if logname == "tool_calls":
        log_path = PROJECT_DIR / "tool_calls.log"
    elif logname == "paywalls":
        log_path = PROJECT_DIR / "logs" / "paywalls.log"
    else:
        log_path = PROJECT_DIR / "logs" / f"{logname}.log"
    if not log_path.exists():
        return jsonify({"error": "file not found"}), 404
    lines = log_path.read_text().splitlines()[-50:]
    return jsonify({"log": logname, "lines": lines})

@app.route("/read/file/<filename>")
def read_file(filename):
    require_token()
    allowed = ["cirrus_bot.py", "cirrus_daily.py", "cirrus_digest.py",
               "cirrus_rag.py", "extract_actions.py", "send_digest.py",
               "space_monitor.py", "cirrus_api.py"]
    if filename not in allowed:
        return jsonify({"error": "file not allowed"}), 403
    file_path = PROJECT_DIR / filename
    return jsonify({"filename": filename, "content": file_path.read_text()})

@app.route("/read/proposal/<name>")
def read_proposal(name):
    """Return the full content of a single proposal file.
    Name must match proposal-YYYY-MM-DD-N.md — no path traversal possible."""
    require_token()
    if not re.fullmatch(r"proposal-\d{4}-\d{2}-\d{2}-\d+\.md", name):
        return jsonify({"error": "invalid proposal name"}), 400
    path = PROJECT_DIR / "digests/proposals" / name
    if not path.exists():
        return jsonify({"error": "not found"}), 404
    return jsonify({"name": name, "content": path.read_text()})

@app.route("/read/digest/<name>")
def read_digest(name):
    """Return a digest, actions, or research output file by name.
    Allowed: daily-*.md, digest-*.md, daily-actions-*.md, weekly-actions-*.md,
    research-*.md. Actions live in digests/actions/, research in
    digests/research/ — resolved automatically. No path traversal possible."""
    require_token()
    if not re.fullmatch(r"[\w][\w\-\.]*\.md", name):
        return jsonify({"error": "invalid file name"}), 400
    digests_dir = PROJECT_DIR / "digests"
    if name.startswith(("daily-actions-", "weekly-actions-")):
        path = digests_dir / "actions" / name
    elif name.startswith("research-"):
        path = digests_dir / "research" / name
    elif name.startswith(("daily-", "digest-")):
        path = digests_dir / name
    else:
        return jsonify({"error": "file type not allowed"}), 403
    if not path.exists():
        return jsonify({"error": "not found"}), 404
    return jsonify({"name": name, "content": path.read_text()})

# ── Admin: Proposals ───────────────────────────────────────────────────────────

@app.route("/admin/proposals", methods=["GET"])
def list_proposals():
    """List all proposals with their current status."""
    require_token()
    proposals_dir = PROJECT_DIR / "digests/proposals"
    if not proposals_dir.exists():
        return jsonify({"proposals": []})
    results = []
    for f in sorted(proposals_dir.glob("proposal-*.md"), reverse=True):
        content = f.read_text()
        status_match = re.search(r"\*\*Status:\*\*\s*(.+)", content)
        title_match  = re.search(r"^#\s*Proposal:\s*(.+)", content, re.MULTILINE)
        results.append({
            "name":   f.name,
            "status": status_match.group(1).strip() if status_match else "unknown",
            "title":  title_match.group(1).strip()[:100] if title_match else f.stem,
        })
    return jsonify({"proposals": results})

@app.route("/admin/proposals/pending", methods=["GET"])
def pending_proposals():
    """List only pending proposals."""
    require_token()
    proposals_dir = PROJECT_DIR / "digests/proposals"
    if not proposals_dir.exists():
        return jsonify({"proposals": []})
    results = []
    for f in sorted(proposals_dir.glob("proposal-*.md"), reverse=True):
        content = f.read_text()
        status_match = re.search(r"\*\*Status:\*\*\s*(.+)", content)
        status = status_match.group(1).strip() if status_match else "unknown"
        if status == "pending review":
            title_match = re.search(r"^#\s*Proposal:\s*(.+)", content, re.MULTILINE)
            results.append({
                "name":   f.name,
                "status": status,
                "title":  title_match.group(1).strip()[:100] if title_match else f.stem,
            })
    return jsonify({"proposals": results})

@app.route("/admin/proposals/reject", methods=["GET", "POST"])
def reject_proposal():
    """Mark one or more proposals as rejected.
    GET:  /admin/proposals/reject?names=proposal-a.md,proposal-b.md&token=<token>
    POST: body {"names": [...]} with X-API-Token header
    """
    require_token()
    if request.method == "GET":
        names = [n.strip() for n in request.args.get("names", "").split(",") if n.strip()]
    else:
        data  = request.get_json() or {}
        names = data.get("names", [])
    if not names:
        return jsonify({"error": "missing names list"}), 400

    proposals_dir = PROJECT_DIR / "digests/proposals"
    results = []
    for name in names:
        if not re.match(r'^proposal-[\w\-]+\.md$', name):
            results.append({"name": name, "status": "skipped", "reason": "invalid filename"})
            continue
        path = proposals_dir / name
        if not path.exists():
            results.append({"name": name, "status": "not_found"})
            continue
        content = path.read_text()
        updated = re.sub(r'\*\*Status:\*\*\s*.+', '**Status:** rejected', content)
        updated = updated.replace(
            "- [ ] Rejected (not a good fit)",
            "- [x] Rejected (not a good fit)"
        )
        path.write_text(updated)
        results.append({"name": name, "status": "rejected"})
    return jsonify({"results": results})

@app.route("/admin/proposals/approve", methods=["GET", "POST"])
def approve_proposal():
    """Mark a proposal as approved.
    GET:  /admin/proposals/approve?name=proposal-a.md&token=<token>
    POST: body {"name": "..."} with X-API-Token header
    """
    require_token()
    if request.method == "GET":
        name = request.args.get("name", "").strip()
    else:
        data = request.get_json() or {}
        name = data.get("name", "")
    if not re.match(r'^proposal-[\w\-]+\.md$', name):
        return jsonify({"error": "invalid filename"}), 400
    path = PROJECT_DIR / "digests/proposals" / name
    if not path.exists():
        return jsonify({"error": "not found"}), 404
    content = path.read_text()
    updated = re.sub(r'\*\*Status:\*\*\s*.+', '**Status:** approved for implementation', content)
    path.write_text(updated)
    return jsonify({"name": name, "status": "approved"})

@app.route("/admin/proposals/implemented", methods=["GET", "POST"])
def implemented_proposal():
    """Mark one or more proposals as implemented and deployed.
    GET:  /admin/proposals/implemented?names=proposal-a.md,proposal-b.md&token=<token>
    POST: body {"names": [...]} with X-API-Token header
    """
    require_token()
    if request.method == "GET":
        names = [n.strip() for n in request.args.get("names", "").split(",") if n.strip()]
    else:
        data  = request.get_json() or {}
        names = data.get("names", [])
    if not names:
        return jsonify({"error": "missing names list"}), 400

    proposals_dir = PROJECT_DIR / "digests/proposals"
    results = []
    for name in names:
        if not re.match(r'^proposal-[\w\-]+\.md$', name):
            results.append({"name": name, "status": "skipped", "reason": "invalid filename"})
            continue
        path = proposals_dir / name
        if not path.exists():
            results.append({"name": name, "status": "not_found"})
            continue
        content = path.read_text()
        updated = re.sub(r'\*\*Status:\*\*\s*.+', '**Status:** implemented', content)
        updated = updated.replace(
            "- [ ] Implemented and deployed",
            "- [x] Implemented and deployed"
        )
        path.write_text(updated)
        results.append({"name": name, "status": "implemented"})
    return jsonify({"results": results})

# ── Admin: Email Omit ──────────────────────────────────────────────────────────

@app.route("/admin/omit", methods=["GET"])
def list_omit():
    """Return current email omit list."""
    require_token()
    omit_path = PROJECT_DIR / "config/email_omit.txt"
    if not omit_path.exists():
        return jsonify({"entries": []})
    entries = [l.strip() for l in omit_path.read_text().splitlines()
               if l.strip() and not l.strip().startswith("#")]
    return jsonify({"entries": entries})

@app.route("/admin/omit", methods=["GET", "POST"])
def add_omit():
    """Add a sender to the email omit list.
    GET:  /admin/omit?sender=email@example.com&token=<token>
    POST: body {"sender": "..."} with X-API-Token header
    """
    require_token()
    if request.method == "GET" and request.args.get("sender"):
        sender = request.args.get("sender", "").strip()
    else:
        data   = request.get_json() or {}
        sender = data.get("sender", "").strip()
    if not sender:
        return jsonify({"error": "missing sender"}), 400
    omit_path = PROJECT_DIR / "config/email_omit.txt"
    existing  = []
    if omit_path.exists():
        existing = [l.strip() for l in omit_path.read_text().splitlines()]
    active = [l.lower() for l in existing if l and not l.startswith("#")]
    if sender.lower() in active:
        return jsonify({"status": "already_exists", "sender": sender})
    with open(omit_path, "a") as f:
        if existing and existing[-1] != "":
            f.write("\n")
        f.write(f"{sender}\n")
    return jsonify({"status": "added", "sender": sender})

# ── Admin: Services ────────────────────────────────────────────────────────────

@app.route("/admin/service/restart", methods=["POST"])
def restart_service():
    """Restart a launchd service.
    Body: {"service": "com.cirrus.bot"}
    """
    require_token()
    data    = request.get_json() or {}
    service = data.get("service", "").strip()
    if service not in ALLOWED_SERVICES:
        return jsonify({"error": f"service not allowed: {service}"}), 400
    uid = os.getuid()
    result = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{uid}/{service}"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return jsonify({"status": "restarted", "service": service})
    return jsonify({"status": "error", "service": service,
                    "stderr": result.stderr.strip()}), 500

@app.route("/admin/service/status", methods=["GET"])
def service_status():
    """List all com.cirrus.* launchd services and their run state."""
    require_token()
    result = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    lines = [l for l in result.stdout.splitlines() if "cirrus" in l.lower()]
    return jsonify({"services": lines})

# ── Admin: Deploy ─────────────────────────────────────────────────────────────

@app.route("/admin/deploy", methods=["GET"])
def deploy():
    """Pull latest from GitHub and optionally restart a service.

    Cowork pushes a file to GitHub, then calls this endpoint so CIRRUS
    pulls it down and restarts the relevant service — no SSH or SCP needed.

    GET: /admin/deploy?job=com.cirrus.bot&token=<token>
         ?job=none   to skip restart
    """
    require_token()
    job = request.args.get("job", "none").strip()

    # Pull latest from GitHub
    git = subprocess.run(
        ["git", "-C", str(PROJECT_DIR), "pull", "--ff-only"],
        capture_output=True, text=True
    )
    git_output = (git.stdout + git.stderr).strip()
    git_ok = git.returncode == 0

    # Optionally restart a service
    restart_info = None
    if job and job != "none":
        if job not in ALLOWED_SERVICES:
            return jsonify({
                "git": {"ok": git_ok, "output": git_output},
                "restart": {"error": f"service not allowed: {job}"}
            }), 400
        uid = os.getuid()
        r = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/{job}"],
            capture_output=True, text=True
        )
        restart_info = {
            "service": job,
            "status": "restarted" if r.returncode == 0 else "error",
            "stderr": r.stderr.strip()
        }

    return jsonify({
        "git": {"ok": git_ok, "output": git_output},
        "restart": restart_info
    })

# ── Write (token-protected) ────────────────────────────────────────────────────

# ── Admin: Upload Config ───────────────────────────────────────────────────────

# Config files that can be uploaded via the API.
# credentials.json is intentionally excluded — overwriting it would revoke API access.
ALLOWED_CONFIG_UPLOADS = {
    "cookies.json",
    "cookie_watchlist.json",
    "sources.json",
    "email_omit.txt",
    "pending_approvals.json",
}

@app.route("/admin/upload-config", methods=["POST"])
def upload_config():
    """Upload (create or overwrite) a file in the config directory.

    Body: {"filename": "cookies.json", "content": "..."}

    - filename must be in the allowlist (credentials.json is excluded)
    - Content is written to PROJECT_DIR/config/<filename>
    - cookies.json is chmod 600 automatically
    """
    require_token()
    data = request.get_json()
    if not data or "filename" not in data or "content" not in data:
        return jsonify({"error": "missing filename or content"}), 400

    filename = data["filename"].strip()

    # Reject path traversal, hidden files, or anything not on the allowlist
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return jsonify({"error": "invalid filename"}), 400
    if filename not in ALLOWED_CONFIG_UPLOADS:
        return jsonify({"error": f"filename not allowed: {filename}"}), 403

    config_path = PROJECT_DIR / "config" / filename
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(data["content"])

    # Restrict permissions on sensitive files
    if filename in ("cookies.json",):
        os.chmod(config_path, 0o600)

    return jsonify({
        "status": "written",
        "filename": filename,
        "bytes": len(data["content"])
    })

@app.route("/write/file/<filename>", methods=["POST"])
def write_file(filename):
    require_token()
    allowed = ["cirrus_bot.py", "cirrus_daily.py", "cirrus_digest.py",
               "cirrus_rag.py", "extract_actions.py", "send_digest.py",
               "space_monitor.py", "config/sources.json"]
    if filename not in allowed:
        return jsonify({"error": "file not allowed"}), 403
    file_path = PROJECT_DIR / filename
    data = request.get_json()
    if not data or "content" not in data:
        return jsonify({"error": "missing content"}), 400
    file_path.write_text(data["content"])
    return jsonify({"status": "written", "filename": filename})

if __name__ == "__main__":
    # Bind to localhost only — Cloudflare tunnel connects via 127.0.0.1,
    # so external access still works via the tunnel. Binding to 0.0.0.0
    # would expose port 5001 to anyone on the local network unnecessarily.
    app.run(host="127.0.0.1", port=5001)
