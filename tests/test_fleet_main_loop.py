import json, os, tempfile, unittest
from unittest import mock
import fleet_daemon as fd


class StubProc:
    """Stand-in for AgentProc: no real processes, fully scripted."""

    def __init__(self, name, alive=True):
        self.name = name
        self._alive = alive
        self.pid = 4242 if alive else None
        self.log_path = os.devnull
        self.failure_times = []
        self.wakes = 0
        self.spawns = 0

    def alive(self):
        return self._alive

    def signal_wake(self):
        self.wakes += 1

    def spawn(self):
        self.spawns += 1
        self._alive = True
        self.pid = 4243


def make_cfg(names):
    return {"fleet": {"device": "testbox"},
            "agents": {n: {"token_file": "/nonexistent-token.json"}
                       for n in names}}


class DaemonTickTest(unittest.TestCase):
    def setUp(self):
        self.state_file = os.path.join(tempfile.mkdtemp(), "fleet-state.json")

    def test_suspend_emits_one_resumed_event_and_nudges_alive_children(self):
        agents = {"a": StubProc("a"), "b": StubProc("b")}
        ctx = fd.new_ctx(make_cfg(agents), agents, token=None,
                         state_file=self.state_file,
                         mono_now=1000.0, wall_now=5000.0)
        # Tick 1: wall clock jumped 1h ahead of monotonic => host slept.
        body = fd.daemon_tick(ctx, mono_now=1010.0, wall_now=5010.0 + 3600)
        self.assertIsNone(body)                     # telemetry is every 2nd tick
        self.assertEqual(agents["a"].wakes, 1)      # all alive children nudged
        self.assertEqual(agents["b"].wakes, 1)
        # Tick 2 (30s mark): the telemetry body carries EXACTLY ONE
        # suspend_resumed event for the cycle.
        body = fd.daemon_tick(ctx, mono_now=1025.0, wall_now=8625.0)
        resumed = [e for e in body["events"] if e["kind"] == "suspend_resumed"]
        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed[0]["for_s"], 3600)
        self.assertEqual(resumed[0]["nudged"], ["a", "b"])
        self.assertEqual(agents["a"].wakes, 1)      # nudged once, not per tick
        # State file mirrors the body; and the NEXT body repeats nothing.
        self.assertEqual(json.load(open(self.state_file)), body)
        fd.daemon_tick(ctx, mono_now=1040.0, wall_now=8640.0)
        body = fd.daemon_tick(ctx, mono_now=1055.0, wall_now=8655.0)
        self.assertEqual([e for e in body["events"]
                          if e["kind"] == "suspend_resumed"], [])

    def test_fleet_id_is_always_derived_device_plus_nodename(self):
        # fleet_id is a build_telemetry PARAMETER, not a fleet.toml key
        # (Task 1 schema has no [fleet] fleet_id) — a stray config key
        # must not override the derived device-nodename identity.
        cfg = make_cfg(["a"])
        cfg["fleet"]["fleet_id"] = "rogue-config-override"
        ctx = fd.new_ctx(cfg, {}, mono_now=1000.0, wall_now=5000.0)
        self.assertEqual(ctx["fleet_id"], f"testbox-{os.uname().nodename}")

    def test_dead_child_respawned_only_after_backoff_delay(self):
        agents = {"a": StubProc("a", alive=False)}
        ctx = fd.new_ctx(make_cfg(agents), agents, token=None,
                         state_file=self.state_file,
                         mono_now=1000.0, wall_now=5000.0)
        # First tick notices the death: failure recorded, no respawn yet.
        fd.daemon_tick(ctx, mono_now=1001.0, wall_now=5001.0)
        self.assertEqual(agents["a"].spawns, 0)
        self.assertEqual(len(agents["a"].failure_times), 1)
        # Still inside respawn_delay(1) == 2s: no respawn.
        fd.daemon_tick(ctx, mono_now=1002.0, wall_now=5002.0)
        self.assertEqual(agents["a"].spawns, 0)
        # Backoff elapsed: exactly one respawn.
        fd.daemon_tick(ctx, mono_now=1003.5, wall_now=5003.5)
        self.assertEqual(agents["a"].spawns, 1)
        self.assertTrue(agents["a"].alive())


class MainWiringTest(unittest.TestCase):
    def test_main_wires_child_logs_under_fleet_logs(self):
        # Plan Task 7: child capture logs live at ~/.ax/fleet/logs/<agent>.log.
        home = tempfile.mkdtemp()
        log_paths = {}

        class FakeProc:
            def __init__(self, name, argv, env, log_path):
                self.name, self.log_path, self.pid = name, log_path, 4242
                log_paths[name] = log_path

            def spawn(self):
                pass

        cfg = make_cfg(["a"])
        with mock.patch.dict(os.environ, {"HOME": home}), \
             mock.patch.object(fd, "AgentProc", FakeProc), \
             mock.patch.object(fd, "load_fleet_config", lambda path: cfg), \
             mock.patch.object(fd, "_acquire_singleton_lock", lambda: None), \
             mock.patch.object(fd, "_load_device_token", lambda: None), \
             mock.patch.object(fd.time, "sleep",
                               side_effect=KeyboardInterrupt), \
             mock.patch.object(fd.sys, "argv", ["fleet_daemon.py"]):
            with self.assertRaises(KeyboardInterrupt):
                fd.main()
        expected_dir = os.path.join(home, ".ax", "fleet", "logs")
        self.assertEqual(log_paths["a"], os.path.join(expected_dir, "a.log"))
        self.assertTrue(os.path.isdir(expected_dir))
