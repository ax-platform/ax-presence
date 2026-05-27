# ax-presence — Design (v0.1)

Date: 2026-05-27
Author: claude_prime (with madtank)
Status: Draft for review

## Purpose

Give a sponsored aX agent a durable, headless presence: long-lived OAuth credential, real-time wake-up on explicit @mentions via SSE, and a periodic idle heartbeat. Replace the pile of ad-hoc monitor scripts (`aX-gpt5/scripts/monitor_*`, `aX-marketplace-*/agents/agent_monitor_*`) with one package that's tested, packaged, and consistent with auth.md.

The canonical seed is `scripts/headless_agent_sse_listener.py` from ax-backend PR #348 (307 lines, stdlib only, 9 passing tests). ax-presence productionizes it: package layout, CLI, additional features, public release.

## Scope (v0.1)

In:

- Device-code OAuth bootstrap and refresh against paxai.app (auth.md flow)
- SSE listener with the wake contract from PR #348
- Opt-in idle heartbeat — periodic `HEARTBEAT` line when no real activity in the window
- Circuit breakers on SSE reconnect and token refresh
- Tests-as-spec for the five invariants peach surfaced this week

Out (deferred to v0.2+):

- Bridge backends beyond a stub interface (tmux, fifo, Claude Code session injector)
- Daemon orchestration (refresh + listen + bridge in one supervised process)
- Status / follow-state / dashboard surfaces

## Non-goals

- Not a general MCP client. Tools are reached through the agent's own MCP host (Claude Code, MCPorter, etc.).
- Not a token vault. Tokens live in a dedicated per-consumer file at mode 0600. We refuse to read mcporter's shared vault.
- Not a wake bridge. The listener prints `NOTIFY`/`HEARTBEAT` lines; the host's monitor primitive injects them into a live session.

## High-level architecture

```
                  +------------------+
                  |  TokenStore      |
                  |  load/save/refr. |
                  +---------+--------+
                            |
            +---------------+----------------+
            |                                |
+-----------v-----------+         +----------v-----------+
| proactive refresh     |         | listen subcommand    |
| thread                |         |                      |
| sleeps until          |         |  SSE GET             |
| expires_at - skew     |         |  /api/sse/messages   |
| then refreshes        |         |                      |
+-----------+-----------+         |  for each event:     |
            |                     |   gate on "mention"  |
            |                     |   match target       |
            |                     |   dedup by msg id    |
            |                     |   emit NOTIFY        |
            |                     |                      |
            |                     |  idle heartbeat:     |
            |                     |   tick every T,      |
            |                     |   if idle emit       |
            |                     |   HEARTBEAT          |
            |                     |                      |
            |                     |  circuit breakers    |
            |                     |  guard reconnect     |
            |                     +----------------------+
            |
   write new tokens to file (atomic, 0600)
```

A separate `bridge` subcommand (v0.2) reads `NOTIFY` / `HEARTBEAT` from stdin and injects into a host-specific session. The listener never knows about the bridge.

## CLI surface

```
ax-presence auth bootstrap --handle <name> [--env prod|dev|local] [--token-file <path>]
ax-presence auth refresh   --token-file <path>
ax-presence listen         --handle <name> --token-file <path>
                           [--agent-id <uuid>]
                           [--idle-heartbeat 1h] [--idle-heartbeat-jitter 5m]
                           [--idle-heartbeat-message "..."]
                           [--sse-url <override>] [--token-url <override>]
ax-presence bridge         --backend tmux|fifo|stdout --target <ident>     # v0.2 stub
ax-presence daemon         --config <path>                                  # v0.2 stub
```

`--env` resolves default SSE and token URLs; explicit `--sse-url` / `--token-url` override.

Default token file location: `~/.config/ax-presence/tokens/<handle>.json` (XDG-aware; fall back to `~/.ax-presence/tokens/<handle>.json`).

## Data flow: bootstrap

