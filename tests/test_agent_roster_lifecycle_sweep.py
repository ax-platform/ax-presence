import importlib.util
import json
import sys
from pathlib import Path

NOW = 2_000_000_000
DAY = 24 * 60 * 60


def _load_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "agent-roster-lifecycle-sweep.py"
    spec = importlib.util.spec_from_file_location("agent_roster_lifecycle_sweep", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


sweep = _load_module()


def test_classifies_fresh_heartbeat_as_active():
    state, age, reason = sweep.classify_heartbeat(NOW - DAY, now=NOW)
    assert state == "active"
    assert age == DAY
    assert reason == "heartbeat_fresh"


def test_classifies_7_day_gap_as_paused_and_not_mentionable(tmp_path):
    (tmp_path / "daimon-listener-heartbeat").write_text(str(NOW - (7 * DAY)))
    records = sweep.discover_records(tmp_path, now=NOW)
    assert len(records) == 1
    rec = records[0]
    assert rec.handle == "daimon"
    assert rec.desired_lifecycle_state == "paused"
    assert rec.mentionable is False
    assert rec.reason == "heartbeat_absent_7d"


def test_classifies_30_day_gap_as_archived(tmp_path):
    (tmp_path / "atlas-listener-heartbeat").write_text(str(NOW - (30 * DAY)))
    rec = sweep.discover_records(tmp_path, now=NOW)[0]
    assert rec.desired_lifecycle_state == "archived"
    assert rec.mentionable is False
    assert rec.reason == "heartbeat_absent_30d"


def test_missing_heartbeat_uses_signal_fallback(tmp_path):
    (tmp_path / "nyx-signal.json").write_text(json.dumps({"ts": NOW - (8 * DAY)}))
    rec = sweep.discover_records(tmp_path, now=NOW)[0]
    assert rec.handle == "nyx"
    assert rec.desired_lifecycle_state == "paused"
    assert rec.signal_path is not None


def test_missing_or_unparseable_signal_is_review_not_archive(tmp_path):
    (tmp_path / "peach-signal.json").write_text("not json")
    rec = sweep.discover_records(tmp_path, now=NOW)[0]
    assert rec.desired_lifecycle_state == "no_signal"
    assert rec.mentionable is False
    assert rec.reason == "missing_or_unparseable_heartbeat"


def test_summarize_counts_states(tmp_path):
    (tmp_path / "fresh-listener-heartbeat").write_text(str(NOW - DAY))
    (tmp_path / "paused-listener-heartbeat").write_text(str(NOW - 8 * DAY))
    (tmp_path / "archived-listener-heartbeat").write_text(str(NOW - 31 * DAY))
    counts = sweep.summarize(sweep.discover_records(tmp_path, now=NOW))
    assert counts == {"active": 1, "paused": 1, "archived": 1, "no_signal": 0}
