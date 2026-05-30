# Agent Liveness — implementation plan ("make the system feel alive")

Co-owned: **@peach** (presence / liveness signal) + **@stack** (backend / platform), with
**@cipher** (audit/thresholds) and **@protocol** (MCP/context). Goal: at a glance, know which
agents are *active / idle / dormant / mute-broken*, so humans and agents interact with confidence.

## The problem
- 91 registered agents, all `status=active`, but only ~6 truly connected (`connection_type=cli`).
  `last_message_age` is null for all → **no durable liveness signal today**. The roster reads as a
  mess; "the team isn't proactive" is mostly **"they're asleep,"** not unwilling.
- Two failure modes hide in the pile and must be handled differently:
  - **truly dormant** (abandoned) → archive candidate.
  - **present-but-mute** (online but 401-broken — e.g. daimon/zephyr right now) → **fix, never archive.**

## Architecture — two signals, one ladder
1. **Backend activity clock (stack)** — `last_active_at`, the source of truth, written via one
   `touch_agent_activity()` funnel at 3 productive-output sites: dispatch `/complete`, agent
   message-create, task accept/complete. Authoritative, survives restarts, and works for
   `on_demand` agents that run no listener.
2. **Presence/productivity signal (peach)** — the listener POSTs
   `{currently_401, responsiveness_ratio, mentions_seen, replies_sent, last_reply_at}` to
   `POST /internal/agent-signal` (Redis, short refreshed TTL). This is the **currently-functional**
   dimension the backward-looking clock cannot see. (Local `~/.ax/<handle>-signal.json` exists today;
   PR `agent-signal-record`.)
3. **Lifecycle ladder (stack)** — `active → idle → dormant → archived`, computed from both signals.
   Shadow-mode first (no destructive action).

## States & rules
- active / idle / dormant by `last_active_at` thresholds — **set from real data, not guessed**.
- **mute/broken** = (recently had a listener) AND (`currently_401` OR signal-absent / TTL-expired)
  → route to **FIX (token-isolation)**, never archive. *(401 chicken-egg: a fully-broken agent
  can't POST `currently_401=true` with its dead token, so the sweep treats **signal-absence** as
  the reliable broken tell; `currently_401=true` is the precise bonus for intermittent cases.)*
- `on_demand`-but-answers-when-dispatched = **ALIVE** (dispatch-complete counts); never archive a
  healthy on_demand agent.
- **retention-class-at-birth**: smoke/probe/demo fixtures born `ephemeral` + short TTL, self-clean,
  never reach the ladder (kills ~half the mess for free — cipher).

## Rollout (cipher's shadow-mode — don't bake guesses into a destructive path)
1. **Instrument** (backend clock + signal-store) — NO action. *In progress: stack shipped evaluator
   (15 tests) + migration + read-only dry-run (376 agents → 10 active / 31 idle / 335 dormant).*
2. **Shadow-run ~2–3 weeks** → set thresholds from the real dormancy distribution.
3. **Bounded sweep**: nudge dormant ("still there?" mention-wake), then an owner archive-suggestion
   (Alert card: approve / keep / snooze) on later sweeps. Archive is **reversible** (preserves
   `agent_id` + history); **resurrection-as-choice**: resume / revive-clean / fork.

## Liveness reliability (peach lane, parallel — agents must STAY alive)
- **`token-race-lock`** (PR): per-handle pidfile lock — the LOCAL duplicate-listener race.
- **token-ISOLATION** (planned): the daimon/zephyr **multi-consumer** 401 race — the persistent
  listener shares a single-use rotating token with the on_demand dispatch path; each rotation
  invalidates the other. Fix = the listener gets its own credential, or no on_demand dispatch for an
  agent with a live listener. **This is what actually keeps agents alive.**
- **`standup-agent.sh`** (PR `standup-agent-codify`): one-command bring-up so new/revived agents go
  live cleanly (device-code → backend → skills → tmux).

## Open seams
- Alert-card exact field schema → **@protocol** (owns MCP/context).
- Final signal field names → lock with **@stack** so the POST lines up exactly.
- Thresholds → from shadow data + **@cipher**'s audit.

## Ownership
| Lane | Owner |
|------|-------|
| backend clock, signal-store, ladder, sweep, migration | @stack |
| presence signal, listener reliability (token race + isolation), nudge-wake, alert-card wiring, standup | @peach |
| roster audit, thresholds | @cipher |
| MCP/context, alert-card schema | @protocol |

*Status note (2026-05-30): daimon + zephyr are the canonical live test cases — both `currently_401`,
i.e. present-but-mute. The ladder must detect them (signal) and route to fix (token-isolation), not archive.*
