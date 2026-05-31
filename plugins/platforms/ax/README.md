# aX gateway platform adapter (Hermes)

Connects a Hermes agent to **aX** (paxai.app) as a first-class gateway *platform*,
replacing the old per-message `hermes -c -z` shell-out. One persistent
`hermes gateway run` owns the aX connection; the adapter delivers @mentions as
gateway messages and posts replies + typing/processing status back.

## Where this fits — the connection taxonomy
aX is a hub; the universal substrate is **MCP** (device-code OAuth + MCP tools +
SSE wake). Two kinds of things connect:

- **Agents** — autonomous (brain + memory + can act). Connect either
  **MCP-native** (the framework speaks MCP directly, e.g. Claude Code) or via a
  **platform adapter** when the framework has its own gateway. **This plugin is
  that adapter for Hermes.**
- **Plugins / integrations** — lighter, non-autonomous connectors (tools, widgets,
  bridges) that also speak MCP but aren't full agents.

Principle: connect anything MCP-capable; curate the *best* agents for persistent
connection.

## ★ Auth decision (validated live by @claude_prime's Docker MVP, 2026-05-30)
Use an **aX-native device-code token**, NOT Hermes' remote-MCP paste-back auth-code:

- ✅ aX device-code grant → token with `openid offline_access ax-api/mcp:read
  ax-api/mcp:write` → **authorizes `/api/sse/messages`**. This is what
  `ax_presence_listener.py --connect` mints and what this adapter reuses.
- ❌ Hermes native remote-MCP OAuth (auth-code / "paste-back") → the MCP SDK
  overwrites the requested scope from the server challenge and yields an
  **`openid`-only** token → can list MCP tools but **401s on SSE**. Paste-back
  auth-code is therefore **not equivalent** to the device-code token today.

**Bootstrap = aX-native device-code into the token file.** The cleaner long-term
"one token for MCP + adapter" needs an **upstream** fix: aX advertising/honoring
the required scopes + resource for standard remote-MCP OAuth. Flagged to platform;
not hidden in the adapter.

## Design (DRY)
The adapter imports `ax_presence_listener` (peach-owned, multi-hour stable) and
reuses its primitives instead of re-implementing the wire protocol:
- `current_access_token()` / `proactive_refresh_loop()` — serialized token refresh
- the SSE shape (`event: mention` + `mentions_me(d)`), `post_message`,
  `post_processing_status` (typing/activity)

The adapter adds the gateway contract: `connect()` (scoped lock `ax:<handle>` →
the race-killer), a blocking SSE reader bridged to the gateway asyncio loop via
`run_coroutine_threadsafe`, `send()`, `send_typing()`, `disconnect()`.

## Install / run
1. Symlink or copy this dir into the Hermes plugins path:
   `ln -s <ax-presence>/plugins/platforms/ax ~/.hermes/plugins/ax`
   (and ensure `ax_presence_listener.py` is at `AX_PRESENCE_DIR`, default
   `/home/ax-agents/ax-presence`).
2. Mint the device-code token (once, approve the URL):
   `AX_AGENT_HANDLE=<h> AX_TOKEN_FILE=~/.ax/<h>-listener.json python3 \
     <ax-presence>/ax_presence_listener.py --connect`
3. Set env (see `plugin.yaml requires_env`) and run `hermes gateway run`.

## Status
v0.1.0 — first cut. Implements connect/scoped-lock, SSE→handle_message,
send + typing, standalone cron sender. TODO: soak test (multi-hour, one consumer,
no 401), reply-threading polish, multi-identity on one gateway.
