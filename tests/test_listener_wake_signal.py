import signal, unittest
from unittest import mock
import ax_presence_listener as listener

class WakeSignalTest(unittest.TestCase):
    def test_handler_sets_wake_flag(self):
        listener._wake_requested = False
        listener._handle_wake_signal(signal.SIGUSR1, None)
        self.assertTrue(listener._wake_requested)

    def test_consume_returns_true_once_then_clears(self):
        listener._wake_requested = True
        self.assertTrue(listener._consume_wake_request())
        self.assertFalse(listener._consume_wake_request())

    def test_presence_beat_refreshes_on_wake_flag(self):
        beats = []
        def fake_urlopen(req, timeout=None):
            beats.append(dict(req.header_items()))
            class R:  # minimal response
                def read(self): return b"{}"
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return R()
        tok = {"access_token": "old", "refresh_token": "r", "client_id": "c",
               "expires_at": 9e9}
        fresh = dict(tok, access_token="fresh")
        listener._wake_requested = True
        with mock.patch.object(listener, "load_tok", return_value=tok), \
             mock.patch.object(listener, "refresh", return_value=fresh) as rf, \
             mock.patch.object(listener.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            listener._presence_beat()
        rf.assert_called_once()   # wake flag forces refresh even with valid TTL
        self.assertEqual(beats[-1].get("Authorization"), "Bearer fresh")


class _FakeSSE:
    """Iterable SSE response double. Tracks reads and close(); raises like a
    closed socket read once close() has been called."""
    def __init__(self, lines=()):
        self._lines = list(lines)
        self.reads = 0
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise ValueError("I/O operation on closed file.")
        if not self._lines:
            raise StopIteration
        self.reads += 1
        return self._lines.pop(0)

    def close(self):
        self.closed = True


class _WakeMidReadSSE(_FakeSSE):
    """Simulates SIGUSR1 arriving while the main thread is blocked in the SSE
    read: the handler runs (closing the stashed response), then the resumed
    read fails because the socket is gone."""
    def __next__(self):
        # Use the module-level `listener` binding, NOT a fresh import: another
        # test file swaps a stub into sys.modules["ax_presence_listener"].
        listener._handle_wake_signal(signal.SIGUSR1, None)
        raise ValueError("I/O operation on closed file.")


class WakeRaceFixTest(unittest.TestCase):
    """Review fixes: one-flag/two-consumer race, dead-socket wake, and
    wake-consumed-before-refresh-succeeds."""

    def setUp(self):
        listener._wake_requested = False
        listener._wake_reconnect = False
        listener._sse_response = None

    def _stream(self, resp):
        with mock.patch.object(listener, "current_access_token", return_value="t"), \
             mock.patch.object(listener.urllib.request, "urlopen", return_value=resp):
            listener.stream()

    def test_handler_sets_both_consumer_flags(self):
        # Separate flags per consumer: the presence beat consuming its flag
        # must not be able to starve stream()'s reconnect, and vice versa.
        listener._handle_wake_signal(signal.SIGUSR1, None)
        self.assertTrue(listener._wake_requested)
        self.assertTrue(listener._wake_reconnect)

    def test_handler_closes_live_sse_response(self):
        # A flag alone cannot interrupt a read blocked on a half-open
        # post-suspend socket (PEP 475 retries it); the handler must close the
        # stashed response so the read raises instead of blocking forever.
        resp = _FakeSSE()
        listener._sse_response = resp
        listener._handle_wake_signal(signal.SIGUSR1, None)
        self.assertTrue(resp.closed)

    def test_handler_tolerates_no_stashed_response(self):
        listener._sse_response = None
        listener._handle_wake_signal(signal.SIGUSR1, None)  # must not raise
        self.assertTrue(listener._wake_requested)

    def test_stream_stashes_response_for_wake_close(self):
        resp = _FakeSSE()
        self._stream(resp)
        self.assertIs(listener._sse_response, resp)

    def test_stream_does_not_break_or_consume_on_beat_flag(self):
        # Churn half of the race: with the (beat-owned) flag set, an arriving
        # SSE line must neither abort the stream nor consume the beat's flag.
        listener._wake_requested = True
        resp = _FakeSSE([b"event: ping\n", b"\n", b"event: ping\n", b"\n"])
        self._stream(resp)
        self.assertEqual(resp.reads, 4)               # drained, no early break
        self.assertTrue(listener._wake_requested)     # beat's flag untouched

    def test_stream_wake_close_returns_cleanly(self):
        # Dead-socket wake: handler closes the stashed response mid-read;
        # stream() must treat the resulting error as a clean wake reconnect
        # (normal return) so main() resets backoff and the circuit breaker
        # never counts it as a failure.
        resp = _WakeMidReadSSE([b"event: ping\n"])
        self._stream(resp)                            # must NOT raise
        self.assertTrue(resp.closed)
        self.assertFalse(listener._wake_reconnect)    # consumed by stream()
        self.assertTrue(listener._wake_requested)     # beat's flag still set

    def test_stream_reraises_genuine_errors(self):
        # Without a wake, a read error is a real failure and must propagate.
        resp = _FakeSSE([b"event: ping\n"])
        resp.closed = True
        with self.assertRaises(ValueError):
            self._stream(resp)

    def test_fresh_connect_clears_stale_wake_reconnect(self):
        # A wake that fired while disconnected is satisfied by the next fresh
        # connection; a later genuine error must not be misread as a wake.
        listener._wake_reconnect = True
        self._stream(_FakeSSE())
        self.assertFalse(listener._wake_reconnect)

    def test_wake_survives_refresh_failure(self):
        # The wake must be consumed only AFTER refresh succeeds; a transient
        # post-wake network error must leave the flag set so the next beat
        # retries the forced refresh instead of falling back to load_tok().
        listener._wake_requested = True
        with mock.patch.object(listener, "refresh", side_effect=OSError("net down")), \
             mock.patch.object(listener, "load_tok",
                               return_value={"access_token": "old", "expires_at": 9e9}):
            with self.assertRaises(OSError):
                listener._presence_beat()
        self.assertTrue(listener._wake_requested)

    def test_presence_loop_has_real_docstring(self):
        # Pre-existing nit: the docstring sat after a call, making it a no-op
        # string expression instead of a docstring.
        self.assertIn("heartbeat", (listener.presence_loop.__doc__ or ""))
