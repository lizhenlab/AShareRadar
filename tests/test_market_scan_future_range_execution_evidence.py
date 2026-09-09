from __future__ import annotations

from copy import deepcopy
import sqlite3

import pytest

from app.services import market_scan_future_range as service
from app.services.market_scan_future_range_artifact import (
    build_future_range_artifact,
    canonical_future_range_artifact_json,
    load_future_range_artifact,
    replay_future_range_artifact,
)
from app.services.market_scan_probability_labels import PREVIOUS_PROBABILITY_LABEL_VERSION, probability_label_contract
from app.services.market_scan_universe import FULL_MARKET_SCOPE
from tests.test_market_scan_future_range import TARGET_DATES, _initialize, _seed_run


@pytest.fixture
def source(tmp_path, monkeypatch):
    path = tmp_path / "future-range-execution.sqlite3"
    _initialize(path)
    run_id = _seed_run(path, scope=FULL_MARKET_SCOPE, with_targets=True)
    monkeypatch.setattr(service, "next_trade_dates", lambda *_args: TARGET_DATES)
    return path, run_id


def _report(source):
    path, run_id = source
    return service.evaluate_market_scan_future_range(
        path, run_ids=[run_id], generated_at="2026-01-08T08:00:00+00:00",
        config=service.FutureRangeConfig(minimum_sample_size=1, minimum_session_count=1, bootstrap_samples=100),
    )["reports"][0]


def _record(report):
    return next(record for record in report["records"] if record["symbol"] == "600001.SH")


def _change_bar(source, day, *, invalid_evidence=False, **changes):
    with sqlite3.connect(source[0]) as connection:
        if invalid_evidence:
            # Exercise the reader against legacy/corrupt raw rows, independent of modern CHECK constraints.
            connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE kline_daily SET " + ", ".join(f"{column} = ?" for column in changes)
            + " WHERE symbol = ? AND date = ?",
            (*changes.values(), "600001.SH", day),
        )


@pytest.mark.parametrize(("day", "metadata", "offset", "reason"), [
    ("2026-01-05", {"session_status": "trading", "open_execution_status": "locked_limit_up"}, 2, "locked_limit_up"),
    ("2026-01-05", {"session_status": "trading", "open_execution_status": "unavailable"}, 3, "execution_metadata_unavailable"),
    ("2026-01-05", {"session_status": "suspended", "open_execution_status": "unavailable", "volume": 0}, 2,
     "execution_metadata_unavailable"),
])
def test_explicit_unexecutable_target_never_becomes_modelled(source, day, metadata, offset, reason):
    _change_bar(source, day, **metadata)
    execution = _record(_report(source))["offsets"][offset - 1]["execution"]

    assert execution["status"] == "unfilled"
    assert execution["reason"] == reason
    assert execution["net_return"] is None and execution["entry_price"] is None


@pytest.mark.parametrize(("column", "value"), [
    ("point_in_time", "yes"),
    ("point_in_time", 2),
    ("session_status", sqlite3.Binary(b"trading")),
    ("corporate_action_status", "invalid"),
    ("adjustment_factor", "not-a-number"),
    ("execution_metadata_version", sqlite3.Binary(b"v1")),
])
def test_malformed_raw_execution_evidence_does_not_authorize_labels_or_hide_prices(source, column, value):
    _change_bar(source, "2026-01-05", invalid_evidence=True, **{column: value})
    record = _record(_report(source))

    assert all(offset["fixed_session_status"] == "available" for offset in record["offsets"])
    for offset in record["offsets"][1:]:
        assert offset["execution"]["status"] == "data_unavailable"
        assert offset["execution"]["reason"] == "execution_label_input_conflict"
        assert offset["execution"]["net_return"] is None


def test_sql_to_label_conversion_preserves_all_execution_metadata(source):
    values = {
        "session_status": "trading", "open_execution_status": "locked_limit_up",
        "corporate_action_status": "effective_event", "adjustment_factor": 1.1,
        "point_in_time": 1, "execution_metadata_version": "execution-v1", "as_of": "2026-01-05 15:15:00",
    }
    _change_bar(source, "2026-01-05", **values)
    with sqlite3.connect(source[0]) as connection:
        connection.row_factory = sqlite3.Row
        raw = service._target_rows(connection, source[1], ("2026-01-05",))["600001.SH"]["2026-01-05"]
    bar, error = service._verified_target_bar(raw, expected_date="2026-01-05")
    assert error is None
    label_bar = service._to_label_kline(bar, raw)

    assert all(getattr(label_bar, column) == value for column, value in values.items())
    assert label_bar.point_in_time is True


