#!/usr/bin/env bash
# agent-move-and-verify.sh — restart an aX/Hermes gateway listener into a destination
# space and verify that presence alone is not treated as ready.
#
# Usage:
#   scripts/agent-move-and-verify.sh <handle> <destination-space-id> [--post-smoke]
#
# Defaults support the two runtime shapes currently used on this host:
#   1. Profile-scoped agents under /home/ax-agents/peach/profiles/<handle>
#      launched as: HERMES_HOME=/home/ax-agents/peach hermes -p <handle> gateway run
#   2. Standalone agent homes under /home/ax-agents/agents/<handle>
#      launched through scripts/standup-gateway.sh.
#
# Proof boundary:
#   - This script verifies restart + fresh destination-space SSE/presence.
#   - If --post-smoke is supplied, it posts a synthetic mention from SMOKE_TOKEN_FILE
#     and polls for a target-agent reply. For final human-facing "ready", prefer a
#     human/operator mention in the destination space and keep the returned message ids.
set -euo pipefail

usage() {
  sed -n '1,36p' "$0" >&2
  exit 64
}

HANDLE="${1:-}"
DEST_SPACE="${2:-}"
shift $(( $# >= 2 ? 2 : $# ))
[ -n "$HANDLE" ] && [ -n "$DEST_SPACE" ] || usage

POST_SMOKE=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --post-smoke) POST_SMOKE=true ;;
    -h|--help) usage ;;
    *) echo "unknown option: $1" >&2; usage ;;
  esac
  shift
done

BASE_URL="${AX_BASE_URL:-https://paxai.app}"
AX_PRESENCE_DIR="${AX_PRESENCE_DIR:-/home/ax-agents/ax-presence}"
PROFILE_BASE="${PROFILE_BASE:-/home/ax-agents/peach}"
PROFILE_RUN="$PROFILE_BASE/profiles/$HANDLE/run-$HANDLE-gateway.sh"
PROFILE_LOG="$PROFILE_BASE/profiles/$HANDLE/$HANDLE-gw.log"
STANDALONE_HOME="${HOME_DIR:-/home/ax-agents/agents/$HANDLE}"
STANDALONE_LOG="$STANDALONE_HOME/$HANDLE-gw.log"
TOKEN_FILE="${TARGET_TOKEN_FILE:-$HOME/.ax/$HANDLE-listener.json}"
SMOKE_TOKEN_FILE="${SMOKE_TOKEN_FILE:-$HOME/.ax/peach-listener.json}"
TMUX_SESSION="${TMUX_SESSION:-$HANDLE-gw}"
CURRENT_LOG=""
export PATH="$HOME/.local/bin:$PATH"

uuid_re='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
[[ "$DEST_SPACE" =~ $uuid_re ]] || { echo "move: destination must be a space UUID, got: $DEST_SPACE" >&2; exit 64; }
command -v tmux >/dev/null || { echo "move: tmux not found" >&2; exit 127; }
command -v python3 >/dev/null || { echo "move: python3 not found" >&2; exit 127; }

log() { printf 'move: %s\n' "$*"; }

patch_profile_run_script() {
  [ -f "$PROFILE_RUN" ] || return 1
  python3 - "$PROFILE_RUN" "$DEST_SPACE" <<'PY'
from pathlib import Path
import re, sys
path = Path(sys.argv[1])
dest = sys.argv[2]
text = path.read_text()
text2 = re.sub(r'^export AX_SPACE_ID=.*$', f'export AX_SPACE_ID={dest}', text, flags=re.M)
if 'export AX_HOME_SPACE="$AX_SPACE_ID"' not in text2:
    text2 = text2.replace(f'export AX_SPACE_ID={dest}\n', f'export AX_SPACE_ID={dest}\nexport AX_HOME_SPACE="$AX_SPACE_ID"\n')
if text2 != text:
    path.write_text(text2)
PY
  log "profile run script points @$HANDLE at $DEST_SPACE ($PROFILE_RUN)"
}

restart_profile_gateway() {
  patch_profile_run_script || return 1
  CURRENT_LOG="$PROFILE_LOG"
  tmux kill-session -t "$TMUX_SESSION" 2>/dev/null && log "stopped tmux $TMUX_SESSION" || true
  sleep 2
  tmux new -d -s "$TMUX_SESSION" "bash $PROFILE_RUN > $PROFILE_LOG 2>&1"
  log "launched tmux $TMUX_SESSION using $PROFILE_RUN"
}

restart_standalone_gateway() {
  log "no profile run script found; using standalone standup-gateway.sh"
  CURRENT_LOG="$STANDALONE_LOG"
  HOME_DIR="$STANDALONE_HOME" AX_PRESENCE_DIR="$AX_PRESENCE_DIR" \
    "$AX_PRESENCE_DIR/scripts/standup-gateway.sh" "$HANDLE" "$DEST_SPACE"
}

