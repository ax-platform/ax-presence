#!/usr/bin/env python3
"""ax-presence fleet daemon — per-device supervisor for agent listeners.

Spec: docs/plans/2026-06-12-fleet-daemon-design.md (Phase 1).
Invariants: never touch child token files (read-only TTL checks only);
one process per agent; never set AX_SPACE_ID in child env.
"""
import argparse, json, os, shlex, sys, time, threading, subprocess, signal, fcntl
import traceback
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
DEAF_THRESHOLD_S = 5400       # tuned from soak cycle 1 (spec §11.4): 1800s bounced
                              # healthy children twice on normal 30-60min evening
                              # mention gaps; quiet-vs-deaf can't be distinguished
                              # locally until phase-2 server-side traffic cross-check

TELEMETRY_EVENT_CAP = 50

DAEMON_VERSION = "0.1.0"
TICK_S = 15                   # daemon_tick cadence (spec §3 watchdog table)
TELEMETRY_EVERY = 2           # every 2nd tick = 30s telemetry beat (spec §7)
RESUME_GRACE_S = 60           # post-resume alert grace window (spec §3)

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
            v = v.strip()
            if v == "true":          # bare TOML booleans must become Python
                v = True             # bools — the string "false" is truthy,
            elif v == "false":       # which read every agent as disabled
                v = False
            else:
                v = v.strip('"')
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
        if a["platform"] != "ax":
            # Phase 1 is single-platform: fail loudly at load rather than
            # silently spawning the ax listener for some other platform.
            raise ValueError(
                f"{path}: [agents.{name}] platform = {a['platform']!r} — "
                "only \"ax\" is supported in Phase 1")
        a["token_file"] = os.path.expanduser(a["token_file"])
    return {"fleet": data.get("fleet", {}), "agents": agents}


def heartbeat_path(name, a):
    """Where agent `name`'s listener writes its heartbeat signal file.
    The daemon injects this into the child env AND reads it for telemetry,
    so child and daemon provably agree on the path (spec §9: the daemon
    talks to children via process lifecycle, signal files, and logs)."""
    return os.path.expanduser(
        a.get("heartbeat_file", f"~/.ax/{name}-listener-heartbeat"))


def signal_path(name, a):
    """Where agent `name`'s listener writes its rich per-agent signal dict
    (connected/currently_401/mentions_seen/replies_sent/...). Must derive
    the path EXACTLY like the listener's hardcoded write
    (ax_presence_listener.py heartbeat_loop: ~/.ax/{AGENT_HANDLE}-signal.json)
    or the daemon reads a file nobody writes. Per-agent `signal_file`
    overrides, mirroring heartbeat_file."""
    return os.path.expanduser(a.get("signal_file", f"~/.ax/{name}-signal.json"))


def read_heartbeat(path):
    """READ-ONLY view of a child-written signal file — the listener-owned
    fields the §7 telemetry contract carries per agent. The REAL listener
    writes a BARE INT epoch to the heartbeat file (ax_presence_listener.py
    heartbeat_loop: f.write(str(int(time.time())))) and the rich dict to the
    signal file, so ANY non-dict content (bare int, missing file, garbage,
    child never started, non-ax platform) => all-None fields, never an error
    — a malformed child file must not be able to kill the daemon."""
    try:
        with open(path) as f:
            hb = json.load(f)
    except Exception:
        hb = None
    if not isinstance(hb, dict):
        hb = {}
    return {"sse_connected": hb.get("connected"),
            "mentions_seen": hb.get("mentions_seen"),
            "replies_sent": hb.get("replies_sent"),
            "currently_401": hb.get("currently_401")}


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
        "AX_HEARTBEAT_FILE": heartbeat_path(name, a),
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


def post_telemetry(body, base, token):
    """Best-effort: the endpoint ships later (nyx's lane); 404s and network
    errors are harmless no-ops, same pattern as the listener's SIGNAL_URL."""
    if not token:
        return
    try:
        urllib.request.urlopen(urllib.request.Request(
            base + "/api/v1/fleet/telemetry", data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + token,
                     "Content-Type": "application/json"}), timeout=10)
    except Exception:
        pass


def write_state_file(path, state):
    """Local mirror of the latest telemetry body — what `fleet status` /
    `fleet top` (phase 1.5) will read."""
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    fd_ = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd_, "w") as f:
        json.dump(state, f, indent=2)


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


