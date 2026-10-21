"""Bounded planned MacBook absence, owned by CIRRUS. No server-wide muting."""
import argparse
from datetime import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile
import time

STATE = Path(__file__).resolve().parent / "logs/macbook-availability.json"


def status(path=STATE, now=None):
    now = time.time() if now is None else now
    try:
        row = json.loads(Path(path).read_text())
        until = float(row["until"])
        started = float(row["started"])
        if not all(math.isfinite(x) for x in (until, started)) or not 0 < until-started <= 168*3600:
            raise ValueError("invalid window")
        return {"mode": "offline" if now < until else "expired",
                "started": started, "until": until,
                "remaining_minutes": max(0, math.ceil((until-now)/60))}
    except FileNotFoundError:
        return {"mode": "online"}
    except (OSError, ValueError, KeyError, TypeError):
        # Invalid state must never suppress monitoring.
        return {"mode": "invalid"}


def change(action, hours=None, path=STATE, now=None):
    now = time.time() if now is None else now
    if action == "offline":
        hours = float(hours)
        if not math.isfinite(hours) or not 0 < hours <= 168:
            raise ValueError("hours must be greater than zero and at most 168")
    elif action != "online":
        raise ValueError("unknown action")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if action == "online":
            path.unlink(missing_ok=True)
        else:
            fd, temp = tempfile.mkstemp(prefix=".availability-", dir=path.parent)
            try:
                with os.fdopen(fd, "w") as out:
                    json.dump({"started": now, "until": now+hours*3600}, out)
                os.replace(temp, path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        result = status(path, now)
        if action == "online":
            result["cleared_at"] = now
        return result


def health():
    try:
        hb = json.loads((STATE.parent / "heartbeats.json").read_text())["macbook"]
        return {"timestamp": datetime.fromisoformat(hb["ts"]).timestamp(),
                "status": hb["status"]}
    except (OSError, ValueError, KeyError, TypeError):
        return {"timestamp": 0, "status": "unknown"}


def overdue(window, heartbeat_time, now=None):
    """An ordinary sleeping notice must not hide a missed planned return."""
    now = time.time() if now is None else now
    return (window.get("mode") == "expired" and
            heartbeat_time < window["until"] <= now)


def clear_returned_window(expected, path=STATE):
    """Do not erase a concurrently extended absence when a late heartbeat arrives."""
    path = Path(path)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = status(path)
        if current.get("until") == expected.get("until") and current["mode"] == "expired":
            path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("offline", "online", "status", "health"))
    parser.add_argument("hours", nargs="?", type=float)
    args = parser.parse_args()
    try:
        result = (status() if args.action == "status" else health() if args.action == "health"
                  else change(args.action, args.hours))
    except (OSError, TypeError, ValueError):
        parser.error("could not record state; offline requires 0 < hours <= 168")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