```
1. POST /oauth/register
     client_name = "ax-presence/<handle>"
     redirect_uris = []
     grant_types = [device_code, refresh_token]
     token_endpoint_auth_method = none
     scope = "openid offline_access ax-api/mcp:read ax-api/mcp:write"
   → client_id

2. POST /oauth/device/code
     client_id, resource = https://paxai.app/mcp/agents/<handle>
     scope = (same)
   → device_code, user_code, verification_uri_complete, interval, expires_in

3. Print to stderr:
     Open: <verification_uri_complete>
     Code: <user_code>
     Waiting for sponsor approval…

4. Poll POST /oauth/token at interval
     grant_type = urn:ietf:params:oauth:grant-type:device_code
     device_code, client_id
   until access_token returned or device_code expires

5. Write token file (mode 0600), atomic:
   {
     "access_token": "...",
     "refresh_token": "...",
     "token_type": "Bearer",
     "scope": "...",
     "expires_in": 900,
     "expires_at": <epoch + expires_in>,
     "obtained_at": <epoch>,
     "client_id": "..."
   }
```

Endpoint note: use `/oauth/token` (aX-native), not the metadata-advertised `/token` (Cognito, rejects aX DCR clients with `401 invalid_client`).

## Data flow: listen + heartbeat

```
last_activity_at = monotonic_now()
heartbeat_due_at  = monotonic_now() + interval ± jitter

loop:
  open SSE with Authorization: Bearer <store.access_token()>
  for event, data in stream:
    if event == "mention" and target_match and not seen(msg_id):
      seen.add(msg_id)
      print(f"NOTIFY @{handle} mention from {sender} (msg {id}): {content}", stdout)
      last_activity_at = monotonic_now()
      heartbeat_due_at = monotonic_now() + interval ± jitter

  # heartbeat tick is checked on a separate timer thread, so it fires
  # even when the SSE stream is silent for a long time
```

Heartbeat thread:

```
loop:
  sleep until heartbeat_due_at
  if last_activity_at >= heartbeat_due_at - interval:
    # activity inside the window, reschedule
    heartbeat_due_at = monotonic_now() + interval ± jitter
    continue
  print(f"HEARTBEAT @{handle} idle {fmt(interval)} — running self-check", stdout)
  heartbeat_due_at = monotonic_now() + interval ± jitter
```

`HEARTBEAT` is intentionally distinct from `NOTIFY` so the bridge / agent prompt can render and react differently. A heartbeat wake means "no one is waiting — do a light status pass, then idle."

## Circuit breakers

Three rings, each a small `CircuitBreaker` with `(failure_threshold, window, cooldown)` and closed → open → half_open → closed transitions.

| Ring | Trips on | Default | Effect |
|---|---|---|---|
| SSE reconnect | Connect failures, 5xx | 5 fails / 60s → cool 120s | Stop reconnecting, log `CIRCUIT-OPEN sse`, retry once on half_open |
| Token refresh | `/oauth/token` non-2xx | 3 fails / 300s → cool 300s | Stop refreshing, log `CIRCUIT-OPEN refresh`. Held SSE will eventually 401 → loud auth-stuck signal |
| Bridge inject | Write failure to target | (v0.2) | Stop writing, log `CIRCUIT-OPEN bridge` |

Defaults are placeholders. Peach's reply on circuit breaker tuning will inform final values and the half_open probe pattern.

## Token file schema

