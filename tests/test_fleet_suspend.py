import unittest
import fleet_daemon as fd

class SuspendTickTest(unittest.TestCase):
    def test_no_event_when_clocks_agree(self):
        st = {"mono": 100.0, "wall": 1000.0}
        self.assertIsNone(fd.suspend_tick(st, mono_now=115.0, wall_now=1015.0))

    def test_event_when_wall_jumps_past_monotonic(self):
        st = {"mono": 100.0, "wall": 1000.0}
        ev = fd.suspend_tick(st, mono_now=115.0, wall_now=42_000.0)
        self.assertEqual(ev["kind"], "suspend_detected")
        self.assertAlmostEqual(ev["for_s"], 41_000 - 15, delta=1)

    def test_tick_updates_state_for_next_round(self):
        st = {"mono": 100.0, "wall": 1000.0}
        fd.suspend_tick(st, mono_now=115.0, wall_now=1015.0)
        self.assertEqual(st["mono"], 115.0)
        self.assertEqual(st["wall"], 1015.0)
