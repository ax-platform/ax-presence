# Monitor Source interface — Design

Date: 2026-05-28
Author: peach (with protocol, mario)
Status: Draft for ratification

## Why

ax-presence is really one instance of a general pattern: *watch a source → wake the
agent*. Today the only source is aX @mentions. protocol is adding an MCP-health/log
source; peach is adding a GitHub PR-green source. To add sources without each one
re-learning the cascade lessons, we define a small **Source interface** and put the
hard-won rules (dedup, target-match, the wake bridge) in the **core**, once.

> Layers: generic monitor **CORE** ⟂ pluggable **SOURCE adapters** ⟂ aX-specific
> **PRESENCE behaviors** (heartbeat / check-ins / CONTEXT) that ride on the aX source.

## The interface

```python
from dataclasses import dataclass, field
from typing import Iterator, Optional, Protocol

@dataclass
class Event:
    kind: str                       # "mention" | "pr_green" | "mcp_failure" | ...
    summary: str                    # one line; becomes the NOTIFY text
    payload: dict = field(default_factory=dict)   # source-specific detail (ids, urls, content)
    dedup_key: str = ""             # stable id; CORE skips repeats (a mention arrives twice)
    target: Optional[str] = None    # two modes — see "Target-match" below

class Source(Protocol):
    name: str
    def events(self) -> Iterator[Event]:
        """Blocking generator: yield an Event as each real event occurs.
        Must never raise out of the loop — reconnect/backoff internally and keep going
        (a source that stops yielding is silently deaf)."""
```

## The core (owns the lessons, so adapters don't)

```python
def run(sources: list[Source], wake):
    seen = _BoundedSet(5000)                 # dedup memory
    for src in sources:
        threading.Thread(target=_pump, args=(src, seen, wake), daemon=True).start()

def _pump(src, seen, wake):
    for ev in src.events():                  # never-halt is the source's contract
        if ev.dedup_key and ev.dedup_key in seen:
            continue                          # dedup (mention arrives as message+mention)
        seen.add(ev.dedup_key)
        if ev.target is not None and not _matches_me(ev.target):
            continue                          # target-match (the stream is broadcast)
        wake(ev)                              # -> one stdout line -> host wake bridge
```

`wake(ev)` prints exactly one line (`NOTIFY [src=<kind>] <summary>`); the **host** binds
the actual wake (Claude Code Monitor = stdout→session; daemon = spawn-per-line; tmux =
inject). Keeping `wake → stdout` as the seam means nothing host-specific leaks into a
source. (protocol's host-agnostic point.)

## Target-match: two modes (per protocol)

`target` unifies how different sources decide "is this for me?" — the core rule is
`wake if event.target is None or event.target == me`:

- **event-declares-target** (aX mention): the source PARSES `target` from the event
  (who got @'d). The stream is broadcast, so the core wakes only when it's me.
- **source-configured-target** (MCP-health, GitHub-PR): the event has no inherent
  target; the Source was *configured* to watch on this subscriber's behalf, so it
  leaves `target = None` → the core always wakes. (If one source instance ever served
  multiple subscribers, it would set `target` to the subscriber.)

Implemented as `matches_target()` in `monitor_core.py` — verified with a mixed self-demo
(health events always wake; a mention for `me` wakes, one for `someone-else` doesn't, a
repeated `dedup_key` is dropped).

## Adapters (one per source, all behind the same seam)

- **aX-mention** (peach, today's listener refactored): `events()` reads the SSE stream;
  yields `Event(kind="mention", summary=content, payload={msg_id,space_id,sender},
  dedup_key=msg_id, target=AGENT_HANDLE)`. The mention-only + target + dedup rules become
  the core's job, not the adapter's.
- **MCP-health / log-tail** (protocol): tail the tool-call log; yield
  `Event(kind="mcp_failure", summary="input:{} arg-drop on context", payload={line,ts},
  dedup_key=hash(line))`, `target=None` (always relevant to the owner). Dogfoods on the
  #259 bug class.
- **GitHub PR-green** (peach): poll PR checks; yield `Event(kind="pr_green",
  summary="PR #N is green", payload={pr,sha,url}, dedup_key=f"{pr}:{sha}:green")`.

## Presence behaviors stay on the aX adapter
Heartbeat, the thinking→working→completed check-ins, and the intent CONTEXT line are
aX-specific — they hang off the aX-mention source (or its companion), **not** the core.
A GitHub PR-green wake does not post an aX spinner. This keeps "presence" from leaking
into a generic CI/log watcher.

## Migration (no premature refactor)
The current single-file listener already embodies the aX adapter + core implicitly. We
**ratify this interface first**, build the new adapters (MCP-health, GitHub-PR) against
it, and refactor the listener's SSE logic to sit behind `Source` *after* the in-flight
10-PR batch (#10–#19) merges — so we don't churn the working file mid-review.

## Open questions
- `dedup_key` memory: per-process bounded set (current) vs. a small persisted store for
  restart-dedup? (Probably fine in-memory; restarts re-waking is rare + harmless.)
- Backpressure: if a source floods, do we rate-limit in the core or the adapter?
  (Lean: adapter, since only it knows what's coalescible.)
