"""Unit tests for the agent_placement_changed live-move SSE control event.

The backend emits ``agent_placement_changed`` on an agent's OLD (still
subscribed) space stream when its owner moves it. The adapter must:
  - recognize the control event BEFORE the mention gate (bypassing
    _event_space_matches / mentions_me / own-post guard / dedup),
  - ignore events naming a different agent,
  - re-resolve the DB-authoritative placement (never trust payload new_space_id),
  - tear down old readers via an epoch bump and respawn for the new space,
  - be single-flight + idempotent (no reconnect storm on duplicate broadcasts).

These reuse the loop-guard test's adapter loader/stubs.
"""
import threading
import types
import unittest
from unittest import mock

from test_ax_adapter_loop_guard import load_adapter_module


def make_move_adapter(module):
    """A bare adapter with just the state the placement handler touches."""
    adapter = module.AXAdapter.__new__(module.AXAdapter)
    adapter.platform = module.Platform("ax")
    adapter.agent_id = "agent-1"
    adapter.handle = "nyx"
    adapter.space_id = "space-A"
    adapter.home_space = "space-A"
    adapter.token_file = "/tmp/tok.json"
    adapter.config = types.SimpleNamespace(platforms={}, home_channel=None)
    adapter._subscribed_spaces = {"space-A"}
    adapter._reader_epoch = 0
    adapter._resub_lock = threading.Lock()
    adapter._resubscribing = False
    adapter._space_token = {}
    adapter._space_token_lock = threading.Lock()
    adapter._reader_threads = []
    adapter._reader_thread = None
    adapter._running = True
    return adapter


PLACEMENT_EVENT = {
    "agent_id": "agent-1",
    "new_space_id": "space-B",
    "old_space_id": "space-A",
    "ts": "2026-06-03T00:00:00+00:00",
}


class PlacementChangedHandlerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_adapter_module()

    def setUp(self):
        self.adapter = make_move_adapter(self.module)

    def test_ignores_event_for_other_agent(self):
        a = self.adapter
        evt = dict(PLACEMENT_EVENT, agent_id="some-other-agent")
        with mock.patch.object(a, "_derive_agent_space_id") as derive:
            a._handle_placement_changed(evt)
        derive.assert_not_called()
        self.assertEqual(a._reader_epoch, 0)

    def test_noop_when_space_unchanged(self):
        a = self.adapter
        # derive returns the SAME space; subscription set already matches.
        with mock.patch.object(a, "_derive_agent_space_id", return_value="space-A"), \
             mock.patch.object(a, "_apply_space_id") as apply_, \
             mock.patch.object(a, "_spawn_readers") as spawn:
            a._handle_placement_changed(dict(PLACEMENT_EVENT, new_space_id="space-A"))
        apply_.assert_not_called()
        spawn.assert_not_called()
        self.assertEqual(a._reader_epoch, 0)  # no epoch bump
        self.assertFalse(a._resubscribing)

    def test_does_not_trust_payload_new_space_id(self):
        """The handler must apply the DERIVED space, not the payload value."""
        a = self.adapter
        # Payload claims space-X, but the DB (derive) says space-B. We must
        # apply space-B.
        with mock.patch.object(a, "_derive_agent_space_id", return_value="space-B"), \
             mock.patch.object(a, "_discover_subscribed_spaces", return_value=["space-B"]), \
             mock.patch.object(a, "_apply_space_id") as apply_, \
             mock.patch.object(a, "_spawn_readers"):
            a._handle_placement_changed(dict(PLACEMENT_EVENT, new_space_id="space-X-FORGED"))
        apply_.assert_called_once_with("space-B")

    def test_happy_path_move_resubscribes(self):
        a = self.adapter
        spawned = []
        with mock.patch.object(a, "_derive_agent_space_id", return_value="space-B"), \
             mock.patch.object(a, "_discover_subscribed_spaces", return_value=["space-B"]), \
             mock.patch.object(a, "_apply_space_id",
                               side_effect=lambda s: setattr(a, "space_id", s)) as apply_, \
             mock.patch.object(a, "_spawn_readers",
                               side_effect=lambda spaces: spawned.append(list(spaces))):
            a._reader_epoch = 5
            a._space_token = {"space-A": ("stale-token", 9e18)}
            a._handle_placement_changed(PLACEMENT_EVENT)

        apply_.assert_called_once_with("space-B")
        self.assertEqual(a.space_id, "space-B")
        self.assertEqual(a._subscribed_spaces, {"space-B"})
        self.assertEqual(a._reader_epoch, 6)  # bumped exactly once
        self.assertEqual(a._space_token, {})  # stale token invalidated
        self.assertEqual(spawned, [["space-B"]])
        self.assertFalse(a._resubscribing)

    def test_single_flight_under_concurrent_readers(self):
        """N readers receive the broadcast; exactly one re-resolves."""
        a = self.adapter
        gate = threading.Event()
        derive_calls = []

        def slow_derive():
            derive_calls.append(1)
            gate.wait(2.0)  # hold the lock so the others race in
            return "space-B"

        with mock.patch.object(a, "_derive_agent_space_id", side_effect=slow_derive), \
             mock.patch.object(a, "_discover_subscribed_spaces", return_value=["space-B"]), \
             mock.patch.object(a, "_apply_space_id",
                               side_effect=lambda s: setattr(a, "space_id", s)), \
             mock.patch.object(a, "_spawn_readers"):
            threads = [threading.Thread(target=a._handle_placement_changed,
                                        args=(PLACEMENT_EVENT,)) for _ in range(8)]
            for t in threads:
                t.start()
            # Give the losers time to hit the _resubscribing guard and return.
            import time
            time.sleep(0.2)
            gate.set()
            for t in threads:
                t.join(2.0)

        self.assertEqual(len(derive_calls), 1)  # exactly one performed the work

    def test_empty_derive_keeps_subscription(self):
        """Suspended/detached -> derive returns empty; do NOT blank readers."""
        a = self.adapter
        with mock.patch.object(a, "_derive_agent_space_id", return_value=""), \
             mock.patch.object(a, "_apply_space_id") as apply_, \
             mock.patch.object(a, "_spawn_readers") as spawn:
            a._handle_placement_changed(PLACEMENT_EVENT)
        apply_.assert_not_called()
        spawn.assert_not_called()
        self.assertEqual(a._subscribed_spaces, {"space-A"})
        self.assertEqual(a._reader_epoch, 0)
        self.assertFalse(a._resubscribing)

    def test_derive_failure_keeps_subscription(self):
        a = self.adapter
        with mock.patch.object(a, "_derive_agent_space_id", side_effect=RuntimeError("401")), \
             mock.patch.object(a, "_apply_space_id") as apply_, \
             mock.patch.object(a, "_spawn_readers") as spawn:
            a._handle_placement_changed(PLACEMENT_EVENT)
        apply_.assert_not_called()
        spawn.assert_not_called()
        self.assertEqual(a._subscribed_spaces, {"space-A"})
        self.assertFalse(a._resubscribing)  # flag reset even on early return


