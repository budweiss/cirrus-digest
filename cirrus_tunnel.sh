#!/bin/bash
# cirrus_tunnel.sh
# Starts a Cloudflare quick tunnel pointing to the local Flask API (port 5001).
# Captures the assigned URL and writes it to digests/tunnel-url.txt so
# Cowork can read it via the SMB mount without needing SSH.
#
# Managed by launchd as com.cirrus.tunnel — do not run manually.

URL_FILE="$HOME/projects/cirrus-digest/digests/tunnel-url.txt"
CLOUDFLARED="/opt/homebrew/bin/cloudflared"

# Clear any stale URL from a previous run
echo "" > "$URL_FILE"

echo "[$(date)] Starting Cloudflare quick tunnel → http://localhost:5001"

# Run cloudflared and watch its output for the assigned URL.
# cloudflared prints the URL to stderr in a banner like:
#   | https://example-words.trycloudflare.com |
"$CLOUDFLARED" tunnel --url http://localhost:5001 2>&1 | while IFS= read -r line; do
    echo "$line"
    # Match the trycloudflare.com URL in the banner
    if echo "$line" | grep -q "trycloudflare.com"; then
        URL=$(echo "$line" | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com')
        if [ -n "$URL" ]; then
            echo "$URL" > "$URL_FILE"
            echo "[$(date)] Tunnel URL saved: $URL"
            # Notify via Telegram so Buddy knows the URL changed after a reboot
            CREDS="$HOME/projects/cirrus-digest/config/credentials.json"
            BOT_TOKEN=$(python3 -c "import json; print(json.load(open('$CREDS'))['telegram_bot_token'])")
            CHAT_ID=$(python3 -c "import json; print(json.load(open('$CREDS'))['telegram_user_id'])")
            curl -s "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
                -d "chat_id=${CHAT_ID}&text=🌐 CIRRUS tunnel restarted.%0ANew URL: ${URL}%0AUpdate local.json if starting a Cowork session." \
                > /dev/null
        fi
    fi
done
