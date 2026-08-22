#!/bin/bash
# restart_job.sh — S73. Restart a CIRRUS job in whichever launchd domain it
# actually lives in, and FAIL LOUDLY if it lives in neither.
#
# WHY THIS EXISTS
# ---------------
# S72 converted every com.cirrus.* job from a user LaunchAgent to a system
# LaunchDaemon. Six callers still said `launchctl kickstart -k gui/501/<label>`.
# For a system daemon that target does not exist, and launchctl's behaviour is
# the worst possible combination:
#
#     $ launchctl kickstart -k gui/501/com.cirrus.bot
#     Could not find service "com.cirrus.bot" in domain for user gui: 501
#     $ echo $?
#     0
#
# It prints an error AND EXITS 0. Every caller therefore reported a restart that
# never happened — including `ssh-restart` (the documented way to restart a
# service), `cirrus-rotate-token` and `rotate-creds.sh`, which means rotated
# credentials were never picked up by the running process. A service that keeps
# serving on old credentials after a "successful" rotation is exactly the silent
# failure this project keeps paying for.
#
# T12 said the fix is a helper that checks system/ first. This is that helper,
# on the box, so there is ONE definition rather than six copies to drift.
#
# Usage:  restart_job.sh com.cirrus.bot
# Exit 0 = restarted, and it says which domain. 7 = label found nowhere.
set -uo pipefail

LABEL="${1:-}"

# T11: this arg reaches `kickstart`, so it is validated as a NAMESPACE we own,
# not as a charset. `com.apple.*` must never be reachable here.
if ! printf '%s' "$LABEL" | grep -qE '^com\.(cirrus|cowork|ollama)\.[0-9A-Za-z_.-]{1,40}$'; then
    echo "  REFUSED: '$LABEL' is not a com.cirrus/com.cowork/com.ollama label"
    exit 2
fi

if sudo launchctl print "system/$LABEL" >/dev/null 2>&1; then
    if sudo launchctl kickstart -k "system/$LABEL" 2>&1; then
        echo "  restarted: system/$LABEL"
        exit 0
    fi
    echo "  !! kickstart FAILED for system/$LABEL"
    exit 7
fi

# Fall back to the GUI domain for anything genuinely still a user agent
# (com.ollama.serve is one today — and does not survive a login-less boot).
UID_N=$(stat -f %u /dev/console 2>/dev/null); [ -z "$UID_N" ] && UID_N=$(id -u)
if launchctl print "gui/$UID_N/$LABEL" >/dev/null 2>&1; then
    if launchctl kickstart -k "gui/$UID_N/$LABEL" 2>&1; then
        echo "  restarted: gui/$UID_N/$LABEL (still a USER AGENT — will not"
        echo "             start after a login-less boot)"
        exit 0
    fi
    echo "  !! kickstart FAILED for gui/$UID_N/$LABEL"
    exit 7
fi

echo "  !! $LABEL not found in system/ or gui/$UID_N — NOTHING WAS RESTARTED"
exit 7
