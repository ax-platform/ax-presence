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
    ax = types.ModuleType("ax_presence_listener")
    ax.proactive_refresh_loop = lambda: None
    ax.current_access_token = lambda: "token"
    ax.connect = lambda **kwargs: None
    ax.post_processing_status = lambda *a, **k: None
    ax.post_message = lambda *a, **k: "reply-1"
    sys.modules["ax_presence_listener"] = ax

    gateway = types.ModuleType("gateway")
    gateway_platforms = types.ModuleType("gateway.platforms")
    base = types.ModuleType("gateway.platforms.base")
    config = types.ModuleType("gateway.config")

    class BasePlatformAdapter:
        def __init__(self, *args, **kwargs):
            self.platform = kwargs.get("platform")

        def build_source(self, **kwargs):
            return types.SimpleNamespace(platform=getattr(self, "platform", None), **kwargs)

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
    sys.modules["gateway.platforms"] = gateway_platforms
    sys.modules["gateway.platforms.base"] = base
    sys.modules["gateway.config"] = config

    spec = importlib.util.spec_from_file_location("ax_adapter_under_test", ADAPTER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class AXAdapterStatusRoutingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_adapter_module()

    def setUp(self):
        self.adapter = self.module.AXAdapter.__new__(self.module.AXAdapter)
        self.adapter.platform = self.module.Platform("ax")
        self.adapter._last_mid = {}
        self.adapter._name_cache = {}
        self.adapter._loop = None
        self.adapter.handle_message = mock.AsyncMock()

    def test_dispatch_marks_triggering_message_as_thread_anchor(self):
        self.adapter._resolve_sender_name = mock.Mock(return_value="Canary")
        self.adapter._loop = types.SimpleNamespace(is_closed=lambda: False)
        with mock.patch.object(self.module.ax, "post_processing_status"), \
             mock.patch.object(self.module.asyncio, "run_coroutine_threadsafe") as schedule:
            self.adapter._dispatch({
                "id": "msg-123",
                "space_id": "space-1",
                "content": "hello",
                "agent_id": "agent-canary",
            })
        event = self.adapter.handle_message.call_args.args[0]
        # Close the coroutine object produced for scheduling so the unit test
        # does not emit an unawaited-coroutine warning.
        schedule.call_args.args[0].close()
        self.assertEqual(event.source.thread_id, "msg-123")
        self.assertEqual(event.source.message_id, "msg-123")
        self.assertEqual(event.message_id, "msg-123")

    def test_send_or_update_status_posts_activity_to_triggering_message_not_chat(self):
        async def run():
            self.adapter._last_mid["space-1"] = "msg-123"
            with mock.patch.object(self.module.ax, "post_processing_status") as post_status, \
                 mock.patch.object(self.module.ax, "post_message") as post_message:
                result = await self.adapter.send_or_update_status(
                    "space-1",
                    "context_pressure",
                    "Compacting context — summarizing earlier conversation",
                )
            self.assertTrue(result.success)
            self.assertEqual(result.message_id, "status")
            post_message.assert_not_called()
            post_status.assert_called_once_with(
                "msg-123",
                "working",
                "Compacting context — summarizing earlier conversation",
                detail={"status_key": "context_pressure", "signal_kind": "gateway_status"},
            )
        asyncio.run(run())

    def test_send_or_update_status_uses_metadata_anchor_before_latest_message(self):
        async def run():
            self.adapter._last_mid["space-1"] = "newer-msg"
            with mock.patch.object(self.module.ax, "post_processing_status") as post_status, \
                 mock.patch.object(self.module.ax, "post_message") as post_message:
                result = await self.adapter.send_or_update_status(
                    "space-1",
                    "approval",
                    "Waiting for approval",
                    metadata={"message_id": "original-msg"},
                )
            self.assertTrue(result.success)
            post_message.assert_not_called()
            post_status.assert_called_once_with(
                "original-msg",
                "working",
                "Waiting for approval",
                detail={"status_key": "approval", "signal_kind": "gateway_status"},
            )
        asyncio.run(run())

    def test_no_reply_output_sentinel_uses_metadata_thread_id_not_latest_message(self):
        async def run():
            self.adapter._last_mid["space-1"] = "newer-msg"
            with mock.patch.object(self.module.ax, "post_processing_status") as post_status, \
                 mock.patch.object(self.module.ax, "post_message") as post_message:
                result = await self.adapter.send("space-1", "no-reply", metadata={"thread_id": "original-msg"})
            self.assertTrue(result.success)
            self.assertEqual(result.message_id, "no_reply")
            post_message.assert_not_called()
            self.assertEqual(post_status.call_args.args[:2], ("original-msg", "skipped"))
            self.assertEqual(post_status.call_args.kwargs["detail"]["reason_code"], "no_reply")
        asyncio.run(run())

    def test_send_routes_gateway_status_fallbacks_to_activity_not_chat(self):
        async def run():
            self.adapter._last_mid["space-1"] = "msg-123"
            status_lines = [
                "🗜️ Compacting context — summarizing earlier conversation so I can continue...",
                "⚠️ **Dangerous command requires approval:**\n`rm -rf tmp`",
                "⏳ Waiting for approval",
                "🔄 Retrying after provider error",
                "🧠 Thinking through the next step",
            ]
            with mock.patch.object(self.module.ax, "post_processing_status") as post_status, \
                 mock.patch.object(self.module.ax, "post_message") as post_message:
                for line in status_lines:
                    result = await self.adapter.send("space-1", line, metadata={"thread_id": "msg-123"})
                    self.assertTrue(result.success)
                    self.assertEqual(result.message_id, "activity")
            post_message.assert_not_called()
            self.assertEqual(post_status.call_count, len(status_lines))
            self.assertEqual(post_status.call_args_list[0].args[:3], (
                "msg-123",
                "working",
                "🗜️ Compacting context — summarizing earlier conversation so I can continue...",
            ))
        asyncio.run(run())

    def test_send_does_not_route_normal_final_reply_to_activity(self):
        async def run():
            with mock.patch.object(self.module.ax, "post_processing_status") as post_status, \
                 mock.patch.object(self.module.ax, "post_message", return_value="reply-1") as post_message:
                result = await self.adapter.send("space-1", "✅ Done — adapter tests pass.", metadata={"thread_id": "msg-123"})
            self.assertTrue(result.success)
            self.assertEqual(result.message_id, "reply-1")
            post_status.assert_not_called()
            post_message.assert_called_once()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
