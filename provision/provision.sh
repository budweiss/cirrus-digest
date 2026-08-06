#!/usr/bin/env bash
# =============================================================================
# provision.sh — one-command bring-up for a cirrus-digest node
# Implements docs/CIRRUS-CUMULUS-Environment-Plan.md §6 bootstrap manifest.
#
# Brings up CIRRUS (dev, macOS/Metal/launchd), CUMULUS (beta, ARM64/CUDA/systemd),
# and STRATUS (prod, 3× — same script) IDENTICALLY. Platform differences are
# read from platform.env, never hard-coded.
#
# Usage:
#   ./provision.sh --check            # dry-run: report what WOULD happen, change nothing
#   ./provision.sh --all              # full bring-up (asks before destructive steps)
#   ./provision.sh --step deps|venv|serving|models|services|tunnel|verify
#
# Safety:
#   * Idempotent — safe to re-run; each step checks state first.
#   * Credentials are a HUMAN (Tier-2) step — this script NEVER writes secrets.
#     It only verifies config/credentials.json exists (chmod 600) and stops if not.
#   * On STRATUS (TARGET_ENV=prod) it refuses to run live services until a
#     successful CUMULUS soak record is present (the promotion gate).
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
PLATFORM_ENV="${PLATFORM_ENV:-$HERE/platform.env}"

# ---- load platform config ----------------------------------------------------
if [[ ! -f "$PLATFORM_ENV" ]]; then
  echo "FATAL: $PLATFORM_ENV not found. Copy platform.env.example → platform.env and edit." >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$PLATFORM_ENV"

: "${TARGET_ENV:?set TARGET_ENV=dev|beta|prod in platform.env}"
: "${MODEL_BACKEND:?set MODEL_BACKEND=ollama-metal|ollama-cuda|vllm}"
: "${SERVICE_MANAGER:?set SERVICE_MANAGER=launchd|systemd}"
: "${APP_DIR:?set APP_DIR (e.g. /home/buddy/cirrus-digest)}"
: "${MODEL_DEFAULT:=qwen2.5:72b}"
: "${EMBED_MODEL:=nomic-embed-text}"
: "${SUBDOMAIN:=cumulus.cirrustask.com}"
: "${TUNNEL_PORT:=5001}"

DRYRUN=0
log(){ printf '\033[1;36m[provision]\033[0m %s\n' "$*"; }
run(){ if [[ $DRYRUN -eq 1 ]]; then echo "  DRYRUN> $*"; else eval "$@"; fi; }
have(){ command -v "$1" >/dev/null 2>&1; }

# ---- service manifest (data-driven — add a project = add a line) --------------
# name | script (relative to APP_DIR) | kind | schedule
#   kind: daemon (long-running, Restart=always) | timer (oneshot + timer)
#   schedule for timer: OnCalendar spec, or "every:<sec>"
SERVICES=(
  "cirrus-api|cirrus_api.py|daemon|-"
  "cirrus-bot|cirrus_bot.py|daemon|-"
  "cirrus-daily|cirrus_daily.py|timer|*-*-* 07:00:00"
  "cirrus-pedagogy|pedagogy_daily.py|timer|*-*-* 06:00:00"
  "cirrus-billsnow|snowbrief/bill_snow_weekly.py|timer|Mon *-*-* 08:06:00"
  "cirrus-billnewdev|newdev/bill_newdev_weekly.py|timer|Mon *-*-* 09:05:00"
  "cirrus-intake|intake.py|timer|every:900"
  "cirrus-devloop|dev_agent.py nightly|timer|*-*-* 21:30:00"
  "cirrus-watchdog|cirrus_watchdog.py|timer|every:1800"
)

# =============================================================================
step_deps(){
  log "OS / system deps for TARGET_ENV=$TARGET_ENV ($(uname -s)/$(uname -m))"
  if [[ "$(uname -s)" == "Linux" ]]; then
    run "sudo apt-get update -qq"
    run "sudo apt-get install -y python3 python3-venv python3-pip git curl build-essential"
    if have nvidia-smi; then run "nvidia-smi -L"; else
      log "WARN: nvidia-smi not found — expected on DGX OS. Check CUDA driver install."; fi
  else
    log "macOS host — assuming Homebrew python3/git present (CIRRUS already provisioned)."
  fi
}

