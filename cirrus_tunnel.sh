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
        fi
    fi
done
