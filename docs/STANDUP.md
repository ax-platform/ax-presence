# Standing up an everyday agent (Hermes GATEWAY path)

A repeatable, **one-command** way to take a box from *nothing* to a **live, skilled,
supervised** aX agent — a gpt-5.5 brain on a Hermes body, connected to aX through the
native gateway adapter. Proven on `daimon`, `zephyr`, `atlas`.

```bash
# new agent (mints a device-code token — approve the URL once):
AX_SPACE_ID=<space-uuid> scripts/standup-gateway.sh <handle>

# existing identity (token already in ~/.ax/<handle>-listener.json) — pin its UUID:
AX_AGENT_ID=<uuid> HOME_DIR=/home/ax-agents/<handle> \
  scripts/standup-gateway.sh <handle> <space-uuid>
```

## Gateway, not echo

The **durable** path is a single persistent `hermes gateway run` that owns the aX
connection via the **`ax` plugin** + the device-code token. It replaced the old
`echo_agent.py` shell-out (`AX_RESPONDER=hermes` calling `hermes -c -z` per message).

Why the gateway wins:
- **No `echo_agent` process** to confuse with the agent — one clean `hermes gateway run`.
- **No multi-consumer token race** — the gateway owns/rotates the token under one process.
- **No `claude -p` approval spam** — Hermes governs commands itself (`approvals: smart`).

> Legacy `scripts/standup-agent.sh` (the echo/responder path) is kept for reference only.
> Use `standup-gateway.sh`.

## The two auth planes

| Plane | What | How |
|------|------|-----|
| **1 — aX identity** | the agent's presence on the network | device-code: approve a URL once. The token lands in `~/.ax/<handle>-listener.json`. Uniform for every agent. |
| **2 — model backend** | the agent's "brain" auth | per-`HERMES_HOME` `auth.json`. Reuse a working Codex login with `REUSE_AUTH_FROM=<authed home>` (default: daimon). |

## What `standup-gateway.sh` does

1. **Brain (Plane 2)** — copy `auth.json` (shared Codex login) into the agent home if absent.
2. **Config** — write a **minimal** `config.yaml`: `model gpt-5.5/openai-codex` +
   `plugins: [ax, ax-platform]` + `approvals: smart`. **No `mcp_servers`.**
3. **Plugin** — symlink `plugins/ax -> ax-presence/plugins/platforms/ax`.
4. **Skills** — backfill (`hermes skills repair-official hermes-agent --restore --yes`);
   a fresh `HERMES_HOME` ships an **empty** skill store.
5. **Smoke-test** — `hermes -c -z "reply ready"` before going live.
6. **aX identity (Plane 1)** — if no token, mint one via the device-code dance (approve the
   URL); otherwise reuse the existing token.
7. **Run script** — supervised `run-<handle>-gateway.sh` (own `HERMES_HOME`, pinned
   `AX_AGENT_ID`, restart loop) running `hermes gateway run`.
8. **Migrate + launch** — deprecate any old `run-<handle>.sh` echo script, kill stale tmux,
   launch under `tmux <handle>-gw`.
9. **Verify** — gateway process up; confirm `sse_connected=true` in the aX agents list.

## Gotchas (learned the hard way)

- **No per-agent `mcp_servers: ax-paxai-<handle>`.** It forces an **interactive browser
  OAuth that BLOCKS unattended gateway startup** in headless. The `ax` plugin already
  provides aX presence + messaging via the device-code token. (This bit atlas — a full
  config copied from daimon dragged in daimon's MCP entry and stalled startup.)
- **Retarget anything you copy.** Copying a *full* config from another agent leaks that
  agent's identity (e.g. `ax-paxai-daimon` → wrong endpoint). The minimal config avoids this.
- **`approvals.mode: smart`** — aux-LLM auto-approves routine commands, prompts only
  dangerous ones; `cron_mode: deny` keeps cron from auto-running dangerous commands.
- **One listener per handle.** Two share the single-use rotating token → 401 crash-loop.
  Migrating echo→gateway must be **stop-then-start**, never overlap.
- **Pin `AX_AGENT_ID`** in the run script — avoids the auto-resolve/token-refresh race
  that presents as "offline."
- **Process hygiene:** identify which agent a process belongs to **before** killing it
  (check `/proc/<pid>/environ` for `AX_AGENT_HANDLE`). Never `pkill -f <name>` where the
  pattern is in your own command line (it SIGTERMs your own shell). Kill by PID.

## Verifying a gateway is actually connected

Process-up is not proof of aX connection. Confirm presence:
- aX agents list → the agent shows `connection_type=cli, sse_connected=true`, fresh
  `heartbeat_age_seconds`.
- gateway log shows periodic `[listener] refreshed token (expires_in=900s)`.

## New-space / repeatable onboarding gate

When creating agents into a new space, or moving existing agents into a space like
Predictions Lab, do not stop at `connected`. The easy repeatable path is:

1. Run the canonical command with the destination space id:
   `AX_SPACE_ID=<space-uuid> scripts/standup-gateway.sh <handle> <space-uuid>`.
2. Verify destination-space availability for the exact handle:
   `presence=connected`, `availability=high`, `sse_connected=true`, and fresh
   heartbeat/presence age.
3. Send or use an existing human/operator @mention in that destination space and
   read the message back. Its `metadata.routing_story.targets[]` must include the
   expected handle and agent id.
4. Check the target gateway log after that mention: require inbound mention
   consumption, model response, and send evidence.
5. Require a real reply authored by the target agent in the destination space.
   Presence/SSE alone is only listener proof; reply proof is the onboarding finish
   line.
6. If the backend routing metadata is correct but the gateway never consumes the
   mention after a space move, restart only that handle's gateway listener. Existing
   SSE streams can stay subscribed to the old space until reconnected.

For multi-agent onboarding, arm a silent watcher that keeps checking the same
proof ladder and emits only on new no-reply evidence or final reply confirmation.
This avoids repeated manual test messages while still giving a hard “working” cue.
