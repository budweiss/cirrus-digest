#!/bin/bash
# set_provider_key.sh — store an LLM provider API key, and PROVE it works first.
#
# S74, 2026-08-23. Written after a one-liner failed with:
#     zsh:read:1: -p: no coprocess
# because CIRRUS runs zsh, where `read -p` means "coprocess", not "prompt".
# The bash-ism worked everywhere I had tested it and nowhere that mattered.
# A heredoc cannot fix it either: piping a script to ssh consumes stdin, so the
# prompt would have nothing to read from. A script on the box is the answer.
#
# WHY IT TESTS BEFORE IT WRITES
# -----------------------------
# The Time Machine passphrase was stored looking perfectly correct — right size,
# right owner, right mode — and was simply the wrong value. Nobody found out
# until a reboot 21 hours later. A credential that is written but never
# exercised is a credential you do not have. So this makes a real API call with
# the key and refuses to store it if the provider rejects it.
#
# THE KEY NEVER: appears on screen, enters shell history, reaches `ps` (no
# argv), or touches persistent disk in plaintext — apply_creds_encrypted.py
# merges it into the .age file in memory.
#
# Usage:  sudo -u buddy bash set_provider_key.sh deepseek deepseek-v4-flash
#         bash set_provider_key.sh <provider> <model>
set -uo pipefail

PROVIDER="${1:-}"
MODEL="${2:-}"
REPO="$(cd "$(dirname "$0")" && pwd)"
SECRETS="$HOME/.config/cirrus-secrets"

# T11: the provider name selects which credential key gets overwritten, so it is
# validated as a NAMESPACE we own, never a charset.
case "$PROVIDER" in
    anthropic|gemini|grok|openai|deepseek) ;;
    *) echo "REFUSED: provider must be one of anthropic|gemini|grok|openai|deepseek"
       echo "Usage: bash set_provider_key.sh <provider> <model>"
       exit 2 ;;
esac
if [ -z "$MODEL" ]; then
    echo "REFUSED: need a model id, e.g. deepseek-v4-flash"
    exit 2
fi

# Endpoint per provider — all OpenAI-compatible except Anthropic/Gemini, and we
# only ever need to prove the key authenticates.
case "$PROVIDER" in
    deepseek) URL="https://api.deepseek.com/v1/chat/completions" ;;
    openai)   URL="https://api.openai.com/v1/chat/completions" ;;
    grok)     URL="https://api.x.ai/v1/chat/completions" ;;
    *)        URL="" ;;
esac

echo "provider : $PROVIDER"
echo "model    : $MODEL"
echo
printf 'Paste the API key (it will NOT be shown): '
# zsh and bash disagree about `read -p`, so prompt above and read plainly here —
# this form works in both.
stty -echo 2>/dev/null
read -r KEY
stty echo 2>/dev/null
echo
[ -z "$KEY" ] && { echo "Nothing entered — no change made."; exit 2; }

# ── 1. PROVE IT WORKS, before storing anything ──────────────────────────────
if [ -n "$URL" ]; then
    echo -n "testing the key against $PROVIDER... "
    BODY=$(printf '{"model":"%s","max_tokens":8,"messages":[{"role":"user","content":"Reply with the single word: OK"}]}' "$MODEL")
    # key goes in a header via @- on stdin, never on the command line (T21)
    RESP=$(printf 'Authorization: Bearer %s\n' "$KEY" \
           | curl -s --max-time 45 -o /tmp/.pk_resp.$$ -w '%{http_code}' \
                  -H @- -H "Content-Type: application/json" \
                  -d "$BODY" "$URL" 2>/dev/null)
    if [ "$RESP" = "200" ]; then
        echo "accepted (HTTP 200)"
    else
        echo "REJECTED (HTTP ${RESP:-none})"
        echo "  provider said:"
        sed -E 's/[A-Za-z0-9_-]{24,}/<redacted>/g' /tmp/.pk_resp.$$ 2>/dev/null | head -4 | sed 's/^/    /'
        rm -f /tmp/.pk_resp.$$
        unset KEY
        echo
        echo "NOTHING WAS WRITTEN. Common causes: no credit on the account, or a"
        echo "model id that has been retired (deepseek-chat and deepseek-reasoner"
        echo "were retired 2026-07-24; the V4 ids are deepseek-v4-flash / -pro)."
        exit 1
    fi
    rm -f /tmp/.pk_resp.$$
fi

# ── 2. store it in the ENCRYPTED source of truth ────────────────────────────
umask 077
UPD="/tmp/.provider-key.$$.json"
# The key travels to python via the ENVIRONMENT, never as an argument — an
# argument is argv, and argv is what `ps` prints (T21, 2026-08-22).
export PK_KEY="$KEY"
unset KEY
if ! python3 - "$UPD" "$PROVIDER" "$MODEL" <<'PY'
import json, os, sys
path, prov, model = sys.argv[1], sys.argv[2], sys.argv[3]
json.dump({f"{prov}_api_key": os.environ["PK_KEY"], f"{prov}_model": model},
          open(path, "w"))
PY
then
    unset PK_KEY; echo "could not build the update file"; exit 1
fi

if ! python3 "$REPO/apply_creds_encrypted.py" "$UPD" \
        "$REPO/config/credentials.json.age" \
        "$SECRETS/age-identity.txt" "$(cat "$SECRETS/age-recipient.txt")"; then
    rm -f "$UPD"; unset PK_KEY
    echo "!! could not update credentials.json.age — nothing changed there."
    exit 1
fi
rm -f "$UPD"
unset PK_KEY

# ── 3. re-materialize and confirm the running copy sees it ──────────────────
"$REPO/materialize_credentials_cirrus.sh" >/dev/null 2>&1
cd "$REPO" && python3 - "$PROVIDER" <<'PY'
import json, sys
sys.path.insert(0, ".")
import llm_providers as L
prov = sys.argv[1]
creds = json.load(open("config/credentials.json"))
print(f"  stored          : {prov}_api_key present={bool(creds.get(prov+'_api_key'))}"
      f"  model={creds.get(prov+'_model')}")
print(f"  available()     : {L.available(creds)}")
print(f"  in the council  : {prov in L.available(creds)}")
PY
echo
echo "Done. No code change is needed — the provider is already in DEFAULT_ORDER,"
echo "so it joins the council on the next dev-loop / business-idea run."
