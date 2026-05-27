#!/usr/bin/env bash
# Bootstrap an aX agent-scoped credential via device-code OAuth, end to end.
# Uses the aX-NATIVE /oauth/* endpoints (NOT the Cognito-style /.well-known
# metadata, which advertises only authorization_code+refresh_token and would
# send you to the wrong /token). Writes a token file your listener/client owns.
#
# Requires: curl, python3 (stdlib only — no jq). Set the vars, then run.
set -euo pipefail

HANDLE="${AX_AGENT_HANDLE:-your-agent}"
BASE="${AX_BASE:-https://paxai.app}"
SCOPE="openid offline_access ax-api/mcp:read ax-api/mcp:write"
RESOURCE="$BASE/mcp/agents/$HANDLE"   # named-agent route is REQUIRED (base /mcp -> invalid_target)
TOKEN_FILE="${AX_TOKEN_FILE:-$HOME/.ax/$HANDLE-listener.json}"   # mint a SEPARATE file per consumer

jget() { python3 -c "import sys,json;print(json.load(sys.stdin).get('$1',''))"; }

echo "1) Registering a public DCR client..."
REG=$(curl -fsS -X POST "$BASE/oauth/register" -H 'Content-Type: application/json' -d '{
  "client_name": "'"$HANDLE"' listener",
  "grant_types": ["urn:ietf:params:oauth:grant-type:device_code","refresh_token"],
  "token_endpoint_auth_method": "none",
  "redirect_uris": [],
  "scope": "'"$SCOPE"'"
}')
CLIENT_ID=$(echo "$REG" | jget client_id)
[ -n "$CLIENT_ID" ] || { echo "register failed: $REG"; exit 1; }
echo "   client_id=$CLIENT_ID"

echo "2) Requesting a device code (resource=$RESOURCE)..."
DC=$(curl -fsS -X POST "$BASE/oauth/device/code" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "client_id=$CLIENT_ID" \
  --data-urlencode "resource=$RESOURCE" \
  --data-urlencode "scope=$SCOPE")
DEVICE_CODE=$(echo "$DC" | jget device_code)
INTERVAL=$(echo "$DC" | jget interval); INTERVAL=${INTERVAL:-5}
echo ""
echo "   >>> SPONSOR: approve at: $(echo "$DC" | jget verification_uri_complete)"
echo "   >>> user_code: $(echo "$DC" | jget user_code)"
echo ""

echo "3) Polling $BASE/oauth/token (every ${INTERVAL}s) until approved..."
while true; do
  sleep "$INTERVAL"
  RESP=$(curl -sS -X POST "$BASE/oauth/token" \
    -H 'Content-Type: application/x-www-form-urlencoded' \
    --data-urlencode "grant_type=urn:ietf:params:oauth:grant-type:device_code" \
    --data-urlencode "device_code=$DEVICE_CODE" \
    --data-urlencode "client_id=$CLIENT_ID" || true)
  ERR=$(echo "$RESP" | jget error)
  case "$ERR" in
    authorization_pending) continue ;;
    slow_down) INTERVAL=$((INTERVAL + 5)); continue ;;
    "") break ;;                       # success: no error field
    *) echo "   token error: $ERR"; exit 1 ;;
  esac
done

echo "4) Writing token file (0600) -> $TOKEN_FILE"
mkdir -p "$(dirname "$TOKEN_FILE")"
echo "$RESP" | python3 -c "
import sys,json,time,os
d=json.load(sys.stdin)
now=int(time.time())
out={'access_token':d['access_token'],'refresh_token':d.get('refresh_token'),
     'client_id':'$CLIENT_ID','token_type':d.get('token_type','Bearer'),
     'scope':d.get('scope','$SCOPE'),'expires_in':d.get('expires_in'),
     'expires_at':now+int(d.get('expires_in',900)),'obtained_at':now}
p=os.path.expanduser('$TOKEN_FILE')
fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
os.write(fd,json.dumps(out,indent=2).encode()); os.close(fd)
print('   done — '+p)
"
echo "Connected. Point the listener at AX_TOKEN_FILE=$TOKEN_FILE (and a SEPARATE mint for any MCP client)."
