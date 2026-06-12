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

TOKEN_WEDGE_S = -600          # expired this long with child alive = wedged
DEAF_THRESHOLD_S = 1800       # uniform start; tune from soak data (spec §11.4)

TELEMETRY_EVENT_CAP = 50

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


def stamp_line(line, now):
    return f"{int(now)} {line}"


def last_receipt_age(log_path, now, _tail_bytes=262_144):
    """Newest inbound receipt (NOTIFY line) age in seconds, or None.
    Receipt-as-truth: this — not sse_connected — is what proves the
    listener actually consumes mentions (fleet-doctor lesson)."""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - _tail_bytes))
            tail = f.read().decode(errors="replace")
    except OSError:
        return None
    newest = None
    for line in tail.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[1].startswith("NOTIFY ") and parts[0].isdigit():
            newest = int(parts[0])
    return None if newest is None else int(now - newest)


def verdict(s):
    if s["disabled"]:
        return "DISABLED"
    if s["crashloop"]:
        return "CRASHLOOP"
    if not s["alive"]:
        return "DOWN"
    if s["token_ttl_s"] is not None and s["token_ttl_s"] < TOKEN_WEDGE_S:
        return "TOKEN"
    if s["receipt_age_s"] is None:
        return "QUIET"
    if s["receipt_age_s"] > DEAF_THRESHOLD_S:
        return "DEAF"
    return "OK"


class AgentProc:
    def __init__(self, name, argv, env, log_path):
        self.name, self.argv, self.env, self.log_path = name, argv, env, log_path
        self.proc, self.pid = None, None
        self.failure_times = []

    def spawn(self):
        logf = open(self.log_path, "ab")
        self.proc = subprocess.Popen(
            self.argv, env=self.env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True)
        self.pid = self.proc.pid
        threading.Thread(target=self._pump, args=(logf,), daemon=True).start()

    def _pump(self, logf):
        for raw in self.proc.stdout:
            line = stamp_line(raw.decode(errors="replace").rstrip("\n"), time.time())
            logf.write(line.encode() + b"\n"); logf.flush()
        logf.close()

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def signal_wake(self):
        os.kill(self.pid, signal.SIGUSR1)

    def terminate(self):
        if self.alive():
            self.proc.terminate()


def wake_fanout(agents):
    """Post-suspend: nudge every alive child to re-verify its token and
    reconnect (child-owned refresh — daemon never touches token files).
    Returns names nudged; dead children are the process watchdog's job."""
    nudged = []
    for ag in agents:
        if ag.alive():
            try:
                ag.signal_wake(); nudged.append(ag.name)
            except OSError:
                pass
    return nudged


def listener_argv():
    return [sys.executable, "-u",
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "ax_presence_listener.py")]


def build_telemetry(fleet, fleet_id, daemon_version, seq, sent_at,
                    device_state, agent_snaps, events):
    """Assemble the §7 telemetry body (frozen contract — golden fixture
    tests/fixtures/telemetry_golden.json is shared with nyx's backend).
    commands_ack is reserved in the schema, not built in MVP."""
    return {
        "device": fleet.get("device", "unknown"),
        "daemon_version": daemon_version,
        "fleet_id": fleet_id,
        "seq": seq, "sent_at": sent_at,
        "device_state": device_state,
        "agents": agent_snaps,
        "events": list(events)[-TELEMETRY_EVENT_CAP:],
        "commands_ack": [],
    }
