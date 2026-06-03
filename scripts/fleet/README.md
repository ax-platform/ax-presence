# Fleet single-pane tools

One place to **run, confirm, detect, and heal** a fleet of aX Hermes gateway agents
instead of babysitting N per-agent tmux windows. Built/validated against the live
fleet on the shared box.

| tool | what it does |
|---|---|
| `ax-peach` | run aX MCP calls **as an agent** via mcporter, self-refreshing the agent's token (refresh_token grant) from its `~/.ax/<handle>-listener.json`. Thin wrapper so scripts can `whoami` / `agents` / `messages` as the agent without re-auth. |
| `fleet-status` | passive single pane: fuses **platform** (aX `agents` API: sse_connected, control/kill-switch), **local** (`/proc` + `ss`), and **receipt** (last inbound in each gateway's `logs/agent.log`). Flags `SUSPECT-DEAF`. |
| `fleet-probe` | active round-trip: send each agent an `@mention`, verify **receipt in its log** (= SSE reader alive). Receipt is authoritative; replies can be loop-suppressed agent→agent. |
| `fleet-doctor` | detect → diagnose **why** → heal. Verdicts: `OK / QUIET / DEAF / DOWN / DISABLED / NO_HOME / TOKEN / MOVED`. `--probe` confirms via round-trip; `--bounce`/`--apply` restarts `DEAF`/`MOVED` gateways (dry-run unless `--apply`); `--alert` posts a summary to aX. |
| `fleet-loop` | the single pane as ONE long-running supervisor process (systemd `--user` isn't available on the box): each cycle runs `fleet-doctor`. `--heal` auto-bounces. |

## Key truths these encode
- **"connected" ≠ "working."** Platform `sse_connected=True` and a live process both
  lie about a stalled SSE reader; `last_active_at` is stale; tmux thinks a deaf/crashed
  agent is healthy. Only an **active round-trip (receipt)** tells the truth.
- **`MOVED`**: an agent whose DB `space_id` changed but whose live connection is still
  bound to the old space — detected by comparing DB placement vs the `connected as @h
  on home space X` log line; healed by a bounce (reconnect re-derives the new space).

## Caveat (genericize before reuse elsewhere)
These currently hard-code box paths (`/home/ax-agents/...`, per-agent log locations,
`ax-peach` for the MCP identity). They're committed here to **preserve the working
single pane**; a follow-up should parameterize paths/handles (a small fleet registry)
so any operator can point them at their own agents.
