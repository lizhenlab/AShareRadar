from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
from random import Random
import sqlite3

import pytest

from app.artifacts.io import canonical_json_bytes, exclusive_atomic_publish, sha256_hex
from app.services import experimental_direction_model as direction
from app.services.experimental_probability_model import (
    DIRECTION_SCHEMA, MODEL_DIRECTORY, ExperimentalEstimator, ExperimentalProbabilityUnavailable,
    experimental_definition, fit_experimental_sample_parameters, load_experimental_model,
)
from app.services.market_scan_experimental_probability import experimental_results
from app.services.market_scan_probability_history import (
    ProbabilityHistoryConfig, ProbabilityHistoryError, backfill_market_scan_probability_history,
    trusted_probability_history_dates,
)
from tests.test_experimental_probability import _database, _estimator
from tests.test_market_scan_probability_history import ANCHOR_DATE, _bar, _source_database


def _series():
    days = trusted_probability_history_dates(ANCHOR_DATE, 360)
    generator = Random(97)
    rows = []
    close = 10.0
    for index, day in enumerate(days):
        opened = close
        close *= 1 + generator.uniform(-.03, .03)
        rows.append(_bar(day, index).model_copy(update={
            "open": opened, "close": close, "high": max(opened, close) + .1,
            "low": min(opened, close) - .1, "volume": generator.uniform(100000, 200000),
        }))
    return days, rows


def _direction_estimator(offset):
    value = _estimator().model_dump()
    value.update(schema_version=DIRECTION_SCHEMA, horizon=offset, target="close_return_positive",
                 historical_recipe_evaluation={"status": "not_evaluated", "target": "close_return_positive", "horizon": offset},
                 direction_evidence={"target_session_offset": offset, "history_manifest_digest": "b" * 64,
                     "calendar_digest": "c" * 64, "samples_digest": "d" * 64, "source_start": "2024-07-31",
                     "source_end": "2026-08-21", "sample_count": 1000})
    value["model"]["coefficients"] = [(.2 if offset == 1 else -.2)] * 11
    return ExperimentalEstimator.model_validate(value)


def _publish_direction(directory, offset):
    value = _direction_estimator(offset).model_dump()
    digest = sha256_hex(canonical_json_bytes(value))
    path = directory / f"experimental-close-d{offset}-{digest}.json"
    exclusive_atomic_publish(path, canonical_json_bytes({"payload": value, "sha256": digest}), max_bytes=256 * 1024)
    return path


def test_close_labels_compare_to_signal_close_not_next_open_or_previous_day():
    days, rows = _series()
    rows[60] = rows[60].model_copy(update={"open": 10., "close": 10., "high": 11., "low": 9.})
    rows[61] = rows[61].model_copy(update={"open": 30., "close": 11., "high": 31., "low": 10.})
    rows[62] = rows[62].model_copy(update={"open": 30., "close": 9., "high": 31., "low": 8.})
    rows[65] = rows[65].model_copy(update={"open": 30., "close": 10., "high": 31., "low": 8.})
    first, targets1, _, _ = direction.direction_samples({"600001.SH": rows}, days, offset=1)
    second, targets2, _, _ = direction.direction_samples({"600001.SH": rows}, days, offset=2)
    fifth, targets5, _, _ = direction.direction_samples({"600001.SH": rows}, days, offset=5)
    assert first[0].target == 1 and second[0].target == 0
    assert first[0].features == second[0].features and first[0].net_return is None
    assert targets1[first[0].sample_id] == days[61] and targets2[second[0].sample_id] == days[62]
    assert fifth[0].target == 0 and fifth[0].features == first[0].features
    assert targets5[fifth[0].sample_id] == days[65]  # Flat close is not positive; never D+6.
    rows[61] = rows[61].model_copy(update={"close": 8.})
    changed, _, _, _ = direction.direction_samples({"600001.SH": rows}, days, offset=1)
    assert changed[0].target == 0 and changed[0].features == first[0].features


@pytest.mark.parametrize("mutation,reason", [("missing", "missing_fixed_target"), ("suspended", "target_no_volume"),
    ("contract", "target_contract_mismatch"), ("invalid_close", "invalid_target_close")])
@pytest.mark.parametrize("offset", [1, 2, 5])
def test_missing_or_suspended_fixed_target_never_moves_to_next_available_bar(mutation, reason, offset):
    days, rows = _series()
    if mutation == "missing":
        del rows[60 + offset]
    else:
        changes = {"suspended": {"volume": 0.}, "contract": {"data_version": "changed"}, "invalid_close": {"close": float("nan")}}
        rows[60 + offset] = rows[60 + offset].model_copy(update=changes[mutation])
    samples, _, _, receipt = direction.direction_samples({"600001.SH": rows}, days, offset=offset)
    assert not any(sample.session_date == days[60] for sample in samples)
    assert receipt["excluded"][reason] >= 1


