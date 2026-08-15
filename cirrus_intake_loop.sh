#!/bin/bash
# Self-restarting loop wrapper for intake.py on CIRRUS (S65), watching
# cirrustask@gmail.com (default INTAKE_ACCOUNT_LABEL, no override needed).
# Mirrors cumulus_intake_loop.sh's pattern -- unifies both boxes onto the
# same adaptive-polling loop instead of CIRRUS's old launchd StartInterval
# (a fixed 900s interval with no way to poll faster after a hit). Run under
# launchd with RunAtLoad + KeepAlive (see com.cirrus.intake.plist), which
# is macOS's equivalent of systemd's Type=simple/Restart=on-failure.
#
# Adaptive cadence: intake.py writes config/intake_fast_poll_until.txt (a
# bare epoch-seconds timestamp) whenever a run actually processes a real
# request -- for the 15 minutes after that, poll every 60s instead of 900s,
# so a client corresponding back-and-forth gets fast replies instead of
# waiting up to 15 minutes per message. Falls back to the normal 900s
# cadence once the window lapses or nothing's been processed recently.
set -u
cd /Users/buddy/projects/cirrus-digest
FAST_POLL_FILE="config/intake_fast_poll_until.txt"
while true; do
    /usr/bin/python3 intake.py || echo "intake.py failed (exit $?), will retry next cycle" >&2
    now=$(date +%s)
    until_ts=0
    if [ -f "$FAST_POLL_FILE" ]; then
        until_ts=$(cat "$FAST_POLL_FILE" 2>/dev/null)
        [[ "$until_ts" =~ ^[0-9]+$ ]] || until_ts=0
    fi
    if [ "$now" -lt "$until_ts" ]; then
        echo "fast-poll window active (until $(date -r "$until_ts" 2>/dev/null || date -d "@$until_ts")) — sleeping 60s"
        sleep 60
    else
        sleep 900
    fi
done