def new_ctx(cfg, agents, token=None, state_file=None, base=None,
            mono_now=None, wall_now=None):
    """Mutable per-daemon state threaded through daemon_tick."""
    fleet = cfg["fleet"]
    return {
        "cfg": cfg, "agents": agents, "token": token,
        "base": base or os.environ.get("AX_BASE", "https://paxai.app"),
        "state_file": state_file or os.path.expanduser("~/.ax/fleet-state.json"),
        "fleet_id": f"{fleet.get('device', 'unknown')}-{os.uname().nodename}",
        "suspend": {"mono": time.monotonic() if mono_now is None else mono_now,
                    "wall": time.time() if wall_now is None else wall_now},
        "mono_start": time.monotonic() if mono_now is None else mono_now,
        "tick": 0, "seq": 0, "grace_until": 0.0, "events": [],
        "last_suspend": None,
        "episodes": {},   # per-agent in-flight verdict episodes (one
                          # bounce/alert per episode, cleared on healthy)
    }


def alert_sponsor(fleet, msg):
    """CRASHLOOP alert hook: run the optional [fleet] alert_cmd with the
    message appended as the last argv. Platform-agnostic by design (spec §9
    — never imports listener/ax code); unset => log only."""
    cmd = fleet.get("alert_cmd")
    if not cmd:
        print(f"[fleet] ALERT (no alert_cmd configured): {msg}", flush=True)
        return
    try:
        subprocess.run(shlex.split(cmd) + [msg], timeout=30, check=False)
    except Exception as e:
        print(f"[fleet] alert_cmd failed: {e!r} — message was: {msg}",
              flush=True)


def _host_load():
    """1-minute load average, or None where unavailable."""
    try:
        return round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        return None


def daemon_tick(ctx, mono_now, wall_now):
    """One 15s supervision pass — pure wiring of the already-tested pieces
    (same injectable-clock tick design as suspend_tick). Returns the
    telemetry body on sending ticks, else None."""
    cfg, agents = ctx["cfg"], ctx["agents"]
    ctx["tick"] += 1

    # Suspend -> nudge every alive child once, open the alert grace window,
    # queue exactly ONE suspend_resumed event for the next telemetry beat.
    ev = suspend_tick(ctx["suspend"], mono_now, wall_now)
    if ev:
        ctx["grace_until"] = wall_now + RESUME_GRACE_S
        ctx["last_suspend"] = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                time.gmtime(wall_now - ev["for_s"])),
            "for_s": ev["for_s"]}
        ctx["events"].append({"kind": "suspend_resumed", "for_s": ev["for_s"],
                              "nudged": wake_fanout(list(agents.values()))})
    in_grace = wall_now < ctx["grace_until"]

    # Respawn dead, non-crashloop children after their backoff delay.
    # Token files stay untouched — the respawned child refreshes its own.
    for name, ag in agents.items():
        if cfg["agents"][name].get("disabled"):
            continue   # disabled: never respawned, never counted as DOWN
        if ag.alive():
            ag.next_spawn_at = None
            continue
        if is_crashloop(ag.failure_times, wall_now):
            continue
        if getattr(ag, "next_spawn_at", None) is None:
            ag.failure_times.append(wall_now)
            ag.next_spawn_at = wall_now + respawn_delay(len(ag.failure_times))
        if wall_now >= ag.next_spawn_at:
            ag.spawn()
            ag.next_spawn_at = None

    # Fresh snapshots (token_ttl read-only, last_receipt_age, alive) -> verdicts.
    snaps = {}
    for name, ag in agents.items():
        a = cfg["agents"][name]
        s = {"alive": ag.alive(), "disabled": bool(a.get("disabled")),
             "crashloop": is_crashloop(ag.failure_times, wall_now),
             "token_ttl_s": token_ttl(a["token_file"], wall_now),
             "receipt_age_s": last_receipt_age(ag.log_path, wall_now)}
        v = verdict(s)
        if in_grace and v in ("DEAF", "TOKEN"):
            # Stale receipts AND long-expired tokens are expected right after
            # resume (spec §3): the child's SIGUSR1-driven refresh/reconnect
            # needs the grace window to land before either may bounce it.
            v = "OK"

        # Watchdog ACTIONS (spec §3), at most one per agent per episode.
        # An episode flag clears only when the agent is healthy again
        # (OK/QUIET) — the bounce's own DOWN->respawn->still-stale arc never
        # re-triggers. Token files stay untouched: a bounce just terminates;
        # the respawned child runs its own refresh path.
        ep = ctx["episodes"].setdefault(name, set())
        if v in ("OK", "QUIET"):
            ep.clear()
        elif v in ("DEAF", "TOKEN") and v not in ep:
            ep.add(v)
            print(f"[fleet] bounce @{name}: verdict {v} — terminating child; "
                  "respawn-with-backoff restarts it", flush=True)
            ag.terminate()
            ctx["events"].append({"kind": "bounce", "agent": name, "reason": v})
            s["alive"] = False
        elif v == "CRASHLOOP" and v not in ep:
            ep.add(v)
            alert_sponsor(cfg["fleet"],
                          f"[fleet] CRASHLOOP: @{name} exited {CRASHLOOP_N}+ "
                          f"times in {CRASHLOOP_WINDOW_S}s — respawns held, "
                          "manual attention needed")

        # Rich fields live in the SIGNAL file (the real listener's heartbeat
        # file is just a bare-int liveness epoch); legacy dict-format
        # heartbeat files still fill any field the signal file doesn't.
        sig = read_heartbeat(signal_path(name, a))
        legacy = read_heartbeat(heartbeat_path(name, a))
        hb = {k: legacy[k] if sig[k] is None else sig[k] for k in sig}
        snaps[name] = {"verdict": v,
                       "pid": ag.pid if s["alive"] else None,
                       "sse_connected": hb["sse_connected"],
                       "last_receipt_age_s": s["receipt_age_s"],
                       "token_ttl_s": s["token_ttl_s"],
                       "mentions_seen": hb["mentions_seen"],
                       "replies_sent": hb["replies_sent"],
                       "currently_401": hb["currently_401"]}

    if ctx["tick"] % TELEMETRY_EVERY:
        return None
    ctx["seq"] += 1
    body = build_telemetry(
        cfg["fleet"], ctx["fleet_id"], DAEMON_VERSION, ctx["seq"],
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(wall_now)),
        {"status": "active", "uptime_s": int(mono_now - ctx["mono_start"]),
         "last_suspend": ctx["last_suspend"],
         "host": {"os": sys.platform, "load": _host_load()}},
        snaps, ctx["events"])
    post_telemetry(body, ctx["base"], ctx["token"])
    write_state_file(ctx["state_file"], body)
    ctx["events"].clear()   # each event ships exactly once
    return body


