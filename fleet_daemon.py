#!/usr/bin/env python3
"""ax-presence fleet daemon — per-device supervisor for agent listeners.

Spec: docs/plans/2026-06-12-fleet-daemon-design.md (Phase 1).
Invariants: never touch child token files (read-only TTL checks only);
one process per agent; never set AX_SPACE_ID in child env.
"""
import json, os, sys, time, threading, subprocess, signal, fcntl
import urllib.request, urllib.error

try:
    import tomllib
except ImportError:           # Python < 3.11 (older Ubuntu boxes)
    tomllib = None

AGENT_DEFAULTS = {"platform": "ax", "catchup": "ask"}

RESPAWN_CAP_S = 300
CRASHLOOP_N = 5
CRASHLOOP_WINDOW_S = 600

SUSPEND_DRIFT_S = 30

# Per-agent identity/state vars the listener binds from its environment
# (ax_presence_listener.py). Any of these inherited from the daemon's own
# env would bind every child to one agent's identity or files (same bug
# class as 80588cba), so child_env scrubs them all before injecting the
# per-agent whitelist. Fleet-wide vars (AX_BASE, AX_INTERNAL_SIGNAL_KEY)
# are intentionally NOT listed — children inherit those.
PER_AGENT_ENV_VARS = (
    "AX_SPACE_ID", "AX_AGENT_ID", "AX_AGENT_HANDLE", "AX_TOKEN_FILE",
    "AX_HEARTBEAT_FILE", "AX_ACTIVITY_FILE", "AX_ACTIVITY_JSON_FILE",
    "AX_REMINDERS_FILE", "AX_HOME_FEED_FILE", "AX_BUSY_MESSAGES_FILE",
)


def _parse_toml_minimal(text):
    """Fallback parser for the flat [section] / key = "value" subset
    fleet.toml uses. Not a general TOML parser by design."""
    out, section = {}, None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            d = out
            for part in section.split("."):
                d = d.setdefault(part, {})
            continue
        if "=" in line and section is not None:
            k, v = (s.strip() for s in line.split("=", 1))
            v = v.strip().strip('"')
            d = out
            for part in section.split("."):
                d = d[part]
            d[k] = v
    return out


def load_fleet_config(path):
    with open(path, "rb") as f:
        raw = f.read()
    data = tomllib.loads(raw.decode()) if tomllib else _parse_toml_minimal(raw.decode())
    agents = data.get("agents") or {}
    if not agents:
        raise ValueError(f"{path}: no [agents.*] sections")
    for name, a in agents.items():
        for k, v in AGENT_DEFAULTS.items():
            a.setdefault(k, v)
        a["token_file"] = os.path.expanduser(a["token_file"])
    return {"fleet": data.get("fleet", {}), "agents": agents}


def child_env(name, cfg):
    """Build the child listener's environment. NEVER sets AX_SPACE_ID —
    the child derives its space from its agent record (bug 80588cba) —
    and strips every inherited per-agent AX_* var so children never share
    an identity, heartbeat, or activity file with the daemon or each other."""
    env = dict(os.environ)
    for var in PER_AGENT_ENV_VARS:
        env.pop(var, None)
    a = cfg["agents"][name]
    env.update({
        "AX_AGENT_HANDLE": name,
        "AX_TOKEN_FILE": a["token_file"],
        "AX_SPONSOR": cfg["fleet"].get("sponsor", "@your-sponsor"),
    })
    return env


def respawn_delay(failures):
    """Exponential backoff for child respawns, capped at RESPAWN_CAP_S."""
    return min(RESPAWN_CAP_S, 2 ** failures) if failures else 1


def is_crashloop(failure_times, now):
    """True when CRASHLOOP_N or more failures landed inside the
    CRASHLOOP_WINDOW_S sliding window ending at `now`."""
    recent = [t for t in failure_times if now - t <= CRASHLOOP_WINDOW_S]
    return len(recent) >= CRASHLOOP_N


def suspend_tick(state, mono_now, wall_now):
    """Detect host suspend: wall-clock advanced while the monotonic clock
    (which pauses during macOS suspend) did not. Mutates state for the
    next tick; returns a suspend event dict or None."""
    drift = (wall_now - state["wall"]) - (mono_now - state["mono"])
    state["mono"], state["wall"] = mono_now, wall_now
    if drift > SUSPEND_DRIFT_S:
        return {"kind": "suspend_detected", "for_s": round(drift)}
    return None


def token_ttl(token_file, now):
    """READ-ONLY token inspection. The daemon must never refresh, reuse,
    or rewrite a child's rotating token file (review-locked invariant);
    the child listener is the sole refresher."""
    try:
        with open(token_file) as f:
            return int(json.load(f).get("expires_at", 0) - now)
    except Exception:
        return None
