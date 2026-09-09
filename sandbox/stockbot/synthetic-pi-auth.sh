#!/bin/sh
# Write synthetic Pi Codex OAuth auth (sentinel credentials, Docker owns refresh
# on the host) then exec the image command. No-op when unset/empty (Compose safe).
set -eu
AGENT_DIR="${PI_CODING_AGENT_DIR:-/root/.pi/agent}"
ID="${STOCKBOT_CODEX_ACCOUNT_ID:-}"
if [ -z "$ID" ]; then
  if [ $# -gt 0 ]; then exec "$@"; else exit 0; fi
fi
case "$ID" in
  *[!A-Za-z0-9_-]*)
    echo "synthetic-pi-auth.sh: ignoring invalid STOCKBOT_CODEX_ACCOUNT_ID" >&2
    if [ $# -gt 0 ]; then exec "$@"; else exit 0; fi
    ;;
esac
mkdir -p "$AGENT_DIR"
printf '{"openai-codex": {"type": "oauth", "access": "DOCKER_SANDBOX_MANAGED", "refresh": "DOCKER_SANDBOX_MANAGED", "expires": 4102444800000, "accountId": "%s"}}' "$ID" > "$AGENT_DIR/auth.json"
chmod 600 "$AGENT_DIR/auth.json"
if [ $# -gt 0 ]; then exec "$@"; else exit 0; fi
