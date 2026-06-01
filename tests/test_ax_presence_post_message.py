import io
import json
import unittest
from unittest import mock

import ax_presence_listener as listener


class _Response:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


class AXPresencePostMessageTest(unittest.TestCase):
    def test_post_message_recovers_id_when_post_returns_empty_body(self):
        calls = []
        headers = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            headers.append(dict(req.header_items()))
            if req.full_url == listener.MESSAGES_URL:
                return _Response(b"")
            if req.full_url.startswith(listener.MESSAGES_URL + "?"):
                body = json.dumps({
                    "messages": [
                        {"id": "other", "content": "different", "parent_id": "parent-1"},
                        {"id": "reply-123", "content": "hello", "parent_id": "parent-1"},
                    ]
                }).encode()
                return _Response(body)
            if req.full_url == listener.MESSAGES_URL + "/reply-123":
                return _Response(b"{}")
            raise AssertionError(f"unexpected url {req.full_url}")

        with mock.patch.object(listener, "load_tok", return_value={"access_token": "token"}), \
             mock.patch.object(listener.urllib.request, "urlopen", side_effect=fake_urlopen):
            mid = listener.post_message("hello", parent_id="parent-1", space_id="space-1")

        self.assertEqual(mid, "reply-123")
        self.assertEqual(listener._last_reply_at > 0, True)
        self.assertIn(listener.MESSAGES_URL, calls)
        self.assertTrue(any(url.startswith(listener.MESSAGES_URL + "?") for url in calls))
        for h in headers:
            self.assertEqual(h.get("X-agent-id"), listener.AGENT_ID)
            self.assertEqual(h.get("X-space-id"), "space-1")

    def test_post_message_returns_none_when_empty_body_cannot_be_recovered(self):
        def fake_urlopen(req, timeout=None):
            if req.full_url == listener.MESSAGES_URL:
                return _Response(b"")
            if req.full_url.startswith(listener.MESSAGES_URL + "?"):
                return _Response(json.dumps({"messages": []}).encode())
            raise AssertionError(f"unexpected url {req.full_url}")

        with mock.patch.object(listener, "load_tok", return_value={"access_token": "token"}), \
             mock.patch.object(listener.urllib.request, "urlopen", side_effect=fake_urlopen):
            mid = listener.post_message("hello", parent_id="parent-1", space_id="space-1")

        self.assertIsNone(mid)


if __name__ == "__main__":
    unittest.main()
