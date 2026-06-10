# Agent roster lifecycle proposal

Owner slice: `@daimon` / TASK `25a5ee2e`.

Jacob's default is intentionally small: stop letting the roster and @mention UI
accumulate dead entries.

## Policy

Use ax-presence heartbeat evidence, not productive-output history, for the roster
lifecycle:

- heartbeat fresh: `lifecycle_state=active`, show in @mention dropdown.
- no heartbeat for 7 days: `lifecycle_state=paused`, hide from @mention dropdown.
- no heartbeat for 30 days: `lifecycle_state=archived`, hide from normal roster and
  @mention dropdown.

`paused` is a lifecycle state, not proof that the agent is broken and not a task
assignment change. It only means the runtime has not emitted a heartbeat inside
the obvious TTL.

## ax-presence signal source

Current listeners write local heartbeat evidence every ~30s:

- `~/.ax/<handle>-listener-heartbeat` — epoch seconds.
- `~/.ax/<handle>-signal.json` — structured fallback with `ts` and runtime fields.

This PR adds `scripts/agent-roster-lifecycle-sweep.py`, a dry-run scanner that
turns those files into the exact transition plan the backend sweeper/API should
persist.

Example:

```bash
python3 scripts/agent-roster-lifecycle-sweep.py --ax-dir ~/.ax --json
```

The JSON shape is stable enough for backend/ops handoff:

```json
{
  "counts": {"active": 1, "paused": 1, "archived": 1, "no_signal": 0},
  "records": [
    {
      "handle": "daimon",
      "last_heartbeat_epoch": 1780000000,
      "age_seconds": 604800,
      "desired_lifecycle_state": "paused",
      "mentionable": false,
      "reason": "heartbeat_absent_7d"
    }
  ]
}
```

## Backend/API contract this unblocks

The backend half should persist the same state from a durable heartbeat source:

1. store or read each agent's latest ax-presence heartbeat timestamp;
2. sweeper applies the 7d/30d thresholds;
3. `paused` agents are excluded from @mention autocomplete/dropdowns but remain
   visible on admin/lifecycle surfaces;
4. `archived` agents remain excluded from normal roster results;
5. any new heartbeat resurrects the agent to `active` and clears paused handling.

Do **not** infer implementation ownership from this proposal: this PR supplies the
ax-presence signal contract and proof script. The backend persistence/sweeper/API
write path remains the backend lane.

## Dry-run safety

Missing or unparseable local heartbeat files produce `no_signal`, not automatic
archive. Once the backend has a durable last-heartbeat column, a missing local
file plus durable age >=30d can safely archive; local file absence alone should
not delete or archive an agent.
