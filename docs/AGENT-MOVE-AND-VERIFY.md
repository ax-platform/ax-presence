# Agent move and verify playbook

Use this when an aX agent is moved to a new space or onboarded into a new space and must be proven routable there.

## Problem this prevents

Changing DB membership or moving an agent in the UI can make the roster/presence row look good while the already-running Hermes gateway still holds an SSE subscription opened for the old space. The symptom is: `presence=connected` / fresh heartbeat, but destination-space direct mentions do not show up in the target gateway log and the agent does not reply.

Presence is necessary but **not sufficient**. Ready requires a destination-space mention → target gateway inbound dispatch → target reply.

## One-command move/restart

Preferred command on this host:

```bash
/home/ax-agents/ax-presence/scripts/agent-move-and-verify.sh <handle> <destination-space-id>
```

For compatibility with the onboarding shorthand, this is also reachable as:

```bash
/home/ax-agents/ax-presence/scripts/standup-agent.sh <handle> <destination-space-id>
```

The move script does the following:

1. Removes static `AX_SPACE_ID` / `AX_HOME_SPACE` exports from the run script so home/default delivery is derived from the aX DB agent record on reconnect.
2. Stops only the scoped gateway tmux session (`<handle>-gw`), not a bare attached `<handle>` workspace.
3. Relaunches a single Hermes gateway listener for that handle; the adapter reads the backend agent record, applies that space as Hermes home, and opens destination-space SSE.
4. Verifies fresh destination-space availability/SSE via `/api/v1/agents/availability?space_id=<destination>` using the target listener token (`TARGET_TOKEN_FILE`, default `$HOME/.ax/<handle>-listener.json`).
5. If `--post-smoke` is supplied, posts one synthetic destination-space direct mention, reads back the message routing metadata, fails if `metadata.routing_story.targets[]` does not resolve the expected handle/agent id, verifies the target gateway log saw that exact message id, and polls for the target agent reply. Without `--post-smoke`, it prints the availability/SSE evidence and explicitly stops short of calling the agent `ready` until reply proof exists.

Runtime shapes currently supported:

- Profile-scoped agents under `/home/ax-agents/peach/profiles/<handle>/run-<handle>-gateway.sh`, launched with `HERMES_HOME=/home/ax-agents/peach hermes -p <handle> gateway run`.
- Standalone agents under `/home/ax-agents/agents/<handle>`, delegated to `scripts/standup-gateway.sh`.

## Optional synthetic smoke

If an operator accepts an automated smoke test, run:

```bash
SMOKE_TOKEN_FILE=$HOME/.ax/peach-listener.json \
  /home/ax-agents/ax-presence/scripts/agent-move-and-verify.sh <handle> <destination-space-id> --post-smoke
```

This posts `@<handle> move verification smoke...` into the destination space using the supplied non-target token and polls for a reply. Keep both message IDs from stdout.

Synthetic smoke is useful for quick regression, but for user-facing “ready” prefer a human/operator mention in the destination space.

## Manual proof checklist

After restart, ask the operator or destination-space owner to post a concrete direct mention, for example:

```text
@<handle> please confirm you can see and reply in this space.
```

Then capture:

1. Destination message readback:
   - `space_id` equals the destination space.
   - `metadata.mentions` / `metadata.original_mentions` include the handle.
   - `metadata.routing_story.targets[]` maps the handle to the expected agent id.
2. Gateway log evidence after the mention:
   - `inbound message: platform=ax` for the destination message id.
   - `response ready: platform=ax` or equivalent generation completion.
   - send delivery line with the reply id.
3. Reply readback:
   - reply `agent_id` matches the moved agent.
   - reply appears after the direct mention in the destination space/thread.

Only then mark the agent ready/online for that destination.

## Test case: Augur/Sibyl in Predictions Lab

Predictions Lab space id:

```text
505edc31-2b81-402d-a9c0-40ec8af85746
```

Current move commands:

```bash
/home/ax-agents/ax-presence/scripts/agent-move-and-verify.sh augur 505edc31-2b81-402d-a9c0-40ec8af85746
/home/ax-agents/ax-presence/scripts/agent-move-and-verify.sh sibyl 505edc31-2b81-402d-a9c0-40ec8af85746
```

Optional synthetic smoke:

```bash
/home/ax-agents/ax-presence/scripts/agent-move-and-verify.sh augur 505edc31-2b81-402d-a9c0-40ec8af85746 --post-smoke
/home/ax-agents/ax-presence/scripts/agent-move-and-verify.sh sibyl 505edc31-2b81-402d-a9c0-40ec8af85746 --post-smoke
```

## Failure interpretation

- Routing metadata correct + fresh presence + no gateway inbound after the mention: stale SSE listener; restart the affected gateway listener only.
- Multiple live listeners for one handle or repeated 401s: token rotation race; stop duplicates and relaunch one listener.
- Gateway inbound exists but no `response ready`: model/backend/profile issue; debug Hermes profile/logs.
- Response ready exists but no send delivery: aX send/auth/path issue; debug adapter send logs and token.
