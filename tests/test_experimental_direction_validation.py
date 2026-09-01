from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path
from random import Random
from statistics import mean
from types import SimpleNamespace

import pytest

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.models.market import Kline
from app.services import experimental_direction_validation as validation
from app.services.choice_sdk import ChoiceError
from app.services.experimental_probability_model import (
    MODEL_FEATURE_NAMES, ExperimentalCalibrator, ExperimentalLogit,
)
from app.services.market_scan_probability import (
    PROBABILITY_CALIBRATOR_VERSION, PROBABILITY_MODEL_VERSION, ProbabilityModelConvergenceError, ProbabilitySample,
)
from tools import validate_experimental_direction as cli


GENERATED_AT = "2026-08-26T12:00:00+08:00"
PROVENANCE = {"manifest_digest": "a" * 64, "source_sha256": "b" * 64,
              "limitations": ["historical_data_previously_observed_not_prospective_oos", "not_cross_source_equivalent"]}


@pytest.fixture(scope="module")
def history():
    sessions = []
    current = date(2024, 1, 2)
    while len(sessions) < 360:
        if current.weekday() < 5:
            sessions.append(current.isoformat())
        current += timedelta(days=1)
    series = {}
    for number, symbol in enumerate(("600001.SH", "000001.SZ", "830001.BJ")):
        generator = Random(97 + number)
        rows = []
        close = 10.0 + number
        for day in sessions:
            opened = close
            close *= 1 + generator.uniform(-.03, .03)
            rows.append(Kline(date=day, open=opened, close=close, high=max(opened, close) + .1,
                              low=min(opened, close) - .1, volume=generator.uniform(100000, 200000),
                              adjustment_mode="qfq", as_of=sessions[-1], data_version="isolated-fixture-v1",
                              source="fake-reconstructed-history", fetched_at=GENERATED_AT))
        series[symbol] = rows
    return series, tuple(sessions)


@pytest.fixture(scope="module")
def report(history):
    series, sessions = history
    return validation.validate_experimental_direction_history(series, sessions, PROVENANCE, generated_at=GENERATED_AT)


def test_fixed_calendar_final_period_is_common_and_each_horizon_is_purged(report, history):
    _series, sessions = history
    assert report["status"] == "completed"
    for offset in (1, 2, 5):
        result = report["horizons"][str(offset)]
        split, fit = result["split"], result["fit"]
        assert result["status"] == "evaluated"
        assert split["test"] == list(sessions[-65:-5])
        assert len(split["calibration"]) == 40
        assert len(split["training_gap"]) == len(split["test_gap"]) == offset
        assert fit["train_session_count"] >= 120
        assert fit["train_label_end"] < split["calibration"][0]
        assert fit["calibration_label_end"] < split["test"][0]
        assert fit["parameters_published"] is False
        assert result["overall"]["expected_symbol_session_count"] == 180
        assert set(result["by_market"]) == {"SH", "SZ", "BJ"}


def test_final_test_targets_cannot_affect_training_calibration_or_predictions(history, report):
    series, sessions = history
    changed = {symbol: list(rows) for symbol, rows in series.items()}
    for rows in changed.values():
        signal_close = rows[-6].close
        for offset in (1, 2, 5):
            index = len(rows) - 6 + offset
            close = signal_close * (.5 if rows[index].close > signal_close else 1.5)
            rows[index] = rows[index].model_copy(update={"open": close, "close": close, "high": close + .1, "low": close - .1})
    changed_report = validation.validate_experimental_direction_history(changed, sessions, PROVENANCE, generated_at=GENERATED_AT)

    assert changed_report["source"]["series_digest"] != report["source"]["series_digest"]
    for offset in ("1", "2", "5"):
        original, altered = report["horizons"][offset], changed_report["horizons"][offset]
        assert original["fit"] == altered["fit"]
        assert original["test_probability_digest"] == altered["test_probability_digest"]
        assert original["test_prediction_digest"] != altered["test_prediction_digest"]