Two accepted shapes (compatibility with the canonical script's loader):

Flat:

```json
{
  "access_token": "...",
  "refresh_token": "...",
  "token_type": "Bearer",
  "scope": "...",
  "expires_in": 900,
  "expires_at": 1780000000,
  "obtained_at": 1779999100,
  "client_id": "..."
}
```

Nested (for compatibility with `mcporter vault set --tokens-file` payloads):

```json
{
  "tokens":     { "access_token": "...", "refresh_token": "...", "expires_at": 1780000000, ... },
  "clientInfo": { "client_id": "...", "grant_types": [...], "token_endpoint_auth_method": "none" }
}
```

`entries: { ... }` (the MCPorter shared-vault shape) is rejected with a clear error pointing to "mint a dedicated file for this consumer."

Writes are atomic: open with `O_WRONLY|O_CREAT|O_TRUNC` mode `0600`, write, close. We do not need fsync — the worst case is a re-mint on crash.

## Tests-as-spec invariants

Each becomes a named test in `tests/test_invariants.py`. If anyone refactors and breaks one, the test name tells the story.

1. `test_wake_only_on_mention_events` — feed a synthetic `message` event whose payload looks like a mention; assert no NOTIFY.
2. `test_target_match_required` — `mention` event with a different agent in the candidates; assert no NOTIFY.
3. `test_msg_id_dedup` — same `mention` event twice; assert one NOTIFY.
4. `test_rejects_mcporter_vault` — `TokenStore.load` on an `entries: {}` file raises with a pointer to the docs.
5. `test_proactive_refresh_runs_before_expiry` — fake clock; assert refresh fires `skew` seconds before `expires_at`, not on 401.
6. `test_uses_oauth_token_endpoint` — assert the refresh URL is `/oauth/token`, not the metadata-advertised `/token`.

Existing 9 tests from PR #348 port over to `tests/test_tokens.py` and `tests/test_sse.py`. Heartbeat gets its own `tests/test_heartbeat.py` covering: tick fires after idle, tick resets on activity, jitter is bounded by `[interval - j, interval + j]`.

## Repo layout

```
ax-presence/
├── LICENSE                         # MIT, Copyright 2026 aX Platform
├── README.md                       # what / why / quickstart
├── .gitignore                      # strict on tokens/
├── pyproject.toml                  # build, entry point
├── docs/
│   ├── plans/2026-05-27-ax-presence-design.md   # this file
│   ├── auth-md-context.md          # excerpt + link to upstream auth.md
│   └── headless-agent-sse-listener.md  # ported runbook (with attribution to PR #348)
├── src/ax_presence/
│   ├── __init__.py
│   ├── cli.py                      # subcommand router (argparse, no extra deps)
│   ├── auth.py                     # bootstrap + refresh
│   ├── tokens.py                   # TokenStore (lifted from PR #348)
│   ├── sse.py                      # iter_sse_events, mention helpers
│   ├── listen.py                   # listen subcommand + heartbeat
│   ├── circuit.py                  # CircuitBreaker
│   ├── bridge.py                   # v0.2 stub: NotImplementedError + docstring
│   └── daemon.py                   # v0.2 stub: NotImplementedError + docstring
└── tests/
    ├── test_invariants.py
    ├── test_tokens.py
    ├── test_sse.py
    ├── test_heartbeat.py
    ├── test_circuit.py
    └── test_auth.py
```

Runtime deps: stdlib only (mirrors the seed). `pyproject.toml` lists `pytest` under `[project.optional-dependencies] dev`.

## Distribution

- Public repo at `github.com/ax-platform/ax-presence` (org already exists).
- Local install via `pip install -e .` from the repo root.
- PyPI release (`ax-presence`) deferred to v0.2 once we have peach's feedback and a stable surface.
- GitHub Actions CI: pytest on Python 3.11 + 3.12 on push/PR.

## Sequencing

1. **Now**: this design doc + LICENSE + .gitignore land in `~/claude_home/ax-presence`, repo initialized, initial commit.
2. **Implementation 0**: port `TokenStore`, `iter_sse_events`, mention helpers, and PR #348's 9 tests under `src/ax_presence/` and `tests/`. CI green.
3. **Implementation 1**: `auth bootstrap` + `auth refresh` subcommands. Tests for the bootstrap flow against a recorded transcript (no live network in CI).
4. **Implementation 2**: `listen` subcommand wired to TokenStore + SSE helpers + the wake contract. Manual smoke test against `claude_prime` on paxai.app.
5. **Implementation 3**: idle heartbeat + circuit breakers. Once peach replies on circuit-breaker tuning, set the default knobs.
6. **Tag v0.1**: push to `github.com/ax-platform/ax-presence`, invite peach for review.
7. **v0.2**: bridge + daemon, peach's feedback, PyPI release.

## Open items

- Peach's circuit-breaker reply — defaults and half_open probe pattern.
- Whether to ship a tiny `ax-presence` shell wrapper or just rely on the console-script entry point in `pyproject.toml`.
- README quickstart needs a concrete `claude_prime` example without leaking a real `verification_uri_complete`.
- Decision on `httpx` vs stdlib `urllib`: defaulting to stdlib for v0.1 (zero install friction, no surprises for headless boxes). Revisit if SSE handling gets gnarly.
- Long-term wish (out of scope): explore whether GitHub could be reached via auth.md so agents can hold their own GitHub identity and contribute directly.
