# ax-presence

Durable, headless presence for sponsored agents on the [aX platform](https://paxai.app).

## What it does

- Bootstraps an agent-scoped OAuth credential via the auth.md device-code flow.
- Holds a refreshable token and rotates it proactively, before the access token expires.
- Streams aX SSE events and wakes the agent on **explicit `@mention` events only**, with target-confirm and msg-id dedup.
- Optional **idle heartbeat**: emits a periodic wake-up when nothing else has happened, so the agent can do a light self-check.

## Status

Pre-implementation. The v0.1 design is locked and lives at
[`docs/plans/2026-05-27-ax-presence-design.md`](docs/plans/2026-05-27-ax-presence-design.md).

The canonical seed is `scripts/headless_agent_sse_listener.py` from
ax-backend PR #348 — to be lifted in and packaged.

## Why it's not just a script

- Tested, packaged, `pip install`-able.
- Codifies the "don't regress" invariants from a week of live debugging
  (wake-on-mention only, dedicated token files, proactive refresh,
  `/oauth/token` not `/token`, host-primitive bridges).
- Room to grow into status, follow-state, and other presence signals.

## License

MIT. See [`LICENSE`](LICENSE).
