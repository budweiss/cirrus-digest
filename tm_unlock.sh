#!/bin/bash
# tm_unlock.sh — S72. Unlock the FileVault-encrypted Time Machine volume at boot,
# with nobody logged in.
#
# WHY THIS EXISTS
# Converting every com.cirrus.* job to a system LaunchDaemon made
# "nobody logged in" the NORMAL state on CIRRUS. Time Machine's destination
# (OWC Envoy Pro FX, APFS role Backup) is FileVault-encrypted and its unlock key
# lives in the LOGIN keychain — so backups silently stopped:
#     backupd: Backup failed: BACKUP_FAILED_TARGETVOL_NOT_FOUND (18)
# This is the fallback for when the System-keychain route does not auto-unlock.
#
# SECRET HANDLING — the passphrase is NEVER printed, logged, or passed on a
# command line (which would expose it in `ps`). It is piped to diskutil on stdin
# and the file is refused unless it is root-owned and mode 600.
#
# The passphrase file is created BY BUDDY, not by this script and not by Claude:
#     sudo touch /etc/cirrus-tm-passphrase
#     sudo chmod 600 /etc/cirrus-tm-passphrase
#     sudo chown root:wheel /etc/cirrus-tm-passphrase
#     sudo vi /etc/cirrus-tm-passphrase        # the passphrase, one line, nothing else
#
# Idempotent and safe to run repeatedly: if the volume is already mounted it does
# nothing. Runs at boot and every 15 min, so a drive attached later still unlocks.
set -uo pipefail

VOLUME="disk5s2"
CRYPTO_USER="B98DC866-AC87-4988-A3AC-12F7813EA3BF"
VOLUME_NAME="OWC Envoy Pro FX"
PASSFILE="/etc/cirrus-tm-passphrase"
LOG="/var/log/cirrus-tm-unlock.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG" 2>/dev/null; }

# Already mounted? Nothing to do. Checked FIRST so the normal path never even
# opens the passphrase file.
if diskutil info "$VOLUME" 2>/dev/null | grep -q "Mounted:.*Yes"; then
    exit 0
fi

if [ ! -f "$PASSFILE" ]; then
    log "no $PASSFILE — cannot unlock. Create it (root:wheel, 600) with the volume passphrase."
    exit 1
fi

# Refuse a passphrase file anyone else can read. A secret stored carelessly is
# worse than no automation, because it looks solved.
MODE=$(stat -f '%Lp' "$PASSFILE" 2>/dev/null)
OWNER=$(stat -f '%Su' "$PASSFILE" 2>/dev/null)
if [ "$MODE" != "600" ] || [ "$OWNER" != "root" ]; then
    log "REFUSING: $PASSFILE is mode ${MODE:-?} owned by ${OWNER:-?} — must be 600 root."
    exit 1
fi

# Refuse an EMPTY file. This is not hypothetical: the natural way to populate it
# is to pipe `security find-generic-password -w`, and that command produces
# NOTHING when the login keychain has no active session — which on this box is
# the normal state now. `tee` then writes a perfectly-permissioned empty file.
# Without this check the script would feed diskutil an empty passphrase and log
# "passphrase rejected", sending someone to hunt a wrong password when the real
# problem is that nobody was logged in when they ran the copy.
if [ ! -s "$PASSFILE" ]; then
    log "REFUSING: $PASSFILE is EMPTY. Most likely cause: it was populated from"
    log "  \`security find-generic-password -w\` with nobody logged in, so the login"
    log "  keychain was locked and produced no output. Re-run it AT THE CONSOLE."
    exit 1
fi

# Passphrase goes in on STDIN. Never as an argument: arguments are visible to any
# user via `ps`, which would defeat the point of the file permissions above.
# Strip a trailing newline. `security find-generic-password -w` appends one, and
# the natural way to populate this file is to pipe that straight in without ever
# displaying the secret — so the common case would otherwise send diskutil a
# passphrase with an extra byte and fail with 'passphrase rejected', sending
# someone hunting a password that was actually correct.
# S73: RETRY, bounded. At boot this job runs from RunAtLoad, and an external USB
# volume is not ready to unlock the instant the device node appears. Observed on
# 2026-08-22: the 14:08:38 boot attempt failed and logged "passphrase rejected"
# — with a passphrase that had unlocked the same volume at 14:00:47 and unlocked
# it again 57 seconds later. `diskutil info` already SUCCEEDED at that point, so
# the existing not-present check could not catch it and the message blamed the
# one thing that was fine.
#
# StartInterval=900 would have healed it within 15 minutes, but that is 15
# minutes with no backup coverage and a log line accusing the passphrase.
ATTEMPTS=10
DELAY=6
for i in $(seq 1 "$ATTEMPTS"); do
    if printf '%s' "$(cat "$PASSFILE")" \
         | diskutil apfs unlockVolume "$VOLUME" \
            -user "$CRYPTO_USER" \
            -stdinpassphrase >/dev/null 2>&1; then
        if [ "$i" -gt 1 ]; then
            log "unlocked and mounted '$VOLUME_NAME' ($VOLUME) on attempt $i"
        else
            log "unlocked and mounted '$VOLUME_NAME' ($VOLUME)"
        fi
        exit 0
    fi
    # Do not keep retrying something that can never succeed: a genuinely absent
    # drive is a different fault and is reported immediately.
    if ! diskutil info "$VOLUME" >/dev/null 2>&1; then
        log "volume $VOLUME not present — drive detached or renumbered. Re-check with: diskutil apfs list"
        exit 1
    fi
    [ "$i" -lt "$ATTEMPTS" ] && sleep "$DELAY"
done

log "unlock FAILED for $VOLUME after $ATTEMPTS attempts over $((ATTEMPTS*DELAY))s."
log "  The volume IS present, so this is most likely a wrong passphrase in"
log "  $PASSFILE. Set it with set_tm_passphrase.sh, which tests before writing."
exit 1
