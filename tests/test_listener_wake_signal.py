import signal, socket, threading, types, unittest
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


class _FakeSock:
    """Socket double. shutdown() is the only call the wake handler may make:
    unlike close(), it takes no buffered-io lock, so it is safe from a signal
    handler that interrupts a blocked read on the SAME thread. After shutdown
    the next read sees EOF, exactly like a real recv() returning 0."""
    def __init__(self):
        self.shutdown_calls = []

    @property
    def shut(self):
        return bool(self.shutdown_calls)

    def shutdown(self, how):
        self.shutdown_calls.append(how)


class _FakeSSE:
    """Iterable SSE response double with the REAL failure mode baked in:
    - mirrors the live shape (`fp.raw._sock`, HTTPResponse.fp is
      socket.makefile('rb') whose raw SocketIO holds the socket), and
    - close() raises RuntimeError while a read is in progress, like CPython's
      BufferedReader same-thread reentrancy guard does when a signal handler
      calls close() under the main thread's blocked read. Any regression back
      to close()-from-the-handler therefore fails here instead of greening a
      mechanism that is a silent no-op in production."""
    def __init__(self, lines=()):
        self._lines = list(lines)
        self.reads = 0
        self.closed = False
        self._reading = False
        self.sock = _FakeSock()
        self.fp = types.SimpleNamespace(raw=types.SimpleNamespace(_sock=self.sock))

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise ValueError("I/O operation on closed file.")
        if self.sock.shut:
            raise StopIteration  # recv() returned 0 after shutdown -> EOF
        if not self._lines:
            raise StopIteration
        self.reads += 1
        return self._lines.pop(0)

    def close(self):
        if self._reading:
            # CPython's buffered-io guard: the blocked read holds the
            # BufferedReader lock, so a same-thread close() is rejected.
            raise RuntimeError("reentrant call inside <_io.BufferedReader>")
        self.closed = True


class _WakeMidReadSSE(_FakeSSE):
    """Simulates SIGUSR1 arriving while the main thread is BLOCKED in the SSE
    read. The handler runs with the read in progress (reentrancy guard live,
    so close() raises RuntimeError); only sock.shutdown() can terminate the
    read, by making the PEP 475-retried recv return EOF."""
    def __next__(self):
        self._reading = True
        try:
            # Use the module-level `listener` binding, NOT a fresh import:
            # another test file swaps a stub into sys.modules[...].
            listener._handle_wake_signal(signal.SIGUSR1, None)
        finally:
            self._reading = False
        if self.sock.shut:
            raise StopIteration  # blocked recv returned EOF after shutdown
        # Nothing broke the socket: PEP 475 resumes the read, which only ends
        # when real bytes arrive. Surface that as bounded reads so a wedged
        # listener FAILS the assertions instead of hanging the suite.
        self.reads += 1
        if self.reads >= 3:
            raise StopIteration
        return b": wedged -- read survived the wake\n"


