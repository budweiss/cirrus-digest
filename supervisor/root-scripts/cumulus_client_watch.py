#!/usr/bin/env python3
"""Fixed, root-owned client-conversation probe for the CUMULUS supervisor (B1).

S78. Mirrors cumulus_creds_health.py exactly, and for the same reason: the
supervisor account **cannot traverse /home/buddy** (mode 750), so it cannot
import the app modules or read the ledgers no matter what the file modes below
that directory say. That was already known for credentials -- the sudoers file
has said so since S63 -- and was simply overlooked when the client-conversation
checks were designed. All four of them shipped unable to run.

Deployed to /usr/local/sbin/cumulus_client_watch.py, owned root:root mode 755
-- deliberately NOT inside supervisor/app/, which cumulus-supervisor can write
to. A script that account could edit and then run via sudo would be a
privilege-escalation hole; this one lives where it cannot touch it.

sudoers grants cumulus-supervisor NOPASSWD run of this exact path **as buddy**.
That is the whole grant: one fixed script, one fixed output shape. The agent
gets a digest, never filesystem access, and cannot ask this for anything else.

Prints JSON on stdout. Emits COUNTS and identifying labels only -- never a
client's prose, never an email body, never a credential.

Usage: cumulus_client_watch.py [hours]      (default 168)
"""
import json
import sys

APP_DIR = "/home/buddy/cirrus-digest"
sys.path.insert(0, APP_DIR)


def main() -> int:
    try:
        hours = int(sys.argv[1]) if len(sys.argv) > 1 else 168
    except ValueError:
        hours = 168
    hours = max(1, min(hours, 24 * 365))

    out = {"hours": hours}
    try:
        import client_promises
        import client_watch
    except Exception as e:
        # A probe that cannot load must NOT print an empty-but-valid result.
        # The caller turns this into "the check did not run", which is the one
        # thing that must never be confused with "nothing is wrong".
        print(json.dumps({"error": f"import failed: {type(e).__name__}: {e}"}))
        return 1

    # Each fold is caught SEPARATELY. One unreadable source must not blank out
    # the other two -- a partial answer is useful, a silently total one is not.
    def fold(name, fn):
        try:
            out[name] = fn()
        except Exception as e:
            out.setdefault("errors", {})[name] = f"{type(e).__name__}: {e}"

    fold("promises_overdue", lambda: [
        {"client": p.get("client"), "state": p.get("state"),
         "age_hours": p.get("age_hours"), "sla_hours": p.get("sla_hours"),
         "promise": str(p.get("promise"))[:160],
         "subject": str(p.get("subject"))[:100]}
        for p in client_promises.overdue()])
    fold("promises_open", lambda: len(client_promises.list_promises(state="open")))
    fold("promises_confirmed",
         lambda: len(client_promises.list_promises(state="confirmed")))

    fold("duplicate_answers", lambda: client_watch.duplicate_answers(hours=hours))
    fold("stalled_threads", lambda: client_watch.stalled_threads())
    fold("high_value_overwrites",
         lambda: client_watch.high_value_overwrites(hours=hours))
    fold("intake_health", client_watch.intake_health)

    print(json.dumps(out, default=str))
    return 1 if out.get("errors") else 0


if __name__ == "__main__":
    sys.exit(main())
