import os, tempfile, unittest
import fleet_daemon as fd

class ReceiptScanTest(unittest.TestCase):
    def _log(self, lines):
        f = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
        f.write("\n".join(lines) + "\n"); f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_age_of_newest_notify_line(self):
        p = self._log(["1000 [status] SSE connected",
                       "1500 NOTIFY @x mention ...",
                       "1700 NOTIFY @x mention ..."])
        self.assertEqual(fd.last_receipt_age(p, now=2000), 300)

    def test_none_when_no_notify_lines(self):
        p = self._log(["1000 [status] SSE connected"])
        self.assertIsNone(fd.last_receipt_age(p, now=2000))

    def test_stamp_line_format(self):
        self.assertEqual(fd.stamp_line("NOTIFY hi", now=1234), "1234 NOTIFY hi")
