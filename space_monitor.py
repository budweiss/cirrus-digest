#!/usr/bin/env python3
"""
CIRRUS Space Monitor
Checks disk usage for digest output folder, Whisper model cache,
and overall home directory. Logs warnings if thresholds are exceeded.
Cleans up old digest files beyond a retention window.
"""

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────

CONFIG_PATH = Path.home() / "projects/cirrus-digest/config/sources.json"

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

DIGEST_CFG = CONFIG["digest"]
OUTPUT_DIR = Path(DIGEST_CFG["output_dir"])
LOG_DIR    = Path(DIGEST_CFG["log_dir"])

# Thresholds
WARN_DIGESTS_GB     = 1.0   # warn if digest folder exceeds 1GB
WARN_WHISPER_GB     = 5.0   # warn if Whisper cache exceeds 5GB
WARN_DISK_FREE_GB   = 50.0  # warn if free disk space drops below 50GB

# Retention
KEEP_DAILY_DAYS     = 30    # keep daily digests for 30 days
KEEP_WEEKLY_DAYS    = 365   # keep weekly digests for 1 year
KEEP_ACTIONS_DAYS   = 90    # keep action files for 90 days

WHISPER_CACHE = Path.home() / ".cache" / "whisper"
ACTIONS_DIR   = OUTPUT_DIR / "actions"

# ── Helpers ──────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    # Also append to space monitor log
    log_file = LOG_DIR / "space-monitor.log"
    with open(log_file, "a") as f:
        f.write(line + "\n")

def folder_size_gb(path: Path) -> float:
    if not path.exists():
        return 0.0
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / (1024 ** 3)

def free_disk_gb() -> float:
    usage = shutil.disk_usage(Path.home())
    return usage.free / (1024 ** 3)

# ── Cleanup ──────────────────────────────────────────────────────────────────

def cleanup_old_files():
    """Remove digest and action files beyond retention window."""
    now = datetime.now()
    removed = 0

    # Daily digests
    if OUTPUT_DIR.exists():
        for f in OUTPUT_DIR.glob("daily-*.md"):
            age = now - datetime.fromtimestamp(f.stat().st_mtime)
            if age.days > KEEP_DAILY_DAYS:
                f.unlink()
                log(f"  Removed old daily digest: {f.name}")
                removed += 1

    # Weekly digests
    if OUTPUT_DIR.exists():
        for f in OUTPUT_DIR.glob("digest-*.md"):
            age = now - datetime.fromtimestamp(f.stat().st_mtime)
            if age.days > KEEP_WEEKLY_DAYS:
                f.unlink()
                log(f"  Removed old weekly digest: {f.name}")
                removed += 1

    # Action files
    if ACTIONS_DIR.exists():
        for f in ACTIONS_DIR.glob("*.md"):
            age = now - datetime.fromtimestamp(f.stat().st_mtime)
            if age.days > KEEP_ACTIONS_DAYS:
                f.unlink()
                log(f"  Removed old actions file: {f.name}")
                removed += 1

    if removed == 0:
        log("  No old files to clean up")
    else:
        log(f"  Cleanup complete: {removed} files removed")

    return removed

# ── Monitor ──────────────────────────────────────────────────────────────────

def run_monitor():
    log("=== CIRRUS Space Monitor ===")

    # Check digest folder
    digest_gb = folder_size_gb(OUTPUT_DIR)
    log(f"Digest folder: {digest_gb:.2f} GB ({OUTPUT_DIR})")
    if digest_gb > WARN_DIGESTS_GB:
        log(f"  ⚠️  WARNING: Digest folder exceeds {WARN_DIGESTS_GB}GB threshold!")

    # Check Whisper cache
    whisper_gb = folder_size_gb(WHISPER_CACHE)
    log(f"Whisper cache: {whisper_gb:.2f} GB ({WHISPER_CACHE})")
    if whisper_gb > WARN_WHISPER_GB:
        log(f"  ⚠️  WARNING: Whisper cache exceeds {WARN_WHISPER_GB}GB threshold!")

    # Check free disk space
    free_gb = free_disk_gb()
    log(f"Free disk space: {free_gb:.1f} GB")
    if free_gb < WARN_DISK_FREE_GB:
        log(f"  ⚠️  WARNING: Free disk below {WARN_DISK_FREE_GB}GB threshold!")

    # Count files
    daily_count  = len(list(OUTPUT_DIR.glob("daily-*.md"))) if OUTPUT_DIR.exists() else 0
    weekly_count = len(list(OUTPUT_DIR.glob("digest-*.md"))) if OUTPUT_DIR.exists() else 0
    action_count = len(list(ACTIONS_DIR.glob("*.md"))) if ACTIONS_DIR.exists() else 0
    log(f"Files: {weekly_count} weekly digests, {daily_count} daily digests, {action_count} action files")

    # Run cleanup
    log("Running cleanup...")
    cleanup_old_files()

    log("=== Space Monitor Complete ===")


if __name__ == "__main__":
    run_monitor()