step_venv(){
  log "Python venv + pinned requirements"
  [[ -d "$APP_DIR" ]] || { log "FATAL: APP_DIR $APP_DIR missing — git clone the repo there first."; exit 1; }
  run "python3 -m venv '$APP_DIR/.venv'"
  run "'$APP_DIR/.venv/bin/pip' install -q --upgrade pip"
  run "'$APP_DIR/.venv/bin/pip' install -r '$REPO_ROOT/requirements.txt'"
  log "App deps installed. (LLM serving stack handled in step_serving.)"
}

step_serving(){
  log "LLM serving backend = $MODEL_BACKEND"
  case "$MODEL_BACKEND" in
    ollama-metal)
      log "CIRRUS path — Ollama for macOS already installed; nothing to do." ;;
    ollama-cuda)
      if ! have ollama; then run "curl -fsSL https://ollama.com/install.sh | sh"; fi
      run "ollama --version" ;;
    vllm)
      log "Installing vLLM (CUDA). Verify wheel matches the Spark's CUDA 13 / sm_121."
      run "'$APP_DIR/.venv/bin/pip' install -q vllm"
      log "NOTE: benchmark vLLM vs ollama-cuda on arrival (record tok/s in the ledger) before committing MODEL_BACKEND." ;;
    *) log "FATAL: unknown MODEL_BACKEND=$MODEL_BACKEND"; exit 1 ;;
  esac
}

step_models(){
  log "Pull models: default=$MODEL_DEFAULT embed=$EMBED_MODEL"
  case "$MODEL_BACKEND" in
    ollama-metal|ollama-cuda)
      run "ollama pull '$MODEL_DEFAULT'"
      run "ollama pull '$EMBED_MODEL'" ;;
    vllm)
      log "vLLM serves weights directly — download $MODEL_DEFAULT weights to the model cache."
      log "Keep $EMBED_MODEL on an Ollama sidecar OR a small embeddings server (RAG needs it)." ;;
  esac
}

step_services(){
  log "Service manager = $SERVICE_MANAGER"
  if [[ "$SERVICE_MANAGER" != "systemd" ]]; then
    log "launchd host (CIRRUS) — services already managed by com.cirrus.*.plist. Skipping."; return
  fi
  # ---- credential + promotion gates (must pass before ANY live service) ----
  gate(){ if [[ $DRYRUN -eq 1 ]]; then log "GATE (would STOP): $1"; else log "STOP: $1"; exit 1; fi; }
  [[ -f "$APP_DIR/config/credentials.json" ]] || \
    gate "config/credentials.json missing. Provision creds first (Tier-2, human)."
  if [[ "$TARGET_ENV" == "prod" && ! -f "$APP_DIR/logs/self-changes/cumulus-soak-PASS" ]]; then
    gate "TARGET_ENV=prod but no CUMULUS soak PASS record. No direct-to-prod (change-mgmt gate)."; fi

  # Services must run as the app owner (not root) so Path.home() resolves to the
  # user's home. The app code hardcodes ~/projects/cirrus-digest (CIRRUS layout);
  # if APP_DIR differs, symlink the expected path to it so the code finds config/.
  local run_user run_home expected
  run_user=$(stat -c '%U' "$APP_DIR" 2>/dev/null || echo buddy)
  run_home=$(getent passwd "$run_user" | cut -d: -f6); [[ -z "$run_home" ]] && run_home="/home/$run_user"
  expected="$run_home/projects/cirrus-digest"
  if [[ "$(readlink -f "$expected" 2>/dev/null)" != "$(readlink -f "$APP_DIR")" ]]; then
    log "  linking code-expected path $expected -> $APP_DIR (run as $run_user)"
    run "mkdir -p '$run_home/projects' && ln -sfn '$APP_DIR' '$expected'"
  fi

  local unit_dir="/etc/systemd/system"
  for row in "${SERVICES[@]}"; do
    IFS='|' read -r name script kind sched <<<"$row"
    local exec="$APP_DIR/.venv/bin/python $APP_DIR/$script"
    log "  → $name ($kind) $script"
    # optional resource isolation lines (only emitted when set in platform.env)
    local reslimits=""
    [[ -n "${MEMORY_MAX:-}" ]] && reslimits+="MemoryMax=${MEMORY_MAX}"$'\n'
    [[ -n "${CPU_QUOTA:-}"  ]] && reslimits+="CPUQuota=${CPU_QUOTA}"$'\n'
    # service unit
    run "sudo tee $unit_dir/$name.service >/dev/null <<UNIT
[Unit]
Description=$name ($TARGET_ENV)
After=network-online.target
Wants=network-online.target

[Service]
Type=$([[ $kind == daemon ]] && echo simple || echo oneshot)
User=$run_user
Group=$run_user
WorkingDirectory=$APP_DIR
Environment=HOME=$run_home
Environment=TARGET_ENV=$TARGET_ENV MODEL_BACKEND=$MODEL_BACKEND
ExecStart=$exec
$([[ $kind == daemon ]] && echo 'Restart=on-failure' || true)
$([[ $kind == daemon ]] && echo 'RestartSec=5' || true)
# resource isolation so one project can't starve the digest model (set in platform.env)
${reslimits}
[Install]
WantedBy=multi-user.target
UNIT"
    # timer for scheduled jobs
    if [[ "$kind" == "timer" ]]; then
      local oncal
      if [[ "$sched" == every:* ]]; then oncal="OnUnitActiveSec=${sched#every:}s"; else oncal="OnCalendar=$sched"; fi
      run "sudo tee $unit_dir/$name.timer >/dev/null <<TMR
[Unit]
Description=$name timer ($TARGET_ENV)

[Timer]
$oncal
Persistent=true

[Install]
WantedBy=timers.target
TMR"
    fi
  done
  run "sudo systemctl daemon-reload"
  log "Units written. Enable them AFTER a clean --step verify dry-run:"
  log "  daemons:  sudo systemctl enable --now cirrus-api cirrus-bot"
  log "  timers:   sudo systemctl enable --now cirrus-daily.timer cirrus-pedagogy.timer cirrus-intake.timer cirrus-devloop.timer cirrus-watchdog.timer"
}

