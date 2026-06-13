"""Watchdog ACTIONS (spec §3): DEAF/TOKEN => bounce the child (terminate;
the respawn-with-backoff path restarts it) once per episode; CRASHLOOP =>
alert the sponsor once per episode via the optional [fleet] alert_cmd hook."""
import contextlib, io, json, os, tempfile, unittest
from unittest import mock
import fleet_daemon as fd


class StubProc:
    def __init__(self, name, log_path, alive=True):
        self.name, self.log_path = name, log_path
        self._alive = alive
        self.pid = 4242 if alive else None
        self.failure_times = []
        self.terminates = 0
        self.spawns = 0

    def alive(self):
        return self._alive

    def signal_wake(self):
        pass

    def spawn(self):
        self.spawns += 1
        self._alive = True
        self.pid = 4243

    def terminate(self):
        self.terminates += 1
        self._alive = False
        self.pid = None


class WatchdogActionsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.dir, "fleet-state.json")
        self.log = os.path.join(self.dir, "a.log")
        self.tok = os.path.join(self.dir, "tok.json")

    def _write_log(self, lines, mode="w"):
        with open(self.log, mode) as f:
            f.write("\n".join(lines) + "\n")

    def _write_token(self, expires_at):
        with open(self.tok, "w") as f:
            json.dump({"expires_at": expires_at}, f)

    def _ctx(self, token_file=None, fleet_extra=None):
        cfg = {"fleet": {"device": "testbox", **(fleet_extra or {})},
               "agents": {"a": {"token_file":
                                token_file or "/nonexistent-token.json"}}}
        self.agents = {"a": StubProc("a", self.log)}
        return fd.new_ctx(cfg, self.agents, token=None,
                          state_file=self.state_file,
                          mono_now=1000.0, wall_now=5000.0)

    def test_deaf_bounces_once_per_episode_with_audit_and_event(self):
        self._write_log(["1000 NOTIFY @a mention"])   # 5500s stale => DEAF
        ctx = self._ctx()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fd.daemon_tick(ctx, mono_now=2500.0, wall_now=6500.0)
        ag = self.agents["a"]
        self.assertEqual(ag.terminates, 1)            # bounced
        self.assertIn("bounce @a", out.getvalue())    # audit line
        # Telemetry beat carries the bounce event exactly once.
        body = fd.daemon_tick(ctx, mono_now=2515.0, wall_now=6515.0)
        self.assertEqual([e for e in body["events"] if e["kind"] == "bounce"],
                         [{"kind": "bounce", "agent": "a", "reason": "DEAF"}])
        # Respawn lands, receipt is STILL stale (same DEAF episode):
        # no second bounce per tick.
        fd.daemon_tick(ctx, mono_now=2530.0, wall_now=6530.0)  # respawns
        self.assertEqual(ag.spawns, 1)
        fd.daemon_tick(ctx, mono_now=2545.0, wall_now=6545.0)
        self.assertEqual(ag.terminates, 1)
        # Fresh receipt ends the episode...
        self._write_log(["6555 NOTIFY @a mention"], mode="a")
        fd.daemon_tick(ctx, mono_now=2560.0, wall_now=6560.0)
        self.assertEqual(ag.terminates, 1)
        # ...so a NEW deaf episode bounces again (mono/wall advance in step:
        # no suspend, no grace window).
        fd.daemon_tick(ctx, mono_now=8060.0, wall_now=12060.0)
        self.assertEqual(ag.terminates, 2)

    def test_token_wedge_bounces_once_per_episode(self):
        self._write_log(["4990 NOTIFY @a mention"])   # receipts fresh
        self._write_token(expires_at=4300)            # 700s past => wedged
        ctx = self._ctx(token_file=self.tok)
        fd.daemon_tick(ctx, mono_now=1015.0, wall_now=5015.0)
        ag = self.agents["a"]
        self.assertEqual(ag.terminates, 1)
        body = fd.daemon_tick(ctx, mono_now=1030.0, wall_now=5030.0)
        self.assertEqual([e for e in body["events"] if e["kind"] == "bounce"],
                         [{"kind": "bounce", "agent": "a", "reason": "TOKEN"}])
        # Still wedged after respawn: same episode, no re-bounce.
        fd.daemon_tick(ctx, mono_now=1045.0, wall_now=5045.0)  # respawns
        fd.daemon_tick(ctx, mono_now=1060.0, wall_now=5060.0)
        self.assertEqual(ag.terminates, 1)
        # Child refreshed its own token => healthy => episode over.
        self._write_token(expires_at=9000)
        self._write_log(["5060 NOTIFY @a mention"], mode="a")
        fd.daemon_tick(ctx, mono_now=1075.0, wall_now=5075.0)
        # Wedge again => new episode => bounce again.
        self._write_token(expires_at=4300)
        fd.daemon_tick(ctx, mono_now=1090.0, wall_now=5090.0)
        self.assertEqual(ag.terminates, 2)

    def test_post_resume_grace_suppresses_token_bounce(self):
        # Right after resume the child's SIGUSR1 refresh needs time to land:
        # a long-expired token inside the grace window is expected, not a
        # wedge — no bounce until the window closes.
        self._write_log(["4990 NOTIFY @a mention"])
        self._write_token(expires_at=4300)            # 700s past => wedged
        ctx = self._ctx(token_file=self.tok)
        ctx["grace_until"] = 5100.0                   # post-resume window open
        fd.daemon_tick(ctx, mono_now=1015.0, wall_now=5015.0)
        self.assertEqual(self.agents["a"].terminates, 0)
        body = fd.daemon_tick(ctx, mono_now=1030.0, wall_now=5030.0)
        self.assertEqual([e for e in body["events"] if e["kind"] == "bounce"],
                         [])
        # Window closed and STILL wedged: now it bounces.
        fd.daemon_tick(ctx, mono_now=1115.0, wall_now=5115.0)
        self.assertEqual(self.agents["a"].terminates, 1)

    def test_crashloop_alerts_sponsor_once_per_episode_via_alert_cmd(self):
        ctx = self._ctx(fleet_extra={"alert_cmd": "notify-send fleet"})
        ag = self.agents["a"]
        ag._alive, ag.pid = False, None
        ag.failure_times = [4990.0] * 5               # crashlooping
        with mock.patch.object(fd.subprocess, "run") as run:
            fd.daemon_tick(ctx, mono_now=1015.0, wall_now=5015.0)
            run.assert_called_once()
            argv = run.call_args[0][0]
            self.assertEqual(argv[:2], ["notify-send", "fleet"])
            self.assertIn("CRASHLOOP", argv[-1])      # message is argv[-1]
            self.assertIn("@a", argv[-1])
            # Same episode on later ticks: alerted ONCE, not per tick.
            fd.daemon_tick(ctx, mono_now=1030.0, wall_now=5030.0)
            fd.daemon_tick(ctx, mono_now=1045.0, wall_now=5045.0)
            run.assert_called_once()

    def test_crashloop_without_alert_cmd_logs_only(self):
        ctx = self._ctx()
        ag = self.agents["a"]
        ag._alive, ag.pid = False, None
        ag.failure_times = [4990.0] * 5
        out = io.StringIO()
        with mock.patch.object(fd.subprocess, "run") as run, \
             contextlib.redirect_stdout(out):
            fd.daemon_tick(ctx, mono_now=1015.0, wall_now=5015.0)
        run.assert_not_called()
        self.assertIn("CRASHLOOP", out.getvalue())


if __name__ == "__main__":
    unittest.main()
