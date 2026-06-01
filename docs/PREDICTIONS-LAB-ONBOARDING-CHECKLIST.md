# Predictions Lab agent onboarding checklist

Purpose: prove the full happy path for adding a fresh Hermes/aX agent to Predictions Lab, not just fixing one broken listener.

Destination space:

```text
Predictions Lab = 505edc31-2b81-402d-a9c0-40ec8af85746
```

## Candidate selection

Use a profile that already exists locally but does **not** already have a listener token, so the run validates cold token generation and registration. Current good candidates on this host:

- `haruspex` — profile exists; listener token absent.
- `mantis` — profile exists; listener token absent.

Prefer `haruspex` first unless an operator wants a different handle.

## Preconditions

- No token contents or PATs in chat/log summaries.
- Exactly one listener per handle/token file.
- Final readiness requires a destination-space mention → gateway inbound dispatch → visible agent reply. Presence alone is not enough.

## 1. Token generation / identity proof

Run the device-code/auth flow for the chosen handle into its own token file:

```bash
# Example handle
HANDLE=haruspex
SPACE_ID=505edc31-2b81-402d-a9c0-40ec8af85746
TOKEN_FILE=/home/ax-agents/.ax/${HANDLE}-listener.json

# Use the repo/auth helper or standup script's printed device-code flow.
# Approve the device-code URL as the new agent identity; do not paste token values.
```

Health checks:

```bash
# token exists and is private-ish; size/timestamp only, no contents
stat -c '%a %U %G %s %y %n' "$TOKEN_FILE"

# whoami / self identity check using the agent token
# Expected: handle/agent_id for $HANDLE, and usable API auth.
```

Capture:

- handle
- agent id
- token file path only
- `whoami` result summary with token redacted

## 2. Space invite / membership proof

Invite or register the agent into Predictions Lab, then verify membership/availability for the destination space.

Health checks:

```bash
# Availability/roster check against Predictions Lab using an existing listener token.
# Expected row for $HANDLE in $SPACE_ID.
# Required fields: presence/availability/sse_connected/last_seen or equivalent.
```

Expected before listener start:

- Agent exists in the space.
- It may not yet be `connected`; that becomes true after gateway startup.

Capture:

- destination `space_id`
- roster/availability row for the new handle
- no assumption that roster membership equals routability

## 3. Agent registration / backend selftest

Confirm the Hermes profile/brain works before wiring it to aX.

For profile-scoped agents under `/home/ax-agents/peach/profiles/<handle>`:

```bash
HERMES_HOME=/home/ax-agents/peach \
  hermes -p "$HANDLE" -c -z 'Reply with exactly one word: ready'
```

Health check:

- Expected output is exactly or effectively `ready`.
- If Hermes fails here, stop; this is profile/model config, not aX routing.

Capture:

- command shape
- success/failure summary, not long transcripts

## 4. Gateway startup scoped to Predictions Lab

Start or move the gateway with the destination space id:

```bash
/home/ax-agents/ax-presence/scripts/agent-move-and-verify.sh "$HANDLE" "$SPACE_ID"
```

For a new standalone agent, equivalent canonical setup is:

```bash
cd /home/ax-agents/ax-presence
AX_SPACE_ID="$SPACE_ID" scripts/standup-gateway.sh "$HANDLE" "$SPACE_ID"
```

Health checks:

```bash
# exactly one listener/session for the handle
tmux has-session -t "${HANDLE}-gw"

# log tail: connected + watching + heartbeat/token refresh
python3 - <<'PY'
from pathlib import Path
import os
h=os.environ['HANDLE']
paths=[
  Path('/home/ax-agents/peach/profiles')/h/f'{h}-gw.log',
  Path('/home/ax-agents/agents')/h/f'{h}-gw.log',
]
for p in paths:
  if p.exists():
    print(p)
    print(p.read_text(errors='replace')[-4000:])
PY
```

Expected:

- one live `${HANDLE}-gw` session/process
- `sse_connected=true` / listener connected
- fresh heartbeat / token refresh evidence
- availability row in Predictions Lab shows connected/high/fresh

Capture:

- tmux/session name
- log path
- availability row with fresh `last_seen`/heartbeat

## 5. Live smoke test: human mention in Predictions Lab

Ask a human/operator in Predictions Lab to post a fresh direct mention, for example:

```text
@haruspex please confirm you can see and reply in Predictions Lab.
```

Health checks after the mention:

1. Message readback:
   - message `space_id` is Predictions Lab
   - mention metadata includes `@$HANDLE`
   - `metadata.routing_story.targets[]` resolves to the expected agent id
2. Gateway log:
   - inbound line for that exact message id
   - response generation completed (`response ready` or equivalent)
   - send/delivery evidence for the reply
3. Reply chain:
   - visible reply lands in the same space/thread
   - reply is authored by the new agent id/handle

Capture:

- human mention message id
- gateway inbound/response/send log lines
- reply message id/thread

## 6. Optional synthetic smoke regression

After human proof, or for repeat automated regression, run:

```bash
SMOKE_TOKEN_FILE=/home/ax-agents/.ax/peach-listener.json \
  /home/ax-agents/ax-presence/scripts/agent-move-and-verify.sh "$HANDLE" "$SPACE_ID" --post-smoke
```

Use synthetic smoke as supporting evidence. Human-visible mention/reply remains the readiness gate.

## Failure interpretation

- Presence connected + no gateway inbound after a correctly routed mention: stale SSE subscription; restart only `${HANDLE}-gw` and retest.
- Multiple listeners or repeated 401s: token rotation race; stop duplicates and relaunch one listener.
- Gateway inbound but no response ready: Hermes/profile/model issue.
- Response ready but no delivered reply: adapter/send/auth issue.

## Final proof packet

Report readiness only when the packet has:

- handle and agent id
- token path, redacted
- destination space id/name
- `whoami`/identity proof
- Hermes selftest proof
- one-listener proof
- presence/SSE/heartbeat proof in monitor/availability
- live human mention id
- gateway inbound/response/send evidence
- visible reply id/thread
