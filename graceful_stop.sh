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

LOG="${GRACEFUL_STOP_LOG:-/var/log/cirrus-graceful-stop.log}"
FAILED=""

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG" 2>/dev/null
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
    launchctl bootout "gui/$(id -u)/com.ollama.serve" 2>/dev/null
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
    log "-- 6. tunnel (LAST — this is the way in) --"; stop_unit com.cirrus.tunnel
else
    log "-- 1. intake --";      stop_unit cumulus-intake.service
    log "-- 2. supervisor --";  stop_unit cumulus-supervisor.service
    log "-- 3. bot --";         stop_unit cirrus-bot.service
    log "-- 4. offer --";       stop_unit cirrus-offer.service "Aggie's app"
    log "-- 5. api --";         stop_unit cirrus-api.service
    log "-- 6. cloudflared (LAST — this is the way in) --"; stop_unit cloudflared.service
fi

if [ -n "$FAILED" ]; then
    log "=== graceful stop INCOMPLETE — would not stop:$FAILED ==="
    log "    the caller should reboot anyway and record this; a stuck process"
    log "    must not turn into a reboot that never happens."
    exit 1
fi
log "=== graceful stop complete — all jobs down cleanly ==="
exit 0
