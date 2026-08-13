#!/bin/bash
# Self-restarting loop wrapper for intake.py on CUMULUS (S63), watching
# cumulus@cumulustask.com via INTAKE_ACCOUNT_LABEL=cumulus-research.
# Built as a Type=simple/Restart=on-failure loop, NOT a systemd .timer --
# this session hit a real, unexplained bug where .timer units on this box
# silently stop firing on their own schedule (see CUMULUS.md sec 9, the
# cumulus-creds-materialize timer saga). This mirrors that proven-working
# fix rather than risk repeating the same problem.
set -u
export INTAKE_ACCOUNT_LABEL=cumulus-research
cd /home/buddy/cirrus-digest
while true; do
    .venv/bin/python3 intake.py || echo "intake.py failed (exit $?), will retry next cycle" >&2
    sleep 900
done
