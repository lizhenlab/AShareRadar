from __future__ import annotations

import json

import pytest

from app.artifacts.io import canonical_json_bytes
from app.services.market_scan_delayed_feedback_contracts import feedback_time, feedback_market_date
from tests.test_market_scan_cohort_feedback import START, cohort, config, freeze_calendar_fixture, label
from tests.test_market_scan_delayed_feedback import config as legacy_config, prediction
from tools import manage_market_scan_cohort_feedback as cli
from tools import manage_market_scan_feedback as legacy_cli


@pytest.fixture(autouse=True)
def frozen_calendar_source(monkeypatch, tmp_path):
    freeze_calendar_fixture(monkeypatch, tmp_path)


def test_cli_captures_calendar_once_uses_internal_time_and_never_overwrites(tmp_path, monkeypatch, capsys) -> None:
    config_path, events_path = tmp_path / "config.json", tmp_path / "events.json"
    config_path.write_bytes(canonical_json_bytes(config()))
    first, second, third = (tmp_path / name for name in ("first.json", "second.json", "third.json"))
    monkeypatch.setattr(cli, "utc_now", lambda: START)
    assert cli.main(["create", "--config", str(config_path), "--output", str(first)]) == 0
    first_bytes = first.read_bytes()
    ledger = json.loads(first_bytes)
    events_path.write_bytes(canonical_json_bytes([cohort()]))
    monkeypatch.setattr(cli, "utc_now", lambda: "2026-08-24T10:00:00+08:00")
    args = ["append", "--ledger", str(first), "--ledger-digest", ledger["ledger_digest"], "--events", str(events_path), "--output", str(second)]
    assert cli.main(args) == 0 and cli.main(args) == 1
    assert first.read_bytes() == first_bytes
    ledger = json.loads(second.read_bytes())
    assert ledger["events"][0]["recorded_at"] == "2026-08-24T02:00:00+00:00"
    monkeypatch.setattr(cli, "capture_cohort_calendar", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("reloaded calendar")))
    assert cli.main(["verify", "--ledger", str(second), "--ledger-digest", ledger["ledger_digest"]]) == 0
    assert cli.main(["verify", "--ledger", str(second), "--ledger-digest", "f" * 64]) == 1
    events_path.write_bytes(canonical_json_bytes([label()]))
    monkeypatch.setattr(cli, "utc_now", lambda: "2026-08-24T15:06:00+08:00")
    assert cli.main(["append", "--ledger", str(second), "--ledger-digest", ledger["ledger_digest"],
                     "--events", str(events_path), "--output", str(third)]) == 0
    assert json.loads(third.read_bytes())["summary"]["consumed_label_count"] == 1
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-12:00"])
def test_extreme_time_inputs_fail_without_traceback_in_both_clis(stamp, tmp_path, monkeypatch, capsys) -> None:
    with pytest.raises(ValueError, match="UTC"):
        feedback_time(stamp)
    old_config_path, old_ledger_path = tmp_path / "legacy-config.json", tmp_path / "legacy-ledger.json"
    old_config_path.write_bytes(canonical_json_bytes(legacy_config()))
    monkeypatch.setattr(legacy_cli, "utc_now", lambda: START)
    assert legacy_cli.main(["create", "--config", str(old_config_path), "--output", str(old_ledger_path)]) == 0
    ledger = json.loads(old_ledger_path.read_bytes())
    events = tmp_path / "bad-time.json"
    events.write_bytes(canonical_json_bytes([{**prediction(), "decision_at": stamp}]))
    assert legacy_cli.main(["append", "--ledger", str(old_ledger_path), "--ledger-digest", ledger["ledger_digest"],
                            "--events", str(events), "--output", str(tmp_path / "bad-legacy.json")]) == 1
    config_path = tmp_path / "config.json"
    config_path.write_bytes(canonical_json_bytes(config()))
    monkeypatch.setattr(cli, "utc_now", lambda: stamp)
    assert cli.main(["create", "--config", str(config_path), "--output", str(tmp_path / "bad-new.json")]) == 1
    assert "Traceback" not in capsys.readouterr().err


def test_cli_horizon_capture_duplicate_json_and_atomic_publish_conflict(tmp_path, monkeypatch) -> None:
    payload = config()
    payload["event_definition"] = {**payload["event_definition"], "horizon_sessions": 1, "target_offset_sessions": 2}
    config_path, output = tmp_path / "config.json", tmp_path / "output.json"
    config_path.write_bytes(canonical_json_bytes(payload))
    monkeypatch.setattr(cli, "utc_now", lambda: START)
    monkeypatch.setattr(cli, "exclusive_atomic_publish", lambda *args, **kwargs: False)
    assert cli.main(["create", "--config", str(config_path), "--output", str(output)]) == 1
    config_path.write_bytes(b'{"x":1,"x":2}')
    assert cli.main(["create", "--config", str(config_path), "--output", str(output)]) == 1


def test_shanghai_conversion_extreme_is_a_validation_error() -> None:
    assert feedback_time("9999-12-31T23:00:00Z").year == 9999
    with pytest.raises(ValueError, match="Shanghai"):
        feedback_market_date("9999-12-31T23:00:00Z")
