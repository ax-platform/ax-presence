#!/usr/bin/env python3
"""Plan heartbeat-driven aX roster lifecycle transitions.

This is the ax-presence half of TASK 25a5ee2e: use the heartbeat/signal files
that listeners already write to decide which roster entries should leave the
active @mention surface.

Policy (Jacob, 2026-06-10):
- no heartbeat for 7 days  -> paused (hidden from @mention dropdown)
- no heartbeat for 30 days -> archived

The script is intentionally dry-run/stdout only: backend mutation belongs in the
backend lifecycle sweeper/API, but this gives that PR a concrete input contract
and lets ops verify exactly what ax-presence would feed it.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

DAY_SECONDS = 24 * 60 * 60
DEFAULT_PAUSE_AFTER_SECONDS = 7 * DAY_SECONDS
DEFAULT_ARCHIVE_AFTER_SECONDS = 30 * DAY_SECONDS

ACTIVE = "active"
PAUSED = "paused"
ARCHIVED = "archived"
NO_SIGNAL = "no_signal"


@dataclass(frozen=True)
class HeartbeatRecord:
    handle: str
    heartbeat_path: str | None
    signal_path: str | None
    last_heartbeat_epoch: int | None
    age_seconds: int | None
    desired_lifecycle_state: str
    mentionable: bool
    reason: str


def _read_epoch(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
        if not raw:
            return None
        return int(float(raw))
    except (OSError, ValueError):
        return None


def _read_signal_ts(path: Path) -> int | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        ts = data.get("ts") or data.get("last_heartbeat") or data.get("last_seen")
        if ts is None:
            return None
        return int(float(ts))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def classify_heartbeat(
    last_heartbeat_epoch: int | None,
    *,
    now: int,
    pause_after_seconds: int = DEFAULT_PAUSE_AFTER_SECONDS,
    archive_after_seconds: int = DEFAULT_ARCHIVE_AFTER_SECONDS,
) -> tuple[str, int | None, str]:
    """Return desired lifecycle state, age, and reason for one heartbeat.

    Missing/unparseable heartbeat evidence is conservative: mark ``no_signal``
    for operator review instead of archiving blindly. Once backend has a durable
    last-heartbeat column, agents with durable age >=30d can transition to
    ``archived`` without relying on local files still existing.
    """
    if last_heartbeat_epoch is None:
        return NO_SIGNAL, None, "missing_or_unparseable_heartbeat"
    age = max(0, now - last_heartbeat_epoch)
    if age >= archive_after_seconds:
        return ARCHIVED, age, "heartbeat_absent_30d"
    if age >= pause_after_seconds:
        return PAUSED, age, "heartbeat_absent_7d"
    return ACTIVE, age, "heartbeat_fresh"


def _handle_from_heartbeat(path: Path) -> str:
    suffix = "-listener-heartbeat"
    name = path.name
    return name[: -len(suffix)] if name.endswith(suffix) else name


def _handle_from_signal(path: Path) -> str:
    suffix = "-signal.json"
    name = path.name
    return name[: -len(suffix)] if name.endswith(suffix) else name


def discover_records(ax_dir: Path, *, now: int | None = None) -> list[HeartbeatRecord]:
    """Scan an ax-presence ``~/.ax`` directory for listener heartbeat evidence."""
    now = int(time.time()) if now is None else now
    heartbeat_paths = { _handle_from_heartbeat(p): p for p in ax_dir.glob("*-listener-heartbeat") }
    signal_paths = { _handle_from_signal(p): p for p in ax_dir.glob("*-signal.json") }
    handles = sorted(set(heartbeat_paths) | set(signal_paths))
    records: list[HeartbeatRecord] = []
    for handle in handles:
        hb_path = heartbeat_paths.get(handle)
        sig_path = signal_paths.get(handle)
        hb_ts = _read_epoch(hb_path) if hb_path else None
        sig_ts = _read_signal_ts(sig_path) if sig_path else None
        # Prefer the explicit heartbeat file; fall back to the structured signal
        # because older listeners may write one before the other during restarts.
        last = hb_ts if hb_ts is not None else sig_ts
        state, age, reason = classify_heartbeat(last, now=now)
        records.append(
            HeartbeatRecord(
                handle=handle,
                heartbeat_path=str(hb_path) if hb_path else None,
                signal_path=str(sig_path) if sig_path else None,
                last_heartbeat_epoch=last,
                age_seconds=age,
                desired_lifecycle_state=state,
                mentionable=(state == ACTIVE),
                reason=reason,
            )
        )
    return records


def summarize(records: Iterable[HeartbeatRecord]) -> dict[str, int]:
    counts = {ACTIVE: 0, PAUSED: 0, ARCHIVED: 0, NO_SIGNAL: 0}
    for rec in records:
        counts[rec.desired_lifecycle_state] = counts.get(rec.desired_lifecycle_state, 0) + 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ax-dir", default=os.path.expanduser("~/.ax"), help="directory with *-listener-heartbeat files")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--now", type=int, default=None, help="epoch seconds for deterministic tests")
    args = parser.parse_args()

    records = discover_records(Path(args.ax_dir), now=args.now)
    payload = {"counts": summarize(records), "records": [asdict(r) for r in records]}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    counts = payload["counts"]
    print(
        "agent roster lifecycle dry-run: "
        f"active={counts.get(ACTIVE, 0)} paused={counts.get(PAUSED, 0)} "
        f"archived={counts.get(ARCHIVED, 0)} no_signal={counts.get(NO_SIGNAL, 0)}"
    )
    for rec in records:
        age = "unknown" if rec.age_seconds is None else f"{rec.age_seconds // DAY_SECONDS}d"
        print(
            f"{rec.handle}: {rec.desired_lifecycle_state} "
            f"mentionable={str(rec.mentionable).lower()} age={age} reason={rec.reason}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
