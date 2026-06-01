#!/usr/bin/env bash
# standup-gateway.sh — nothing -> a live Hermes GATEWAY agent on aX, in one command.
#
#   nothing  ->  brain authed  ->  skilled  ->  aX identity  ->  gateway online under tmux
#
# This is the DURABLE path that replaced the old echo_agent shell-out: a single
# persistent `hermes gateway run` owns the aX connection via the `ax` plugin and
# the device-code token. No echo_agent, no per-message claude -p approval spam,
# no multi-consumer token race. Proven on daimon, zephyr, atlas.
#
# Usage:
#   scripts/standup-gateway.sh <handle> [space_id]
#     handle    agent handle (e.g. canary)                 (required)
#     space_id  aX space                                   (default: $AX_SPACE_ID)
#
# Env knobs:
#   AGENTS_ROOT      where agent homes live   (default: /home/ax-agents/agents)
#   HOME_DIR         override the agent home  (default: $AGENTS_ROOT/<handle>)
#   REUSE_AUTH_FROM  an authed HERMES_HOME whose auth.json (codex login) to copy
#                    (default: /home/ax-agents/daimon — the shared-Codex-login pattern)
#   AX_AGENT_ID      pin the agent UUID in the run script (recommended; avoids the
#                    auto-resolve/token-refresh race). If unset, the adapter resolves
#                    it from the token and the script prints a reminder to pin it.
#   AX_PRESENCE_DIR  ax-presence checkout     (default: /home/ax-agents/ax-presence)
#   AX_SPONSOR       failure-alert handle     (default: @madtank)
#
# Hard-won notes baked in:
#   * config is MINIMAL (zephyr pattern): model + plugins[ax,ax-platform] + smart
#     approvals. NO per-agent `ax-paxai-*` mcp_servers — that forces an interactive
#     browser OAuth that BLOCKS unattended gateway startup in headless.
#   * Hermes backend auth is PER-HERMES_HOME (auth.json) — copy it per agent.
#   * a fresh HERMES_HOME ships an EMPTY skill store — skills MUST be backfilled.
#   * `approvals.mode: smart` => aux-LLM auto-approves routine, prompts only dangerous.
#   * ONE listener per handle (two share the single-use rotating token -> 401 fight).
#   * idempotent: re-running re-writes config/run-script and relaunches cleanly,
#     and MIGRATES an agent off the old echo path (deprecates run-<handle>.sh).
set -euo pipefail

HANDLE="${1:?usage: standup-gateway.sh <handle> [space_id]}"
SPACE_ID="${2:-${AX_SPACE_ID:-}}"
AGENTS_ROOT="${AGENTS_ROOT:-/home/ax-agents/agents}"
HOME_DIR="${HOME_DIR:-$AGENTS_ROOT/$HANDLE}"
REUSE_AUTH_FROM="${REUSE_AUTH_FROM:-/home/ax-agents/daimon}"
AX_PRESENCE_DIR="${AX_PRESENCE_DIR:-/home/ax-agents/ax-presence}"
SPONSOR="${AX_SPONSOR:-@madtank}"
TOKEN_FILE="$HOME/.ax/$HANDLE-listener.json"
export PATH="$HOME/.local/bin:$PATH"

[ -n "$SPACE_ID" ] || { echo "standup: set space_id (arg 2) or AX_SPACE_ID" >&2; exit 1; }
command -v hermes  >/dev/null || { echo "standup: hermes not installed" >&2; exit 1; }
command -v python3 >/dev/null || { echo "standup: python3 not found" >&2; exit 1; }

echo "standup: @$HANDLE  home=$HOME_DIR  space=$SPACE_ID  token=$TOKEN_FILE"
mkdir -p "$HOME_DIR" "$HOME/.ax"

# ── 1. brain backend (Plane 2): per-HERMES_HOME auth.json (shared codex login) ──
if [ ! -f "$HOME_DIR/auth.json" ]; then
  [ -f "$REUSE_AUTH_FROM/auth.json" ] || {
    echo "standup: no auth.json and REUSE_AUTH_FROM=$REUSE_AUTH_FROM has none." >&2
    echo "  Run: HERMES_HOME=$HOME_DIR hermes auth add openai-codex   (then re-run)" >&2
    exit 2; }
  echo "standup: reusing backend login from $REUSE_AUTH_FROM"
  cp "$REUSE_AUTH_FROM/auth.json" "$HOME_DIR/auth.json"
  chmod 600 "$HOME_DIR/auth.json"
fi

# ── 2. config.yaml: MINIMAL zephyr pattern (no mcp OAuth block) ────────────────
cat > "$HOME_DIR/config.yaml" <<'YAML'
model:
  default: gpt-5.5
  provider: openai-codex          # rides the shared Codex login (no API key)

plugins:
  enabled:
    - ax                          # aX gateway adapter (ax-presence plugins/platforms/ax)
    - ax-platform

approvals:
  mode: smart                     # aux-LLM auto-approves routine, prompts only dangerous
  timeout: 60
  cron_mode: deny                 # cron never auto-runs a dangerous command

# NOTE: intentionally NO per-agent `mcp_servers: ax-paxai-<handle>`. That entry
# forces an interactive browser OAuth that blocks unattended gateway startup in
# headless. The `ax` plugin already gives aX presence + messaging via the
# device-code token (AX_TOKEN_FILE). Add an MCP server only as a deliberate,
# one-time interactive step if you want the richer per-agent MCP toolset.
mcp_servers: {}
YAML
echo "standup: wrote minimal config.yaml"

