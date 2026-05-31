import importlib.util
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LISTENER_PATH = ROOT / "ax_presence_listener.py"


def load_listener_module():
    os.environ.setdefault("AX_SPACE_ID", "space-1")
    os.environ.setdefault("AX_AGENT_ID", "agent-1")
    os.environ.setdefault("AX_AGENT_HANDLE", "peach")
    spec = importlib.util.spec_from_file_location("ax_presence_listener_under_test", LISTENER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProcessingStatusBodyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_listener_module()

    def test_build_processing_body_accepts_detail_for_no_reply_status(self):
        body = self.module.build_processing_body(
            "msg-123",
            "skipped",
            activity="no reply",
            detail={"reason_code": "no_reply", "signal_kind": "no_reply"},
        )
        self.assertEqual(body["message_id"], "msg-123")
        self.assertEqual(body["status"], "skipped")
        self.assertEqual(body["activity"], "no reply")
        self.assertEqual(body["detail"]["reason_code"], "no_reply")

    def test_build_processing_body_accepts_safe_tool_progress_fields(self):
        body = self.module.build_processing_body(
            "msg-123",
            "working",
            activity="Running tests",
            tool_name="terminal",
            progress={"current": "2", "total": "3", "unit": "steps"},
        )
        self.assertEqual(body["tool_name"], "terminal")
        self.assertEqual(body["progress"], {"current": 2, "total": 3, "unit": "steps"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
