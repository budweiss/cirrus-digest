#!/usr/bin/env python3
"""
CIRRUS System Tools
Executable tools for the agent tool registry.
Each function takes keyword args (matching its tool schema) and returns a plain string.
"""

import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path


# ── Network Speed ─────────────────────────────────────────────────────────────

def check_network_speed(**kwargs) -> str:
    """
    Run a network speed test on CIRRUS.
    Tries macOS built-in networkquality first, falls back to speedtest-cli.
    """
    # Approach 1: macOS networkquality (built-in since macOS 12 Monterey)
    if shutil.which("networkquality"):
        try:
            result = subprocess.run(
                ["networkquality", "-s"],
                capture_output=True, text=True, timeout=60
            )
            output = result.stdout + result.stderr
            # Parse key lines
            lines = {
                line.split(":")[0].strip(): line.split(":", 1)[1].strip()
                for line in output.splitlines()
                if ":" in line and line.strip()
            }
            download = lines.get("Downlink capacity", "unknown")
            upload   = lines.get("Uplink capacity",   "unknown")
            latency  = lines.get("Idle Latency",      "unknown")
            # Strip RPM suffix if present (e.g. "10.158 milliseconds | 5906 RPM")
            latency  = latency.split("|")[0].strip()
            return (
                f"Network speed (CIRRUS):\n"
                f"  Download: {download}\n"
                f"  Upload:   {upload}\n"
                f"  Latency:  {latency}"
            )
        except Exception as e:
            pass  # fall through to next approach

    # Approach 2: speedtest-cli
    if shutil.which("speedtest"):
        try:
            result = subprocess.run(
                ["speedtest", "--simple"],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0:
                return f"Network speed (CIRRUS):\n{result.stdout.strip()}"
        except Exception:
            pass

    # Approach 3: Python speedtest module
    try:
        result = subprocess.run(
            ["python3", "-m", "speedtest", "--simple"],
            capture_output=True, text=True, timeout=90
        )
        if result.returncode == 0:
            return f"Network speed (CIRRUS):\n{result.stdout.strip()}"
    except Exception:
        pass

    return (
        "Could not run speed test — networkquality and speedtest-cli not available. "
        "Install with: pip3 install speedtest-cli"
    )


# ── Disk Space ────────────────────────────────────────────────────────────────

def check_disk_space(**kwargs) -> str:
    """
    Check disk usage on CIRRUS for key paths.
    """
    try:
        result = subprocess.run(
            ["df", "-h", "/", str(Path.home())],
            capture_output=True, text=True, timeout=10
        )
        lines = result.stdout.strip().splitlines()
        # Also check projects dir size
        projects = Path.home() / "projects"
        projects_size = "unknown"
        if projects.exists():
            du = subprocess.run(
                ["du", "-sh", str(projects)],
                capture_output=True, text=True, timeout=10
            )
            projects_size = du.stdout.split()[0] if du.stdout else "unknown"

        return (
            f"Disk usage (CIRRUS):\n"
            f"{result.stdout.strip()}\n\n"
            f"  ~/projects/ total: {projects_size}"
        )
    except Exception as e:
        return f"Error checking disk space: {e}"


# ── System Health ─────────────────────────────────────────────────────────────

def _format_vm_memory(output: str) -> str:
    """Convert vm_stat's page counts using its reported hardware page size."""
    match = re.search(r"page size of (\d+) bytes", output)
    if not match or int(match.group(1)) <= 0:
        raise ValueError("vm_stat page size missing or invalid")
    page = int(match.group(1))
    vm = {}
    for line in output.splitlines():
        match = re.match(r"(.+?):\s+(\d+)\.\s*$", line)
        if match:
            vm[match.group(1).strip()] = int(match.group(2))
    categories = (
        ("Pages active", "active"),
        ("Pages wired down", "wired"),
        ("Pages free", "free"),
        ("Pages inactive", "inactive"),
        ("Pages speculative", "speculative"),
        ("Pages occupied by compressor", "compressor physical"),
    )
    # Missing counters are an unreadable sample, not zero memory usage.
    values = [f"{vm[key] * page / 2**30:.1f} GiB {label}"
              for key, label in categories]
    return ("Memory snapshot: " + ", ".join(values)
            + ". These categories are not the installed RAM total.")


def check_system_health(**kwargs) -> str:
    """
    CPU load average and memory pressure on CIRRUS.
    """
    lines = []

    # Load average
    try:
        with open("/proc/loadavg") as f:
            load = f.read().split()
            lines.append(f"Load average: {load[0]} (1m) {load[1]} (5m) {load[2]} (15m)")
    except FileNotFoundError:
        # macOS — use uptime
        try:
            result = subprocess.run(["uptime"], capture_output=True, text=True, timeout=5)
            lines.append(f"Uptime: {result.stdout.strip()}")
        except Exception:
            pass

    # S590: with no hardware line, /ask called CIRRUS a "Mac Mini (implied)" and
    # a Grok fallback a Cray. Name it from the hardware profile; only these three
    # fields are read, never the serial number or platform UUID.
    try:
        result = subprocess.run(
            ["system_profiler", "-json", "SPHardwareDataType"], capture_output=True,
            text=True, timeout=15, check=True
        )
        hw = json.loads(result.stdout)["SPHardwareDataType"][0]
        parts = [hw["machine_name"], hw["machine_model"], hw["chip_type"]]
        if not all(isinstance(p, str) and re.fullmatch(r"[A-Za-z0-9 ,.()-]{1,40}", p) for p in parts):
            raise ValueError("unexpected hardware profile")
        lines.append(f"Hardware: {parts[0]} ({parts[1]}), {parts[2]}.")
    except Exception:
        lines.append("Hardware: unknown (hardware profile query failed).")

    # Installed RAM is a hardware value, never a sum of selected VM counters.
    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
            timeout=5, check=True
        )
        total = int(result.stdout.strip())
        if total <= 0:
            raise ValueError("invalid installed memory")
        lines.append(f"Installed RAM: {total / 2**30:.1f} GiB ({total} bytes).")
    except Exception:
        lines.append("Installed RAM: unknown (hardware memory query failed).")

    # Memory (macOS): Apple Silicon uses 16 KiB pages; Intel can use 4 KiB.
    try:
        result = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=5, check=True
        )
        lines.append(_format_vm_memory(result.stdout))
    except Exception:
        lines.append("Memory snapshot: unknown (vm_stat unavailable or invalid).")

    # CPU temp (macOS, needs osx-cpu-temp or similar — skip if unavailable)
    if shutil.which("osx-cpu-temp"):
        try:
            result = subprocess.run(
                ["osx-cpu-temp"], capture_output=True, text=True, timeout=5
            )
            lines.append(f"CPU temp: {result.stdout.strip()}")
        except Exception:
            pass

    return "System health (CIRRUS):\n" + "\n".join(f"  {l}" for l in lines) if lines \
        else "Could not retrieve system health info."