def test_missing_final_targets_keep_fixed_dates_and_disable_gap_compressed_bootstrap(history, report):
    series, sessions = history
    missing = {symbol: [row for row in rows if row.date != sessions[-5]] for symbol, rows in series.items()}
    result = validation.validate_experimental_direction_history(missing, sessions, PROVENANCE, generated_at=GENERATED_AT)
    for offset in ("1", "2", "5"):
        horizon = result["horizons"][offset]
        assert horizon["split"] == report["horizons"][offset]["split"]
        assert horizon["fit"] == report["horizons"][offset]["fit"]
        assert horizon["overall"]["label_eligible_count"] == 177
        assert horizon["overall"]["label_unavailable_count"] == 3
        balanced = horizon["overall"]["date_balanced_metrics"]
        assert balanced["scored_session_count"] == 59
        assert balanced["improvement_ci95"] is None
        assert "not_compressed" in balanced["interval_limitation"]


def test_market_with_zero_test_coverage_remains_in_report(history, report):
    series, sessions = history
    changed = {symbol: list(rows) for symbol, rows in series.items()}
    start = sessions[-65]
    changed["830001.BJ"] = [row.model_copy(update={"volume": 0.}) if row.date >= start else row for row in changed["830001.BJ"]]
    result = validation.validate_experimental_direction_history(changed, sessions, PROVENANCE, generated_at=GENERATED_AT)
    for offset in ("1", "2", "5"):
        horizon = result["horizons"][offset]
        assert horizon["fit"] == report["horizons"][offset]["fit"]
        beijing = horizon["by_market"]["BJ"]
        assert beijing["expected_symbol_session_count"] == beijing["label_unavailable_count"] == 60
        assert beijing["label_coverage"] == beijing["prediction_coverage"] == 0
        assert beijing["unique_scored_session_count"] == 0
        assert beijing["pooled_metrics"] is None and beijing["date_balanced_metrics"] is None
        assert len(beijing["daily"]) == 60 and all(day["metrics"] is None for day in beijing["daily"])


def test_market_absent_from_source_is_not_reported_as_fully_covered(history):
    series, sessions = history
    result = validation.validate_experimental_direction_history({"600001.SH": series["600001.SH"]}, sessions, PROVENANCE, generated_at=GENERATED_AT)
    for horizon in result["horizons"].values():
        for market in ("SZ", "BJ"):
            group = horizon["by_market"][market]
            assert group["status"] == "no_input_symbols"
            assert group["expected_symbol_session_count"] == 0
            assert group["prediction_coverage"] is None


def test_missing_calibration_dates_do_not_move_calibration_or_holdout_backward(history, report):
    series, sessions = history
    removed = report["horizons"]["1"]["split"]["calibration"][10]
    changed = {symbol: [row for row in rows if row.date != removed] for symbol, rows in series.items()}
    result = validation.validate_experimental_direction_history(changed, sessions, PROVENANCE, generated_at=GENERATED_AT)
    for offset in ("1", "2", "5"):
        horizon = result["horizons"][offset]
        assert horizon["split"] == report["horizons"][offset]["split"]
        assert horizon["status"] == "insufficient_data" and horizon["fit"] is None
        assert horizon["reason"] == "insufficient_eligible_train_or_fixed_calibration_sessions"
        assert horizon["overall"]["pooled_metrics"] is None


def test_numeric_fit_and_calibration_only_receive_pretest_partitions(history, monkeypatch):
    series, sessions = history
    original_model_fit = validation.fit_probability_logistic_model
    original_calibrator_fit = validation.fit_probability_platt_calibrator
    trained, calibrated = {}, {}

    def model_fit(samples, names, config):
        trained[config.horizon] = list(samples)
        return original_model_fit(samples, names, config)

    def calibrator_fit(probabilities, labels, config):
        calibrated[config.horizon] = list(labels)
        return original_calibrator_fit(probabilities, labels, config)

    monkeypatch.setattr(validation, "fit_probability_logistic_model", model_fit)
    monkeypatch.setattr(validation, "fit_probability_platt_calibrator", calibrator_fit)
    result = validation.validate_experimental_direction_history(series, sessions, PROVENANCE, generated_at=GENERATED_AT)
    assert set(trained) == set(calibrated) == {1, 2, 5}
    for offset in (1, 2, 5):
        horizon = result["horizons"][str(offset)]
        assert {sample.session_date for sample in trained[offset]} == set(horizon["split"]["train"])
        assert max(sample.session_date for sample in trained[offset]) < horizon["split"]["calibration"][0]
        assert len(calibrated[offset]) == 40 * len(series)
        assert horizon["fit"]["calibration_base_rate"] == mean(calibrated[offset])
        assert horizon["overall"]["pooled_metrics"]["model"]["base_rate"] == mean(calibrated[offset])


