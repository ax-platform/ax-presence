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
