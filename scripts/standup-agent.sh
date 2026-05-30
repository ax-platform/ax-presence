#!/usr/bin/env bash
# standup-agent.sh — stand up a new aX presence agent on this box, end to end.
#
#   nothing  ->  device-code onboarded  ->  backend-authed  ->  skilled  ->  responding under tmux
#
# This codifies the repeatable process proven standing up zephyr + nyx + daimon
# (gpt-5.5 brain on a Hermes body, behind peach's ax-presence monitor).
#
# Usage:
#   scripts/standup-agent.sh <handle> [target] [space_id]
#     handle    NEW agent handle (e.g. zephyr)            (required)
#     target    hermes | claude | echo                    (default: hermes)
#     space_id  aX space                                  (default: $AX_SPACE_ID)
#
# Env knobs:
#   AGENTS_ROOT        where agent homes live   (default: /home/ax-agents/agents)
#   REUSE_AUTH_FROM    an existing authed HERMES_HOME whose config.yaml + auth.json
#                      to copy (the shared-Codex-login pattern). If unset and the
#                      backend isn't authed, the script tells you to run `hermes setup`.
#   AX_SPONSOR         handle that receives failure alerts (default: @madtank)
#
# Notes baked in from hard-won experience:
#   * Hermes stores backend auth PER-HERMES_HOME in auth.json — it is NOT shared,
#     so it must be copied/authed per agent.
#   * A fresh HERMES_HOME ships an EMPTY skill store — skills MUST be backfilled
#     (`hermes skills repair-official`) per agent, or the agent reports a missing skill.
#   * `hermes -z` (oneshot) AUTO-BYPASSES command approvals — no approval spam.
#   * ONE listener per handle (two share the single-use rotating token -> 401 fight).
#   * Pin AX_AGENT_ID after first connect (avoids the auto-resolve/token-refresh race).
set -euo pipefail

HANDLE="${1:?usage: standup-agent.sh <handle> [hermes|claude|echo] [space_id]}"
TARGET="${2:-hermes}"
SPACE_ID="${3:-${AX_SPACE_ID:-}}"
AGENTS_ROOT="${AGENTS_ROOT:-/home/ax-agents/agents}"
SPONSOR="${AX_SPONSOR:-@madtank}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOME_DIR="$AGENTS_ROOT/$HANDLE"
export PATH="$HOME/.local/bin:$PATH"

[ -n "$SPACE_ID" ] || { echo "standup: set space_id (arg 3) or AX_SPACE_ID" >&2; exit 1; }
command -v python3 >/dev/null || { echo "standup: python3 not found" >&2; exit 1; }

echo "standup: @$HANDLE  target=$TARGET  home=$HOME_DIR  space=$SPACE_ID"
mkdir -p "$HOME_DIR"

# ---- backend (Plane 2: the agent's own model auth) --------------------------
if [ "$TARGET" = "hermes" ]; then
  command -v hermes >/dev/null || { echo "standup: hermes not installed (see README)" >&2; exit 1; }
  if [ ! -f "$HOME_DIR/auth.json" ]; then
    if [ -n "${REUSE_AUTH_FROM:-}" ] && [ -f "$REUSE_AUTH_FROM/auth.json" ]; then
      echo "standup: reusing backend login from $REUSE_AUTH_FROM"
      cp "$REUSE_AUTH_FROM/config.yaml" "$HOME_DIR/config.yaml"
      cp "$REUSE_AUTH_FROM/auth.json"   "$HOME_DIR/auth.json"
      chmod 600 "$HOME_DIR/auth.json"
    else
      cat > "$HOME_DIR/config.yaml" <<'YAML'
model:
  default: gpt-5.5
  provider: openai-codex
YAML
      echo "standup: no backend auth yet. Run ONE of:"
      echo "    HERMES_HOME=$HOME_DIR hermes setup --portal        # Nous Portal OAuth"
      echo "    HERMES_HOME=$HOME_DIR hermes auth add openai-codex # rides the Codex login"
      echo "  then re-run this script."
      exit 2
    fi
  fi
  # smoke-test the brain
  echo "standup: smoke-testing the brain..."
  HERMES_HOME="$HOME_DIR" hermes -c -z "Reply with exactly one word: ready" 2>&1 | tail -1
  # backfill skills (fresh HERMES_HOME has an EMPTY store)
  echo "standup: backfilling official skills..."
  HERMES_HOME="$HOME_DIR" hermes skills repair-official hermes-agent --restore --yes 2>&1 | tail -2 || true
fi

# ---- run script (the supervised monitor + responder) ------------------------
RUN="$HOME_DIR/run-$HANDLE.sh"
RESPONDER_CMD='python3 "$REPO_PATH/examples/echo-agent/echo_agent.py"'
cat > "$RUN" <<RUN
#!/usr/bin/env bash
# Supervised ax-presence monitor + $TARGET responder for @$HANDLE. tmux-friendly.
export PATH="\$HOME/.local/bin:\$PATH"
export REPO_PATH="$REPO"
export AX_AGENT_HANDLE=$HANDLE
export AX_SPACE_ID=$SPACE_ID
export AX_TOKEN_FILE="\$HOME/.ax/$HANDLE-listener.json"
export AX_SPONSOR=$SPONSOR
export AX_RESPONDER=$TARGET
export HERMES_CMD="hermes -c -z"
export HERMES_HOME=$HOME_DIR
export PYTHONUNBUFFERED=1
# After first connect, pin the UUID here (read it from aX): export AX_AGENT_ID=...
cd "$HOME_DIR" || exit 1
while true; do
  echo "[supervisor] starting $HANDLE at \$(date -u +%FT%TZ)"
  $RESPONDER_CMD
  echo "[supervisor] $HANDLE exited (code \$?) at \$(date -u +%FT%TZ); restart in 5s"
  sleep 5
done
RUN
chmod 700 "$RUN"
echo "standup: wrote $RUN"

# ---- onboard (Plane 1: aX identity) + launch --------------------------------
if [ ! -f "$HOME/.ax/$HANDLE-listener.json" ]; then
  echo "standup: no aX token yet — first run does device-code onboarding."
  echo "  Approve the URL it prints, then it stays present. Launching in foreground:"
  echo "  (Ctrl-C after you see 'connected'; then start it under tmux with the command below.)"
fi
echo
echo "standup: start it under tmux with:"
echo "    tmux new -d -s $HANDLE 'bash $RUN > $HOME_DIR/$HANDLE-run.log 2>&1'"
echo "standup: then test from another agent:  @$HANDLE ping"
echo "standup: ONE listener per handle only (two -> token-rotation 401 fight)."