def test_metadata_only_change_is_digest_bound_while_price_ranges_stay_identical(source):
    _change_bar(source, "2026-01-05", execution_metadata_version="execution-v1")
    first = _report(source)
    _change_bar(source, "2026-01-05", execution_metadata_version="execution-v2")
    second = _report(source)
    before = build_future_range_artifact(first, generated_at=first["generated_at"])
    after = build_future_range_artifact(second, generated_at=second["generated_at"])

    assert first["config"]["research_version"] == "fixed-session-future-range-v3-execution-phase-separated"
    assert before["integrity"]["integrity_digest"] != after["integrity"]["integrity_digest"]
    for left, right in zip(_record(first)["offsets"], _record(second)["offsets"], strict=True):
        assert {key: value for key, value in left.items() if key != "execution"} == {
            key: value for key, value in right.items() if key != "execution"
        }
        assert left["execution"]["status"] == right["execution"]["status"]
        assert left["execution"]["net_return"] == right["execution"]["net_return"]
    assert replay_future_range_artifact(before) == first
    assert replay_future_range_artifact(after) == second


@pytest.mark.parametrize("research_version", ["fixed-session-future-range-v1", "fixed-session-future-range-v2-execution-evidence"])
def test_legacy_columns_and_frozen_artifact_never_gain_execution_authority(source, tmp_path, research_version):
    with sqlite3.connect(source[0]) as connection:
        connection.row_factory = sqlite3.Row
        raw = connection.execute(
            "SELECT symbol,date,open,close,high,low,volume,adjustment_mode,as_of,data_version,contract_version,"
            "source,fetched_at,fallback_used FROM kline_daily WHERE symbol='600001.SH' AND date='2026-01-05'",
        ).fetchone()
    bar, error = service._verified_target_bar(raw, expected_date="2026-01-05")
    assert error is None
    label_bar = service._to_label_kline(bar, raw)
    assert label_bar.session_status == label_bar.open_execution_status == label_bar.corporate_action_status == "unknown"
    assert label_bar.point_in_time is False and label_bar.execution_metadata_version is None

    legacy = deepcopy(_report(source))
    legacy["config"]["research_version"] = research_version
    old_contract = probability_label_contract(label_version=PREVIOUS_PROBABILITY_LABEL_VERSION)
    legacy["config"]["execution_label_contract"] = old_contract
    if research_version == "fixed-session-future-range-v1":
        legacy["config"].pop("execution_evidence_policy", None)
    for record in legacy["records"]:
        for offset in record["offsets"]:
            offset["execution"]["label_version"] = old_contract["label_version"]
            offset["execution"]["execution_model"] = old_contract["execution_model"]
            if research_version == "fixed-session-future-range-v1":
                offset["execution"].pop("execution_bar_evidence", None)
    artifact = build_future_range_artifact(legacy, generated_at=legacy["generated_at"])
    frozen_path = tmp_path / "frozen-legacy.json"
    frozen_path.write_text(canonical_future_range_artifact_json(artifact), encoding="utf-8")
    frozen_bytes = frozen_path.read_bytes()

    _change_bar(source, "2026-01-05", session_status="trading", open_execution_status="locked_limit_up")
    assert replay_future_range_artifact(load_future_range_artifact(frozen_path)) == legacy
    assert frozen_path.read_bytes() == frozen_bytes
    assert _record(_report(source))["offsets"][1]["execution"]["status"] == "unfilled"


@pytest.mark.parametrize("opening_status", ["locked_limit_down", "unavailable"])
def test_close_label_releases_opening_restriction_without_changing_price_ranges(source, opening_status):
    before = _report(source)
    _change_bar(source, "2026-01-06", session_status="trading", open_execution_status=opening_status)
    after = _report(source)
    execution = _record(after)["offsets"][1]["execution"]

    assert execution["status"] == "modelled" and execution["model_limited"] is True
    assert "open-status-entry-only" in execution["execution_model"]
    for left, right in zip(_record(before)["offsets"], _record(after)["offsets"], strict=True):
        assert {key: value for key, value in left.items() if key != "execution"} == {
            key: value for key, value in right.items() if key != "execution"
        }
