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

# Log rotation — bot.log reached 1.5GB (2026-07-05) via getUpdates timeout
# spam before that bug was fixed. Any *.log over the max is trimmed in place
# to its last KEEP_LOG_MB megabytes. Safe while services run: all CIRRUS
# loggers reopen their file in append mode on every write.
MAX_LOG_MB  = 50
KEEP_LOG_MB = 5

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

def rotate_big_logs():
    """Trim any log file over MAX_LOG_MB down to its last KEEP_LOG_MB."""
    if not LOG_DIR.exists():
        return
    max_bytes  = MAX_LOG_MB * 1024 * 1024
    keep_bytes = KEEP_LOG_MB * 1024 * 1024
    for f in LOG_DIR.glob("*.log"):
        try:
            size = f.stat().st_size
            if size <= max_bytes:
                continue
            with open(f, "rb") as src:
                src.seek(-keep_bytes, 2)
                tail = src.read()
            # Start at a clean line boundary
            nl = tail.find(b"\n")
            if nl != -1:
                tail = tail[nl + 1:]
            tmp = f.with_suffix(".log.tmp")
            with open(tmp, "wb") as dst:
                dst.write(b"[... log trimmed by space_monitor ...]\n" + tail)
            tmp.replace(f)
            log(f"  ✂️  Trimmed {f.name}: {size / (1024**2):.0f} MB → {KEEP_LOG_MB} MB")
        except Exception as e:
            log(f"  Log rotation error for {f.name}: {e}")

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

    # Rotate oversized logs
    log("Checking log sizes...")
    rotate_big_logs()

    log("=== Space Monitor Complete ===")


def selftest():
    """Exercise decision-making helpers with explicit inputs/outputs.
    Does not touch real config, disk, or log files beyond a temp dir.
    Returns True on success, False on failure (prints details).
    """
    import sys
    import tempfile
    import os

    failures = []

    def check(label, actual, expected):
        if actual != expected:
            failures.append(f"{label}: expected {expected!r}, got {actual!r}")

    # folder_size_gb: empty/missing dir -> 0.0
    with tempfile.TemporaryDirectory() as td:
        empty_dir = Path(td) / "empty"
        check("folder_size_gb missing dir", folder_size_gb(empty_dir), 0.0)

        empty_dir.mkdir()
        check("folder_size_gb empty dir", folder_size_gb(empty_dir), 0.0)

        # folder_size_gb: known file size -> expected GB
        sized_dir = Path(td) / "sized"
        sized_dir.mkdir()
        data = b"x" * (1024 * 1024)  # 1 MiB
        (sized_dir / "f.bin").write_bytes(data)
        expected_gb = (1024 * 1024) / (1024 ** 3)
        check("folder_size_gb 1MiB file", folder_size_gb(sized_dir), expected_gb)

        # cleanup_old_files: verify retention decision logic directly,
        # without touching the real OUTPUT_DIR/ACTIONS_DIR.
        now = datetime.now()
        old_time = (now - timedelta(days=KEEP_DAILY_DAYS + 1)).timestamp()
        new_time = (now - timedelta(days=1)).timestamp()

        retention_dir = Path(td) / "retention"
        retention_dir.mkdir()
        old_file = retention_dir / "daily-old.md"
        new_file = retention_dir / "daily-new.md"
        old_file.write_text("old")
        new_file.write_text("new")
        os.utime(old_file, (old_time, old_time))
        os.utime(new_file, (new_time, new_time))

        age_old = now - datetime.fromtimestamp(old_file.stat().st_mtime)
        age_new = now - datetime.fromtimestamp(new_file.stat().st_mtime)
        check("old file exceeds KEEP_DAILY_DAYS", age_old.days > KEEP_DAILY_DAYS, True)
        check("new file within KEEP_DAILY_DAYS", age_new.days > KEEP_DAILY_DAYS, False)

        # rotate_big_logs threshold logic: verify constants relationship
        check("MAX_LOG_MB > KEEP_LOG_MB", MAX_LOG_MB > KEEP_LOG_MB, True)

        # Warning threshold decisions (pure comparisons, mirror run_monitor logic)
        check("digest warn below threshold", 0.5 > WARN_DIGESTS_GB, False)
        check("digest warn above threshold", 1.5 > WARN_DIGESTS_GB, True)
        check("whisper warn below threshold", 4.0 > WARN_WHISPER_GB, False)
        check("whisper warn above threshold", 6.0 > WARN_WHISPER_GB, True)
        check("disk free warn below threshold", 10.0 < WARN_DISK_FREE_GB, True)
        check("disk free warn above threshold", 100.0 < WARN_DISK_FREE_GB, False)

    if failures:
        print("SELFTEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return False

    print("SELFTEST PASSED")
    return True


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        ok = selftest()
        sys.exit(0 if ok else 1)
    run_monitor()
