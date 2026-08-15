#!/bin/bash
# Self-restarting loop wrapper for intake.py on CUMULUS (S63), watching
# cumulus@cumulustask.com via INTAKE_ACCOUNT_LABEL=cumulus-research.
# Built as a Type=simple/Restart=on-failure loop, NOT a systemd .timer --
# this session hit a real, unexplained bug where .timer units on this box
# silently stop firing on their own schedule (see CUMULUS.md sec 9, the
# cumulus-creds-materialize timer saga). This mirrors that proven-working
# fix rather than risk repeating the same problem.
#
# S65: adaptive cadence. intake.py writes config/intake_fast_poll_until.txt
# (a bare epoch-seconds timestamp) whenever a run actually processes a real
# request -- for the 15 minutes after that, poll every 60s instead of 900s,
# so a client corresponding back-and-forth gets fast replies instead of
# waiting up to 15 minutes per message. Falls back to the normal 900s
# cadence once the window lapses or nothing's been processed recently.
set -u
export INTAKE_ACCOUNT_LABEL=cumulus-research
cd /home/buddy/cirrus-digest
FAST_POLL_FILE="config/intake_fast_poll_until.txt"
while true; do
    .venv/bin/python3 intake.py || echo "intake.py failed (exit $?), will retry next cycle" >&2
    now=$(date +%s)
    until_ts=0
    if [ -f "$FAST_POLL_FILE" ]; then
        until_ts=$(cat "$FAST_POLL_FILE" 2>/dev/null)
        [[ "$until_ts" =~ ^[0-9]+$ ]] || until_ts=0
    fi
    if [ "$now" -lt "$until_ts" ]; then
        echo "fast-poll window active (until $(date -d "@$until_ts" 2>/dev/null || date -r "$until_ts")) — sleeping 60s"
        sleep 60
    else
        sleep 900
    fi
done
