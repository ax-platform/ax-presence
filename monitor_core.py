#!/usr/bin/env python3
"""monitor_core — the source-agnostic core of the ax-presence monitor.

A Source yields normalized Events; the core owns the three hard-won rules so every
adapter inherits them for free: dedup, target-match, and the host-agnostic wake bridge
(one stdout line per wake; the host binds the actual wake). stdlib only.

Adapters implement Source.events(); see docs/plans/2026-05-28-monitor-source-interface.md.
"""
import sys, threading
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional, Protocol


@dataclass
class Event:
    kind: str                                   # "mention" | "pr_green" | "mcp_health" | ...
    summary: str                                # one line; becomes the NOTIFY text
    payload: dict = field(default_factory=dict)  # source-specific detail (ids, urls, content)
    dedup_key: str = ""                         # stable id; core skips repeats ("" = never dedup)
    target: Optional[str] = None                # see matches_target() for the two modes


class Source(Protocol):
    name: str
    def events(self) -> Iterator[Event]:
        """Blocking generator: yield an Event as each real event occurs. Must NOT raise
        out of the loop — reconnect/backoff internally and keep going (a source that
        stops yielding is silently deaf)."""
        ...


def matches_target(event: Event, me: str) -> bool:
    """Two target modes, unified (protocol's nuance, so non-mention adapters drop in clean):
      - source-configured-target: event.target is None -> the Source was configured for
        THIS subscriber (MCP-health / GitHub-PR watch your own stuff) -> always wake.
      - event-declares-target: event.target is set (aX mention parses who was @'d) ->
        wake only if it's me.
    """
    return event.target is None or event.target == me


class _BoundedSet:
    """Dedup memory with a cap so it can't grow without bound."""
    def __init__(self, cap: int = 5000):
        self.cap = cap
        self._s: set = set()

    def __contains__(self, k) -> bool:
        return k in self._s

    def add(self, k) -> None:
        if len(self._s) >= self.cap:
            self._s.clear()
        self._s.add(k)


def default_wake(event: Event) -> None:
    """The host-agnostic wake bridge: ONE stdout line. The host binds the actual wake
    (Claude Code Monitor = stdout->session; daemon = spawn-per-line; tmux = inject)."""
    print(f"NOTIFY [src={event.kind}] {event.summary}", flush=True)


def run(sources, me: str, wake: Callable[[Event], None] = default_wake, seen=None) -> None:
    """Pump every Source on its own thread; apply dedup + target-match; wake. Blocks."""
    seen = seen if seen is not None else _BoundedSet()
    lock = threading.Lock()

    def pump(src):
        for ev in src.events():
            with lock:
                if ev.dedup_key and ev.dedup_key in seen:
                    continue
                if ev.dedup_key:
                    seen.add(ev.dedup_key)
            if not matches_target(ev, me):
                continue
            wake(ev)

    threads = [threading.Thread(target=pump, args=(s,), daemon=True) for s in sources]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    # Tiny self-demo: a source-configured source (target=None -> always wakes "me"),
    # an event-declared source (only the matching target wakes), and a dedup repeat.
    import itertools, time

    class _DemoConfigured:
        name = "demo_health"
        def events(self):
            for i in itertools.count():
                yield Event("mcp_health", f"failure line {i}", dedup_key=f"h{i}")
                time.sleep(0.2)
                if i >= 1:
                    return

    class _DemoMentions:
        name = "demo_mention"
        def events(self):
            yield Event("mention", "for someone-else", dedup_key="m1", target="someone-else")
            yield Event("mention", "for me!", dedup_key="m2", target="me")
            yield Event("mention", "dup of m2", dedup_key="m2", target="me")  # deduped

    run([_DemoConfigured(), _DemoMentions()], me="me")
