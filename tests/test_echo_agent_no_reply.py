import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ECHO_PATH = ROOT / "examples" / "echo-agent" / "echo_agent.py"


def load_echo_module():
    """Load echo_agent with minimal stubs so tests never touch network/SSE."""
    ax = types.ModuleType("ax_presence_listener")
    setattr(ax, "BASE", "https://paxai.app")
    setattr(ax, "AGENT_HANDLE", "zephyr")
    setattr(ax, "SPACE_ID", "space-1")
    setattr(ax, "TOKEN_FILE", "/tmp/ax-token.json")
    setattr(ax, "current_access_token", lambda: "token")
    setattr(ax, "load_tok", lambda: {"access_token": "token"})
    setattr(ax, "mentions_me", lambda d: True)
    setattr(ax, "post_processing_status", lambda *a, **k: None)

    mc = types.ModuleType("monitor_core")
    class Event:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
    setattr(mc, "Event", Event)
    setattr(mc, "run", lambda *a, **k: None)

    responders = types.ModuleType("responders")
    setattr(responders, "get", lambda name: (lambda content, who: "echo"))

    sys.modules["ax_presence_listener"] = ax
    sys.modules["monitor_core"] = mc
    sys.modules["responders"] = responders

    spec = importlib.util.spec_from_file_location("echo_agent_under_test", ECHO_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class EchoAgentNoReplyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_echo_module()

    def test_responder_no_reply_sentinel_variants_are_suppressed(self):
        positive = ["NO_REPLY", "no-reply", "no_reply", "no reply", " no-reply! "]
        for text in positive:
            with self.subTest(text=text):
                self.assertTrue(self.module._is_no_reply_output_sentinel(text))

    def test_responder_no_reply_sentinel_does_not_match_prose(self):
        negative = [
            "please send no-reply as literal text",
            "no reply needed after this update",
            "normal answer",
        ]
        for text in negative:
            with self.subTest(text=text):
                self.assertFalse(self.module._is_no_reply_output_sentinel(text))

    def test_no_reply_safe_word_accepts_space_and_hyphen_only_as_exact_command(self):
        positive = ["no reply", "no-reply", "@zephyr no reply", "@zephyr no-reply"]
        negative = ["noreply", "no reply needed", "please no-reply as text"]
        for text in positive:
            with self.subTest(text=text):
                self.assertTrue(self.module._is_no_reply_safe_word(text))
        for text in negative:
            with self.subTest(text=text):
                self.assertFalse(self.module._is_no_reply_safe_word(text))

    def test_echo_reply_translates_responder_no_reply_to_status_without_post(self):
        posts = []

        class ImmediateThread:
            def __init__(self, target, daemon=False):
                self.target = target
                self.daemon = daemon

            def start(self):
                self.target()

        event = types.SimpleNamespace(
            payload={"mid": "msg-123", "content": "settled", "space_id": "space-1", "who": "atlas"}
        )
        with mock.patch.object(self.module, "RESPONDER", lambda content, who: "no-reply"), \
             mock.patch.object(self.module.ax, "post_processing_status") as post_status, \
             mock.patch.object(self.module, "_post_reply", side_effect=lambda *a: posts.append(a)), \
             mock.patch.object(self.module.threading, "Thread", ImmediateThread):
            self.module.echo_reply(event)

        post_status.assert_any_call(
            "msg-123",
            "skipped",
            activity="no reply",
            detail={
                "reason": "no reply",
                "label": "no reply",
                "reason_code": "no_reply",
                "signal_kind": "no_reply",
                "safe_word": "no reply/no-reply",
                "signal_only": True,
                "emoji": "",
            },
        )
        self.assertEqual(posts, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
