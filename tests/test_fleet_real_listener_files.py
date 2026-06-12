"""The daemon must consume what the REAL listener writes.

ax_presence_listener.py heartbeat_loop writes TWO files:
  - AX_HEARTBEAT_FILE gets a BARE INT epoch:  f.write(str(int(time.time())))
  - ~/.ax/{AGENT_HANDLE}-signal.json gets the rich dict (handle/ts/connected/
    currently_401/mentions_seen/replies_sent/last_reply_at/responsiveness_ratio)

The original read_heartbeat() expected a JSON dict in the heartbeat file, so
json.load("1765500000") -> int and .get() raised AttributeError, killing the
daemon ~15-30s after a real child started. These tests pin the real formats.
"""
import json, os, tempfile, time, unittest
import fleet_daemon as fd

NONE_FIELDS = {"sse_connected": None, "mentions_seen": None,
               "replies_sent": None, "currently_401": None}


def write_real_heartbeat(path, epoch=None):
    """EXACTLY what the listener writes (ax_presence_listener.py:452-454)."""
    fd_ = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd_, "w") as f:
        f.write(str(int(epoch if epoch is not None else time.time())))


def write_real_signal(path, **overrides):
    """EXACTLY the dict the listener writes (ax_presence_listener.py:458-466)."""
    sig = {"handle": "a", "ts": int(time.time()), "connected": True,
           "currently_401": False, "mentions_seen": 142,
           "replies_sent": 131, "last_reply_at": int(time.time()),
           "responsiveness_ratio": round(131 / 142, 3)}
    sig.update(overrides)
    sfd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(sfd, "w") as sf:
        json.dump(sig, sf)


class ReadHeartbeatRealFormatTest(unittest.TestCase):
    """read_heartbeat must accept the listener's bare-int epoch and any
    legacy/none/garbage content: non-dict => all-None fields, NEVER raise."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.path = os.path.join(self.d, "hb")

    def test_bare_int_epoch_the_real_listener_format(self):
        write_real_heartbeat(self.path, epoch=1765500000)
        self.assertEqual(fd.read_heartbeat(self.path), NONE_FIELDS)

    def test_legacy_dict_content_still_yields_fields(self):
        with open(self.path, "w") as f:
            json.dump({"connected": True, "mentions_seen": 7,
                       "replies_sent": 5, "currently_401": False}, f)
        self.assertEqual(fd.read_heartbeat(self.path),
                         {"sse_connected": True, "mentions_seen": 7,
                          "replies_sent": 5, "currently_401": False})

    def test_garbage_and_non_dict_json_never_raise(self):
        for content in ("not json {{{", "null", '"a string"', "[1, 2]", ""):
            with self.subTest(content=content):
                with open(self.path, "w") as f:
                    f.write(content)
                self.assertEqual(fd.read_heartbeat(self.path), NONE_FIELDS)


class SignalPathTest(unittest.TestCase):
    """The daemon derives ~/.ax/<agent>-signal.json the same way the
    listener does, with a per-agent signal_file override mirroring
    heartbeat_file."""

    def test_default_mirrors_listener_hardcoded_path(self):
        self.assertEqual(fd.signal_path("night_owl", {}),
                         os.path.expanduser("~/.ax/night_owl-signal.json"))

    def test_signal_file_config_override_with_expanduser(self):
        a = {"signal_file": "~/elsewhere/a-sig.json"}
        self.assertEqual(fd.signal_path("a", a),
                         os.path.expanduser("~/elsewhere/a-sig.json"))


class _StubProc:
    def __init__(self, name, log_path):
        self.name, self.log_path = name, log_path
        self.pid = 4411
        self.failure_times = []

    def alive(self):
        return True

    def signal_wake(self):
        pass

    def spawn(self):
        pass


class DaemonTickRealFilesTest(unittest.TestCase):
    """End-to-end through daemon_tick with the files a REAL child produces:
    bare-int heartbeat + rich signal.json. The daemon must not die, and the
    rich telemetry fields must come from the signal file."""

    def test_tick_survives_real_files_and_sources_fields_from_signal(self):
        d = tempfile.mkdtemp()
        log = os.path.join(d, "a.log")
        with open(log, "w") as f:
            f.write("4990 NOTIFY @a mention\n")
        tok = os.path.join(d, "tok.json")
        with open(tok, "w") as f:
            json.dump({"expires_at": 5630}, f)
        hb = os.path.join(d, "a-listener-heartbeat")
        write_real_heartbeat(hb, epoch=5000)
        sig = os.path.join(d, "a-signal.json")
        write_real_signal(sig, ts=5000, last_reply_at=4990)
        cfg = {"fleet": {"device": "laptop"},
               "agents": {"a": {"token_file": tok, "heartbeat_file": hb,
                                "signal_file": sig}}}
        agents = {"a": _StubProc("a", log)}
        ctx = fd.new_ctx(cfg, agents, token=None,
                         state_file=os.path.join(d, "state.json"),
                         mono_now=1000.0, wall_now=5000.0)
        fd.daemon_tick(ctx, mono_now=1015.0, wall_now=5015.0)   # the old code
        body = fd.daemon_tick(ctx, mono_now=1030.0, wall_now=5030.0)  # died here
        snap = body["agents"]["a"]
        self.assertEqual(snap["verdict"], "OK")
        self.assertIs(snap["sse_connected"], True)
        self.assertEqual(snap["mentions_seen"], 142)
        self.assertEqual(snap["replies_sent"], 131)
        self.assertIs(snap["currently_401"], False)

    def test_tick_survives_real_heartbeat_with_no_signal_file_yet(self):
        d = tempfile.mkdtemp()
        log = os.path.join(d, "a.log")
        open(log, "w").close()
        hb = os.path.join(d, "a-listener-heartbeat")
        write_real_heartbeat(hb)
        cfg = {"fleet": {"device": "laptop"},
               "agents": {"a": {"token_file": os.path.join(d, "no-tok.json"),
                                "heartbeat_file": hb,
                                "signal_file": os.path.join(d, "absent.json")}}}
        agents = {"a": _StubProc("a", log)}
        ctx = fd.new_ctx(cfg, agents, token=None,
                         state_file=os.path.join(d, "state.json"),
                         mono_now=1000.0, wall_now=5000.0)
        fd.daemon_tick(ctx, mono_now=1015.0, wall_now=5015.0)
        body = fd.daemon_tick(ctx, mono_now=1030.0, wall_now=5030.0)
        snap = body["agents"]["a"]
        self.assertIsNone(snap["sse_connected"])
        self.assertIsNone(snap["mentions_seen"])


if __name__ == "__main__":
    unittest.main()
