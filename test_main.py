"""Unit tests for NEXUS slot notifier."""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from main import Config, earlier_slots, load_known, parse_slot_time, save_known


@pytest.fixture
def config():
    return Config(
        location_id=5020,
        current_appt=datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc),
        twilio_sid="test_sid",
        twilio_token="test_token",
        twilio_from="+15551234567",
        twilio_to="+15559876543",
        poll_interval_s=120,
        state_path=Path("state.json"),
    )


class TestParseSlotTime:
    def test_parse_iso8601(self):
        ts = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)
        assert parse_slot_time("2026-05-15T09:00") == ts

    def test_parse_iso8601_with_seconds(self):
        ts = datetime(2026, 5, 15, 9, 30, 45, tzinfo=timezone.utc)
        assert parse_slot_time("2026-05-15T09:30:45") == ts


class TestEarlierSlots:
    def test_no_slots(self):
        assert earlier_slots([], datetime.now(timezone.utc)) == []

    def test_filter_earlier_only(self, config):
        slots = [
            {"startTimestamp": "2026-05-01T09:00", "active": True},  # earlier
            {"startTimestamp": "2026-06-15T10:00", "active": True},  # later
            {"startTimestamp": "2026-06-01T14:00", "active": True},  # earlier
        ]
        result = earlier_slots(slots, config.current_appt)
        assert len(result) == 2
        assert result[0]["startTimestamp"] == "2026-05-01T09:00"
        assert result[1]["startTimestamp"] == "2026-06-01T14:00"

    def test_skip_inactive(self, config):
        slots = [
            {"startTimestamp": "2026-05-01T09:00", "active": False},
        ]
        assert earlier_slots(slots, config.current_appt) == []

    def test_skip_missing_timestamp(self, config):
        slots = [
            {"active": True},
        ]
        assert earlier_slots(slots, config.current_appt) == []

    def test_default_active_true(self, config):
        slots = [
            {"startTimestamp": "2026-05-01T09:00"},  # no 'active' key
        ]
        result = earlier_slots(slots, config.current_appt)
        assert len(result) == 1

    def test_exact_boundary(self, config):
        # Slot exactly at current appointment should not be included.
        slots = [
            {"startTimestamp": "2026-06-08T09:00", "active": True},
        ]
        assert earlier_slots(slots, config.current_appt) == []


class TestLoadSaveKnown:
    def test_load_nonexistent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            assert load_known(path) == set()

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            known = {"2026-05-01T09:00", "2026-05-15T14:00"}
            save_known(path, known)
            loaded = load_known(path)
            assert loaded == known

    def test_load_empty_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            save_known(path, set())
            assert load_known(path) == set()

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            save_known(path, {"slot1", "slot2", "slot3"})
            assert load_known(path) == {"slot1", "slot2", "slot3"}
            assert json.loads(path.read_text()) == ["slot1", "slot2", "slot3"]


class TestSlotDeduplication:
    def test_new_slot_triggers_alert(self, config):
        with tempfile.TemporaryDirectory() as tmpdir:
            config.state_path = Path(tmpdir) / "state.json"
            slots = [{"startTimestamp": "2026-05-01T09:00", "active": True}]
            known = load_known(config.state_path)
            earlier = earlier_slots(slots, config.current_appt)
            current_keys = {s["startTimestamp"] for s in earlier}
            new_keys = current_keys - known
            assert new_keys == {"2026-05-01T09:00"}

    def test_repeated_slot_no_alert(self, config):
        with tempfile.TemporaryDirectory() as tmpdir:
            config.state_path = Path(tmpdir) / "state.json"
            # First poll sees and remembers slot.
            save_known(config.state_path, {"2026-05-01T09:00"})
            slots = [{"startTimestamp": "2026-05-01T09:00", "active": True}]
            known = load_known(config.state_path)
            earlier = earlier_slots(slots, config.current_appt)
            current_keys = {s["startTimestamp"] for s in earlier}
            new_keys = current_keys - known
            assert new_keys == set()

    def test_forgotten_slot_retriggers(self, config):
        with tempfile.TemporaryDirectory() as tmpdir:
            config.state_path = Path(tmpdir) / "state.json"
            # Slot was known but disappears, then reappears.
            save_known(config.state_path, {"2026-05-01T09:00"})
            slots = []  # Slot gone in second poll.
            known = load_known(config.state_path)
            earlier = earlier_slots(slots, config.current_appt)
            current_keys = {s["startTimestamp"] for s in earlier}
            gone = known - current_keys
            known -= gone
            save_known(config.state_path, known)
            # Now it reappears.
            slots = [{"startTimestamp": "2026-05-01T09:00", "active": True}]
            earlier = earlier_slots(slots, config.current_appt)
            current_keys = {s["startTimestamp"] for s in earlier}
            new_keys = current_keys - load_known(config.state_path)
            assert new_keys == {"2026-05-01T09:00"}
