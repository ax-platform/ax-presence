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