class SSEReaderControlBranchTest(unittest.TestCase):
    """The raw SSE line processor must route the control event to the handler
    and NOT to the mention dispatch path."""

    @classmethod
    def setUpClass(cls):
        cls.module = load_adapter_module()

    def _make(self):
        a = make_move_adapter(self.module)
        return a

    def test_control_event_routed_to_handler_not_dispatch(self):
        a = self._make()
        # Build an SSE response iterator delivering one placement event.
        lines = [
            b"event: agent_placement_changed\n",
            b'data: {"agent_id": "agent-1", "new_space_id": "space-B", '
            b'"old_space_id": "space-A", "space_id": "space-A", '
            b'"ts": "2026-06-03T00:00:00+00:00"}\n',
            b"\n",
        ]

        class _Resp:
            def __iter__(self_inner):
                return iter(lines)

        handled = []
        with mock.patch.object(self.module.ax, "current_access_token", return_value="t"), \
             mock.patch.object(a, "_access_token_for_space", return_value="t"), \
             mock.patch.object(a, "_handle_placement_changed",
                               side_effect=lambda d: handled.append(d)) as h, \
             mock.patch.object(a, "_dispatch") as dispatch, \
             mock.patch("urllib.request.urlopen", return_value=_Resp()):
            # reader returns after handling the control event (its space set is
            # now stale). Run synchronously in this thread.
            a._sse_reader("space-A", my_epoch=a._reader_epoch)

        h.assert_called_once()
        dispatch.assert_not_called()
        # The handler received the payload incl. the broker-injected old space_id;
        # routing it pre-_event_space_matches means the old space_id is not a
        # rejection reason.
        self.assertEqual(handled[0]["new_space_id"], "space-B")

    def test_stale_reader_epoch_stops_dispatch(self):
        """An old-generation reader self-terminates on its next line."""
        a = self._make()
        a._reader_epoch = 7  # current generation
        lines = [
            b"event: mention\n",
            b'data: {"id": "m1", "space_id": "space-A"}\n',
            b"\n",
        ]

        class _Resp:
            def __iter__(self_inner):
                return iter(lines)

        with mock.patch.object(a, "_access_token_for_space", return_value="t"), \
             mock.patch.object(a, "_dispatch") as dispatch, \
             mock.patch("urllib.request.urlopen", return_value=_Resp()):
            # This reader was started at epoch 6, but current is 7 -> stale.
            a._sse_reader("space-A", my_epoch=6)

        dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
