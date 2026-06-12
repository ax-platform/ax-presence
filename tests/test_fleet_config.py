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

    def test_unknown_platform_fails_loudly_at_load(self):
        # Phase 1 is single-platform: a typo'd or future platform key must
        # fail at config load, not silently spawn an ax listener for it.
        bad = SAMPLE + ('\n[agents.hermes_bot]\n'
                        'token_file = "~/.ax/hermes-listener.json"\n'
                        'platform = "hermes"\n')
        with self.assertRaises(ValueError) as cm:
            fd.load_fleet_config(self._write(bad))
        self.assertIn("hermes", str(cm.exception))
        self.assertIn("platform", str(cm.exception))

    def test_both_parsers_agree_disabled_is_a_real_boolean(self):
        # On Python <3.11 the fallback parser used to return the STRING
        # "false", which is truthy — a 'disabled = false' agent would have
        # read as disabled. true/false literals must become booleans and
        # both parsers must agree.
        try:
            import tomllib
        except ImportError:
            tomllib = None
        sample = (SAMPLE
                  + "\n[agents.night_owl]\n"
                  + 'token_file = "~/.ax/night_owl-listener.json"\n'
                  + "disabled = true\n"
                  + "\n[agents.spark]\n"
                  + 'token_file = "~/.ax/spark-listener.json"\n'
                  + "disabled = false\n")
        d = fd._parse_toml_minimal(sample)
        self.assertIs(d["agents"]["night_owl"]["disabled"], True)
        self.assertIs(d["agents"]["spark"]["disabled"], False)
        if tomllib:
            t = tomllib.loads(sample)
            self.assertEqual(t["agents"]["night_owl"]["disabled"],
                             d["agents"]["night_owl"]["disabled"])
            self.assertEqual(t["agents"]["spark"]["disabled"],
                             d["agents"]["spark"]["disabled"])
