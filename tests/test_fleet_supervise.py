import os, signal, sys, time, unittest
import fleet_daemon as fd

STUB = [sys.executable, "-c", "import time; time.sleep(60)"]

class SuperviseTest(unittest.TestCase):
    def test_spawn_records_pid_and_child_runs(self):
        ag = fd.AgentProc("stub", STUB, env=dict(os.environ), log_path=os.devnull)
        ag.spawn()
        self.addCleanup(ag.terminate)
        self.assertTrue(ag.alive())
        self.assertGreater(ag.pid, 0)

    def test_terminate_then_not_alive(self):
        ag = fd.AgentProc("stub", STUB, env=dict(os.environ), log_path=os.devnull)
        ag.spawn(); ag.terminate()
        deadline = time.time() + 5
        while ag.alive() and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(ag.alive())

    def test_wake_fanout_signals_alive_children(self):
        ag = fd.AgentProc("stub", STUB, env=dict(os.environ), log_path=os.devnull)
        ag.spawn(); self.addCleanup(ag.terminate)
        sent = fd.wake_fanout([ag])
        self.assertEqual(sent, ["stub"])   # SIGUSR1 delivered without killing it
        self.assertTrue(ag.alive())
