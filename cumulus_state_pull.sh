#!/bin/bash
# cumulus_state_pull.sh — CIRRUS pulls cumulus1's NON-GIT state directly.
#
# S99 (Buddy, leaving for a week): "I like to make sure my pc (macbook) is not
# depended on what runs on Cirrus or Cumulus."
#
# The gap this closes: cirrus_backup.sh runs on the MACBOOK at 20:02 and is the
# only thing that copies cumulus1's non-git state anywhere. Its last leg relays
# that state to CIRRUS so Time Machine sweeps it — but the whole chain starts on
# a laptop. With the laptop away, cumulus1's encrypted credentials and the
# supervisor's audit trail exist nowhere but cumulus1's own disk, which is the
# exact exposure S63 created this backup to remove.
#
# This runs ON CIRRUS, reaches cumulus1 over the LAN link the deploy path already
# uses (buddy@192.168.0.204), and lands in the SAME directory the Mac relays to —
# so Time Machine's existing nightly sweep (~02:30, OWC Envoy Pro FX) picks it up
# with no new backup infrastructure. The Mac's version can keep running; this is
# belt and braces, not a replacement, and they are idempotent against each other.
#
# WHAT IS DELIBERATELY NOT COPIED: config/credentials.json.pre-encryption-backup.
# It is PLAINTEXT. Copying a second machine's worth of plaintext secrets increases
# exposure; it does not reduce risk. Same decision as cirrus_backup.sh — do not
# "improve" this by making the copy exhaustive.
#
# Prints NO secret values. It copies an ENCRYPTED blob and reports sizes only.
set -uo pipefail

CUM="buddy@192.168.0.204"
DEST="$HOME/Backups/cumulus-nongit"
LOG="$HOME/Library/Logs/cumulus-state-pull.log"
RC=0

ts()  { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" >> "$LOG"; echo "[$(ts)] $*"; }

mkdir -p "$DEST/config" "$DEST/supervisor-state" "$DEST/logs" "$(dirname "$LOG")"

# S64 shape: retry with backoff rather than failing on one transient blip.
reachable() {
    local attempt wait
    for attempt in 1 2 3 4; do
        ssh -o BatchMode=yes -o ConnectTimeout=8 "$CUM" true 2>/dev/null && return 0
        wait=$((attempt * 10))
        [ "$attempt" -lt 4 ] && sleep "$wait"
    done
    return 1
}

if ! reachable; then
    log "FAIL: cumulus1 unreachable from CIRRUS after 4 attempts — nothing pulled"
    exit 1
fi

# 1. the encrypted credentials blob
if ssh -o BatchMode=yes -o ConnectTimeout=15 "$CUM" \
      'cat ~/cirrus-digest/config/credentials.json.age' > "$DEST/config/credentials.json.age.tmp" 2>/dev/null \
   && [ -s "$DEST/config/credentials.json.age.tmp" ]; then
    mv "$DEST/config/credentials.json.age.tmp" "$DEST/config/credentials.json.age"
    log "OK  credentials.json.age ($(wc -c < "$DEST/config/credentials.json.age") bytes, encrypted)"
else
    rm -f "$DEST/config/credentials.json.age.tmp"
    log "FAIL credentials.json.age not pulled"; RC=1
fi

# 2. the supervisor's non-secret audit files. Its state dir is owned 750 by the
#    cumulus-supervisor account, so buddy cannot read it without sudo.
SUP_OK=1
for f in ledger.jsonl spend-ledger.jsonl CHANGES.md; do
    if ssh -o BatchMode=yes -o ConnectTimeout=15 "$CUM" \
          "sudo -n cat /opt/cumulus-supervisor/state/$f 2>/dev/null" \
          > "$DEST/supervisor-state/$f.tmp" 2>/dev/null && [ -s "$DEST/supervisor-state/$f.tmp" ]; then
        mv "$DEST/supervisor-state/$f.tmp" "$DEST/supervisor-state/$f"
    else
        rm -f "$DEST/supervisor-state/$f.tmp"; SUP_OK=0
    fi
done
if [ "$SUP_OK" -eq 1 ]; then
    log "OK  supervisor state: ledger + spend-ledger + CHANGES.md"
else
    log "WARN supervisor state incomplete (fine if the agent is not armed yet)"
fi

# 3. runtime logs — the other thing that exists only on cumulus1's disk
if rsync -az --timeout=120 --exclude='*.pre-encryption-backup' \
        -e 'ssh -o BatchMode=yes -o ConnectTimeout=15' \
        "$CUM:cirrus-digest/logs/" "$DEST/logs/" >/dev/null 2>&1; then
    log "OK  runtime logs ($(du -sh "$DEST/logs" 2>/dev/null | cut -f1))"
else
    log "WARN runtime logs rsync failed"
fi

# Prove Time Machine will actually take it. A backup that TM excludes is not a
# backup, and "it is in my home directory" is an assumption, not a check.
if command -v tmutil >/dev/null 2>&1; then
    if tmutil isexcluded "$DEST" 2>/dev/null | grep -qi '\[Excluded\]'; then
        log "FAIL Time Machine EXCLUDES $DEST — this copy would never be swept"; RC=1
    else
        log "OK  Time Machine includes $DEST"
    fi
fi

log "done (exit $RC)"
exit $RC
