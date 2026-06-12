import json
import time
import unittest
import urllib.error
from unittest import mock

import ax_presence_listener as listener


class _Response:
    def __init__(self, body=b"{}"):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


def _http_401(url):
    return urllib.error.HTTPError(url, 401, "Unauthorized", hdrs=None, fp=None)


def _tok(access="stale", expires_in_s=-100):
    now = int(time.time())
    return {"access_token": access, "refresh_token": "rt", "client_id": "cid",
            "expires_at": now + expires_in_s, "obtained_at": now}


class PresenceBeatTest(unittest.TestCase):
    """The platform heartbeat must never post with a token the file already
    says is expired (the laptop-sleep 401 storm, 2026-06-12)."""

    def test_presence_beat_refreshes_expired_token_before_posting(self):
        beats = []

        def fake_urlopen(req, timeout=None):
            self.assertEqual(req.full_url, listener.HEARTBEAT_URL)
            beats.append(dict(req.header_items()))
            return _Response()

        with mock.patch.object(listener, "load_tok", return_value=_tok()), \
             mock.patch.object(listener, "refresh", return_value=_tok("fresh", 900)) as rf, \
             mock.patch.object(listener.urllib.request, "urlopen", side_effect=fake_urlopen):
            listener._presence_beat()

        rf.assert_called_once()
        self.assertEqual(len(beats), 1)
        self.assertEqual(beats[0].get("Authorization"), "Bearer fresh")

    def test_presence_beat_forces_refresh_and_retries_once_on_401(self):
        beats = []

        def fake_urlopen(req, timeout=None):
            beats.append(dict(req.header_items()))
            if len(beats) == 1:
                raise _http_401(req.full_url)
            return _Response()

        with mock.patch.object(listener, "load_tok", return_value=_tok("looks-valid", 900)), \
             mock.patch.object(listener, "refresh", return_value=_tok("fresh2", 900)) as rf, \
             mock.patch.object(listener.urllib.request, "urlopen", side_effect=fake_urlopen):
            listener._presence_beat()

        rf.assert_called_once()
        self.assertEqual(len(beats), 2)
        self.assertEqual(beats[1].get("Authorization"), "Bearer fresh2")
        self.assertFalse(listener._currently_401)

    def test_presence_beat_gives_up_after_one_retry_on_persistent_401(self):
        beats = []

        def fake_urlopen(req, timeout=None):
            beats.append(req.full_url)
            raise _http_401(req.full_url)

        with mock.patch.object(listener, "load_tok", return_value=_tok("looks-valid", 900)), \
             mock.patch.object(listener, "refresh", return_value=_tok("fresh3", 900)) as rf, \
             mock.patch.object(listener.urllib.request, "urlopen", side_effect=fake_urlopen):
            with self.assertRaises(urllib.error.HTTPError):
                listener._presence_beat()

        rf.assert_called_once()
        self.assertEqual(len(beats), 2)
        self.assertTrue(listener._currently_401)


class ProactiveTickTest(unittest.TestCase):
    """The refresh timer must survive host suspend: it re-reads wall-clock
    expiry every slice instead of trusting one long time.sleep (which does not
    advance during macOS sleep)."""

    def test_tick_sleeps_at_most_sixty_seconds_even_when_expiry_is_far(self):
        with mock.patch.object(listener, "load_tok", return_value=_tok("ok", 3600)), \
             mock.patch.object(listener, "refresh") as rf:
            sleep_for = listener._proactive_tick()

        rf.assert_not_called()
        self.assertLessEqual(sleep_for, 60)
        self.assertGreater(sleep_for, 0)

    def test_tick_refreshes_immediately_when_wall_clock_passed_expiry(self):
        with mock.patch.object(listener, "load_tok", return_value=_tok()), \
             mock.patch.object(listener, "refresh", return_value=_tok("fresh", 900)) as rf:
            sleep_for = listener._proactive_tick()

        rf.assert_called_once()
        self.assertLessEqual(sleep_for, 60)

    def test_tick_backs_off_but_does_not_raise_when_refresh_fails(self):
        with mock.patch.object(listener, "load_tok", return_value=_tok()), \
             mock.patch.object(listener, "refresh", side_effect=OSError("boom")):
            sleep_for = listener._proactive_tick()

        self.assertGreater(sleep_for, 0)
        self.assertLessEqual(sleep_for, 60)


class StaleTokenCallSitesTest(unittest.TestCase):
    """Best-effort posts must also use the refreshing token path, not the raw
    file token (they 401-spammed alongside the heartbeat during the incident)."""

    def _capture(self, expect_url):
        sent = {}

        def fake_urlopen(req, timeout=None):
            if req.full_url == expect_url:
                sent.update(dict(req.header_items()))
                return _Response()
            return _Response()

        return sent, fake_urlopen

    def test_post_processing_status_refreshes_expired_token(self):
        sent, fake_urlopen = self._capture(listener.PROCESSING_URL)
        with mock.patch.object(listener, "load_tok", return_value=_tok()), \
             mock.patch.object(listener, "refresh", return_value=_tok("fresh", 900)), \
             mock.patch.object(listener.urllib.request, "urlopen", side_effect=fake_urlopen):
            listener.post_processing_status("msg-1", "working")
        self.assertEqual(sent.get("Authorization"), "Bearer fresh")

    def test_post_message_refreshes_expired_token(self):
        sent, fake_urlopen = self._capture(listener.MESSAGES_URL)
        with mock.patch.object(listener, "load_tok", return_value=_tok()), \
             mock.patch.object(listener, "refresh", return_value=_tok("fresh", 900)), \
             mock.patch.object(listener.urllib.request, "urlopen", side_effect=fake_urlopen):
            listener.post_message("hello", space_id="space-1")
        self.assertEqual(sent.get("Authorization"), "Bearer fresh")

    def test_alert_refreshes_expired_token(self):
        sent, fake_urlopen = self._capture(listener.MESSAGES_URL)
        with mock.patch.object(listener, "load_tok", return_value=_tok()), \
             mock.patch.object(listener, "refresh", return_value=_tok("fresh", 900)), \
             mock.patch.object(listener.urllib.request, "urlopen", side_effect=fake_urlopen):
            listener.alert("test alert")
        self.assertEqual(sent.get("Authorization"), "Bearer fresh")


if __name__ == "__main__":
    unittest.main()