def test_bootstrap_clusters_calendar_dates_and_uses_horizon_blocks(history, monkeypatch):
    series, sessions = history
    calls = []

    def interval(rows, seed_text, count, *, block_length_sessions):
        calls.append((rows, seed_text, count, block_length_sessions))
        return [-.1, .1]

    monkeypatch.setattr(validation, "date_block_bootstrap_ci", interval)
    validation.validate_experimental_direction_history(series, sessions, PROVENANCE, generated_at=GENERATED_AT)
    assert len(calls) == 3 * 4 * 2
    for rows, _seed, count, length in calls:
        assert [day for day, _value in rows] == list(sessions[-65:-5])
        assert len(rows) == 60 and count == 1000 and length in {1, 2, 5}


def _sample(sample_id, day, label, first_value=0.):
    features = dict.fromkeys(MODEL_FEATURE_NAMES, 0.)
    features[MODEL_FEATURE_NAMES[0]] = first_value
    return ProbabilitySample(sample_id=sample_id, session_date=day, features=features, target=label)


def test_date_balanced_proper_scores_do_not_overweight_larger_cross_sections():
    first, second = "2025-01-02", "2025-01-03"
    samples = [_sample("close-d1:a:600001.SH", first, 1)]
    samples.extend(_sample(f"close-d1:b:{symbol}", second, 0) for symbol in ("600001.SH", "000001.SZ", "830001.BJ"))
    predictions = [validation._Prediction(sample, .9) for sample in samples]
    split = validation._Split((), (), (), (), (first, second))
    group = validation._group_report(samples, predictions, Counter(), split, ("600001.SH", "000001.SZ", "830001.BJ"), .5, 1)

    assert group["pooled_metrics"]["model"]["brier_score"] == pytest.approx(.61)
    balanced = group["date_balanced_metrics"]
    assert balanced["model"]["brier_score"] == pytest.approx(.41)
    assert balanced["calibration_rate_baseline"]["brier_score"] == .25
    assert balanced["constant_half_baseline"]["brier_score"] == .25
    assert balanced["model"]["classification_accuracy_at_half"] == .5
    assert balanced["brier_skill_vs_calibration_rate"] == pytest.approx(-.64)


def test_prediction_guard_matches_runtime_eight_sigma_boundary():
    model = ExperimentalLogit(version=PROBABILITY_MODEL_VERSION, feature_names=list(MODEL_FEATURE_NAMES),
                             means=[0.] * 11, scales=[1.] * 11, coefficients=[0.] * 11, intercept=0.,
                             l2_strength=1., iterations=1, converged=True)
    calibrator = ExperimentalCalibrator(version=PROBABILITY_CALIBRATOR_VERSION, intercept=0., slope=1.,
                                       iterations=1, converged=True, fit_partition="calibration_only")
    fitted = validation._Fit(model, calibrator, .5, {})
    boundary = _sample("close-d1:2025-01-02:600001.SH", "2025-01-02", 1, 8.)
    outside = _sample("close-d1:2025-01-02:830001.BJ", "2025-01-02", 1, 8.001)
    predictions, rejected = validation._predict_test(fitted, [boundary, outside])

    assert len(predictions) == 1 and predictions[0].sample is boundary
    assert rejected == {"830001.BJ": 1}
    assert predictions[0].probability == .5


def test_empty_test_coverage_never_becomes_fabricated_zero_probability(history):
    series, sessions = history
    changed = {symbol: [row.model_copy(update={"volume": 0.}) if row.date >= sessions[-65] else row for row in rows]
               for symbol, rows in series.items()}
    result = validation.validate_experimental_direction_history(changed, sessions, PROVENANCE, generated_at=GENERATED_AT)
    assert result["status"] == "unavailable"
    for horizon in result["horizons"].values():
        assert horizon["status"] == "no_evaluable_test_rows"
        assert horizon["overall"]["predicted_count"] == 0
        assert horizon["overall"]["pooled_metrics"] is None
        assert horizon["overall"]["prediction_coverage"] == 0


