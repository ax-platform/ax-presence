import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = ROOT / "plugins" / "platforms" / "ax" / "adapter.py"


def load_adapter_module():
    """Load the adapter with minimal gateway stubs for no-reply tests."""
    gateway = types.ModuleType("gateway")
    platforms = types.ModuleType("gateway.platforms")
    base = types.ModuleType("gateway.platforms.base")
    config = types.ModuleType("gateway.config")

    class BasePlatformAdapter:
        def __init__(self, *args, **kwargs):
            pass

    class SendResult:
        def __init__(self, success, message_id=None, error=None, raw_response=None, retryable=False, continuation_message_ids=()):
            self.success = success
            self.message_id = message_id
            self.error = error
            self.raw_response = raw_response
            self.retryable = retryable
            self.continuation_message_ids = continuation_message_ids

    class MessageEvent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class MessageType:
        TEXT = "text"

    class Platform:
        def __init__(self, name):
            self.name = name

    base.BasePlatformAdapter = BasePlatformAdapter
    base.SendResult = SendResult
    base.MessageEvent = MessageEvent
    base.MessageType = MessageType
    config.Platform = Platform

    sys.modules["gateway"] = gateway
    sys.modules["gateway.platforms"] = platforms
    sys.modules["gateway.platforms.base"] = base
    sys.modules["gateway.config"] = config

    ax = types.ModuleType("ax_presence_listener")
    ax.SSE_URL = "https://example.test/sse"
    ax.MESSAGES_URL = "https://example.test/api/v1/messages"
    ax.current_access_token = lambda: "token"
    ax.mentions_me = lambda d: True
    ax.post_processing_status = lambda *args, **kwargs: None
    ax.post_message = lambda *args, **kwargs: "posted"
    sys.modules["ax_presence_listener"] = ax

    spec = importlib.util.spec_from_file_location("ax_adapter_under_test", ADAPTER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def make_adapter(module):
    adapter = module.AXAdapter.__new__(module.AXAdapter)
    adapter._last_mid = {}
    return adapter


class AXAdapterNoReplyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_adapter_module()

    def setUp(self):
        self.adapter = make_adapter(self.module)

    def test_no_reply_safe_word_matches_only_standalone_command_case_insensitive(self):
        positive = [
            "no reply",
            "NO REPLY",
            "no-reply",
            "NO-REPLY",
            "@nyx no reply",
            "@nyx no-reply",
            "no reply @nyx",
            "no-reply @nyx",
        ]
        for text in positive:
            with self.subTest(text=text):
                self.assertTrue(self.adapter._is_no_reply_safe_word({"content": text}))

    def test_no_reply_safe_word_does_not_match_when_embedded_in_other_text(self):
        negative = [
            "@nyx noreply",
            "@nyx no replies expected",
            "@nyx reply no",
            "Status update; no reply needed.",
            "Please treat this as No Reply — broadcast only.",
            "@nyx please investigate why no reply label is missing",
        ]
        for text in negative:
            with self.subTest(text=text):
                self.assertFalse(self.adapter._is_no_reply_safe_word({"content": text}))

    def test_mark_no_reply_posts_skipped_no_reply_status(self):
        with mock.patch.object(self.module.ax, "post_processing_status") as post_status:
            self.adapter._mark_no_reply({"id": "msg-123", "content": "@nyx no reply"})

        post_status.assert_called_once_with(
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

    def test_no_reply_output_sentinel_matches_only_exact_token(self):
        positive = ["NO_REPLY", "no-reply", "no_reply", "no reply", " no-reply! "]
        negative = [
            "please send no-reply as text",
            "no reply needed after this update",
            "normal answer",
        ]
        for text in positive:
            with self.subTest(text=text):
                self.assertTrue(self.adapter._is_no_reply_output_sentinel(text))
        for text in negative:
            with self.subTest(text=text):
                self.assertFalse(self.adapter._is_no_reply_output_sentinel(text))

    def test_send_translates_no_reply_output_sentinel_to_no_reply_status(self):
        async def run():
            self.adapter._last_mid["space-1"] = "msg-123"
            with mock.patch.object(self.module.ax, "post_processing_status") as post_status, \
                 mock.patch.object(self.module.ax, "post_message") as post_message:
                result = await self.adapter.send("space-1", "no-reply")
            self.assertTrue(result.success)
            self.assertEqual(result.message_id, "no_reply")
            post_message.assert_not_called()
            post_status.assert_called_once_with(
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

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
