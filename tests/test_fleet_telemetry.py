import json, os, tempfile, unittest
from pathlib import Path
import fleet_daemon as fd

GOLDEN = Path(__file__).parent / "fixtures" / "telemetry_golden.json"


class _StubProc:
    """Scripted AgentProc stand-in (no real processes)."""

    def __init__(self, name, log_path):
        self.name, self.log_path = name, log_path
        self.pid = 4411
        self.failure_times = []

    def alive(self):
        return True

    def signal_wake(self):
        pass

    def spawn(self):
        pass


class RuntimeShapeTest(unittest.TestCase):
    """The golden fixture is the shared backend contract — the body that
    the REAL daemon_tick snapshot path sends must have the same shape."""

    def test_daemon_tick_body_matches_golden_shape(self):
        golden = json.loads(GOLDEN.read_text())
        d = tempfile.mkdtemp()
        log = os.path.join(d, "a.log")
        with open(log, "w") as f:
            f.write("4990 NOTIFY @a mention\n")          # fresh receipt -> OK
        tok = os.path.join(d, "tok.json")
        with open(tok, "w") as f:
            json.dump({"expires_at": 5630}, f)
        # REAL listener formats (ax_presence_listener.py heartbeat_loop):
        # heartbeat file = bare int epoch, rich dict = <handle>-signal.json.
        hb = os.path.join(d, "a-listener-heartbeat")
        with open(hb, "w") as f:
            f.write(str(5000))
        sig = os.path.join(d, "a-signal.json")
        with open(sig, "w") as f:
            json.dump({"handle": "a", "ts": 5000, "connected": True,
                       "currently_401": False, "mentions_seen": 142,
                       "replies_sent": 131, "last_reply_at": 4990,
                       "responsiveness_ratio": 0.923}, f)
        cfg = {"fleet": {"device": "laptop"},
               "agents": {"a": {"token_file": tok, "heartbeat_file": hb,
                                "signal_file": sig}}}
        agents = {"a": _StubProc("a", log)}
        ctx = fd.new_ctx(cfg, agents, token=None,
                         state_file=os.path.join(d, "state.json"),
                         mono_now=1000.0, wall_now=5000.0)
        fd.daemon_tick(ctx, mono_now=1015.0, wall_now=5015.0)
        body = fd.daemon_tick(ctx, mono_now=1030.0, wall_now=5030.0)
        # Same top-level, device_state, host, and per-agent keys as golden.
        self.assertEqual(set(body), set(golden))
        self.assertEqual(set(body["device_state"]), set(golden["device_state"]))
        self.assertEqual(set(body["device_state"]["host"]),
                         set(golden["device_state"]["host"]))
        self.assertEqual(set(body["agents"]["a"]),
                         set(golden["agents"]["claude_prime"]))
        # And the listener-owned fields really come from the signal file.
        snap = body["agents"]["a"]
        self.assertEqual(snap["verdict"], "OK")
        self.assertIs(snap["sse_connected"], True)
        self.assertEqual(snap["mentions_seen"], 142)
        self.assertEqual(snap["replies_sent"], 131)
        self.assertIs(snap["currently_401"], False)

    def test_heartbeat_fields_default_to_none_when_file_missing(self):
        hb = fd.read_heartbeat("/nonexistent/heartbeat.json")
        self.assertEqual(hb, {"sse_connected": None, "mentions_seen": None,
                              "replies_sent": None, "currently_401": None})

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
            events=[{"kind": "suspend_resumed", "for_s": 41020, "tokens_refreshed": 3},
                    {"kind": "bounce", "agent": "night_owl", "reason": "DEAF",
                     "result": "receipt_fresh"}])
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
