#!/bin/bash
# scheduled_reboot_once.sh — S72. A ONE-TIME scheduled reboot of CIRRUS, as a
# dry run before arming anything monthly.
#
# WHY IT LIVES ON CIRRUS: the MacBook sleeps (com.cowork.nosleep is not loaded
# and `pmset` sleep is 1 min), so a laptop-side timer would very likely miss the
# slot. CIRRUS is always on.
#
# WHY IT DELETES ITSELF FIRST: launchd's StartCalendarInterval has no notion of a
# specific YEAR — Month+Day+Hour would fire again next 22 August. Removing the
# plist before rebooting makes this genuinely one-time, and doing it FIRST means
# a hung reboot still cannot leave a job armed that nobody remembers.
#
# It records the pre-reboot boot time so the restart can be PROVEN afterwards
# rather than inferred from the box being reachable — S72 learned that the hard
# way when a reboot that never happened reported success.
set -uo pipefail
PLIST="/Library/LaunchDaemons/com.cirrus.rebootonce.plist"
STATE="/var/log/cirrus-scheduled-reboot.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$STATE" 2>/dev/null; }

log "=== scheduled one-time reboot firing ==="
log "boot time BEFORE: $(sysctl -n kern.boottime)"
log "daemons loaded before: $(launchctl print system 2>/dev/null | grep -c 'com\.cirrus\.')"
log "TM last result before: $(defaults read /Library/Preferences/com.apple.TimeMachine.plist 2>/dev/null | grep -m1 'RESULT' | tr -d ' ,')"

# Disarm BEFORE rebooting, so this can never fire twice.
#
# S73: do NOT `launchctl bootout` our own label here. This script IS the job, so
# bootout terminates the process that is running it — on 2026-08-22 the reboot
# silently never happened: the log stops one line before "disarmed", the plist
# was still on disk, and the box was up 19h later with the ORIGINAL boot time.
# Removing the plist is the whole disarm we need: the reboot clears the loaded
# job, and with no plist there is nothing to bootstrap at the next boot.
rm -f "$PLIST"
if [ -f "$PLIST" ]; then
    log "!! could not remove $PLIST — REFUSING to reboot rather than leave a job armed"
    exit 1
fi
log "disarmed: $PLIST removed"

# S73 (Buddy): bring the long-running jobs down deliberately first, rather than
# letting the shutdown signal catch them wherever they are. Chiefly this waits
# for intake to be BETWEEN CYCLES so its IMAP cursor is not interrupted mid-poll.
# Deliberately NON-BLOCKING: a job that will not stop is recorded and we reboot
# anyway — refusing to reboot because something is stuck recreates the other
# failure this file already had, a reboot that silently never happened.
# S73: NOT "$HOME/..." — a LaunchDaemon inherits no HOME unless its plist sets
# EnvironmentVariables, and this plist does not. With `set -u` that is an
# instant abort: the 13:30 run logged "disarmed" and died on the very next line,
# so the reboot did not happen AGAIN — this time because of the graceful-stop
# wiring added to make it safer. Derive from $0, which launchd always provides.
GRACEFUL="$(cd "$(dirname "$0")" && pwd)/graceful_stop.sh"
if [ -x "$GRACEFUL" ]; then
    log "graceful stop: bringing jobs down in order"
    if bash "$GRACEFUL" >>"$STATE" 2>&1; then
        log "graceful stop: all jobs down cleanly"
    else
        log "!! graceful stop INCOMPLETE (see lines above) — rebooting anyway"
    fi
else
    log "!! $GRACEFUL missing — rebooting without a graceful stop"
fi

log "rebooting now"
sync
/sbin/shutdown -r now
SHUT_RC=$?
[ "$SHUT_RC" -ne 0 ] && log "!! /sbin/shutdown returned $SHUT_RC — reboot NOT initiated"

# If we are still alive well past the point where the box should be going down,
# say so IN THE LOG. A reboot that fails silently is the exact defect this file
# was written to prove against, and it hit this file itself.
sleep 90
log "!! still running 90s after /sbin/shutdown — the reboot did NOT take effect"
exit 1
