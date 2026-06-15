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

    def test_deaf_when_disconnected_and_stale(self):
        # Stale receipts AND a dropped SSE socket = genuinely deaf -> bounce.
        self.assertEqual(
            fd.verdict(snap(receipt_age_s=5500, sse_connected=False)), "DEAF")

    def test_unknown_connection_with_stale_feed_is_quiet_not_deaf(self):
        # Missing signal evidence is unknown, not proof that the listener is
        # disconnected. Do not bounce/page on receipt staleness alone.
        self.assertEqual(
            fd.verdict(snap(receipt_age_s=5500, sse_connected=None)), "QUIET")

    def test_connected_but_stale_feed_is_quiet_not_deaf(self):
        # Soak cycle 1 showed idle channels can legitimately send nothing for
        # hours. Only an explicitly dropped SSE socket is DEAF; receipt-age
        # staleness alone remains QUIET.
        self.assertEqual(
            fd.verdict(snap(receipt_age_s=5500, sse_connected=True)), "QUIET")


    def test_quiet_evening_gap_is_not_deaf(self):
        self.assertEqual(fd.verdict(snap(receipt_age_s=3600)), "OK")

    def test_quiet_when_connected_but_no_recent_receipt(self):
        self.assertEqual(fd.verdict(snap(receipt_age_s=5500)), "QUIET")

    def test_space_drift_has_its_own_verdict(self):
        self.assertEqual(fd.verdict(snap(space_state="drift")), "SPACE")

    def test_current_401_is_token_drift_even_before_expiry(self):
        self.assertEqual(fd.verdict(snap(currently_401=True)), "TOKEN")

    def test_quiet_when_no_receipt_data_yet(self):
        self.assertEqual(fd.verdict(snap(receipt_age_s=None)), "QUIET")

    def test_deaf_when_no_receipt_data_and_listener_disconnected(self):
        self.assertEqual(fd.verdict(snap(receipt_age_s=None, sse_connected=False)), "DEAF")
