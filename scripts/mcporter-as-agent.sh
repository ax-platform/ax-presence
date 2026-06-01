#!/usr/bin/env bash
# Configure mcporter to act as your aX agent over MCP, reusing your EXISTING
# listener access token (no second device-code mint, no OAuth refresher).
#
# The listener stays the sole refresher of ~/.ax/<handle>-listener.json; this only
# reads the current access token and installs it as a static bearer header in
# mcporter, so there's no single-use refresh-token rotation race.
#
# Usage:  scripts/mcporter-as-agent.sh <handle>
# Re-run it whenever a call 401s (the access token has a ~15-min TTL; the listener
# keeps the file refreshed, so re-running picks up a fresh token).
set -euo pipefail

HANDLE="${1:-}"
if [[ -z "$HANDLE" ]]; then
  echo "usage: $0 <agent-handle>" >&2
  exit 2
fi

TOKEN_FILE="${AX_TOKEN_FILE:-$HOME/.ax/${HANDLE}-listener.json}"
if [[ ! -f "$TOKEN_FILE" ]]; then
  echo "no listener token at $TOKEN_FILE — run the listener device-code flow first" >&2
  exit 1
fi

TOK="$(python3 -c "import json;print(json.load(open('$TOKEN_FILE'))['access_token'])")"
BASE="${AX_BASE:-https://paxai.app}"
SERVER="ax-paxai-${HANDLE}"

mcporter config add "$SERVER" \
  --url "${BASE}/mcp/agents/${HANDLE}" \
  --header "Authorization=Bearer ${TOK}" \
  --scope home \
  --description "${HANDLE} identity on aX MCP (existing listener access token)"

echo "configured '$SERVER' -> ${BASE}/mcp/agents/${HANDLE}"
echo "verify:  mcporter call ${SERVER}.whoami action=get"
