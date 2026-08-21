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
launchctl bootout "system/com.cirrus.rebootonce" 2>/dev/null
rm -f "$PLIST"
if [ -f "$PLIST" ]; then
    log "!! could not remove $PLIST — REFUSING to reboot rather than leave a job armed"
    exit 1
fi
log "disarmed: $PLIST removed"

log "rebooting now"
sync
/sbin/shutdown -r now