_LOCK_FH = None
def _acquire_singleton_lock():
    """One fleet daemon per device — same flock pattern as the listener's
    per-handle lock (ax_presence_listener.py _acquire_singleton_lock):
    flock auto-releases on process death, so a crash frees the lock."""
    global _LOCK_FH
    lock_path = os.path.expanduser("~/.ax/fleet-daemon.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"[fleet] another fleet daemon already holds {lock_path} — "
              "refusing to start a second. Exiting.", flush=True)
        sys.exit(0)
    fh.write(str(os.getpid())); fh.flush()
    _LOCK_FH = fh


def _load_device_token():
    """Static device bearer for telemetry POSTs, read ONCE at startup from
    AX_FLEET_TOKEN_FILE (mechanism is nyx's open question, spec §11.3).
    No token => local-only mode: state file only, zero network I/O."""
    path = os.environ.get("AX_FLEET_TOKEN_FILE")
    try:
        token = open(path).read().strip() if path else None
    except OSError:
        token = None
    if not token:
        print("[fleet] no device token — local-only mode (state file, no "
              "telemetry POSTs)", flush=True)
    return token


def run_one_cycle(ctx):
    """One supervision cycle: sleep TICK_S, then one daemon_tick. A tick
    exception (one malformed child file, one bad read) must NEVER kill the
    daemon — a dead daemon orphans every child — so it is caught and logged
    with its traceback to stderr, and main()'s while-True carries on.
    KeyboardInterrupt/SystemExit (BaseException) still propagate: clean
    shutdown paths are not swallowed."""
    time.sleep(TICK_S)
    try:
        daemon_tick(ctx, time.monotonic(), time.time())
    except Exception:
        print("[fleet] daemon_tick raised — daemon continues:",
              file=sys.stderr, flush=True)
        traceback.print_exc()


def main():
    ap = argparse.ArgumentParser(description="ax-presence fleet daemon")
    ap.add_argument("--config", default=os.path.expanduser("~/.ax/fleet.toml"))
    args = ap.parse_args()
    _acquire_singleton_lock()
    cfg = load_fleet_config(args.config)
    log_dir = os.path.expanduser("~/.ax/fleet/logs")
    os.makedirs(log_dir, mode=0o700, exist_ok=True)
    agents = {}
    for name in cfg["agents"]:
        ag = AgentProc(name, listener_argv(), child_env(name, cfg),
                       os.path.join(log_dir, f"{name}.log"))
        if cfg["agents"][name].get("disabled"):
            # Tracked (reports DISABLED in telemetry) but never spawned.
            print(f"[fleet] @{name} disabled — not spawning", flush=True)
        else:
            ag.spawn()
            print(f"[fleet] spawned @{name} pid {ag.pid}", flush=True)
        agents[name] = ag

    def _shutdown(signum, frame):
        print(f"[fleet] {signal.Signals(signum).name} — terminating children, "
              "exiting", flush=True)
        for ag in agents.values():
            ag.terminate()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)   # Ctrl-C: same clean shutdown

    ctx = new_ctx(cfg, agents, token=_load_device_token())
    while True:
        run_one_cycle(ctx)


if __name__ == "__main__":
    main()
