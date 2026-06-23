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

# Read tunnel token from credentials.json
TOKEN=$(python3 -c "import json; print(json.load(open('$CREDS'))['cloudflare_tunnel_token'])")

if [ -z "$TOKEN" ]; then
    echo "[$(date)] ERROR: cloudflare_tunnel_token not found in credentials.json"
    exit 1
fi

echo "[$(date)] Starting permanent Cloudflare tunnel → https://cirrus.cirrustask.com → http://localhost:5001"

# Notify via Telegram that tunnel is starting
BOT_TOKEN=$(python3 -c "import json; print(json.load(open('$CREDS'))['telegram_bot_token'])")
CHAT_ID=$(python3 -c "import json; print(json.load(open('$CREDS'))['telegram_user_id'])")
curl -s "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d "chat_id=${CHAT_ID}&text=🌐 CIRRUS tunnel started.%0AURL: https://cirrus.cirrustask.com" \
    > /dev/null

# Run the named tunnel with local ingress config
exec "$CLOUDFLARED" tunnel --config "$CONFIG" run --token "$TOKEN"
