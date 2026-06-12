import json, os, tempfile, unittest, urllib.error
from unittest import mock
import fleet_daemon as fd

class TelemetryPostTest(unittest.TestCase):
    def test_404_is_swallowed(self):
        err = urllib.error.HTTPError("u", 404, "nf", None, None)
        with mock.patch.object(fd.urllib.request, "urlopen", side_effect=err):
            fd.post_telemetry({"seq": 1}, base="https://x", token="t")  # must not raise

    def test_skips_when_no_token(self):
        with mock.patch.object(fd.urllib.request, "urlopen") as up:
            fd.post_telemetry({"seq": 1}, base="https://x", token=None)
            up.assert_not_called()

    def test_state_file_written_0600(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "fleet-state.json")
        fd.write_state_file(p, {"seq": 7})
        self.assertEqual(json.load(open(p))["seq"], 7)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
