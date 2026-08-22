#!/bin/bash
# graceful_stop.sh — S73. Bring the long-running jobs down deliberately BEFORE a
# reboot, instead of letting the shutdown signal catch them wherever they are.
#
# Buddy, 2026-08-22: "should we add this step in the procedure for both Cirrus
# and Cumulus?"
#
# WHY, honestly. For most jobs here this changes little: launchd and systemd both
# send SIGTERM and wait before SIGKILL. The two things it genuinely buys:
#
#   1. INTAKE has a position it must not lose. cirrus_intake_loop.sh holds an
#      IMAP UID cursor in config/intake_state.json and rewrites it once per
#      cycle. Killed mid-poll, it can re-process or skip mail — S64 already lost
#      a require_prefix email with zero notice. So intake is not merely stopped,
#      it is stopped AT A SAFE POINT: we wait (bounded) for its child to be a
#      bare `sleep`, meaning it is between cycles.
#   2. It separates outcomes. A job that was stopped cleanly and does not come
#      back is a boot problem; a job killed mid-write that comes back wrong is a
#      different problem. Without this step every post-reboot fault looks alike.
#
# It does NOT block the reboot. A unit that refuses to stop is reported and the
# caller carries on — refusing to reboot because a process is stuck is how you
# get today's other failure, a reboot that silently never happened.
#
# Ordering: state-holders first, then clients, then servers, and the NETWORK
# ROUTE LAST — com.cirrus.tunnel / cloudflared is how we are talking to the box.
#
# Usage: graceful_stop.sh            # stop everything in order
#        graceful_stop.sh --dry-run  # print the plan, touch nothing
# Exit 0 = everything down. 1 = something would not stop (named in the output).
set -uo pipefail

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

# Pick a log we can actually WRITE, once, quietly. In the reboot path this runs
# as root and /var/log is fine; run by hand as buddy it is not, and the first
# version emitted a "Permission denied" per line — twenty errors while nothing
# was wrong (T9: ask what your check prints when everything is FINE).
LOG="${GRACEFUL_STOP_LOG:-/var/log/cirrus-graceful-stop.log}"
if ! { : >> "$LOG"; } 2>/dev/null; then
    # Also no $HOME here. This line survived a `sudo env -i` test only because
    # root CAN write /var/log, so the branch never ran — the right context but
    # the wrong branch. Derive from $0 like the caller does.
    SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
    for alt in "$SELF_DIR/logs" "$SELF_DIR" /tmp; do
        [ -d "$alt" ] && { LOG="$alt/graceful-stop.log"; break; }
    done
    { : >> "$LOG"; } 2>/dev/null || LOG=/dev/null
fi
FAILED=""

log() {
    local line
    line="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
    printf '%s\n' "$line"
    printf '%s\n' "$line" >> "$LOG" 2>/dev/null
}

# ── platform ────────────────────────────────────────────────────────────────
case "$(uname -s)" in
    Darwin) PLATFORM=cirrus ;;
    Linux)  PLATFORM=cumulus ;;
    *)      log "!! unknown platform $(uname -s) — refusing to guess"; exit 1 ;;
esac
log "=== graceful stop on $PLATFORM ${DRY:+(dry run)} ==="

# ── intake: wait for a SAFE POINT, not just any point ───────────────────────
# Only implemented for CIRRUS, where the shape is verified: com.cirrus.intake
# runs cirrus_intake_loop.sh, whose child is `sleep 900` between cycles. On
# CUMULUS cumulus-intake.service is a different shape and systemd's own
# TimeoutStopSec handles it — claiming otherwise would be a check that lies.
wait_for_intake_idle() {
    local max="${1:-120}" pid child i
    pid=$(pgrep -f cirrus_intake_loop.sh | head -1)
    if [ -z "$pid" ]; then
        log "  intake: loop not running — nothing to wait for"
        return 0
    fi
    for i in $(seq 1 "$max"); do
        child=$(pgrep -P "$pid" 2>/dev/null | head -1)
        if [ -z "$child" ]; then
            log "  intake: no child — between cycles, safe"
            return 0
        fi
        case "$(ps -o comm= -p "$child" 2>/dev/null)" in
            *sleep*) log "  intake: child is sleep — between cycles, safe (waited ${i}s)"
                     return 0 ;;
        esac
        sleep 1
    done
    # Bounded, and "gave up" is a reported outcome, not something to sleep
    # through (T19). Stopping anyway is correct: the cursor file is rewritten
    # per cycle, so the worst case is one cycle repeated, not lost mail.
    log "  intake: STILL BUSY after ${max}s — stopping mid-cycle, may re-poll one batch"
    return 1
}

