# Fleet Daemon Phase 1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build `fleet_daemon.py` — a per-device supervisor that spawns/respawns agent listeners, runs the suspend/receipt/process/token watchdogs, and assembles §7-schema telemetry — plus the listener's SIGUSR1 wake handler.

**Architecture:** Single stdlib-only module `fleet_daemon.py` (matches repo style: one file per unit, like `ax_presence_listener.py` / `monitor_core.py`). All watchdog logic is pure "tick" functions over snapshot dicts (same pattern as `_proactive_tick` from PR #32) so everything unit-tests without sleeping or spawning. The loop shells are thin. The daemon NEVER writes child token files (review-locked invariant) — it reads TTLs and bounces; children refresh themselves.

**Tech Stack:** Python stdlib only (tomllib with <3.11 fallback, subprocess, threading, urllib, json, fcntl). unittest for tests (repo idiom: `tests/test_*.py`, `import ax_presence_listener as listener` style imports).

**Spec:** `docs/plans/2026-06-12-fleet-daemon-design.md` (reviewed head fd03c43). Phase 1 scope only — NO CLI/TUI (phase 1.5), NO catch-up triage (phase 3), NO command execution (phase 4).

**Review-locked invariants (violating any of these fails review):**
1. Daemon never refreshes/reuses/rewrites child token files — read-only TTL inspection + bounce only.
2. One process per agent; child owns its token exclusively.
3. Never set `AX_SPACE_ID` in child env (the 80588cba space-binding bug).
4. Telemetry POST is best-effort: 404/network errors must not break the daemon (endpoint ships later in nyx's lane).
5. Suspend wake emits ONE summary event, never per-failure spam.

---

### Task 1: Config loader — `fleet.toml`

**Files:**
- Create: `fleet_daemon.py`
- Test: `tests/test_fleet_config.py`

**Step 1: Write the failing test**

```python
import os, tempfile, unittest
import fleet_daemon as fd

SAMPLE = """
[fleet]
device = "laptop"
sponsor = "@madtank"

[agents.claude_prime]
token_file = "~/.ax/claude_prime-listener.json"
platform = "ax"
catchup = "ask"

[agents.canvas]
token_file = "~/.ax/canvas-listener.json"
"""

class FleetConfigTest(unittest.TestCase):
    def _write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        f.write(text); f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_loads_fleet_and_agents(self):
        cfg = fd.load_fleet_config(self._write(SAMPLE))
        self.assertEqual(cfg["fleet"]["device"], "laptop")
        self.assertEqual(cfg["fleet"]["sponsor"], "@madtank")
        self.assertIn("claude_prime", cfg["agents"])
        self.assertIn("canvas", cfg["agents"])

    def test_token_file_is_tilde_expanded(self):
        cfg = fd.load_fleet_config(self._write(SAMPLE))
        self.assertTrue(os.path.isabs(cfg["agents"]["canvas"]["token_file"]))

    def test_defaults_applied(self):
        cfg = fd.load_fleet_config(self._write(SAMPLE))
        self.assertEqual(cfg["agents"]["canvas"]["platform"], "ax")
        self.assertEqual(cfg["agents"]["canvas"]["catchup"], "ask")

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            fd.load_fleet_config("/nonexistent/fleet.toml")

    def test_no_agents_raises(self):
        with self.assertRaises(ValueError):
            fd.load_fleet_config(self._write("[fleet]\ndevice = \"x\"\n"))
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fleet_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fleet_daemon'`

**Step 3: Write minimal implementation**

Create `fleet_daemon.py`:

```python
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
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_fleet_config.py -v`
Expected: 5 PASS. Also run the fallback explicitly: temporarily assert `fd._parse_toml_minimal(SAMPLE)["agents"]["canvas"]["token_file"]` parses — add one test for `_parse_toml_minimal` directly:

```python
    def test_minimal_parser_matches_subset(self):
        d = fd._parse_toml_minimal(SAMPLE)
        self.assertEqual(d["fleet"]["device"], "laptop")
        self.assertEqual(d["agents"]["claude_prime"]["catchup"], "ask")
```

**Step 5: Commit**

```bash
git add fleet_daemon.py tests/test_fleet_config.py
git commit -m "feat(fleet): fleet.toml config loader with <3.11 fallback parser"
```

---

### Task 2: Child env construction

**Files:**
- Modify: `fleet_daemon.py`
- Test: `tests/test_fleet_child_env.py`

**Step 1: Write the failing test**

```python
import unittest
import fleet_daemon as fd

CFG = {"fleet": {"device": "laptop", "sponsor": "@madtank"},
       "agents": {"claude_prime": {"token_file": "/abs/tok.json",
                                   "platform": "ax", "catchup": "ask"}}}

class ChildEnvTest(unittest.TestCase):
    def test_identity_env_set(self):
        env = fd.child_env("claude_prime", CFG)
        self.assertEqual(env["AX_AGENT_HANDLE"], "claude_prime")
        self.assertEqual(env["AX_TOKEN_FILE"], "/abs/tok.json")
        self.assertEqual(env["AX_SPONSOR"], "@madtank")

    def test_never_sets_space_id(self):
        # the 80588cba space-binding bug: space must be derived by the child
        env = fd.child_env("claude_prime", CFG)
        self.assertNotIn("AX_SPACE_ID", env)

    def test_inherits_parent_env_without_leaking_other_agents(self):
        env = fd.child_env("claude_prime", CFG)
        self.assertIn("PATH", env)
```

**Step 2: Run to verify failure** — `python3 -m pytest tests/test_fleet_child_env.py -v` → FAIL (`child_env` missing).

**Step 3: Implementation**

```python
def child_env(name, cfg):
    """Build the child listener's environment. NEVER sets AX_SPACE_ID —
    the child derives its space from its agent record (bug 80588cba)."""
    env = dict(os.environ)
    env.pop("AX_SPACE_ID", None)
    a = cfg["agents"][name]
    env.update({
        "AX_AGENT_HANDLE": name,
        "AX_TOKEN_FILE": a["token_file"],
        "AX_SPONSOR": cfg["fleet"].get("sponsor", "@your-sponsor"),
    })
    return env
```

**Step 4: Verify pass**, **Step 5: Commit** `feat(fleet): child env construction (no AX_SPACE_ID, ever)`

---

### Task 3: Respawn backoff + crashloop policy

**Files:** Modify `fleet_daemon.py`; Test `tests/test_fleet_backoff.py`

**Step 1: Failing test**

```python
import unittest
import fleet_daemon as fd

class BackoffTest(unittest.TestCase):
    def test_delay_grows_exponentially_and_caps(self):
        delays = [fd.respawn_delay(n) for n in range(8)]
        self.assertEqual(delays[0], 1)
        self.assertTrue(all(b >= a for a, b in zip(delays, delays[1:])))
        self.assertLessEqual(max(delays), 300)

    def test_crashloop_when_5_failures_inside_10_minutes(self):
        now = 10_000
        recent = [now - 60 * i for i in range(5)]
        self.assertTrue(fd.is_crashloop(recent, now))

    def test_not_crashloop_when_failures_are_old(self):
        now = 10_000
        old = [now - 3600 * i for i in range(1, 6)]
        self.assertFalse(fd.is_crashloop(old, now))
```

**Step 3: Implementation**

```python
RESPAWN_CAP_S = 300
CRASHLOOP_N = 5
CRASHLOOP_WINDOW_S = 600


def respawn_delay(failures):
    return min(RESPAWN_CAP_S, 2 ** failures) if failures else 1


def is_crashloop(failure_times, now):
    recent = [t for t in failure_times if now - t <= CRASHLOOP_WINDOW_S]
    return len(recent) >= CRASHLOOP_N
```

**Commit:** `feat(fleet): respawn backoff + crashloop policy`

---

### Task 4: Suspend detection tick (pure)

**Files:** Modify `fleet_daemon.py`; Test `tests/test_fleet_suspend.py`

**Step 1: Failing test**

```python
import unittest
import fleet_daemon as fd

class SuspendTickTest(unittest.TestCase):
    def test_no_event_when_clocks_agree(self):
        st = {"mono": 100.0, "wall": 1000.0}
        self.assertIsNone(fd.suspend_tick(st, mono_now=115.0, wall_now=1015.0))

    def test_event_when_wall_jumps_past_monotonic(self):
        st = {"mono": 100.0, "wall": 1000.0}
        ev = fd.suspend_tick(st, mono_now=115.0, wall_now=42_000.0)
        self.assertEqual(ev["kind"], "suspend_detected")
        self.assertAlmostEqual(ev["for_s"], 41_000 - 15, delta=1)

    def test_tick_updates_state_for_next_round(self):
        st = {"mono": 100.0, "wall": 1000.0}
        fd.suspend_tick(st, mono_now=115.0, wall_now=1015.0)
        self.assertEqual(st["mono"], 115.0)
        self.assertEqual(st["wall"], 1015.0)
```

**Step 3: Implementation**

```python
SUSPEND_DRIFT_S = 30


def suspend_tick(state, mono_now, wall_now):
    """Detect host suspend: wall-clock advanced while the monotonic clock
    (which pauses during macOS suspend) did not. Mutates state for the
    next tick; returns a suspend event dict or None."""
    drift = (wall_now - state["wall"]) - (mono_now - state["mono"])
    state["mono"], state["wall"] = mono_now, wall_now
    if drift > SUSPEND_DRIFT_S:
        return {"kind": "suspend_detected", "for_s": round(drift)}
    return None
```

**Commit:** `feat(fleet): suspend detection tick (monotonic vs wall drift)`

---

### Task 5: Read-only token TTL snapshot

**Files:** Modify `fleet_daemon.py`; Test `tests/test_fleet_token_snapshot.py`

**Step 1: Failing test**

```python
import json, os, tempfile, unittest
import fleet_daemon as fd

class TokenSnapshotTest(unittest.TestCase):
    def _tok(self, expires_at):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"access_token": "x", "refresh_token": "r",
                   "expires_at": expires_at}, f)
        f.close(); self.addCleanup(os.unlink, f.name)
        return f.name

    def test_ttl_positive_for_future_expiry(self):
        p = self._tok(2_000)
        self.assertEqual(fd.token_ttl(p, now=1_400), 600)

    def test_ttl_negative_for_expired(self):
        p = self._tok(1_000)
        self.assertEqual(fd.token_ttl(p, now=1_400), -400)

    def test_never_modifies_the_file(self):
        p = self._tok(2_000)
        before = open(p).read()
        fd.token_ttl(p, now=1_400)
        self.assertEqual(open(p).read(), before)

    def test_unreadable_file_returns_none(self):
        self.assertIsNone(fd.token_ttl("/nonexistent.json", now=0))
```

**Step 3: Implementation**

```python
def token_ttl(token_file, now):
    """READ-ONLY token inspection. The daemon must never refresh, reuse,
    or rewrite a child's rotating token file (review-locked invariant);
    the child listener is the sole refresher."""
    try:
        with open(token_file) as f:
            return int(json.load(f).get("expires_at", 0) - now)
    except Exception:
        return None
```

**Commit:** `feat(fleet): read-only token TTL snapshot`

---

### Task 6: Listener SIGUSR1 wake handler

**Files:**
- Modify: `ax_presence_listener.py` (signal install near line ~724 where SIGTERM/INT/HUP handlers live; flag consumption in `stream()`'s reconnect loop and `_presence_beat`)
- Test: `tests/test_listener_wake_signal.py`

The review removed the *assumption* this existed; this task builds it. Contract: SIGUSR1 ⇒ child re-verifies its token (its own refresh path) and forces an SSE reconnect. Handler only sets a flag (signal-safety); loops consume it.

**Step 1: Failing test**

```python
import signal, unittest
from unittest import mock
import ax_presence_listener as listener

class WakeSignalTest(unittest.TestCase):
    def test_handler_sets_wake_flag(self):
        listener._wake_requested = False
        listener._handle_wake_signal(signal.SIGUSR1, None)
        self.assertTrue(listener._wake_requested)

    def test_consume_returns_true_once_then_clears(self):
        listener._wake_requested = True
        self.assertTrue(listener._consume_wake_request())
        self.assertFalse(listener._consume_wake_request())

    def test_presence_beat_refreshes_on_wake_flag(self):
        beats = []
        def fake_urlopen(req, timeout=None):
            beats.append(dict(req.header_items()))
            class R:  # minimal response
                def read(self): return b"{}"
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return R()
        tok = {"access_token": "old", "refresh_token": "r", "client_id": "c",
               "expires_at": 9e9}
        fresh = dict(tok, access_token="fresh")
        listener._wake_requested = True
        with mock.patch.object(listener, "load_tok", return_value=tok), \
             mock.patch.object(listener, "refresh", return_value=fresh) as rf, \
             mock.patch.object(listener.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            listener._presence_beat()
        rf.assert_called_once()   # wake flag forces refresh even with valid TTL
        self.assertEqual(beats[-1].get("Authorization"), "Bearer fresh")
```

**Step 2: Verify failure** — AttributeError `_handle_wake_signal`.

**Step 3: Implementation** (in `ax_presence_listener.py`)

```python
_wake_requested = False


def _handle_wake_signal(signum, frame):
    """SIGUSR1 from the fleet daemon: host woke from suspend. Only set a
    flag here (signal-handler safety); loops consume it."""
    globals()["_wake_requested"] = True


def _consume_wake_request():
    if _wake_requested:
        globals()["_wake_requested"] = False
        return True
    return False
```

In `_presence_beat()`, before the first `_post`: if `_consume_wake_request()`, call `refresh()` and post with that token instead of `current_access_token()`. In `stream()`'s read loop, check `_wake_requested` on each iteration/timeout and break to the reconnect path when set. In `main()`, alongside the existing SIGTERM/INT/HUP installs: `signal.signal(signal.SIGUSR1, _handle_wake_signal)`.

**Step 4: Verify** new test passes AND the full suite stays green (`python3 -m pytest tests/ -q` — the Task-6 beat change must not break `test_listener_token_resilience.py`).

**Step 5: Commit** `feat(listener): SIGUSR1 wake handler — child-owned refresh + reconnect`

---

### Task 7: Child log capture with timestamps + receipt scan

**Files:** Modify `fleet_daemon.py`; Test `tests/test_fleet_receipt.py`

Daemon captures each child's stdout/stderr to `~/.ax/fleet/logs/<agent>.log`, prefixing each line with epoch seconds (children don't timestamp NOTIFY lines). Receipt age = newest `NOTIFY ` line.

**Step 1: Failing test**

```python
import os, tempfile, unittest
import fleet_daemon as fd

class ReceiptScanTest(unittest.TestCase):
    def _log(self, lines):
        f = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
        f.write("\n".join(lines) + "\n"); f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_age_of_newest_notify_line(self):
        p = self._log(["1000 [status] SSE connected",
                       "1500 NOTIFY @x mention ...",
                       "1700 NOTIFY @x mention ..."])
        self.assertEqual(fd.last_receipt_age(p, now=2000), 300)

    def test_none_when_no_notify_lines(self):
        p = self._log(["1000 [status] SSE connected"])
        self.assertIsNone(fd.last_receipt_age(p, now=2000))

    def test_stamp_line_format(self):
        self.assertEqual(fd.stamp_line("NOTIFY hi", now=1234), "1234 NOTIFY hi")
```

**Step 3: Implementation**

```python
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
```

(The pump thread that applies `stamp_line` to child stdout is wired in Task 10 with the spawn code.)

**Commit:** `feat(fleet): timestamped child log capture + receipt-age scan`

---

### Task 8: Verdict classification (pure)

**Files:** Modify `fleet_daemon.py`; Test `tests/test_fleet_verdict.py`

Snapshot shape: `{"alive": bool, "crashloop": bool, "token_ttl_s": int|None, "receipt_age_s": int|None, "sse_connected": bool|None, "disabled": bool}`.

**Step 1: Failing test**

```python
import unittest
import fleet_daemon as fd

def snap(**kw):
    base = dict(alive=True, crashloop=False, token_ttl_s=600,
                receipt_age_s=60, sse_connected=True, disabled=False)
    base.update(kw)
    return base

class VerdictTest(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(fd.verdict(snap()), "OK")

    def test_disabled_wins_over_everything(self):
        self.assertEqual(fd.verdict(snap(disabled=True, alive=False)), "DISABLED")

    def test_down_when_not_alive(self):
        self.assertEqual(fd.verdict(snap(alive=False)), "DOWN")

    def test_crashloop(self):
        self.assertEqual(fd.verdict(snap(alive=False, crashloop=True)), "CRASHLOOP")

    def test_token_wedge(self):
        self.assertEqual(fd.verdict(snap(token_ttl_s=-7200)), "TOKEN")

    def test_deaf_when_connected_but_no_receipt(self):
        self.assertEqual(fd.verdict(snap(receipt_age_s=3600)), "DEAF")

    def test_quiet_when_no_receipt_data_yet(self):
        self.assertEqual(fd.verdict(snap(receipt_age_s=None)), "QUIET")
```

**Step 3: Implementation**

```python
TOKEN_WEDGE_S = -600          # expired this long with child alive = wedged
DEAF_THRESHOLD_S = 1800       # uniform start; tune from soak data (spec §11.4)


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
```

**Commit:** `feat(fleet): verdict classification (fleet-doctor vocabulary, in-process)`

---

### Task 9: Telemetry assembly (§7 schema) + golden test

**Files:** Modify `fleet_daemon.py`; Test `tests/test_fleet_telemetry.py`; Create `tests/fixtures/telemetry_golden.json`

**Step 1: Failing test**

```python
import json, unittest
from pathlib import Path
import fleet_daemon as fd

GOLDEN = Path(__file__).parent / "fixtures" / "telemetry_golden.json"

class TelemetryBuildTest(unittest.TestCase):
    def test_matches_golden_contract(self):
        body = fd.build_telemetry(
            fleet={"device": "laptop"}, fleet_id="jacob-laptop-x9f2",
            daemon_version="0.2.0", seq=48211,
            sent_at="2026-06-12T16:04:31Z",
            device_state={"status": "active", "uptime_s": 184211,
                          "last_suspend": {"at": "2026-06-12T03:41:00Z", "for_s": 41020},
                          "host": {"os": "darwin", "load": 0.4}},
            agent_snaps={"claude_prime": {
                "verdict": "OK", "pid": 4411, "sse_connected": True,
                "last_receipt_age_s": 38, "token_ttl_s": 660,
                "mentions_seen": 142, "replies_sent": 131, "currently_401": False}},
            events=[{"kind": "suspend_resumed", "for_s": 41020, "tokens_refreshed": 3}])
        self.assertEqual(body, json.loads(GOLDEN.read_text()))

    def test_commands_ack_reserved_and_empty(self):
        body = fd.build_telemetry(fleet={"device": "x"}, fleet_id="f",
                                  daemon_version="0.2.0", seq=1, sent_at="t",
                                  device_state={}, agent_snaps={}, events=[])
        self.assertEqual(body["commands_ack"], [])

    def test_events_capped_at_50(self):
        body = fd.build_telemetry(fleet={"device": "x"}, fleet_id="f",
                                  daemon_version="0.2.0", seq=1, sent_at="t",
                                  device_state={}, agent_snaps={},
                                  events=[{"kind": "e", "n": i} for i in range(80)])
        self.assertEqual(len(body["events"]), 50)
        self.assertEqual(body["events"][-1]["n"], 79)  # newest kept
```

The golden file is the §7 example verbatim (write it from the spec). **This file is the shared contract fixture — nyx's backend tests must consume the identical file** (spec §12).

**Step 3: Implementation**

```python
TELEMETRY_EVENT_CAP = 50


def build_telemetry(fleet, fleet_id, daemon_version, seq, sent_at,
                    device_state, agent_snaps, events):
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
```

**Commit:** `feat(fleet): telemetry assembly matching frozen §7 contract (golden fixture)`

---

### Task 10: Spawn/supervise loop + wake fanout (thin shell)

**Files:** Modify `fleet_daemon.py`; Test `tests/test_fleet_supervise.py`

The only task that touches real processes — test with a stub child (`python3 -c "import time; time.sleep(60)"`), not a real listener.

**Step 1: Failing test**

```python
import os, signal, sys, time, unittest
import fleet_daemon as fd

STUB = [sys.executable, "-c", "import time; time.sleep(60)"]

class SuperviseTest(unittest.TestCase):
    def test_spawn_records_pid_and_child_runs(self):
        ag = fd.AgentProc("stub", STUB, env=dict(os.environ), log_path=os.devnull)
        ag.spawn()
        self.addCleanup(ag.terminate)
        self.assertTrue(ag.alive())
        self.assertGreater(ag.pid, 0)

    def test_terminate_then_not_alive(self):
        ag = fd.AgentProc("stub", STUB, env=dict(os.environ), log_path=os.devnull)
        ag.spawn(); ag.terminate()
        deadline = time.time() + 5
        while ag.alive() and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(ag.alive())

    def test_wake_fanout_signals_alive_children(self):
        ag = fd.AgentProc("stub", STUB, env=dict(os.environ), log_path=os.devnull)
        ag.spawn(); self.addCleanup(ag.terminate)
        sent = fd.wake_fanout([ag])
        self.assertEqual(sent, ["stub"])   # SIGUSR1 delivered without killing it
        self.assertTrue(ag.alive())
```

**Step 3: Implementation**

```python
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
```

Listener argv builder: `listener_argv() = [sys.executable, "-u", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ax_presence_listener.py")]`.

**Commit:** `feat(fleet): child supervision + post-suspend wake fanout`

---

### Task 11: Telemetry POST (best-effort) + state file

**Files:** Modify `fleet_daemon.py`; Test `tests/test_fleet_post.py`

**Step 1: Failing test**

```python
import json, os, tempfile, unittest, urllib.error
from unittest import mock
import fleet_daemon as fd

class TelemetryPostTest(unittest.TestCase):
    def test_404_is_swallowed(self):
        err = urllib.error.HTTPError("u", 404, "nf", None, None)
        with mock.patch.object(fd.urllib.request, "urlopen", side_effect=err):
            fd.post_telemetry({"seq": 1}, base="https://x", token="t")  # must not raise

    def test_skips_when_no_token(self):
        with mock.patch.object(fd.urllib.request, "urlopen") as up:
            fd.post_telemetry({"seq": 1}, base="https://x", token=None)
            up.assert_not_called()

    def test_state_file_written_0600(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "fleet-state.json")
        fd.write_state_file(p, {"seq": 7})
        self.assertEqual(json.load(open(p))["seq"], 7)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
```

**Step 3: Implementation**

```python
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
```

Device token: read once from `AX_FLEET_TOKEN_FILE` env if set (static bearer for now; mechanism is nyx's open question §11.3). No token ⇒ local-only mode, log once at startup.

**Commit:** `feat(fleet): best-effort telemetry POST + local state mirror`

---

### Task 12: Main wiring — singleton, loops, clean shutdown

**Files:** Modify `fleet_daemon.py`; Test `tests/test_fleet_main_loop.py`

Wire one `daemon_tick(ctx)` function (testable) called by `main()`'s loop every 15s: suspend_tick → on suspend event: wake_fanout + grace window + queue ONE `suspend_resumed` event; respawn dead non-crashloop children (respecting `respawn_delay`); recompute snapshots (token_ttl, last_receipt_age, alive) → verdicts; every 2nd tick (30s): build_telemetry → post_telemetry + write_state_file. Test `daemon_tick` with stub AgentProcs and injected clocks — assert: a tick after a simulated suspend produces exactly one `suspend_resumed` event in the next telemetry body and nudges all alive children; a dead child gets respawned only after its backoff delay. `main()`: parse `--config` (default `~/.ax/fleet.toml`), singleton lock on `~/.ax/fleet-daemon.lock` (reuse the listener's `_acquire_singleton_lock` pattern at `ax_presence_listener.py:860`), install SIGTERM → terminate all children → exit. Keep `main()` under ~40 lines; everything it calls is already tested.

**Commit:** `feat(fleet): daemon main loop — singleton, tick wiring, clean shutdown`

---

### Task 13: Full-suite + live smoke

**Step 1:** `python3 -m pytest tests/ -q` — everything green (was 74 before this plan; expect ~100+).
**Step 2:** Write `~/.ax/fleet.toml` for the laptop (claude_prime only, since canvas's token may not exist locally — check `ls ~/.ax/*-listener.json` first).
**Step 3:** Stop the session Monitor running the bare listener; relaunch under `python3 -u fleet_daemon.py --config ~/.ax/fleet.toml` via Monitor (same grep filter). Confirm: child spawns, SSE connects, `~/.ax/fleet-state.json` appears with verdict OK, telemetry runs in local-only mode (no device token yet).
**Step 4:** Live suspend test: `pmset sleepnow` is too disruptive mid-session — instead send the daemon's pid a simulated suspend (temporarily set `SUSPEND_DRIFT_S=−1`? NO — use the unit-tested path; the REAL test is the overnight lid-close, which is the soak).
**Step 5:** Commit any smoke fixes; open PR referencing the spec: "Phase 1 of docs/plans/2026-06-12-fleet-daemon-design.md".

**Soak exit criteria (spec §12):** ≥3 real laptop suspend/resume cycles, zero manual intervention, zero alert storms, exactly one `suspend_resumed` event per cycle in `~/.ax/fleet-state.json`.