def test_targets_follow_trading_calendar_and_training_is_purged_per_direction():
    days, rows = _series()
    index = next(index for index in range(60, len(days) - 2) if date.fromisoformat(days[index]).weekday() == 4)
    for offset in (1, 2, 5):
        samples, targets, _, receipt = direction.direction_samples({"600001.SH": rows}, days, offset=offset)
        sample = next(item for item in samples if item.session_date == days[index])
        assert targets[sample.sample_id] == days[index + offset]
        assert (date.fromisoformat(targets[sample.sample_id]) - date.fromisoformat(sample.session_date)).days > offset
        fit = fit_experimental_sample_parameters(samples, targets, horizon=offset, purge_sessions=offset)
        assert fit["train_session_count"] >= 120 and fit["calibration_session_count"] == 40
        assert fit["train_label_end"] < fit["calibration_start"]
        assert receipt["excluded"]["target_outside_source"] == offset


@pytest.mark.parametrize("offset", [0, 3, True])
def test_invalid_direction_offsets_are_not_h5_aliases(offset):
    with pytest.raises(ExperimentalProbabilityUnavailable):
        direction.direction_samples({}, [], offset=offset)


def test_duplicate_dates_and_unordered_calendars_fail_closed():
    days, rows = _series()
    with pytest.raises(ExperimentalProbabilityUnavailable, match="日历"):
        direction.direction_samples({"600001.SH": rows}, list(reversed(days)), offset=1)
    with pytest.raises(ExperimentalProbabilityUnavailable, match="日期重复"):
        direction.direction_samples({"600001.SH": [*rows, rows[0]]}, days, offset=1)


@pytest.mark.parametrize("field,value", [("horizon", 5), ("target", "net_return_positive"),
    ("cost_contract", {"fees": .001}), ("historical_recipe_evaluation", {"status": "calibrated_shadow"}),
    ("direction_evidence", None), ("source_integrity_digest", "e" * 64)])
def test_direction_artifacts_cannot_borrow_h5_labels_or_verification(field, value):
    payload = _direction_estimator(1).model_dump()
    payload[field] = value
    with pytest.raises(ValueError):
        ExperimentalEstimator.model_validate(payload)


@pytest.mark.parametrize("field,value", [("target_session_offset", True), ("target_session_offset", 1.0),
    ("source_start", "20240731"), ("source_end", "2024-01-01"), ("excluded", {"missing": -1}), ("sample_count", 1)])
def test_direction_evidence_rejects_misleading_types_dates_and_counts(field, value):
    payload = _direction_estimator(1).model_dump()
    payload["direction_evidence"][field] = value
    with pytest.raises(ValueError):
        ExperimentalEstimator.model_validate(payload)


def test_direction_horizon_rejects_boolean_and_float_aliases():
    for horizon in (True, 1.0):
        payload = _direction_estimator(1).model_dump()
        payload["horizon"] = horizon
        with pytest.raises(ValueError):
            ExperimentalEstimator.model_validate(payload)


def test_each_runtime_kind_loads_its_own_model_and_keeps_baseline_ranks(tmp_path):
    database, run, items = _database(tmp_path)
    before, originals = database.read_bytes(), deepcopy(items)
    directory = tmp_path / MODEL_DIRECTORY
    h5 = next(directory.glob("experimental-h5-*.json"))
    original_h5 = h5.read_bytes()
    with pytest.raises(ExperimentalProbabilityUnavailable, match="尚未生成"):
        experimental_results(database, run, items, prediction_kind="close_d1")
    _publish_direction(directory, 1)
    _publish_direction(directory, 2)
    _publish_direction(directory, 5)
    values = {}
    for kind in ("net_h5", "close_d1", "close_d2", "close_d5"):
        result = experimental_results(database, run, items, prediction_kind=kind, sort="base_rank")
        values[kind] = result
        definition = experimental_definition(kind)
        assert (result["horizon"], result["target"], result["reference"]) == (definition["horizon"], definition["target"], definition["reference"])
        assert [row["base_rank"] for row in result["items"]] == [1, 2]
        assert result["filters"]["prediction_kind"] == kind and result["formal_filter_qualified"] is False
        assert result["production_ranking_effect"] == "none"
    assert values["close_d1"]["target_session_date"] == "2026-08-26"
    assert values["close_d2"]["target_session_date"] == "2026-08-27"
    assert values["close_d5"]["target_session_date"] == "2026-09-01"
    assert values["net_h5"]["target_session_date"] == "2026-09-02"
    assert len({value["model_digest"] for value in values.values()}) == 4
    assert values["close_d1"]["items"][0]["probability"] != values["close_d2"]["items"][0]["probability"]
    assert h5.read_bytes() == original_h5 and database.read_bytes() == before and items == originals
    filtered = experimental_results(database, run, items, prediction_kind="close_d2", market="SZ", minimum=0., keyword="000001", page_size=1)
    assert filtered["total"] == 1 and filtered["items"][0]["symbol"] == "000001.SZ"


