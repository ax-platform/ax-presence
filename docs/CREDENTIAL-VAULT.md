# Credential vault for ax-presence agents

**Direction (madtank):** stop storing agent credentials in plaintext; use a vault — mcporter
is the preferred option (it's also an MCP client, so it serves multiple purposes). This doc is the
design; implementation = a small `vault.py` + routing the listener's token I/O through it.

## Problem
Agents store device-code tokens as **plaintext** `~/.ax/<handle>-listener.json` (`access_token`,
single-use rotating `refresh_token`, `client_id`, `resource`). Plus: git PAT in `~/.git-credentials`,
Codex `auth.json`, and the new `INTERNAL_DISPATCH_API_KEY` in env. Issues:
- Plaintext secrets on disk — easy to leak into shared dirs / backups / logs (a PAT already leaked
  into a transcript once and had to be rotated).
- Refresh tokens are **single-use / rotate** → two readers/refreshers race and 400/401 (the
  token-race we keep hitting: daimon/zephyr). A scattered-file model makes the race easy to hit.
- No shared tooling: mcporter, the listener, and every other consumer each roll their own file I/O.

## mcporter vault (the model to match)
- Store: `~/.mcporter/credentials.json`, per-server alias (`ax-paxai-<handle>`, not bare `ax`).
- Seed once with a tokens-file shaped `{tokens:{access_token,token_type,refresh_token,expires_in,
  scope}, clientInfo:{client_id,grant_types,token_endpoint_auth_method:"none",redirect_uris:[]}}`
  via `mcporter vault set ax-paxai-<handle> --tokens-file <path>`.
- mcporter then **refreshes transparently** on use. Device-code seeding keeps it headless-friendly.

## Proposal: a `vault.py` abstraction in ax-presence
`load()` / `save(tokens)` with **pluggable backends** selected by `AX_VAULT` (default = today's
behavior, zero breakage):
1. **`file`** (default) — current `~/.ax/<handle>-listener.json`, 0600. No change for existing agents.
2. **`mcporter`** — read/write `~/.mcporter/credentials.json` under `ax-paxai-<handle>`; shares creds
   with mcporter + other MCP tooling. Seed from device-code (no browser).
3. **`keyring`** — OS keychain via the `keyring` lib (no plaintext at rest). Optional dep.

`load_tok()` / `save_tok()` / `connect()` route through `vault` — a one-line swap at each call site.

## CRITICAL: single-refresher invariant
Refresh tokens rotate (single-use). If BOTH the listener's `proactive_refresh_loop` AND mcporter
refresh the same vault entry, they race → 400/401 → broken agent. **Exactly one refresher per entry.**
- `AX_VAULT_REFRESHER=listener|mcporter` (default `listener`): the non-owner is READ-ONLY on the
  access token. This is the same root cause as the daimon/zephyr **multi-consumer token race** — a
  vault with a single enforced refresher is part of that fix (see token-isolation work).

## Migration
On first run with a non-file backend, auto-import the existing `~/.ax/<handle>-listener.json` into the
vault, then chmod-000/rename the old file so nothing reads stale creds.

## Security
- Never write tokens to shared dirs — vault stays in the agent's private home.
- 0600 / keychain at rest; **redact tokens from ALL logs** — never print token values (route-to-stderr
  is not enough). A fail-open "redaction" that prints on no-match is worse than none.
- `token_endpoint_auth_method: "none"` (public client) is correct for device-code.

## Open questions
1. Does mcporter's vault tolerate our `resource=/mcp/agents/<handle>` named-agent route on refresh?
   (Verify before trusting mcporter-owned refresh.)
2. Keychain availability on the Graviton/headless box (likely none → `file` or `mcporter` there).
3. A `--vault-status` one-shot (backend, ttl, which process owns refresh) for debuggability.

## Effort
Small: ~one `vault.py` (3 backends, ~120 lines) + routing `load_tok`/`save_tok`/`connect` through it +
a migration shim. In-lane (ax-presence). Default `file` = zero breakage; opt into `mcporter`/`keyring`.
