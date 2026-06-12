import unittest
import fleet_daemon as fd

class BackoffTest(unittest.TestCase):
    def test_delay_grows_exponentially_and_caps(self):
        delays = [fd.respawn_delay(n) for n in range(8)]
        self.assertEqual(delays[0], 1)
        self.assertTrue(all(b >= a for a, b in zip(delays, delays[1:])))
        self.assertLessEqual(max(delays), 300)

    def test_crashloop_when_5_failures_inside_10_minutes(self):
        now = 10_000
        recent = [now - 60 * i for i in range(5)]
        self.assertTrue(fd.is_crashloop(recent, now))

    def test_not_crashloop_when_failures_are_old(self):
        now = 10_000
        old = [now - 3600 * i for i in range(1, 6)]
        self.assertFalse(fd.is_crashloop(old, now))