# ── Ollama Models ─────────────────────────────────────────────────────────────

def list_ollama_models(**kwargs) -> str:
    """
    List all models currently installed in Ollama on CIRRUS.
    """
    if shutil.which("ollama"):
        try:
            result = subprocess.run(
                ["ollama", "list"],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0:
                return f"Ollama models on CIRRUS:\n{result.stdout.strip()}"
            return f"ollama list error: {result.stderr.strip()}"
        except Exception as e:
            return f"Error running ollama list: {e}"

    # Fallback: check Ollama API
    try:
        import requests
        resp = requests.get("http://localhost:11434/api/tags", timeout=5)
        models = resp.json().get("models", [])
        if not models:
            return "No models found in Ollama."
        lines = [f"  {m['name']}  ({round(m.get('size', 0) / 1e9, 1)}GB)" for m in models]
        return "Ollama models on CIRRUS:\n" + "\n".join(lines)
    except Exception as e:
        return f"Could not reach Ollama API: {e}"


# ── Service Status ─────────────────────────────────────────────────────────────

def check_service_status(service_name: str = "", **kwargs) -> str:
    """
    Check whether a launchd service is running on CIRRUS.
    service_name: e.g. 'com.cirrus.bot', 'com.cirrus.daily', 'com.cirrus.offer'
    Omit to list all com.cirrus.* services.
    """
    try:
        if service_name:
            # T33-OK: called as a BOT TOOL, and the bot is launchd-spawned, so
            # it inherits the system bootstrap namespace where these daemons are
            # visible. Verified S104: the identical call over ssh returns
            # "NOT loaded" for every com.cirrus.* job, so if this module is ever
            # driven from a shell instead of from the bot, it will lie quietly.
            result = subprocess.run(
                ["launchctl", "list", service_name],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                return f"Service '{service_name}' is NOT loaded (or doesn't exist)."
            # PID is first field on first data line; "-" means not running
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[0] != "PID":
                    pid = parts[0]
                    status = "running" if pid != "-" else "loaded but NOT running"
                    return f"Service '{service_name}': {status} (PID: {pid})"
            return f"Service '{service_name}': loaded\n{result.stdout.strip()}"
        else:
            # List all cirrus services
            # T33-OK: same reasoning as the branch above — bot-spawned, system
            # namespace. Over ssh this one returns "No com.cirrus.* services".
            result = subprocess.run(
                ["launchctl", "list"],
                capture_output=True, text=True, timeout=5
            )
            cirrus_lines = [
                l for l in result.stdout.splitlines()
                if "cirrus" in l.lower()
            ]
            if not cirrus_lines:
                return "No com.cirrus.* services found in launchctl list."
            header = "PID\tStatus\tLabel"
            return "CIRRUS services:\n" + header + "\n" + "\n".join(cirrus_lines)
    except Exception as e:
        return f"Error checking service status: {e}"


# ── Tool Error Log ────────────────────────────────────────────────────────────

def check_tool_errors(lines: int = 20, **kwargs) -> str:
    """
    Read the last N lines of tool_calls.log so CIRRUS can self-diagnose.
    Only returns ERR lines unless there are none (then shows all recent).
    """
    log_path = Path.home() / "projects/cirrus-digest/tool_calls.log"
    if not log_path.exists():
        return "tool_calls.log not found — no tool calls have been logged yet."
    try:
        with open(log_path) as f:
            all_lines = f.readlines()
        recent = all_lines[-lines:]
        err_lines = [l for l in recent if "[ERR]" in l]
        if err_lines:
            return (
                f"Recent tool errors ({len(err_lines)} of last {lines} calls):\n"
                + "".join(err_lines).strip()
            )
        return (
            f"No errors in the last {len(recent)} tool calls. Recent activity:\n"
            + "".join(recent[-5:]).strip()
        )
    except Exception as e:
        return f"Error reading tool log: {e}"


# ── Dispatch table (used by registry.py) ──────────────────────────────────────

TOOL_FUNCTIONS = {
    "check_network_speed":  check_network_speed,
    "check_disk_space":     check_disk_space,
    "check_system_health":  check_system_health,
    "list_ollama_models":   list_ollama_models,
    "check_service_status": check_service_status,
    "check_tool_errors":    check_tool_errors,
}
