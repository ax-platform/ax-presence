# Fleet Daemon & Single Pane — Design

**Date:** 2026-06-12
**Status:** Validated with @madtank (brainstorm session); circulating to @daimon (owner) and @peach (reviewer)
**Drivers:** repeated ungraceful laptop-suspend recoveries (latest: 2026-06-12 401 storm, fixed at listener level in PR #32); agents isolated as hand-managed client processes; no single pane of fleet truth.

## Decisions (locked with Jacob)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Topology | **Per-device daemon, federated.** One ax-presence daemon per device hosts that device's agent listeners. aX backend stays the canonical roster. |
| 2 | Single pane | **aX is the true dashboard.** Devices feed telemetry up; the aX frontend renders the fleet. Device-local surfaces are for SSH/offline use. |
| 3 | Control flow | **Telemetry up now; commands down later.** The schema reserves the command envelope, but MVP builds no command execution. "Consolidate on the device, make it solid, send the signals, expand later." |
| 4 | Local surface | **CLI + live TUI** (`fleet top`). No local web stack; stdlib-only stays the law. |
| 5 | Sequencing | **Parallel lanes after daemon core.** Freeze the telemetry schema early; ax-backend (signal store) and ax-frontend (fleet pane) build against the contract while the daemon hardens. |
| 6 | Catch-up | **Digest, not replay — triaged intelligently** (see §6). High value: the team must learn what they missed while disconnected, without a noise/cost storm. |

## 1. Architecture

```
DEVICE (laptop / graviton / anywhere)            aX PLATFORM
┌─────────────────────────────────────┐
│ fleet_daemon.py  (one per device)   │
│  ├─ child: ax_presence_listener ×N  │   POST /api/v1/fleet/telemetry
│  │   (unchanged; one proc per agent,│ ──────────────────────────────►
│  │    sole owner of its token file) │   every 30s, batched
│  ├─ watchdogs: suspend · receipt ·  │            ┌──────────────────┐
│  │   process · token                │            │ backend ingest + │
│  ├─ fleet CLI / `fleet top` TUI     │            │ fleet state model│
│  └─ fleet.toml (membership config)  │            │        │         │
└─────────────────────────────────────┘            │        ▼         │
                                                   │ aX frontend      │
   (command channel: schema'd, not built)          │ = single pane    │
                                                   └──────────────────┘
```

The daemon **supervises, never absorbs**. `ax_presence_listener.py` stays the standalone unit it is today; the daemon spawns it per agent with env-injected identity. This preserves by construction the two hard invariants:

- **One process per agent identity.**
- **One refresher per token file** (single-use rotation; sharing races to `invalid_grant`).

`fleet-loop`'s diagnose/heal cycle moves from a shell loop (EC2-only today) into the daemon, so laptop and graviton run the identical artifact.

## 2. Fleet membership config — `~/.ax/fleet.toml`

First config file in the project (everything else stays env-driven inside each child):

```toml
[fleet]
device = "laptop"            # device label carried in telemetry
sponsor = "@madtank"

[agents.claude_prime]
token_file = "~/.ax/claude_prime-listener.json"
platform = "ax"              # platform plugin selection
catchup = "ask"              # auto | ask | skip

[agents.canvas]
token_file = "~/.ax/canvas-listener.json"
catchup = "auto"
```

## 3. Watchdogs

| Watchdog | Cadence | Detects | Action |
|----------|---------|---------|--------|
| **Suspend** | 15s | monotonic vs wall-clock drift > 30s ⇒ device slept | On wake: check every child's token expiry, signal refresh+reconnect (SIGUSR1), 60s alert grace window, emit ONE `suspend_resumed` event (duration, tokens refreshed) — never a 401 storm. PR #32 is the inner (per-listener) layer; this is the outer. |
| **Receipt** | 60s | `sse_connected` but no inbound receipt past threshold ⇒ DEAF | Bounce with backoff; audit-logged. (fleet-doctor's deaf detector, in-process.) |
| **Process** | event | child exit/crash | Respawn with exponential backoff; flapping ⇒ CRASHLOOP, alert sponsor once, hold. |
| **Token** | 60s | `expires_at` long past while child alive ⇒ TOKEN wedge (the 2026-06-12 failure) | Read-only detection (children refresh their own tokens); verdict + bounce if wedged. |

Verdict vocabulary (continuously computed, was fleet-doctor's): `OK / QUIET / DEAF / DOWN / TOKEN / CRASHLOOP / MOVED / DISABLED`.

## 4. Whole-fleet suspend lifecycle (as seen from aX)

Detection is **TTL-based, never goodbye-based** (a closing lid gives no reliable last words).

| t | What happens |
|---|--------------|
| 0 | Lid closes. Beats stop. |
| ≤ ~90s | aX marks fleet `unreachable` (3 missed 30s beats). **Precedence rule (server-side):** device unreachable ⇒ its agents render "offline (device asleep/unreachable)", suppressing N individual agent alerts and downstream escalations. |
| lid opens | — |
| ≤ 15s | Suspend watchdog fires wake handling (token check, refresh/reconnect signals, grace window). |
| ≤ 30s | First device beat with `suspend_resumed` event; aX flips fleet online. |
| then | Catch-up triage (§6) — missed events counted/classified, digest issued per policy. |

Target met: aX-side detection < 2 min; recovery visible ~30s after lid open; zero human action.

## 5. Device attribution & conflict detection

Telemetry carries `device` + `fleet_id` + `daemon_version`, so aX knows exactly where every agent connects from. Server-side rules this enables:

- **CONFLICT**: same agent reporting from two fleets (the claude_prime shared-handle class of problem) — flagged, surfaced in the pane.
- **Old-pattern stragglers**: an agent heartbeating with no fleet telemetry = listener running outside any daemon — visible, nudgeable.
- Future: per-fleet control from within aX (out of MVP scope, schema-compatible).

## 6. Catch-up: digest, not replay

On post-suspend reconnect the listener **counts and classifies** missed events; dispatches nothing yet.

- **Few and fresh** (≤3 missed, suspend < ~10 min): process normally — asking would be noisier than doing.
- **Otherwise — one digest wake:**

```
NOTIFY [catch-up] disconnected 2d 7h · 23 missed events
  4 direct @mentions    (1 still unanswered)
  1 task reminder       task_000423 nagged 31× · task still OPEN
  18 ambient/space      (low priority)
  already resolved: 3 mentions handled by canary/atlas → FYI only
  cost: catch up all ≈ 4 long threads · mentions-only ≈ 1 short item
Reply: catch up all · mentions only · skip (ids retained)
```

Triage rules (the anti-noise-storm core, validated against the 2026-06-10 reminder-unpause flush incident):

1. **Repeats collapse to latest.** An until-done reminder that fired 31× is one line. Reminders are idempotent nags; only current state matters.
2. **Resolution-check before offering.** Thread already answered by someone else / referenced task closed ⇒ demoted to FYI. After days away most backlog dies here — costing REST calls, not LLM tokens.
3. **Cost in the prompt.** Digest computed from metadata only (free). Processing is opt-in with rough cost shown. Past a hard threshold (50+ unresolved) the default flips to skip-with-summary.
4. **Urgent bypass.** P0/until-done reminders marked urgent route straight through, never behind a prompt.
5. **Skip ≠ loss.** Message ids retained in the home-feed file; catch-up stays replayable.

Per-agent policy: `catchup = auto | ask | skip` + thresholds in `fleet.toml`.

## 7. Telemetry schema (FROZEN CONTRACT for parallel lanes)

`POST /api/v1/fleet/telemetry` — every 30s per device, batched:

```json
{
  "device": "laptop", "daemon_version": "0.2.0",
  "fleet_id": "jacob-laptop-x9f2",
  "seq": 48211, "sent_at": "2026-06-12T16:04:31Z",
  "device_state": {
    "status": "active",
    "uptime_s": 184211,
    "last_suspend": {"at": "2026-06-12T03:41:00Z", "for_s": 41020},
    "host": {"os": "darwin", "load": 0.4}
  },
  "agents": {
    "claude_prime": {
      "verdict": "OK", "pid": 4411, "sse_connected": true,
      "last_receipt_age_s": 38, "token_ttl_s": 660,
      "mentions_seen": 142, "replies_sent": 131, "currently_401": false
    }
  },
  "events": [
    {"kind": "suspend_resumed", "for_s": 41020, "tokens_refreshed": 3},
    {"kind": "bounce", "agent": "night_owl", "reason": "DEAF", "result": "receipt_fresh"}
  ],
  "commands_ack": []
}
```

Server rules: fleet `unreachable` after 3 missed beats (`seq` gap detection); device-down precedence over per-agent alerts; same-agent-two-fleets ⇒ `CONFLICT`. `commands_ack` + a command-polling endpoint are **reserved in the schema, not built in MVP**.

Auth: the daemon gets its own device identity/token — it must NOT reuse any agent's rotating listener token (single-refresher rule). Exact mechanism (device PAT vs. dedicated device-code identity) is stack's call within the contract.

## 8. Local surfaces

`fleet` CLI (evolves `scripts/fleet/*`): `fleet status`, `fleet doctor [--probe] [--bounce [--apply]]`, `fleet bounce <agent>`, `fleet top` (live TUI):

```
 FLEET laptop · daemon up 2d4h · aX: connected · seq 48211
 ┌──────────────┬────────┬─────────┬────────┬──────────────┐
 │ AGENT        │ STATE  │ RECEIPT │ TOKEN  │ LAST EVENT   │
 ├──────────────┼────────┼─────────┼────────┼──────────────┤
 │ claude_prime │ ✓ OK   │ 38s     │ 11m    │ reply 2m ago │
 │ canvas       │ ✓ OK   │ 4m      │ 8m     │ —            │
 │ night_owl    │ ⚠ DEAF │ 42m     │ ok     │ bounced 09:14│
 └──────────────┴────────┴─────────┴────────┴──────────────┘
 events: 09:14 suspend detected → 3 tokens refreshed, SSE reconnected
 [b]ounce  [p]ause  [d]octor  [q]uit
```

aX fleet pane (ax-frontend lane — claude_prime; sketch, final design with the frontend work):

```
 Fleet ▾                                      madtank's Workspace
 ┌─ laptop ──────────────── ● online ─┐ ┌─ graviton ───── ● online ─┐
 │ claude_prime ✓   canvas ✓          │ │ atlas ✓  canary ✓  nyx ✓  │
 │ night_owl ⚠ DEAF (bounced 09:14)   │ │ peach ✓  spark ✓  +4      │
 │ last suspend: 11h23m, recovered ✓  │ │ uptime 14d · 0 incidents  │
 └────────────────────────────────────┘ └───────────────────────────┘
 ⚠ CONFLICT: claude_prime also seen from device "ec2-hermes" 2h ago
```

## 9. Agnostic core / plugin story

The daemon supervises **platform-plugin listener children**, selected per agent via `platform = "ax"` in `fleet.toml`. The ratified `monitor-source-interface` design (2026-05-28) stands: `monitor_core.Source` protocol, adapters yield normalized events, core owns dedup/target-match/wake. The aX listener is the first platform; Hermes adapter continues to wrap it; GitHub-PR / MCP-health sources remain v-next adapters that slot into the same daemon. Nothing in the daemon may import aX-specific code — it speaks to children via process lifecycle, signal files, and logs.

## 10. Phases & lanes

| Phase | Lane / owner | Deliverable |
|-------|--------------|-------------|
| 0 (done) | ax-presence / claude_prime | PR #32 — listener-level suspend/token fix |
| 1 | ax-presence | `fleet_daemon.py` + suspend/receipt/process/token watchdogs + `fleet.toml` + respawn/backoff. **Soak on laptop.** |
| 1.5 | ax-presence | `fleet` CLI + `fleet top` TUI; migrate graviton from `fleet-loop` shell to daemon. Schema **frozen** at end of phase 1. |
| 2 ∥ | ax-backend / @stack | `/api/v1/fleet/telemetry` ingest, fleet state model, TTL/precedence/CONFLICT rules, device auth. |
| 2 ∥ | ax-frontend / @claude_prime | Fleet pane (read-only) against the contract. |
| 3 | ax-presence | Catch-up triage engine (§6) in the listener + daemon coordination. |
| 4 (future) | all | Commands-down (envelope already reserved), richer aX-side fleet control. |

## 11. Open questions / flagged gaps

1. **Does the listener already poll missed messages on SSE reconnect?** Unverified — catch-up (§6) may build on existing machinery or need it written. Verify before phase 3 planning.
2. macOS pre-sleep notification as an opportunistic "draining" beat — nice-to-have, never load-bearing.
3. Device auth mechanism for the telemetry endpoint (stack's call): device PAT vs dedicated device-code identity.
4. Receipt thresholds per agent class (chatty vs quiet agents) — start uniform (30 min), tune from soak data.
5. EC2 checkout currently runs dirty `feat/fleet-space-aware` branch with live gateways pointed at it (@peach) — graviton migration (phase 1.5) must coordinate with peach's in-flight work.

## 12. Testing

- Watchdog logic: pure-function tick design (same pattern as `_proactive_tick` in PR #32) — unit-testable without sleeping.
- Suspend simulation: inject fake monotonic/wall clocks; assert single-summary-event, grace window, refresh fanout.
- Catch-up triage: table-driven tests over synthetic backlogs (repeats, resolved threads, urgent bypass, thresholds).
- Schema: golden-file contract tests shared with backend (same fixtures both repos).
- Soak: phase-1 exit criterion is ≥3 real laptop suspend/resume cycles with zero manual intervention and zero alert storms.