# ── stop one unit and PROVE it stopped ──────────────────────────────────────
stop_unit() {
    local unit="$1" note="${2:-}" i
    if [ "$DRY" = "1" ]; then
        log "  would stop: $unit ${note:+($note)}"
        return 0
    fi
    case "$PLATFORM" in
        cirrus)
            sudo launchctl bootout "system/$unit" 2>/dev/null
            for i in $(seq 1 15); do
                sudo launchctl print "system/$unit" >/dev/null 2>&1 || { log "  stopped: $unit"; return 0; }
                sleep 1
            done
            ;;
        cumulus)
            sudo systemctl stop "$unit" 2>/dev/null
            for i in $(seq 1 15); do
                systemctl is-active --quiet "$unit" || { log "  stopped: $unit"; return 0; }
                sleep 1
            done
            ;;
    esac
    log "  !! WOULD NOT STOP: $unit"
    FAILED="$FAILED $unit"
    return 1
}

# ── the local model (CIRRUS only) ───────────────────────────────────────────
# Unload resident models first so the GPU is released before the server goes.
# Nothing is learned or written by inference, so this is hygiene and a clean
# measurement, not data safety — say so rather than implying otherwise.
stop_ollama() {
    local m
    command -v ollama >/dev/null 2>&1 || { log "  ollama: not installed"; return 0; }
    if [ "$DRY" = "1" ]; then log "  would stop: com.ollama.serve + unload resident models"; return 0; fi
    for m in $(ollama ps 2>/dev/null | tail -n +2 | awk '{print $1}'); do
        log "  ollama: unloading $m"
        ollama stop "$m" >/dev/null 2>&1
    done
    # T12, caught by trap-lint on this very file: `gui/$(id -u)` is WRONG here.
    # In the reboot path this script runs as ROOT, so id -u is 0 and gui/0 has no
    # aqua session — the bootout would have silently done nothing. Resolve the
    # real console user, and check system/ FIRST so this keeps working after
    # com.ollama.serve is converted to a LaunchDaemon (which it needs to be: as a
    # user agent it does not come back after a login-less boot at all).
    local target uid
    if sudo launchctl print system/com.ollama.serve >/dev/null 2>&1; then
        target="system/com.ollama.serve"
    else
        uid=$(stat -f %u /dev/console 2>/dev/null)
        [ -z "$uid" ] && uid=$(id -u)
        target="gui/$uid/com.ollama.serve"
    fi
    log "  ollama: stopping $target"
    sudo launchctl bootout "$target" 2>/dev/null || launchctl bootout "$target" 2>/dev/null
    sleep 2
    if pgrep -f "ollama serve" >/dev/null 2>&1; then
        log "  !! WOULD NOT STOP: com.ollama.serve"
        FAILED="$FAILED com.ollama.serve"
        return 1
    fi
    log "  stopped: com.ollama.serve"
}

# ── the order ───────────────────────────────────────────────────────────────
if [ "$PLATFORM" = "cirrus" ]; then
    export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH
    log "-- 1. intake (holds the IMAP cursor) --"
    [ "$DRY" = "1" ] || wait_for_intake_idle 120
    stop_unit com.cirrus.intake "IMAP cursor"
    log "-- 2. bot --";    stop_unit com.cirrus.bot
    log "-- 3. offer --";  stop_unit com.cirrus.offer "Aggie's app"
    log "-- 4. api --";    stop_unit com.cirrus.api
    log "-- 5. local model --"; stop_ollama
    # The tunnel is deliberately NOT stopped. See the note below.
else
    log "-- 1. intake --";      stop_unit cumulus-intake.service
    log "-- 2. supervisor --";  stop_unit cumulus-supervisor.service
    log "-- 3. bot --";         stop_unit cirrus-bot.service
    log "-- 4. offer --";       stop_unit cirrus-offer.service "Aggie's app"
    log "-- 5. api --";         stop_unit cirrus-api.service
    # cloudflared is deliberately NOT stopped. See the note below.
fi

# ── why the tunnel is NOT in the list ───────────────────────────────────────
# It was, as step 6, "LAST — this is the way in". That was exactly backwards.
#
# The runner drives a reboot over `ssh cirrus-cf`, which RIDES the tunnel. So
# stopping the tunnel killed the very ssh session running this script, and the
# NEXT ssh — the one that issues `shutdown -r now` — could no longer connect.
# The box would have been left with every service stopped, the tunnel down, and
# NO reboot: strictly worse than doing nothing at all.
#
# A dry run could never have shown this. Nothing is stopped in a dry run, so the
# connection never drops. It is an ordering hazard that exists only when the
# steps really execute.
#
# And stopping it bought nothing anyway: cloudflared holds no writable state, so
# there is no such thing as an unclean stop for it. The reboot takes it down a
# second later regardless.
log "-- tunnel/cloudflared: intentionally left running --"
log "   (it holds no state, and stopping it would kill the connection that"
log "    issues the reboot — the reboot takes it down a moment later anyway)"

if [ -n "$FAILED" ]; then
    log "=== graceful stop INCOMPLETE — would not stop:$FAILED ==="
    log "    the caller should reboot anyway and record this; a stuck process"
    log "    must not turn into a reboot that never happens."
    exit 1
fi
log "=== graceful stop complete — all jobs down cleanly ==="
exit 0
