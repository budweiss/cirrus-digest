#!/usr/bin/env python3
"""
CIRRUS Flask API
Allows Cowork/Claude to trigger digest runs and make changes remotely.
Runs on port 5000, accessible via Tailscale only.
"""

from flask import Flask, jsonify
import subprocess
import os
from pathlib import Path

app = Flask(__name__)
PROJECT_DIR = Path.home() / "projects/cirrus-digest"

# Security — only accept requests from Tailscale subnet
ALLOWED_SUBNETS = ["100."]

def is_allowed(request):
    ip = request.remote_addr
    return any(ip.startswith(s) for s in ALLOWED_SUBNETS)

SECRET_TOKEN = "CIRRUS-BUDDY-2026-XK9"

def check_ip():
    from flask import request, abort
    token = request.headers.get("X-API-Token")
    if token != SECRET_TOKEN:
        abort(403)

@app.route("/status")
def status():
    return jsonify({"status": "ok", "cirrus": "running"})

@app.route("/run/daily")
def run_daily():
    proc = subprocess.Popen(
        ["python3", "cirrus_daily.py"],
        cwd=PROJECT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    return jsonify({"status": "started", "pid": proc.pid, "job": "daily"})

@app.route("/run/weekly")
def run_weekly():
    proc = subprocess.Popen(
        ["python3", "cirrus_digest.py"],
        cwd=PROJECT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    return jsonify({"status": "started", "pid": proc.pid, "job": "weekly"})

@app.route("/run/bot/restart")
def restart_bot():
    subprocess.run(["launchctl", "stop", "com.cirrus.bot"])
    subprocess.run(["launchctl", "start", "com.cirrus.bot"])
    return jsonify({"status": "restarted", "job": "bot"})

@app.route("/read/log/<logname>")
def read_log(logname):
    allowed = ["daily", "daily-error", "bot", "bot-error", "digest"]
    if logname not in allowed:
        return jsonify({"error": "log not found"}), 404
    log_path = PROJECT_DIR / "logs" / f"{logname}.log"
    if not log_path.exists():
        return jsonify({"error": "file not found"}), 404
    lines = log_path.read_text().splitlines()[-50:]
    return jsonify({"log": logname, "lines": lines})

@app.route("/read/file/<filename>")
def read_file(filename):
    allowed = ["cirrus_bot.py", "cirrus_daily.py", "cirrus_digest.py",
               "cirrus_rag.py", "extract_actions.py", "send_digest.py",
               "space_monitor.py"]
    if filename not in allowed:
        return jsonify({"error": "file not allowed"}), 403
    file_path = PROJECT_DIR / filename
    return jsonify({"filename": filename, "content": file_path.read_text()})

@app.route("/write/file/<filename>", methods=["POST"])
def write_file(filename):
    from flask import request
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
    app.run(host="0.0.0.0", port=5001)
