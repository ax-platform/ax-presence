import unittest
import fleet_daemon as fd

def snap(**kw):
    base = dict(alive=True, crashloop=False, token_ttl_s=600,
                receipt_age_s=60, sse_connected=True, disabled=False)
    base.update(kw)
    return base

class VerdictTest(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(fd.verdict(snap()), "OK")

    def test_disabled_wins_over_everything(self):
        self.assertEqual(fd.verdict(snap(disabled=True, alive=False)), "DISABLED")

    def test_down_when_not_alive(self):
        self.assertEqual(fd.verdict(snap(alive=False)), "DOWN")

    def test_crashloop(self):
        self.assertEqual(fd.verdict(snap(alive=False, crashloop=True)), "CRASHLOOP")

    def test_token_wedge(self):
        self.assertEqual(fd.verdict(snap(token_ttl_s=-7200)), "TOKEN")

    def test_deaf_when_connected_but_no_receipt(self):
        self.assertEqual(fd.verdict(snap(receipt_age_s=3600)), "DEAF")

    def test_quiet_when_no_receipt_data_yet(self):
        self.assertEqual(fd.verdict(snap(receipt_age_s=None)), "QUIET")
