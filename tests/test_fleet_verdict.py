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

    def test_quiet_when_connected_but_no_recent_receipt(self):
        self.assertEqual(fd.verdict(snap(receipt_age_s=3600)), "QUIET")

    def test_deaf_when_listener_disconnected_and_no_recent_receipt(self):
        self.assertEqual(fd.verdict(snap(receipt_age_s=3600, sse_connected=False)), "DEAF")

    def test_space_drift_has_its_own_verdict(self):
        self.assertEqual(fd.verdict(snap(space_state="drift")), "SPACE")

    def test_current_401_is_token_drift_even_before_expiry(self):
        self.assertEqual(fd.verdict(snap(currently_401=True)), "TOKEN")

    def test_quiet_when_no_receipt_data_yet(self):
        self.assertEqual(fd.verdict(snap(receipt_age_s=None)), "QUIET")
