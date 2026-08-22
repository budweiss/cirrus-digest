#!/bin/bash
# cirrus_tunnel.sh
# Starts the permanent named Cloudflare tunnel (cirrus → cirrus.cirrustask.com → port 5001).
# Token is read from credentials.json — never hardcoded here.
# No URL capture needed — URL is always https://cirrus.cirrustask.com
#
# Managed by launchd as com.cirrus.tunnel — do not run manually.

CLOUDFLARED="/opt/homebrew/bin/cloudflared"
CREDS="$HOME/projects/cirrus-digest/config/credentials.json"
CONFIG="$HOME/.cloudflared/config.yml"

# S73: the token goes to a FILE, never onto the command line.
#
# This wrapper used to `exec cloudflared ... --token "$TOKEN"`, which puts the
# live tunnel token in argv where ANY `ps` can read it. That is exactly how it
# leaked into a transcript on 2026-08-22: a routine `ps -axo user,pid,command`
# diagnostic, with no interest in secrets, printed it. CUMULUS already did this
# correctly (`--token-file /etc/cloudflared/token`); CIRRUS did not.
#
# The file lives beside credentials.json — i.e. INSIDE the credentials RAM disk,
# which a reboot wipes — so the secret never touches persistent storage. The
# tunnel already depends on that RAM disk being up (it reads credentials.json
# from it), so this adds no new ordering requirement.
umask 077
TOKEN=$(python3 -c "import json; print(json.load(open('$CREDS'))['cloudflare_tunnel_token'])")

if [ -z "$TOKEN" ]; then
    echo "[$(date)] ERROR: cloudflare_tunnel_token not found in credentials.json"
    exit 1
fi

CREDS_DIR=$(dirname "$(readlink "$CREDS" 2>/dev/null || echo "$CREDS")")
TOKENFILE="$CREDS_DIR/.tunnel-token"
printf '%s' "$TOKEN" > "$TOKENFILE" || {
    echo "[$(date)] ERROR: could not write $TOKENFILE"; exit 1; }
chmod 600 "$TOKENFILE"
unset TOKEN

echo "[$(date)] Starting permanent Cloudflare tunnel → https://cirrus.cirrustask.com → http://localhost:5001"

# Notify via Telegram that tunnel is starting
# S73: the BOT TOKEN was in the URL, and a URL is argv — so every tunnel start
# briefly published the Telegram bot token to `ps` too. Same class of leak as
# the tunnel token above, found while fixing it. `curl --config -` reads the
# url from STDIN, which argv never sees.
BOT_TOKEN=$(python3 -c "import json; print(json.load(open('$CREDS'))['telegram_bot_token'])")
CHAT_ID=$(python3 -c "import json; print(json.load(open('$CREDS'))['telegram_user_id'])")
printf 'url = "https://api.telegram.org/bot%s/sendMessage"\ndata = "chat_id=%s&text=%s"\nsilent\n' \
    "$BOT_TOKEN" "$CHAT_ID" \
    "🌐 CIRRUS tunnel started.%0AURL: https://cirrus.cirrustask.com" \
    | curl --config - > /dev/null 2>&1
unset BOT_TOKEN

# Run the named tunnel with local ingress config
exec "$CLOUDFLARED" --config "$CONFIG" tunnel run --token-file "$TOKENFILE"