class WakeRaceFixTest(unittest.TestCase):
    """Review fixes: one-flag/two-consumer race, dead-socket wake, and
    wake-consumed-before-refresh-succeeds."""

    def setUp(self):
        listener._wake_requested = False
        listener._wake_reconnect = False
        listener._sse_response = None
        listener._sse_socket = None

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

    def test_handler_shuts_down_live_sse_socket(self):
        # A flag alone cannot interrupt a read blocked on a half-open
        # post-suspend socket (PEP 475 retries it), and close() is forbidden
        # from the handler (BufferedReader reentrancy guard -> RuntimeError
        # while the main thread is blocked in the read). The handler must
        # shutdown() the stashed SOCKET — no io lock involved — so the blocked
        # recv returns EOF and the read terminates.
        resp = _FakeSSE()
        listener._sse_response = resp
        listener._sse_socket = resp.sock
        listener._handle_wake_signal(signal.SIGUSR1, None)
        self.assertEqual(resp.sock.shutdown_calls, [socket.SHUT_RDWR])
        self.assertFalse(resp.closed)  # close() is the wedge, never call it

    def test_handler_never_calls_close_mid_read(self):
        # Direct regression guard for the production wedge: handler fires
        # while the read is in progress; a close() attempt would raise the
        # (modeled) reentrant RuntimeError and previously got swallowed by a
        # bare except, leaving the listener wedged.
        resp = _FakeSSE()
        resp._reading = True
        listener._sse_response = resp
        listener._sse_socket = resp.sock
        listener._handle_wake_signal(signal.SIGUSR1, None)  # must not raise
        self.assertTrue(resp.sock.shut)

    def test_handler_tolerates_no_stashed_response(self):
        listener._sse_response = None
        listener._sse_socket = None
        listener._handle_wake_signal(signal.SIGUSR1, None)  # must not raise
        self.assertTrue(listener._wake_requested)

    def test_handler_tolerates_dead_socket(self):
        class _DeadSock:
            def shutdown(self, how):
                raise OSError(57, "Socket is not connected")
        listener._sse_socket = _DeadSock()
        listener._handle_wake_signal(signal.SIGUSR1, None)  # must not raise
        self.assertTrue(listener._wake_requested)

    def test_stream_stashes_response_for_wake_close(self):
        resp = _FakeSSE()
        self._stream(resp)
        self.assertIs(listener._sse_response, resp)

    def test_stream_stashes_underlying_socket_for_wake_shutdown(self):
        # stream() must dig the socket out at connect time (fp.raw._sock, the
        # HTTPResponse shape) so the handler does zero discovery work.
        resp = _FakeSSE()
        self._stream(resp)
        self.assertIs(listener._sse_socket, resp.sock)

    def test_double_models_buffered_reader_reentrancy(self):
        # Guard on the double itself: close() during a blocked read must raise
        # like the real BufferedReader, so a regression back to close()-from-
        # the-handler can never green-light against this double again.
        resp = _FakeSSE([b"x\n"])
        resp._reading = True
        with self.assertRaises(RuntimeError):
            resp.close()

    def test_stream_does_not_break_or_consume_on_beat_flag(self):
        # Churn half of the race: with the (beat-owned) flag set, an arriving
        # SSE line must neither abort the stream nor consume the beat's flag.
        listener._wake_requested = True
        resp = _FakeSSE([b"event: ping\n", b"\n", b"event: ping\n", b"\n"])
        self._stream(resp)
        self.assertEqual(resp.reads, 4)               # drained, no early break
        self.assertTrue(listener._wake_requested)     # beat's flag untouched

    def test_stream_wake_shutdown_returns_cleanly(self):
        # Dead-socket wake, with the read in progress (reentrancy guard live):
        # the handler shuts the socket down, the blocked read ends with EOF —
        # NOT an exception — and stream() must classify that as a clean wake
        # reconnect (normal return, flag consumed) so main() resets backoff
        # and the circuit breaker never counts it as a failure.
        resp = _WakeMidReadSSE([b"event: ping\n"])
        self._stream(resp)                            # must NOT raise
        self.assertTrue(resp.sock.shut)               # shutdown, not close()
        self.assertEqual(resp.reads, 0)               # wake ended the read; no
                                                      # real bytes were needed
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


class WakeRealSocketTest(unittest.TestCase):
    """End-to-end against a REAL socket pair + makefile('rb') — the same shape
    as HTTPResponse.fp — with a real signal delivered to the main thread while
    it is blocked in the read. This is the exact production scenario the
    doubles previously could not represent: a close() from the handler hits
    CPython's BufferedReader reentrancy guard (RuntimeError), gets swallowed,
    PEP 475 retries the recv, and the listener stays wedged until real bytes
    arrive. sock.shutdown() must instead terminate the read with EOF."""

    def test_wake_signal_unblocks_real_blocked_read(self):
        a, b = socket.socketpair()
        rf = a.makefile("rb")
        self.addCleanup(b.close)
        self.addCleanup(a.close)
        self.addCleanup(rf.close)
        listener._wake_requested = False
        listener._wake_reconnect = False
        listener._sse_response = None
        listener._sse_socket = a
        self.addCleanup(setattr, listener, "_sse_socket", None)
        # Watchdog: if the wake fails to break the read, feed real bytes so
        # the test FAILS visibly (readline returns them) instead of hanging
        # the whole suite — the empirical signature of the original bug.
        watchdog = threading.Timer(
            2.0, lambda: b.sendall(b"watchdog: read survived the wake\n"))
        watchdog.daemon = True
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        old = signal.signal(signal.SIGALRM, listener._handle_wake_signal)
        self.addCleanup(signal.signal, signal.SIGALRM, old)
        self.addCleanup(signal.setitimer, signal.ITIMER_REAL, 0)
        signal.setitimer(signal.ITIMER_REAL, 0.15)
        line = rf.readline()        # blocks; only the wake may terminate it
        self.assertEqual(line, b"")  # EOF from shutdown, not watchdog bytes
        self.assertTrue(listener._wake_requested)
        self.assertTrue(listener._wake_reconnect)
