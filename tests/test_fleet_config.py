import os, tempfile, unittest
import fleet_daemon as fd

SAMPLE = """
[fleet]
device = "laptop"
sponsor = "@madtank"

[agents.claude_prime]
token_file = "~/.ax/claude_prime-listener.json"
platform = "ax"
catchup = "ask"

[agents.canvas]
token_file = "~/.ax/canvas-listener.json"
"""

class FleetConfigTest(unittest.TestCase):
    def _write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        f.write(text); f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_loads_fleet_and_agents(self):
        cfg = fd.load_fleet_config(self._write(SAMPLE))
        self.assertEqual(cfg["fleet"]["device"], "laptop")
        self.assertEqual(cfg["fleet"]["sponsor"], "@madtank")
        self.assertIn("claude_prime", cfg["agents"])
        self.assertIn("canvas", cfg["agents"])

    def test_token_file_is_tilde_expanded(self):
        cfg = fd.load_fleet_config(self._write(SAMPLE))
        self.assertTrue(os.path.isabs(cfg["agents"]["canvas"]["token_file"]))

    def test_defaults_applied(self):
        cfg = fd.load_fleet_config(self._write(SAMPLE))
        self.assertEqual(cfg["agents"]["canvas"]["platform"], "ax")
        self.assertEqual(cfg["agents"]["canvas"]["catchup"], "ask")

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            fd.load_fleet_config("/nonexistent/fleet.toml")

    def test_no_agents_raises(self):
        with self.assertRaises(ValueError):
            fd.load_fleet_config(self._write("[fleet]\ndevice = \"x\"\n"))

    def test_minimal_parser_matches_subset(self):
        d = fd._parse_toml_minimal(SAMPLE)
        self.assertEqual(d["fleet"]["device"], "laptop")
        self.assertEqual(d["agents"]["claude_prime"]["catchup"], "ask")