verify_presence() {
  python3 - "$BASE_URL" "$DEST_SPACE" "$HANDLE" "$TOKEN_FILE" <<'PY'
import json, sys, time, urllib.request
base, space, handle, token_file = sys.argv[1:]
token = json.load(open(token_file))["access_token"]
try:
    body = json.dumps({"space_id": space}).encode()
    req = urllib.request.Request(
        f"{base}/api/spaces/switch",
        data=body,
        method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        switched = json.loads(r.read().decode() or "{}")
    token = switched.get("new_token") or switched.get("access_token") or token
except Exception as exc:
    last = {"space_switch_error": f"{type(exc).__name__}: {exc}"}
else:
    last = None
for _ in range(30):
    req = urllib.request.Request(
        f"{base}/api/v1/agents/availability?space_id={space}",
        headers={"Authorization": "Bearer " + token, "X-Space-Id": space},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
        data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception as exc:
        last = {"parse_or_request_error": f"{type(exc).__name__}: {exc}"}
        time.sleep(2)
        continue
    for a in data.get("agents", []):
        if a.get("name") == handle:
            last = a
            age = a.get("presence_age_seconds")
            fresh = a.get("presence") == "connected" and a.get("sse_connected") is True and (age is None or float(age) < 90)
            if fresh:
                print(json.dumps({
                    "handle": handle,
                    "space_id": space,
                    "presence": a.get("presence"),
                    "availability": a.get("availability"),
                    "sse_connected": a.get("sse_connected"),
                    "presence_age_seconds": a.get("presence_age_seconds"),
                    "last_active": a.get("last_active"),
                }, sort_keys=True))
                sys.exit(0)
    time.sleep(2)
print(json.dumps({"handle": handle, "space_id": space, "fresh_presence": False, "last_seen": last}, sort_keys=True))
sys.exit(10)
PY
}

post_smoke_and_wait() {
  python3 - "$BASE_URL" "$DEST_SPACE" "$HANDLE" "$SMOKE_TOKEN_FILE" <<'PY'
import json, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone
base, space, handle, token_file = sys.argv[1:]
base_token = json.load(open(token_file))["access_token"]
# Message POST/list endpoints are session-space sensitive. Switch the smoke
# sender token into the destination space before posting/readback, otherwise a
# home-space-scoped token can 2xx/HTML/empty-body and leave no readable smoke id.
token = base_token
try:
    switch_body = json.dumps({"space_id": space}).encode()
    switch_req = urllib.request.Request(
        f"{base}/api/spaces/switch",
        data=switch_body,
        method="POST",
        headers={"Authorization": "Bearer " + base_token, "Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(switch_req, timeout=20) as r:
        switch_raw = r.read()
    switch_data = json.loads(switch_raw.decode() if isinstance(switch_raw, bytes) else switch_raw) if switch_raw else {}
    token = switch_data.get("new_token") or switch_data.get("access_token") or base_token
except Exception as exc:
    print(json.dumps({"space_switch_error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json", "X-Space-Id": space}
content = f"@{handle} move verification smoke: reply with READY and your handle."
body = json.dumps({"content": content, "space_id": space, "channel": "main", "message_type": "text"}).encode()
req = urllib.request.Request(f"{base}/api/v1/messages", data=body, method="POST", headers=headers)
with urllib.request.urlopen(req, timeout=20) as r:
    raw = r.read()
try:
    posted = json.loads(raw.decode() if isinstance(raw, bytes) else raw) if raw else {}
except Exception:
    posted = {}
msg = posted.get("message") or posted
mid = msg.get("id")
created = msg.get("created_at")
if not mid:
    # Some deployed message paths return an empty body; recover by finding the
    # unique smoke content in the destination-space recent messages.
    qs = urllib.parse.urlencode({"space_id": space, "limit": 30})
    req = urllib.request.Request(f"{base}/api/v1/messages?{qs}", headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
    try:
        data = json.loads(raw.decode() if isinstance(raw, bytes) else raw) if raw else {}
    except Exception:
        data = {}
    for m in data.get("messages", data if isinstance(data, list) else []):
        if (m.get("content") or "") == content:
            mid = m.get("id")
            created = m.get("created_at")
            break
if not mid:
    print(json.dumps({"smoke_message_id": None, "posted_but_unreadable": True, "error": "POST/list did not return the smoke message; check SMOKE_TOKEN_FILE membership for destination space"}, sort_keys=True))
    sys.exit(21)
print(json.dumps({"smoke_message_id": mid, "created_at": created}, sort_keys=True))

def ts(v):
    if not v: return 0.0
    return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
start = ts(created) or time.time()
agent_id = None
routing_target = None
# Resolve target id from routing readback. This is part of the create/move
# confidence bar: presence plus a posted mention is not enough unless the
# destination-space message resolved to the expected target.
if mid:
    req = urllib.request.Request(f"{base}/api/v1/messages/{mid}", headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        readback = json.load(r)
    rb = readback.get("message") or readback
    md = rb.get("metadata") or {}
    for t in ((md.get("routing_story") or {}).get("targets") or []):
        target_name = t.get("agent_name") or t.get("name") or t.get("handle")
        if str(target_name or "").lstrip("@").lower() == handle.lower():
            routing_target = t
            agent_id = t.get("agent_id") or t.get("id")
            break
print(json.dumps({"routing_target_found": bool(routing_target), "target_agent_id": agent_id, "routing_target": routing_target}, sort_keys=True))
if not routing_target or not agent_id:
    print(json.dumps({"reply_found": False, "after_smoke_message_id": mid, "error": "smoke message readback did not include the expected routing_story target"}, sort_keys=True))
    sys.exit(22)
for _ in range(90):
    qs = urllib.parse.urlencode({"space_id": space, "limit": 80})
    req = urllib.request.Request(f"{base}/api/v1/messages?{qs}", headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    for m in data.get("messages", data if isinstance(data, list) else []):
        if ts(m.get("created_at")) <= start:
            continue
        if agent_id and m.get("agent_id") != agent_id:
            continue
        if not agent_id and (m.get("agent_name") or "").lower() != handle.lower():
            continue
        if m.get("parent_id") != mid:
            continue
        content_text = m.get("content") or ""
        if "ready" not in content_text.lower() or handle.lower() not in content_text.lower():
            continue
        print(json.dumps({"reply_message_id": m.get("id"), "reply_created_at": m.get("created_at"), "agent_id": m.get("agent_id"), "parent_id": m.get("parent_id")}, sort_keys=True))
        sys.exit(0)
    time.sleep(2)
print(json.dumps({"reply_found": False, "after_smoke_message_id": mid}, sort_keys=True))
sys.exit(20)
PY
}

verify_log_dispatch() {
  local msg_id="$1"
  [ -n "$CURRENT_LOG" ] || return 1
  python3 - "$CURRENT_LOG" "$msg_id" <<'PY'
from pathlib import Path
import json, sys, time
log_path, msg_id = sys.argv[1:]
for _ in range(45):
    text = Path(log_path).read_text(errors="replace") if Path(log_path).exists() else ""
    inbound = "aX inbound dispatch:" in text and f"msg={msg_id}" in text
    sent = "aX send delivered:" in text and f"parent={msg_id}" in text
    if inbound:
        print(json.dumps({"gateway_log": log_path, "inbound_dispatch_for": msg_id, "send_delivery_for_parent": sent}, sort_keys=True))
        raise SystemExit(0)
    time.sleep(2)
print(json.dumps({"gateway_log": log_path, "inbound_dispatch_for": msg_id, "found": False}, sort_keys=True))
raise SystemExit(30)
PY
}

log "restarting @$HANDLE into destination space $DEST_SPACE"
if [ -f "$PROFILE_RUN" ]; then
  restart_profile_gateway
else
  restart_standalone_gateway
fi

sleep 8
log "checking destination-space SSE/presence"
verify_presence

if [ "$POST_SMOKE" = true ]; then
  [ -f "$SMOKE_TOKEN_FILE" ] || { echo "move: --post-smoke needs SMOKE_TOKEN_FILE (default $SMOKE_TOKEN_FILE)" >&2; exit 65; }
  log "posting synthetic mention and polling for reply"
  set +e
  smoke_out="$(post_smoke_and_wait)"
  smoke_rc=$?
  set -e
  printf '%s\n' "$smoke_out"
  smoke_msg="$(python3 -c 'import json,sys
for line in sys.stdin:
    try: d=json.loads(line)
    except Exception: continue
    if d.get("smoke_message_id"):
        print(d["smoke_message_id"]); break' <<<"$smoke_out")"
  if [ -z "$smoke_msg" ]; then
    echo "move: smoke posted but no smoke_message_id was parsed" >&2
    exit 66
  fi
  log "checking gateway log for exact smoke-message dispatch"
  verify_log_dispatch "$smoke_msg"
  if [ "$smoke_rc" -ne 0 ]; then
    echo "move: smoke did not reach reply proof (post_smoke_and_wait rc=$smoke_rc)" >&2
    exit "$smoke_rc"
  fi
else
  log "presence/SSE verified; final ready still requires destination mention -> reply proof"
fi
