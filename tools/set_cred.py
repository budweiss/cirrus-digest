#!/usr/bin/env python3
"""
set_cred.py — securely set ONE field in this box's config/credentials.json.
===============================================================================
Runs ON a server (CIRRUS/CUMULUS). Reads:
  * the field NAME from the env var  FIELD   (non-secret)
  * the secret VALUE from STDIN               (never on argv/env/ps/logs)

Writes the value into config/credentials.json (atomic replace, chmod 600) and
prints ONLY the field name + length — never the value. Idempotent.

Invoked by the MacBook Terminal helper runner/set-llm-key.sh, which pipes the
key in over ssh so it never touches chat, shell history, or git.

  echo -n "<secret>" | FIELD=gemini_api_key python3 tools/set_cred.py
"""
import json
import os
import sys
import tempfile

ALLOWED = {
    "anthropic_api_key", "claude_model", "claude_dev_model",
    "gemini_api_key", "gemini_model",
    "openai_api_key", "openai_model",
    "grok_api_key", "grok_model",
    "deepseek_api_key", "deepseek_model",
    "brave_api_key",
    # S57: X (Twitter) API for the Bill HOA lead-monitor (v2 recent search).
    "x_bearer_token", "x_api_key", "x_api_secret",
}

CREDS = "config/credentials.json"


def main():
    field = os.environ.get("FIELD", "").strip()
    if field not in ALLOWED:
        print(f"ERROR: FIELD '{field}' not in allowlist", file=sys.stderr)
        sys.exit(2)
    value = sys.stdin.read().strip()
    if not value:
        print("ERROR: empty value on stdin — nothing written", file=sys.stderr)
        sys.exit(3)
    try:
        d = json.load(open(CREDS))
    except Exception as e:
        print(f"ERROR: cannot read {CREDS}: {e}", file=sys.stderr)
        sys.exit(4)
    d[field] = value
    fd, tmp = tempfile.mkstemp(dir="config")
    try:
        with os.fdopen(fd, "w") as o:
            json.dump(d, o, indent=2)
            o.write("\n")
        os.replace(tmp, CREDS)
        os.chmod(CREDS, 0o600)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print(f"set {field} (len {len(value)}) in {CREDS}; chmod 600")


if __name__ == "__main__":
    main()
