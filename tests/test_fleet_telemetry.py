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
