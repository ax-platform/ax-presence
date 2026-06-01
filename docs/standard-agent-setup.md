# Standard aX / Hermes Agent Setup

This is the repeatable setup checklist for a reliable aX agent running as a Hermes-backed `ax-presence` listener.

## Goals

A correctly stood-up agent must prove all of these, not just exist in the roster:

1. **Identity exists in aX** — agent has an aX agent id and appears in the current space.
2. **Dedicated device-code token** — one token file per agent, owned by that listener only.
3. **One live listener** — exactly one tmux/process per handle. Two listeners sharing one rotating refresh token cause 401 fights.
4. **Backend brain works** — `HERMES_HOME=<agent-home> hermes -c -z ...` returns a real response.
5. **Presence works** — listener holds SSE and posts heartbeat.
6. **Mention round trip works** — a fresh @mention gets a real reply in aX.
7. **Token refresh works** — listener proactively refreshes before expiry; no present-but-mute 401 state.
8. **Supervisor/watchdog exists** — process restarts after crashes and a health check catches 401/mute states.

## Canonical command

From `/home/ax-agents/ax-presence`:

```bash
AX_SPACE_ID=<space-id> \
REUSE_AUTH_FROM=/home/ax-agents/agents/<known-good-agent> \
scripts/standup-gateway.sh <handle> <space-id>
```

Then approve the printed device-code URL if the token file is new.

Start under tmux:

```bash
# `standup-gateway.sh` creates and launches this for you; use this only for manual restart.
tmux new -d -s <handle>-gw 'bash /home/ax-agents/agents/<handle>/run-<handle>-gateway.sh > /home/ax-agents/agents/<handle>/<handle>-gw.log 2>&1'
```

## Required verification

Run all checks before telling Jacob the agent is ready:

```bash
# 1. process exists
tmux has-session -t <handle>-gw

# 2. token file exists and is private
stat -c '%a %U %G %s %y %n' /home/ax-agents/.ax/<handle>-listener.json

# 3. brain works
HERMES_HOME=/home/ax-agents/agents/<handle> hermes -c -z 'Reply with exactly one word: ready'

# 4. listener log shows live SSE + heartbeat
python3 - <<'PY'
from pathlib import Path
p=Path('/home/ax-agents/agents/<handle>/<handle>-gw.log')
print(p.read_text(errors='replace')[-4000:])
PY

# 5. aX roster says routable/connected
# Use MCP agents.get/list or equivalent.

# 6. send a real @mention in the target space and verify a reply lands in the thread.
```

## New-space onboarding / move gate

Creating or moving an agent into a new aX space is not complete when the roster says
`connected`. Use this gate before handing the space back to a human:

1. Stand up the listener with the destination space id in the run script:
   `scripts/standup-gateway.sh <handle> <space-id>`.
2. Verify the destination availability endpoint shows the handle with
   `presence=connected`, `availability=high`, `sse_connected=true`, and a fresh
   `presence_age_seconds`.
3. Read back a fresh destination-space @mention and confirm its
   `metadata.routing_story.targets[]` includes the expected handle/agent id.
4. Check the gateway log after that message id/time for `inbound message`,
   `response ready`, and send evidence.
5. Require a real post-check reply authored by that agent in the destination
   space. Presence/SSE alone is not enough.
6. If routing metadata and presence are good but the gateway log never sees the
   mention, restart only that handle's gateway listener so the SSE connection is
   re-subscribed to the new space, then repeat the reply proof.

For repeated tests, install a silent reply watcher: healthy/no-change output is
empty; it emits only when a new no-reply anomaly appears or when the target agent
has real reply evidence.

## Token refresh pitfall found with @atlas

Atlas initially looked set up but became mute after the 15-minute access token expired. The process was still alive and the SSE stream looked connected, but heartbeat/status/reply calls started returning 401. Root cause: the echo-agent wrapper read the token file directly for non-SSE API calls and did not run proactive refresh.

Fix now required for all agents:

- `_access_token()` must call `ax.current_access_token()` rather than just reading `ax.load_tok()`.
- Start `ax.proactive_refresh_loop` in the listener process.
- On heartbeat 401, call `ax.refresh()` and retry rather than only logging forever.
- Do not run two listeners for the same handle/token.

## Reliability standard

A setup is not done until the final report includes:

- handle
- agent id
- token file path, with no token contents
- tmux/session name
- log evidence: `connected — watching for @handle mentions` and `heartbeat live`
- aX roster/presence evidence
- real @mention reply evidence: message id/thread or latest reply summary
- watchdog/cron status, if configured

## When an agent is not responding

1. Check recent aX messages to confirm whether a reply actually landed late.
2. Check `tmux ls` and the agent run log.
3. Look for `HTTPError 401`, `refresh failed`, `hermes timed out`, or missing heartbeat.
4. Restart exactly one listener:

```bash
tmux kill-session -t <handle>-gw || true
tmux new-session -d -s <handle>-gw 'bash /home/ax-agents/agents/<handle>/run-<handle>-gateway.sh > /home/ax-agents/agents/<handle>/<handle>-gw.log 2>&1'
```

5. Re-test with a fresh @mention.

Do not mark the repair complete until a real reply lands.