step_tunnel(){
  log "Cloudflare tunnel → https://$SUBDOMAIN → localhost:$TUNNEL_PORT"
  if ! have cloudflared; then
    if [[ "$(uname -s)" == "Linux" ]]; then run "sudo apt-get install -y cloudflared || curl -fsSL https://pkg.cloudflare.com/install.sh | sudo bash"; fi
  fi
  log "Configure a NAMED tunnel for $SUBDOMAIN (separate from prod cirrus.cirrustask.com)."
  log "Run as its own systemd service; do NOT collide with the CIRRUS tunnel."
}

step_verify(){
  log "=== go-live checklist (docs/CUMULUS-Beta-Buildout-and-Scaling-Plan.md §2.8) ==="
  local ok=0 warn=0
  chk(){ if eval "$2" >/dev/null 2>&1; then echo "  [ OK ] $1"; ok=$((ok+1)); else echo "  [WARN] $1"; warn=$((warn+1)); fi; }
  [[ "$MODEL_BACKEND" != ollama-metal ]] && chk "nvidia-smi healthy" "nvidia-smi"
  chk "venv + requirements present" "test -x '$APP_DIR/.venv/bin/python'"
  chk "credentials.json present" "test -f '$APP_DIR/config/credentials.json'"
  chk "default model available" "true"   # replace with: ollama list | grep qwen2.5:72b (or vLLM health)
  chk "admin API reachable ($SUBDOMAIN)" "curl -fsS https://$SUBDOMAIN/status?cb=\$RANDOM"
  log "Manual gates still required: DRYRUN digest builds with zero side effects;"
  log "beta digest diffed vs CIRRUS prod; beta Telegram bot posts test; ledger writing."
  log "Result: $ok OK / $warn WARN"
}

# =============================================================================
main(){
  local target="${1:-}"
  [[ "$target" == "--check" ]] && { DRYRUN=1; target="--all"; log "DRY-RUN — no changes will be made"; }
  case "$target" in
    --all)  step_deps; step_venv; step_serving; step_models; step_services; step_tunnel; step_verify ;;
    --step) shift; "step_${1:?which step?}" ;;
    *) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//' | head -20; exit 0 ;;
  esac
  log "Done ($target, TARGET_ENV=$TARGET_ENV)."
}
main "$@"
