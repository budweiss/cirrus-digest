"""Durable local handoff from the sole Telegram poller to Skywarden."""
import json
import os
from pathlib import Path
import re
import tempfile


def forward(update, allowed_id, directory=None):
    directory = directory or os.environ.get("SKYWARDEN_TELEGRAM_INBOX")
    message = update.get("message", {})
    if not directory or message.get("from", {}).get("id") != allowed_id:
        return False
    target = Path(directory)
    # Provisioned by the operator; a missing inbox must stop acknowledgement.
    uid = update["update_id"]
    if type(uid) is not int:
        raise ValueError("invalid update id")
    fd, name = tempfile.mkstemp(prefix=".update-", dir=target)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(update, out)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(name, 0o640)
        os.replace(name, target / (str(uid) + ".json"))
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return bool(re.match(r"^[0-9a-f]{12}\s+", message.get("text", "").strip()))
