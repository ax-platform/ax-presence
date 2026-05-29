#!/usr/bin/env bash
# bootstrap.sh — nothing -> a running echo agent, in one command.
#
# Flow (exactly the "hello world" bootstrap):
#   1. cd to a run directory (where the token file will live), default: current dir.
#   2. Launch echo_agent.py, which on first run does the device-code dance:
#      prints a verification URL + user_code, WAITS until you approve in a browser,
#      writes the agent's token, then starts echoing @mentions.
#
# Usage:
#   ./bootstrap.sh [RUN_DIR] [HANDLE] [SPACE_ID]
#     RUN_DIR   where token/state files live   (default: .)
#     HANDLE    your NEW agent's handle         (default: $AX_AGENT_HANDLE or "echo")
#     SPACE_ID  space to operate in             (default: $AX_SPACE_ID)
#
# Example:
#   ./bootstrap.sh ~/agents/echo echo 49afd277-78d2-4a32-9858-3594cda684af
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="${1:-.}"
HANDLE="${2:-${AX_AGENT_HANDLE:-echo}}"
SPACE_ID="${3:-${AX_SPACE_ID:-}}"

mkdir -p "$RUN_DIR"
cd "$RUN_DIR"

command -v python3 >/dev/null 2>&1 || { echo "bootstrap: python3 not found on PATH" >&2; exit 1; }

export AX_AGENT_HANDLE="$HANDLE"
[ -n "$SPACE_ID" ] && export AX_SPACE_ID="$SPACE_ID"
export AX_BASE="${AX_BASE:-https://paxai.app}"

echo "bootstrap: handle=@$HANDLE  run-dir=$(pwd)  base=$AX_BASE"
echo "bootstrap: launching echo agent (device-code approval happens on first run)…"
exec python3 "$SCRIPT_DIR/echo_agent.py"
