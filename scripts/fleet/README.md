# Fleet single-pane tools

One place to **run, confirm, detect, and heal** a fleet of aX Hermes gateway agents
instead of babysitting N per-agent tmux windows. Built/validated against the live
fleet on the shared box.

| tool | what it does |
|---|---|
| `ax-peach` | run aX MCP calls **as an agent** via mcporter, self-refreshing the agent's token (refresh_token grant) from its `~/.ax/<handle>-listener.json`. Thin wrapper so scripts can `whoami` / `agents` / `messages` as the agent without re-auth. |
| `fleet-status` | passive single pane: fuses **platform** (aX `agents` API: sse_connected, control/kill-switch, **space**), **local** (`/proc` + `ss`), and **receipt** (last inbound in each gateway's log). **Groups agents by space** and flags `SUSPECT-DEAF`. |
| `fleet-probe` | active round-trip: send each agent an `@mention`, verify **receipt in its log** (= SSE reader alive). Receipt is authoritative; replies can be loop-suppressed agent→agent. |
| `fleet-doctor` | detect → diagnose **why** → heal. Verdicts: `OK / QUIET / DEAF / DOWN / DISABLED / NO_HOME / TOKEN / MOVED`. Shows each agent's **space**. `--probe` confirms via round-trip; `--bounce`/`--apply` restarts `DEAF`/`MOVED` gateways (dry-run unless `--apply`); `--alert` posts a summary to aX. |
| `fleet-loop` | the single pane as ONE long-running supervisor process (systemd `--user` isn't available on the box): each cycle runs `fleet-doctor`. `--heal` auto-bounces. |

## Key truths these encode
- **"connected" ≠ "working."** Platform `sse_connected=True` and a live process both
  lie about a stalled SSE reader; `last_active_at` is stale; tmux thinks a deaf/crashed
  agent is healthy. Only **receipt of an inbound** (round-trip probe, or the agent's own
  recent inbound) tells the truth.
- **Space-aware, uniform verdict.** Agents are **grouped by space** so a cross-space
  agent is never mistaken for broken. But the deaf rule is the **same in every space**:
  `up + sse_connected + no recent receipt → DEAF → bounce`. The active probe only works
  *within the identity's own space* (the platform refuses cross-space sends to prevent
  leakage), so cross-space agents are confirmed by their **own-log receipt** at the same
  threshold — and bounced the same way when genuinely deaf.
- **Receipt = newest of rotated+current log**, matching either `aX inbound dispatch` or
  `inbound message: platform=ax`, and resolving `-p <profile>` agents' logs under
  `profiles/<h>/logs/`. (Earlier versions grepped the rotated log last and read a stale
  timestamp, and missed profile-agent logs → false `DEAF`.)
- **`MOVED`**: an agent whose DB `space_id` changed but whose live connection is still
  bound to the old space — detected by comparing DB placement vs the `connected as @h
  on home space X` log line; healed by a bounce (reconnect re-derives the new space).

## Configure for your own box / identity (env-overridable)
The tools default to this box but read these env vars so another operator can point them
at their own agents without editing code:

| env | default | meaning |
|---|---|---|
| `AX_FLEET_HOME` | `/home/ax-agents` | base dir holding agent homes |
| `AX_FLEET_IDENTITY` | `peach` | the probing agent's handle; anchors "my home space" |
| `AX_FLEET_MCP` | `$AX_FLEET_HOME/$AX_FLEET_IDENTITY/bin/ax-peach` | command that runs aX MCP calls **as the fleet identity** |
| `AX_FLEET_BOUNCE_LOG` | `$AX_FLEET_HOME/$AX_FLEET_IDENTITY/logs/fleet-bounces.log` | where bounces are logged |

`ax-peach` is the **template** for the as-agent MCP wrapper — copy it for your identity
(it self-refreshes that agent's `~/.ax/<handle>-listener.json` token) and set
`AX_FLEET_MCP` to it. Example:

```bash
AX_FLEET_IDENTITY=myagent AX_FLEET_MCP=~/bin/ax-myagent fleet-status
```

Still box-shaped: gateway discovery assumes local `hermes gateway run` processes with
`AX_AGENT_HANDLE`/`HERMES_HOME` in their env, and bounce assumes a supervisor loop that
relaunches on `SIGTERM`. Those match the `standup-gateway.sh` layout in this repo.
