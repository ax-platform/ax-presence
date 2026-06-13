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

    def test_deaf_when_no_connection_evidence_and_stale(self):
        # No signal file yet (sse_connected=None) + stale receipts: we can't
        # prove the listener is connected, so treat as DEAF. (This is the
        # watchdog-episode path: a child with no signal file bounces.)
        self.assertEqual(
            fd.verdict(snap(receipt_age_s=5500, sse_connected=None)), "DEAF")

    def test_connected_but_stale_feed_is_quiet_not_deaf(self):
        # Soak cycle 1 (2026-06-13): THREE false-positive bounces, the last at
        # the raised 5400s line — an idle overnight channel legitimately sends
        # nothing for hours, so a pure receipt-age threshold false-positives at
        # ANY value. A listener whose SSE socket is up is QUIET, not deaf; only
        # an actually-dropped socket is DEAF. (The half-open case — connected
        # flag true but socket dead — needs the stream-activity clock from the
        # monitor-hardening detector, task 9d1c13cb.)
        self.assertEqual(
            fd.verdict(snap(receipt_age_s=5500, sse_connected=True)), "QUIET")

    def test_quiet_evening_gap_is_not_deaf(self):
        self.assertEqual(fd.verdict(snap(receipt_age_s=3600)), "OK")

    def test_quiet_when_no_receipt_data_yet(self):
        self.assertEqual(fd.verdict(snap(receipt_age_s=None)), "QUIET")
