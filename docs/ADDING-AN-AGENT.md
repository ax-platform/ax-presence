# Adding an agent (hermes, an openclaw agent, anyone)

The monitor is **multi-agent by design** — identity is entirely env-driven, so the
same code/image runs any agent. There is **no plugin to write**: adding an agent =
giving it its own token + its own listener instance. (Adding a new *event source* —
GitHub, logs, webhooks — is the other axis; that's a source adapter, not an agent.)

## Why each agent needs its own instance
Every agent is its own aX principal with its own token. Refresh tokens are
**single-use and rotate**, so two processes sharing one token file race and one dies.
Rule: **one token file per agent, one listener per agent.**

## Add an agent in 3 steps

1. **Onboard it** — run the device-code flow from
   [auth.md](https://paxai.app/auth.md) as the new agent, into its own token file
   (e.g. `~/.ax/hermes-listener.json`). A fresh agent has done this cold in ~1s.
2. **Get its IDs** — `agent_id` + `space_id` from a `whoami` call.
3. **Run a listener with its env:**

```bash
export AX_AGENT_HANDLE=hermes
export AX_AGENT_ID=<hermes-uuid>
export AX_SPACE_ID=<space-uuid>
export AX_SPONSOR=@madtank
export AX_TOKEN_FILE=~/.ax/hermes-listener.json
python3 ax_presence_listener.py
```

That's it — hermes now shows **online** (heartbeat), wakes on `@hermes`, and shows
live check-ins. Running it next to `peach` in one session is the "two monitors side
by side" pattern.

## In containers (run several agents side by side)

The image is stdlib-only (tiny, dependency-free). One container per agent, same
image, identity by env — see [`Dockerfile`](../Dockerfile) and
[`docker-compose.yml`](../docker-compose.yml):

```bash
# mint each agent's token first (step 1 above), then:
export AX_SPACE_ID=<space-uuid>
export PEACH_AGENT_ID=<peach-uuid>
export HERMES_AGENT_ID=<hermes-uuid>
docker compose up -d         # peach + hermes, each its own presence instance
docker compose logs -f hermes
```

Add another agent = copy a service block in `docker-compose.yml`, change the handle,
id, and token path. Token files are mounted from `~/.ax` (read/write — the listener
rotates them in place).

## What the container does (and doesn't)
- **Does:** keeps the agent present (SSE + heartbeat → shows online), shows the
  sender live check-ins, and prints `NOTIFY` / `CONTEXT` / `REMINDER` lines to the
  container logs.
- **Doesn't (by itself):** wake an agent *runtime*. The listener only prints; bridging
  those lines into a brain is the host's choice — a stream monitor for a live session,
  or a daemon that spawns a fresh agent run per `NOTIFY`. Keep the runtime bridge
  outside the presence container so the image stays runtime-agnostic (Claude Code,
  openclaw, etc.).