def test_model_file_renaming_cannot_cross_kind_boundaries(tmp_path):
    path = _publish_direction(tmp_path, 1)
    document = json.loads(path.read_text())
    alternate = tmp_path / f"experimental-close-d2-{document['sha256']}.json"
    alternate.write_bytes(path.read_bytes())
    with pytest.raises(ExperimentalProbabilityUnavailable, match="校验失败"):
        load_experimental_model(tmp_path, prediction_kind="close_d2")
    with pytest.raises(ExperimentalProbabilityUnavailable, match="尚未生成"):
        load_experimental_model(tmp_path, prediction_kind="net_h5")


def test_direction_runtime_rejects_calendar_gaps_without_changing_h5(tmp_path):
    database, run, items = _database(tmp_path)
    directory = tmp_path / MODEL_DIRECTORY
    _publish_direction(directory, 1)
    _publish_direction(directory, 2)
    _publish_direction(directory, 5)
    days = trusted_probability_history_dates(run.data_date, 22)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE kline_daily SET date=? WHERE symbol=? AND date=?",
                           (days[0], "600001.SH", days[10]))
    for kind in ("close_d1", "close_d2", "close_d5"):
        result = experimental_results(database, run, items, prediction_kind=kind)
        assert result["coverage"]["unavailable_reasons"]["missing_fixed_session_window"] == 1
        assert all(item["symbol"] != "600001.SH" for item in result["items"])
    h5 = experimental_results(database, run, items)
    assert any(item["symbol"] == "600001.SH" for item in h5["items"])


@pytest.fixture
def attested_history(tmp_path):
    days, rows = _series()

    class Provider:
        source_name = "fake-tencent-qfq"

        async def kline(self, symbol, limit=360):
            assert limit == len(days)
            return rows

    source = _source_database(tmp_path / "live.sqlite3", ("600001.SH", "000001.SZ", "830001.BJ"))
    return asyncio.run(backfill_market_scan_probability_history(
        source, tmp_path / "history" / "history.sqlite3", tmp_path / "manifests",
        config=ProbabilityHistoryConfig(symbol_limit=3, minimum_symbols_total=3, minimum_symbols_per_market=1),
        provider=Provider(), generated_at="2026-08-11T09:00:00+00:00",
    ))


def test_builder_uses_attested_readonly_history_and_publishes_independent_models(attested_history, tmp_path):
    history = attested_history
    before = history.database_path.read_bytes()
    for offset in (1, 2, 5):
        path = direction.build_direction_model(history.manifest_path, history.database_path, tmp_path / "models", offset=offset)
        estimator, digest = load_experimental_model(path.parent, prediction_kind=f"close_d{offset}")
        assert path.name == f"experimental-close-d{offset}-{digest}.json"
        assert estimator.horizon == offset and estimator.direction_evidence.target_session_offset == offset
        assert estimator.source_sha256 == sha256_hex(before)
        assert estimator.historical_recipe_evaluation["status"] == "not_evaluated"
        assert estimator.train_label_end < estimator.calibration_start
    assert history.database_path.read_bytes() == before


def test_builder_rejects_changed_source_without_creating_models(attested_history, tmp_path):
    history = attested_history
    with sqlite3.connect(history.database_path) as connection:
        connection.execute("UPDATE kline_daily SET volume=volume+1")
    with pytest.raises(ProbabilityHistoryError):
        direction.build_direction_model(history.manifest_path, history.database_path, tmp_path / "models", offset=1)
    assert not (tmp_path / "models").exists()


def test_builder_rejects_active_database_sidecars(tmp_path):
    database = tmp_path / "history.sqlite3"
    Path(str(database) + "-wal").write_bytes(b"not a static source")
    with pytest.raises(ExperimentalProbabilityUnavailable, match="sidecar"):
        direction.build_direction_model(tmp_path / "manifest.json", database, tmp_path / "models", offset=1)


@pytest.mark.parametrize("kind,offset", [("close_d1", 1), ("close_d2", 2), ("close_d5", 5)])
def test_direction_cli_requires_manifest_database_and_binds_selected_target(tmp_path, monkeypatch, capsys, kind, offset):
    from tools import build_experimental_probability as cli

    calls = []
    manifest, database, output = tmp_path / "manifest.json", tmp_path / "history.sqlite3", tmp_path / "models"

    def build(received_manifest, received_database, received_output, *, offset):
        calls.append((received_manifest, received_database, received_output, offset))
        return output / "model.json"

    monkeypatch.setattr(cli, "build_direction_model", build)
    monkeypatch.setattr(cli.sys, "argv", ["build", "--prediction-kind", kind, "--history-manifest", str(manifest),
        "--database", str(database), "--output-dir", str(output)])
    assert cli.main() == 0 and calls == [(manifest, database, output, offset)]
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["prediction_kind"] == kind
    monkeypatch.setattr(cli.sys, "argv", ["build", "--prediction-kind", kind, "--source", "old-h5.json"])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 2 and len(calls) == 1