def test_fit_nonconvergence_is_reported_without_guessing_or_skipping_to_another_model(history, monkeypatch):
    series, sessions = history

    def failed(*_args):
        raise ProbabilityModelConvergenceError("test nonconvergence")

    monkeypatch.setattr(validation, "fit_probability_logistic_model", failed)
    result = validation.validate_experimental_direction_history(series, sessions, PROVENANCE, generated_at=GENERATED_AT)
    assert result["status"] == "unavailable"
    assert all(horizon["fit"] is None and horizon["reason"] == "test nonconvergence" for horizon in result["horizons"].values())


def test_insufficient_history_does_not_shorten_final_test_or_calibration(history):
    series, sessions = history
    selected = sessions[:280]
    clipped = {symbol: rows[:280] for symbol, rows in series.items()}
    result = validation.validate_experimental_direction_history(clipped, selected, PROVENANCE, generated_at=GENERATED_AT)
    assert result["status"] == "unavailable"
    for horizon in result["horizons"].values():
        assert horizon["reason"] == "insufficient_fixed_calendar_sessions"
        assert horizon["fit"] is None and horizon["split"] is None


@pytest.mark.parametrize("mutation", ["duplicate_calendar", "reversed_calendar", "bad_date", "duplicate_bar", "raw_bar", "invalid_symbol", "unbound", "naive_time", "future_calendar"])
def test_invalid_source_contracts_fail_before_fitting(history, monkeypatch, mutation):
    original_series, original_sessions = history
    series = {symbol: list(rows) for symbol, rows in original_series.items()}
    sessions, provenance, generated_at = list(original_sessions), deepcopy(PROVENANCE), GENERATED_AT
    if mutation == "duplicate_calendar":
        sessions.append(sessions[-1])
    elif mutation == "reversed_calendar":
        sessions.reverse()
    elif mutation == "bad_date":
        sessions[0] = "20240102"
    elif mutation == "duplicate_bar":
        series["600001.SH"].append(series["600001.SH"][0])
    elif mutation == "raw_bar":
        series["600001.SH"][0] = series["600001.SH"][0].model_copy(update={"adjustment_mode": "none"})
    elif mutation == "invalid_symbol":
        series["600001.INVALID"] = series.pop("600001.SH")
    elif mutation == "unbound":
        provenance.pop("manifest_digest")
    elif mutation == "naive_time":
        generated_at = "2026-08-26T12:00:00"
    else:
        generated_at = "2024-01-01T12:00:00+08:00"
    monkeypatch.setattr(validation, "fit_probability_logistic_model", lambda *_args: pytest.fail("invalid source reached fitting"))
    with pytest.raises(ValueError):
        validation.validate_experimental_direction_history(series, sessions, provenance, generated_at=generated_at)


def test_repeated_replay_never_claims_new_independent_or_live_authority(history, report):
    series, sessions = history
    repeated = validation.validate_experimental_direction_history(series, sessions, PROVENANCE, generated_at=GENERATED_AT)
    assert repeated == report
    assert repeated["evidence_kind"] == "historical_isolated_replay_not_prospective"
    assert repeated["formal_filter_qualified"] is False
    assert repeated["production_ranking_effect"] == "none"
    assert repeated["online_models_modified"] is repeated["existing_online_estimators_validated"] is False
    assert repeated["protocol"]["test_used_for_selection"] is False
    assert repeated["protocol"]["previous_exposure_excluded"] is False
    assert repeated["source"]["provenance"]["limitations"] == PROVENANCE["limitations"]
    repeated["source"]["provenance"]["limitations"].append("local report annotation")
    assert "local report annotation" not in PROVENANCE["limitations"]