# ── 3. plugins/ax symlink ──────────────────────────────────────────────────────
mkdir -p "$HOME_DIR/plugins"
ln -sfn "$AX_PRESENCE_DIR/plugins/platforms/ax" "$HOME_DIR/plugins/ax"

# ── 4. skills backfill (fresh HERMES_HOME has an EMPTY store) ───────────────────
echo "standup: backfilling official skills…"
HERMES_HOME="$HOME_DIR" hermes skills repair-official hermes-agent --restore --yes >/dev/null 2>&1 || true

# ── 5. smoke-test the brain ────────────────────────────────────────────────────
echo "standup: smoke-testing the brain…"
HERMES_HOME="$HOME_DIR" hermes -c -z "Reply with exactly one word: ready" 2>&1 | tail -1

# ── 6. aX identity (Plane 1): mint the device-code token if missing ────────────
if [ ! -f "$TOKEN_FILE" ]; then
  echo "standup: no aX token yet — minting via device-code (approve the URL below)."
  echo "  (echo_agent mints the token, then we stop it and run the gateway.)"
  ( cd "$HOME_DIR" && AX_AGENT_HANDLE="$HANDLE" AX_SPACE_ID="$SPACE_ID" \
      AX_TOKEN_FILE="$TOKEN_FILE" PYTHONUNBUFFERED=1 \
      python3 "$AX_PRESENCE_DIR/examples/echo-agent/echo_agent.py" ) &
  MINT_PID=$!
  echo "standup: waiting for token file to appear (approve the URL)…"
  for _ in $(seq 1 300); do [ -f "$TOKEN_FILE" ] && break; sleep 2; done
  kill "$MINT_PID" 2>/dev/null || true
  [ -f "$TOKEN_FILE" ] || { echo "standup: token never appeared — aborting." >&2; exit 3; }
  echo "standup: token minted -> $TOKEN_FILE"
else
  echo "standup: reusing existing aX token $TOKEN_FILE"
fi

# ── 7. run script (supervised gateway, mirrors daimon/zephyr) ──────────────────
RUN="$HOME_DIR/run-$HANDLE-gateway.sh"
{
  echo '#!/usr/bin/env bash'
  echo "# @$HANDLE as a Hermes GATEWAY with the aX platform adapter (durable path)."
  echo "# tmux:  tmux new -d -s $HANDLE-gw 'bash $RUN > $HOME_DIR/$HANDLE-gw.log 2>&1'"
  echo 'export PATH="$HOME/.local/bin:$PATH"'
  echo "export HERMES_HOME=$HOME_DIR"
  echo "export AX_AGENT_HANDLE=$HANDLE"
  if [ -n "${AX_AGENT_ID:-}" ]; then
    echo "export AX_AGENT_ID=$AX_AGENT_ID"
  else
    echo "# export AX_AGENT_ID=<uuid>   # TODO pin after first connect (avoids resolve race)"
  fi
  echo "export AX_SPACE_ID=$SPACE_ID"
  echo "export AX_TOKEN_FILE=\"$TOKEN_FILE\""
  echo "export AX_PRESENCE_DIR=$AX_PRESENCE_DIR"
  echo 'export AX_HOME_SPACE="$AX_SPACE_ID"'
  echo "export AX_SPONSOR=$SPONSOR"
  echo 'export AX_ALLOW_ALL_USERS=true'
  echo 'export PYTHONUNBUFFERED=1'
  echo 'cd "$HERMES_HOME" || exit 1'
  echo 'while true; do'
  echo '  echo "[supervisor] starting '"$HANDLE"' gateway at $(date -u +%FT%TZ)"'
  echo '  hermes gateway run'
  echo '  echo "[supervisor] '"$HANDLE"' gateway exited (code $?) at $(date -u +%FT%TZ); restart in 5s"'
  echo '  sleep 5'
  echo 'done'
} > "$RUN"
chmod 700 "$RUN"
echo "standup: wrote $RUN"

# ── 8. migrate off any old echo path + relaunch the gateway (idempotent) ───────
# SAFETY: only kill a bare `<handle>` tmux session when there is an echo
# run-<handle>.sh to migrate. A bare `<handle>` session may be an unrelated
# interactive/attached terminal (e.g. tmux 'peach' = a human's session) — never
# kill that blindly.
if [ -f "$HOME_DIR/run-$HANDLE.sh" ]; then
  tmux kill-session -t "$HANDLE" 2>/dev/null && echo "standup: stopped old echo tmux '$HANDLE'" || true
  mv "$HOME_DIR/run-$HANDLE.sh" "$HOME_DIR/run-$HANDLE.sh.echo-DEPRECATED"
  echo "standup: deprecated old echo run-$HANDLE.sh"
fi
tmux kill-session -t "$HANDLE-gw" 2>/dev/null && echo "standup: stopped existing '$HANDLE-gw'" || true
sleep 2
tmux new -d -s "$HANDLE-gw" "bash $RUN > $HOME_DIR/$HANDLE-gw.log 2>&1"
echo "standup: launched $HANDLE-gw"

# ── 9. verify ──────────────────────────────────────────────────────────────────
sleep 8
if pgrep -f "hermes gateway run" >/dev/null && tmux has-session -t "$HANDLE-gw" 2>/dev/null; then
  echo "standup: ✓ @$HANDLE gateway process is up under tmux '$HANDLE-gw'."
  echo "standup:   verify presence: aX agents list -> @$HANDLE sse_connected=true"
  echo "standup:   log: tail -f $HOME_DIR/$HANDLE-gw.log"
else
  echo "standup: ⚠ gateway not detected — check $HOME_DIR/$HANDLE-gw.log" >&2
  tail -20 "$HOME_DIR/$HANDLE-gw.log" 2>/dev/null || true
  exit 4
fi
