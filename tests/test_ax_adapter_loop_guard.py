import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = ROOT / "plugins" / "platforms" / "ax" / "adapter.py"


def load_adapter_module():
    """Load the adapter with minimal gateway stubs for guard-only tests."""
    gateway = types.ModuleType("gateway")
    platforms = types.ModuleType("gateway.platforms")
    base = types.ModuleType("gateway.platforms.base")
    config = types.ModuleType("gateway.config")

    class BasePlatformAdapter:
        def __init__(self, *args, **kwargs):
            pass

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

    setattr(base, "BasePlatformAdapter", BasePlatformAdapter)
    setattr(base, "SendResult", SendResult)
    setattr(base, "MessageEvent", MessageEvent)
    setattr(base, "MessageType", MessageType)
    setattr(config, "Platform", Platform)

    sys.modules.setdefault("gateway", gateway)
    sys.modules.setdefault("gateway.platforms", platforms)
    sys.modules["gateway.platforms.base"] = base
    sys.modules["gateway.config"] = config

    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("ax_adapter_under_test", ADAPTER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def make_adapter(module):
    adapter = module.AXAdapter.__new__(module.AXAdapter)
    adapter.platform = module.Platform("ax")
    adapter._agent_ack_window = {}
    adapter._last_mid = {}
    adapter._loop = None
    return adapter


class AXAdapterLoopGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_adapter_module()

    def setUp(self):
        self.adapter = make_adapter(self.module)

    def test_peer_short_ack_mention_is_suppressed_immediately_before_dispatch(self):
        ack_shapes = [
            "@peach roger",
            "roger @peach",
            "@peach — ack.",
            "thanks @peach",
            "@peach standing by",
        ]

        for text in ack_shapes:
            with self.subTest(text=text):
                self.assertTrue(
                    self.adapter._is_agent_ack_loop_candidate({"agent_id": "atlas-id", "content": text})
                )

    def test_human_short_ack_mentions_are_not_suppressed_by_agent_guard(self):
        self.assertFalse(self.adapter._is_agent_ack_loop_candidate({"content": "@peach roger"}))

    def test_substantive_peer_handoff_with_mention_is_allowed(self):
        self.assertFalse(self.adapter._is_agent_ack_loop_candidate({
            "agent_id": "atlas-id",
            "content": "@peach please check the token refresh log before rolling this to canary",
        }))

    def test_two_agent_ack_exchange_does_not_cascade(self):
        peach = make_adapter(self.module)
        atlas = make_adapter(self.module)

        # Simulate atlas acknowledging peach, then peach acknowledging atlas. Both
        # are peer-agent short-ack mentions and must be dropped at adapter ingress;
        # no model dispatch should occur, so the exchange cannot grow into a cascade.
        atlas_to_peach = {"agent_id": "atlas-id", "content": "@peach ack"}
        peach_to_atlas = {"agent_id": "peach-id", "content": "roger @atlas"}

        self.assertTrue(peach._is_agent_ack_loop_candidate(atlas_to_peach))
        self.assertTrue(atlas._is_agent_ack_loop_candidate(peach_to_atlas))

    def test_second_short_non_ack_peer_mention_in_window_is_suppressed(self):
        ticks = iter([1000.0, 1010.0, 1010.0])
        with mock.patch.object(self.module.time, "time", side_effect=lambda: next(ticks)):
            first = {"agent_id": "atlas-id", "content": "@peach baseline unchanged"}
            second = {"agent_id": "atlas-id", "content": "@peach retained"}

            self.assertFalse(self.adapter._is_agent_ack_loop_candidate(first))
            self.assertTrue(self.adapter._is_agent_ack_loop_candidate(second))

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

    def test_mark_no_reply_posts_no_reply_status_without_dispatch(self):
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

        import asyncio
        asyncio.run(run())

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

        import asyncio
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

        import asyncio
        asyncio.run(run())

    def test_send_does_not_route_normal_emoji_final_reply_to_activity(self):
        async def run():
            with mock.patch.object(self.module.ax, "post_processing_status") as post_status, \
                 mock.patch.object(self.module.ax, "post_message", return_value="reply-1") as post_message:
                result = await self.adapter.send("space-1", "✅ Done — adapter tests pass.", metadata={"thread_id": "msg-123"})
            self.assertTrue(result.success)
            self.assertEqual(result.message_id, "reply-1")
            post_status.assert_not_called()
            post_message.assert_called_once()

        import asyncio
        asyncio.run(run())

    def test_send_or_update_status_uses_metadata_message_id_when_available(self):
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

        import asyncio
        asyncio.run(run())

    def test_send_or_update_status_uses_thread_id_metadata_as_activity_anchor(self):
        async def run():
            self.adapter._last_mid["space-1"] = "newer-msg"
            with mock.patch.object(self.module.ax, "post_processing_status") as post_status, \
                 mock.patch.object(self.module.ax, "post_message") as post_message:
                result = await self.adapter.send_or_update_status(
                    "space-1",
                    "interim",
                    "Still working",
                    metadata={"thread_id": "original-msg"},
                )
            self.assertTrue(result.success)
            post_message.assert_not_called()
            post_status.assert_called_once_with(
                "original-msg",
                "working",
                "Still working",
                detail={"status_key": "interim", "signal_kind": "gateway_status"},
            )

        import asyncio
        asyncio.run(run())

    def test_no_reply_output_sentinel_uses_metadata_thread_id_not_latest_message(self):
        async def run():
            self.adapter._last_mid["space-1"] = "newer-msg"
            with mock.patch.object(self.module.ax, "post_processing_status") as post_status, \
                 mock.patch.object(self.module.ax, "post_message") as post_message:
                result = await self.adapter.send(
                    "space-1",
                    "NO_REPLY",
                    metadata={"thread_id": "original-msg"},
                )
            self.assertTrue(result.success)
            self.assertEqual(result.message_id, "no_reply")
            post_message.assert_not_called()
            self.assertEqual(post_status.call_args.args[0], "original-msg")

        import asyncio
        asyncio.run(run())

    def test_dispatch_marks_triggering_message_as_thread_anchor(self):
        built_sources = []
        original_build_source = self.adapter.build_source

        def capture_source(**kwargs):
            source = original_build_source(**kwargs)
            built_sources.append(source)
            return source

        self.adapter.build_source = capture_source
        with mock.patch.object(self.adapter, "_resolve_sender_name", return_value="madtank"), \
             mock.patch.object(self.module.ax, "post_processing_status"):
            self.adapter._dispatch({
                "id": "msg-123",
                "space_id": "space-1",
                "sender_id": "user-1",
                "content": "@peach check this",
            })

        self.assertEqual(self.adapter._last_mid["space-1"], "msg-123")
        self.assertEqual(built_sources[0].thread_id, "msg-123")
        self.assertEqual(built_sources[0].message_id, "msg-123")


if __name__ == "__main__":
    unittest.main(verbosity=2)
