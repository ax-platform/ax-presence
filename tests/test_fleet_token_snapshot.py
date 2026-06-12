import json, os, tempfile, unittest
import fleet_daemon as fd

class TokenSnapshotTest(unittest.TestCase):
    def _tok(self, expires_at):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"access_token": "x", "refresh_token": "r",
                   "expires_at": expires_at}, f)
        f.close(); self.addCleanup(os.unlink, f.name)
        return f.name

    def test_ttl_positive_for_future_expiry(self):
        p = self._tok(2_000)
        self.assertEqual(fd.token_ttl(p, now=1_400), 600)

    def test_ttl_negative_for_expired(self):
        p = self._tok(1_000)
        self.assertEqual(fd.token_ttl(p, now=1_400), -400)

    def test_never_modifies_the_file(self):
        p = self._tok(2_000)
        before = open(p).read()
        fd.token_ttl(p, now=1_400)
        self.assertEqual(open(p).read(), before)

    def test_unreadable_file_returns_none(self):
        self.assertIsNone(fd.token_ttl("/nonexistent.json", now=0))
