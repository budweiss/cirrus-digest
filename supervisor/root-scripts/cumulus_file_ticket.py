#!/usr/bin/env python3
"""Fixed, root-owned repair-ticket filer for the CUMULUS supervisor (S91).

Deployed to /usr/local/sbin/cumulus_file_ticket.py, owned root:root mode 755 —
NOT synced as part of supervisor/app/ (which cumulus-supervisor itself can
write to). A script cumulus-supervisor could edit and then run via sudo would
be a privilege-escalation hole, so this one lives where it cannot touch it.

WHY THIS EXISTS (S91, 2026-09-01)
---------------------------------
cirrus-modelhealth.service failed every morning from 2026-08-31. Skywarden did
everything right and it still cost a human two hours:

    heartbeat        -> failed units: cirrus-modelhealth.service
    check_service_status / tail_journal   -> diagnosed
    restart_service  -> FAILED (it was a deterministic code bug)
    send_telegram    -> escalated to Buddy

Detection, diagnosis, attempted repair and escalation all worked. But
`restart_service` is the only repair on the allowlist, and a code defect is
immune to restarts, so the only remaining move was to wake a human. Skywarden
had `send_telegram`, `request_guidance` and `request_opus_upgrade` — three ways
to TALK to Buddy and zero ways to FILE WORK.

This is that missing verb. It hands the diagnosis Skywarden already gathered to
the dev-loop, which can actually write code, instead of to a chat message that
scrolls away.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It files a ticket. It does not build, test, approve, deploy or restart
anything. dev_loop.ticket_create classifies the ticket by risk tier; only a
Tier-1 ticket is ever picked up by dev_agent, and what dev_agent produces lands
in `awaiting-confirm` for Buddy's one tap. Buddy's S91 decision, in his words:
file -> build -> await your tap. Nothing reaches a box without him.

The requester, origin and project are HARDCODED here rather than passed in, so
the agent cannot file on behalf of someone else or point the ticket at another
project. The only things it controls are the unit name (which its own tool
validates against ALLOWED_UNITS before calling this) and the free-text
diagnosis body, both of which are length-capped by dev_loop.ticket_create.

Usage — EVERYTHING arrives on stdin as one JSON object, and the script takes no
arguments at all:
    echo '{"unit": "...", "diagnosis": "..."}' | cumulus_file_ticket.py

That is deliberate. The sudoers file for this agent states the rule plainly:
"No wildcards (sudoers wildcard argument matching is exploitable) — every
command spelled out in full." A script taking a dynamic title argument would
have forced a `... *` wildcard grant. Moving the payload to stdin keeps the
sudoers entry an exact, argument-free path match, and as a bonus keeps a
journal excerpt out of the process table, which is world-readable.

Prints one JSON object: {"id": ..., "tier": ..., "tier_name": ..., "status": ...}
"""
import json
import re
import sys

APP_DIR = "/home/buddy/cirrus-digest"

# Fixed identity for anything filed through this path. Not caller-supplied:
# a ticket that could claim any requester is a ticket that can impersonate one.
REQUESTER = "skywarden"
ORIGIN = "skywarden-repair"
PROJECTS = ["cirrus-digest"]

sys.path.insert(0, APP_DIR)


def main() -> int:
    if len(sys.argv) > 1:
        # Argument-free by contract, so the sudoers grant needs no wildcard.
        # Refuse loudly rather than silently ignore: a caller passing argv is a
        # caller who thinks it is doing something this script is not doing.
        print(json.dumps({"error": "takes no arguments; send JSON on stdin"}))
        return 2

    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("payload is not a JSON object")
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": f"bad stdin JSON: {type(e).__name__}: {e}"}))
        return 3

    unit = str(payload.get("unit", "")).strip()
    detail = str(payload.get("diagnosis", "")).strip()
    if not unit:
        print(json.dumps({"error": "need a unit"}))
        return 3
    if not detail:
        # A ticket with no evidence is worse than none — it sends the dev-loop
        # to write a patch against a description of a symptom. Refuse it here
        # rather than let a thin ticket look like a filed repair.
        print(json.dumps({"error": "need a non-empty diagnosis"}))
        return 3

    # Second gate on the unit name. tools.file_repair_ticket already checks it
    # against ALLOWED_UNITS, but this script is the privileged half and must not
    # trust its caller: the charset below cannot express a path, a shell
    # metacharacter, or a traversal.
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", unit):
        print(json.dumps({"error": f"unit name refused: {unit[:40]!r}"}))
        return 3

    title = f"{unit} is failing and a restart does not fix it"
    detail = f"Unit: {unit}\n\n{detail}"

    try:
        import dev_loop
    except Exception as e:  # noqa: BLE001 — the caller only sees ok/fail
        print(json.dumps({"error": f"cannot import dev_loop: {type(e).__name__}: {e}"}))
        return 4

    try:
        ticket = dev_loop.ticket_create(
            requester=REQUESTER,
            projects=PROJECTS,
            title=title,
            detail=detail,
            origin=ORIGIN,
            project_dir=APP_DIR,
        )
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": f"ticket_create failed: {type(e).__name__}: {e}"}))
        return 5

    spec = ticket.get("dev_spec") or {}
    print(json.dumps({
        "id": ticket.get("id", ""),
        "spec_id": spec.get("id", ""),
        "tier": ticket.get("tier"),
        "tier_name": ticket.get("tier_name", ""),
        "status": ticket.get("status", ""),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