def test_cli_publishes_only_new_research_report_and_does_not_modify_inputs(tmp_path, monkeypatch, capsys, history, report):
    series, sessions = history
    archive = tmp_path / "derived-history"
    archive.mkdir()
    database, manifest = archive / "history.sqlite3", archive / "manifest.json"
    database.write_bytes(b"immutable source placeholder")
    manifest.write_text("immutable manifest placeholder")
    online = tmp_path / "online-model.json"
    online.write_text("unchanged online model")
    before = {path: path.read_bytes() for path in (database, manifest, online)}
    loaded = []

    def load(manifest_path, database_path):
        loaded.append((manifest_path, database_path))
        return SimpleNamespace(series=series, sessions=sessions, provenance=PROVENANCE)

    monkeypatch.setattr(cli, "_load_history", load)
    monkeypatch.setattr(cli, "validate_experimental_direction_history", lambda *_args, **_kwargs: deepcopy(report))
    output = tmp_path / "validation"
    result = cli.main(["--choice-history-manifest", str(manifest), "--choice-history-database", str(database), "--output-dir", str(output)])

    assert result == 0 and loaded == [(manifest, database)]
    summary = json.loads(capsys.readouterr().out)
    target = Path(summary["path"])
    envelope = json.loads(target.read_text())
    assert envelope["payload"] == report
    assert envelope["sha256"] == sha256_hex(canonical_json_bytes(report))
    assert list(output.iterdir()) == [target]
    assert not target.name.startswith("experimental-close-")
    assert all(path.read_bytes() == value for path, value in before.items())
    assert summary["online_models_modified"] is False


def test_cli_source_validation_failure_creates_no_output(tmp_path, monkeypatch, capsys):
    def fail(*_args):
        raise ChoiceError("source receipts no longer match")

    monkeypatch.setattr(cli, "_load_history", fail)
    output = tmp_path / "validation"
    result = cli.main(["--choice-history-manifest", str(tmp_path / "manifest"), "--database", str(tmp_path / "db"), "--output-dir", str(output)])
    assert result == 2 and not output.exists()
    assert json.loads(capsys.readouterr().err)["status"] == "failed"


def test_cli_rejects_symlink_output_before_loading_any_history(tmp_path, monkeypatch):
    directory = tmp_path / "real"
    directory.mkdir()
    link = tmp_path / "link"
    link.symlink_to(directory, target_is_directory=True)
    monkeypatch.setattr(cli, "_load_history", lambda *_args: pytest.fail("unsafe destination reached loader"))
    assert cli.main(["--choice-history-manifest", "unused", "--database", "unused", "--output-dir", str(link)]) == 2
    assert not list(directory.iterdir())


@pytest.mark.parametrize("directory", [".workbuddy-ai", ".git", ".codex", ".agents", ".venv"])
def test_cli_rejects_protected_output_before_loading_history(tmp_path, monkeypatch, directory):
    monkeypatch.setattr(cli, "_load_history", lambda *_args: pytest.fail("unsafe destination reached loader"))
    output = tmp_path / directory / "validation"
    assert cli.main(["--choice-history-manifest", "unused", "--database", "unused", "--output-dir", str(output)]) == 2
    assert not output.exists()


@pytest.mark.parametrize("archive,child", [
    ("original", ""), ("original", "validation"),
    ("manifest", ""), ("manifest", "validation"),
    ("database", ""), ("database", "validation"),
])
def test_cli_rejects_report_in_original_or_derived_choice_archive(tmp_path, monkeypatch, archive, child):
    original = tmp_path / "choice-source"
    manifest_directory = tmp_path / "choice-derived-manifest"
    database_directory = tmp_path / "choice-derived-database"
    for directory in (original, manifest_directory, database_directory):
        directory.mkdir()
    manifest = manifest_directory / "history.manifest.json"
    database = database_directory / "history.sqlite3"
    history = SimpleNamespace(provenance={"source": {"directory": str(original)}})
    monkeypatch.setattr(cli, "_load_history", lambda *_args: history)
    monkeypatch.setattr(cli, "validate_experimental_direction_history", lambda *_args, **_kwargs: pytest.fail("unsafe output reached fitting"))
    protected = {"original": original, "manifest": manifest_directory, "database": database_directory}[archive]
    assert cli.main([
        "--choice-history-manifest", str(manifest), "--database", str(database),
        "--output-dir", str(protected / child),
    ]) == 2
    assert all(not list(directory.iterdir()) for directory in (original, manifest_directory, database_directory))
