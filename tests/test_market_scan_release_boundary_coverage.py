from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
from functools import partial
import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.db.market_scan_integrity import (
    MarketScanSnapshotSealError,
    market_scan_snapshot_digest,
    seal_market_scan_snapshot,
    verify_market_scan_snapshot,
)
from app.models.market_scan import MarketScanResultItem, MarketScanRun
from app.models.market_scan_snapshot import (
    FrozenFullMarketSnapshotIntegrityError,
    validate_frozen_full_market_snapshot,
)
from app.services import market_scan_probability_capture as probability_capture
from app.services import market_scan_probability_source as probability_source
from app.services.market_scan_probability import (
    PROBABILITY_COST_MODEL_VERSION,
    ProbabilityConfig,
    ProbabilityReplayError,
    ProbabilitySample,
    VerifiedProbabilityDeploymentEstimator,
    build_probability_filter_qualification,
    fit_shadow_probability,
    predict_shadow_probability,
    seal_probability_filter_authorization_artifact,
    seal_probability_deployment_artifact,
    stable_probability_hash,
    verify_probability_filter_authorization_artifact,
    verify_probability_deployment_artifact,
    verify_shadow_probability_evidence,
)
from tests.test_market_scan_probability import (
    _complete_test_label_contract,
    _filter_authorization,
    _signal_samples,
    _small_config,
)
from tests.test_market_scan_probability_capture import (
    _FakeCache as CaptureFakeCache,
    _archive_info as capture_archive_info,
    _run as capture_run,
)
from tests.test_market_scan_probability_source import (
    _build_current_source as build_current_source,
    _capture_current_source as capture_current_source,
    _result_item as source_result_item,
    _run as source_contract_run,
    _source_projection as source_contract_projection,
)
from tests.test_strategy_execution import (
    _disable_market_scan_immutability,
    _environment,
)


@pytest.fixture(scope="module")
def fitted_probability_evidence() -> dict[str, object]:
    return fit_shadow_probability(
        _signal_samples(43),
        config=_small_config(),
        generated_at="2026-08-11T08:00:00Z",
    )


@pytest.fixture(scope="module")
def insufficient_probability_evidence() -> dict[str, object]:
    return fit_shadow_probability(
        _signal_samples(20),
        config=ProbabilityConfig(horizon=1, bootstrap_samples=100),
        generated_at="2026-08-11T08:00:00Z",
    )


@pytest.fixture(scope="module")
def isotonic_probability_evidence() -> dict[str, object]:
    return fit_shadow_probability(
        _signal_samples(43),
        config=ProbabilityConfig(
            horizon=1,
            minimum_train_sessions=12,
            minimum_calibration_sessions=6,
            minimum_test_sessions=6,
            minimum_bin_sessions=1,
            minimum_isotonic_calibration_sessions=6,
            bootstrap_samples=100,
        ),
        generated_at="2026-08-11T08:00:00Z",
    )


@pytest.fixture(scope="module")
def authorization_boundary() -> tuple[dict[str, object], dict[str, object]]:
    evidence = fit_shadow_probability(
        _signal_samples(100),
        config=ProbabilityConfig(
            horizon=1,
            label_contract=_complete_test_label_contract(),
            minimum_train_sessions=12,
            minimum_calibration_sessions=6,
            minimum_test_sessions=30,
            minimum_bin_sessions=1,
            bootstrap_samples=100,
        ),
        generated_at="2026-08-11T08:00:00Z",
    )
    return evidence, _filter_authorization(evidence)


def _reseal_evidence(evidence: dict[str, object]) -> dict[str, object]:
    evidence["evidence_digest"] = stable_probability_hash(
        {key: value for key, value in evidence.items() if key != "evidence_digest"}
    )
    return evidence


def _reseal_fold(fold: dict[str, object]) -> None:
    fold["fold_digest"] = stable_probability_hash(
        {key: value for key, value in fold.items() if key != "fold_digest"}
    )


@pytest.mark.parametrize(
    "case",
    (
        "top_level_version",
        "unsupported_status",
        "calibrated_without_model",
        "research_probability",
        "blank_generated_at",
        "model_version",
        "model_not_converged",
        "model_iterations",
        "folds_not_array",
        "fitted_without_folds",
        "walk_forward_count",
        "fold_id",
        "training_cutoff",
        "test_session_count",
        "fold_sequence",
        "active_fold_binding",
        "fold_model_digest",
        "fold_model_version",
        "split_not_array",
        "split_duplicate_date",
        "split_short_train",
        "split_bad_gap_length",
        "split_overlap",
        "candidate_count",
        "candidate_identity",
        "prediction_count",
        "prediction_session_count",
        "duplicate_prediction_id",
        "unknown_prediction_fold",
        "prediction_outside_fold",
        "prediction_value",
        "prediction_reference",
        "fold_prediction_count",
        "unexpected_isotonic_prediction",
        "input_digest",
        "selection_qualification",
        "selection_boolean",
        "broken_contract_shape",
    ),
)
def test_public_probability_replay_rejects_resealed_boundary_counterexamples(
    fitted_probability_evidence: dict[str, object],
    case: str,
) -> None:
    evidence = deepcopy(fitted_probability_evidence)
    folds = cast(list[dict[str, Any]], evidence["folds"])
    predictions = cast(list[dict[str, Any]], evidence["predictions"])
    counts = cast(dict[str, Any], evidence["counts"])

    if case == "top_level_version":
        evidence["schema_version"] = "market-scan-shadow-probability-v999"
    elif case == "unsupported_status":
        evidence["status"] = "published"
    elif case == "calibrated_without_model":
        evidence["model"] = None
    elif case == "research_probability":
        evidence["probability"] = 0.5
    elif case == "blank_generated_at":
        evidence["generated_at"] = ""
    elif case == "model_version":
        model = cast(dict[str, object], evidence["model"])
        model["version"] = "unregistered-model"
        evidence["model_digest"] = stable_probability_hash(model)
    elif case == "model_not_converged":
        model = cast(dict[str, object], evidence["model"])
        model["converged"] = False
        evidence["model_digest"] = stable_probability_hash(model)
    elif case == "model_iterations":
        model = cast(dict[str, object], evidence["model"])
        model["iterations"] = 0
        evidence["model_digest"] = stable_probability_hash(model)
    elif case == "folds_not_array":
        evidence["folds"] = None
    elif case == "fitted_without_folds":
        evidence["folds"] = []
    elif case == "walk_forward_count":
        counts["walk_forward_fold_count"] += 1
    elif case == "fold_id":
        folds[0]["fold_id"] = 9
        _reseal_fold(folds[0])
    elif case == "training_cutoff":
        folds[0]["training_cutoff"] = "2020-01-01"
        _reseal_fold(folds[0])
    elif case == "test_session_count":
        folds[0]["test_session_count"] += 1
        _reseal_fold(folds[0])
    elif case == "fold_sequence":
        split = cast(dict[str, list[str]], folds[1]["split"])
        split["train_dates"][0] = "2024-01-01"
        _reseal_fold(folds[1])
    elif case == "active_fold_binding":
        evidence["base_rate"] = float(evidence["base_rate"]) + 0.01
    elif case == "fold_model_digest":
        folds[0]["model_digest"] = "a" * 64
        _reseal_fold(folds[0])
    elif case == "fold_model_version":
        model = cast(dict[str, object], folds[0]["model"])
        model["version"] = "unregistered-fold-model"
        folds[0]["model_digest"] = stable_probability_hash(model)
        _reseal_fold(folds[0])
    elif case == "split_not_array":
        cast(dict[str, object], folds[0]["split"])["train_dates"] = "not-an-array"
        _reseal_fold(folds[0])
    elif case == "split_duplicate_date":
        split = cast(dict[str, list[str]], folds[0]["split"])
        split["train_dates"][1] = split["train_dates"][0]
        _reseal_fold(folds[0])
    elif case == "split_short_train":
        split = cast(dict[str, list[str]], folds[0]["split"])
        split["train_dates"] = split["train_dates"][:-1]
        _reseal_fold(folds[0])
    elif case == "split_bad_gap_length":
        split = cast(dict[str, list[str]], folds[0]["split"])
        split["train_gap_dates"] = split["train_gap_dates"][:-1]
        _reseal_fold(folds[0])
    elif case == "split_overlap":
        split = cast(dict[str, list[str]], folds[0]["split"])
        split["train_gap_dates"][0] = split["train_dates"][-1]
        _reseal_fold(folds[0])
    elif case == "candidate_count":
        evidence["calibration_candidates"] = []
    elif case == "candidate_identity":
        cast(list[dict[str, object]], evidence["calibration_candidates"])[0]["id"] = "unknown"
    elif case == "prediction_count":
        counts["out_of_sample_observation_count"] += 1
    elif case == "prediction_session_count":
        counts["out_of_sample_session_count"] += 1
    elif case == "duplicate_prediction_id":
        predictions[1]["sample_id"] = predictions[0]["sample_id"]
    elif case == "unknown_prediction_fold":
        predictions[0]["fold_id"] = 99
    elif case == "prediction_outside_fold":
        predictions[0]["session_date"] = "2099-01-01"
    elif case == "prediction_value":
        predictions[0]["raw_probability"] += 0.01
    elif case == "prediction_reference":
        predictions[0]["reference_base_rate"] += 0.01
    elif case == "fold_prediction_count":
        predictions.pop()
    elif case == "unexpected_isotonic_prediction":
        predictions[0]["isotonic_probability"] = 0.5
    elif case == "input_digest":
        evidence["input_digest"] = "short"
    elif case == "selection_qualification":
        cast(dict[str, object], evidence["selection_qualification"])["passed"] = True
    elif case == "selection_boolean":
        evidence["selection_qualified"] = not bool(evidence["selection_qualified"])
    else:
        evidence["contract"] = None

    _reseal_evidence(evidence)
    with pytest.raises(ProbabilityReplayError):
        verify_shadow_probability_evidence(evidence)


@pytest.mark.parametrize(
    "case",
    ("unfitted_fold", "empty_prediction_metrics", "invalid_selection"),
)
def test_public_probability_replay_rejects_invalid_unfitted_evidence(
    insufficient_probability_evidence: dict[str, object],
    case: str,
) -> None:
    evidence = deepcopy(insufficient_probability_evidence)
    if case == "unfitted_fold":
        evidence["folds"] = [{}]
    elif case == "empty_prediction_metrics":
        evidence["calibration_metrics"] = {}
    else:
        evidence["fit_status"] = "fitted_oos"
    _reseal_evidence(evidence)

    with pytest.raises(ProbabilityReplayError):
        verify_shadow_probability_evidence(evidence)


def test_public_probability_replay_rejects_isotonic_prediction_tamper(
    isotonic_probability_evidence: dict[str, object],
) -> None:
    evidence = deepcopy(isotonic_probability_evidence)
    cast(list[dict[str, float]], evidence["predictions"])[0]["isotonic_probability"] += 0.01
    _reseal_evidence(evidence)

    with pytest.raises(ProbabilityReplayError, match="Isotonic"):
        verify_shadow_probability_evidence(evidence)


def test_public_probability_replay_and_predict_bind_complete_inputs(
    fitted_probability_evidence: dict[str, object],
) -> None:
    changed_samples = deepcopy(_signal_samples(43))
    changed_samples[0] = ProbabilitySample(
        sample_id=changed_samples[0].sample_id,
        session_date=changed_samples[0].session_date,
        features=changed_samples[0].features,
        target=1 - int(changed_samples[0].target or 0),
    )
    with pytest.raises(ProbabilityReplayError, match="完整输入重放"):
        verify_shadow_probability_evidence(fitted_probability_evidence, changed_samples)

    with pytest.raises(ValueError, match="feature schema"):
        predict_shadow_probability(
            fitted_probability_evidence,
            {"trend": 1.0},
            sample_id="missing-risk-feature",
        )


@pytest.mark.parametrize(
    "case",
    (
        "prediction_feature_schema",
        "prediction_bad_date",
        "prediction_fold_bool",
        "prediction_sample_blank",
        "prediction_raw_bool",
        "prediction_outcome_bool",
        "predictions_not_array",
        "predictions_empty",
        "fold_model_feature_order",
        "fold_model_means_not_array",
        "fold_model_means_bool",
        "fold_model_dimension",
        "fold_model_scale_zero",
        "fold_model_feature_bool",
        "fold_baseline_dimension",
        "fold_baseline_probability_range",
        "fold_base_rate_range",
        "evidence_horizon_bool",
    ),
)
def test_public_probability_replay_rejects_persisted_type_and_dimension_counterexamples(
    fitted_probability_evidence: dict[str, object],
    case: str,
) -> None:
    evidence = deepcopy(fitted_probability_evidence)
    folds = cast(list[dict[str, Any]], evidence["folds"])
    predictions = cast(list[dict[str, Any]], evidence["predictions"])
    fold = folds[0]
    model = cast(dict[str, Any], fold["model"])
    baseline = cast(dict[str, Any], fold["empirical_bayes_baseline"])

    if case == "prediction_feature_schema":
        cast(dict[str, object], predictions[0]["features"]).pop("risk")
    elif case == "prediction_bad_date":
        predictions[0]["session_date"] = "not-a-date"
    elif case == "prediction_fold_bool":
        predictions[0]["fold_id"] = True
    elif case == "prediction_sample_blank":
        predictions[0]["sample_id"] = " "
    elif case == "prediction_raw_bool":
        predictions[0]["raw_probability"] = True
    elif case == "prediction_outcome_bool":
        predictions[0]["outcome"] = True
    elif case == "predictions_not_array":
        evidence["predictions"] = None
    elif case == "predictions_empty":
        evidence["predictions"] = []
    elif case == "fold_model_feature_order":
        model["feature_names"] = list(reversed(cast(list[str], model["feature_names"])))
    elif case == "fold_model_means_not_array":
        model["means"] = "not-an-array"
    elif case == "fold_model_means_bool":
        cast(list[object], model["means"])[0] = True
    elif case == "fold_model_dimension":
        model["means"] = []
    elif case == "fold_model_scale_zero":
        cast(list[float], model["scales"])[0] = 0.0
    elif case == "fold_model_feature_bool":
        first_name = cast(list[str], model["feature_names"])[0]
        cast(dict[str, object], predictions[0]["features"])[first_name] = True
    elif case == "fold_baseline_dimension":
        cast(list[float], baseline["probabilities"]).pop()
    elif case == "fold_baseline_probability_range":
        baseline["probabilities"] = [2.0] * len(cast(list[float], baseline["probabilities"]))
    elif case == "fold_base_rate_range":
        fold["base_rate"] = 2.0
    else:
        evidence["horizon"] = True

    if case.startswith("fold_model"):
        fold["model_digest"] = stable_probability_hash(model)
        _reseal_fold(fold)
    elif case.startswith("fold_baseline"):
        fold["baseline_digest"] = stable_probability_hash(baseline)
        _reseal_fold(fold)
    elif case == "fold_base_rate_range":
        _reseal_fold(fold)
    _reseal_evidence(evidence)

    with pytest.raises(ProbabilityReplayError):
        verify_shadow_probability_evidence(evidence)


@pytest.mark.parametrize(
    "case",
    (
        "dimension",
        "bounds_not_array",
        "probability_not_array",
        "probability_range",
    ),
)
def test_public_probability_replay_rejects_isotonic_model_counterexamples(
    isotonic_probability_evidence: dict[str, object],
    case: str,
) -> None:
    evidence = deepcopy(isotonic_probability_evidence)
    fold = cast(list[dict[str, Any]], evidence["folds"])[0]
    calibrator = cast(dict[str, Any], fold["isotonic_calibrator"])
    if case == "dimension":
        calibrator["probabilities"] = []
    elif case == "bounds_not_array":
        calibrator["upper_bounds"] = "not-an-array"
    elif case == "probability_not_array":
        calibrator["probabilities"] = "not-an-array"
    else:
        calibrator["probabilities"] = [2.0] * len(
            cast(list[float], calibrator["probabilities"])
        )
    fold["isotonic_calibrator_digest"] = stable_probability_hash(calibrator)
    _reseal_fold(fold)
    _reseal_evidence(evidence)

    with pytest.raises(ProbabilityReplayError):
        verify_shadow_probability_evidence(evidence)


@pytest.mark.parametrize(
    "case",
    (
        "blank_generated_at",
        "blank_sample_id",
        "duplicate_sample_id",
        "invalid_session_date",
        "non_boolean_executable",
        "invalid_target",
    ),
)
def test_public_probability_fit_rejects_invalid_input_rows(case: str) -> None:
    samples = deepcopy(_signal_samples(2))
    if case == "blank_generated_at":
        with pytest.raises(ValueError, match="generated_at"):
            fit_shadow_probability(samples, config=_small_config(), generated_at=" ")
        return
    if case == "blank_sample_id":
        samples[0] = replace(samples[0], sample_id="")
    elif case == "duplicate_sample_id":
        samples[1] = replace(samples[1], sample_id=samples[0].sample_id)
    elif case == "invalid_session_date":
        samples[0] = replace(samples[0], session_date="not-a-date")
    elif case == "non_boolean_executable":
        samples[0] = replace(samples[0], executable=cast(Any, "yes"))
    else:
        samples[0] = replace(samples[0], target=cast(Any, 2))

    with pytest.raises(ValueError):
        fit_shadow_probability(samples, config=_small_config(), generated_at="2026-08-11T08:00:00Z")


def test_public_probability_fit_and_hash_accept_canonical_boundary_values() -> None:
    sample = replace(_signal_samples(1)[0], target=True)
    evidence = fit_shadow_probability(
        [sample],
        config=_small_config(),
        generated_at="2026-08-11T08:00:00Z",
    )
    empty = fit_shadow_probability(
        [],
        config=_small_config(),
        generated_at="2026-08-11T08:00:00Z",
    )
    assert evidence["status"] == "insufficient_data"
    assert empty["status"] == "insufficient_data"
    assert stable_probability_hash(sample) == stable_probability_hash(sample)
    assert stable_probability_hash(date(2026, 8, 11)) == stable_probability_hash("2026-08-11")
    with pytest.raises(ValueError, match="不可序列化"):
        stable_probability_hash(object())


def _invalid_probability_config(case: str) -> dict[str, object]:
    base: dict[str, object] = {"horizon": 1, "bootstrap_samples": 100}
    contract = _complete_test_label_contract()
    if case == "horizon":
        base["horizon"] = 4
    elif case == "target":
        base["target"] = "future_leak"
    elif case == "cost_model":
        base["cost_model_version"] = " "
    elif case == "positive_integer":
        base["minimum_train_sessions"] = 0
    elif case == "label_coverage":
        base["minimum_label_coverage"] = 0.0
    elif case == "positive_float":
        base["l2_strength"] = float("inf")
    elif case == "bootstrap":
        base["bootstrap_samples"] = 99
    else:
        base["label_contract"] = contract
        base["cost_model_version"] = PROBABILITY_COST_MODEL_VERSION
        mutations: dict[str, Callable[[dict[str, object]], None]] = {
            "empty_label_contract": lambda value: value.clear(),
            "label_version": lambda value: value.__setitem__("label_version", "bad"),
            "label_cost": lambda value: value.__setitem__("cost_model_version", "bad"),
            "missing_assumption": lambda value: value.pop("execution_model"),
            "blank_text": lambda value: value.__setitem__("execution_model", " "),
            "horizons": lambda value: value.__setitem__("horizons", [1, 1]),
            "entry_offset": lambda value: value.__setitem__("entry_session_offset", 0),
            "target_offsets": lambda value: value.__setitem__("target_session_offsets", {"1": 1}),
            "target_definitions": lambda value: value.__setitem__("target_definitions", []),
            "notional": lambda value: value.__setitem__("execution_notional", True),
            "participation": lambda value: value.__setitem__("max_daily_participation_rate", 2.0),
        }
        mutations[case](contract)
    return base


@pytest.mark.parametrize(
    "case",
    (
        "horizon",
        "target",
        "cost_model",
        "positive_integer",
        "label_coverage",
        "positive_float",
        "bootstrap",
        "empty_label_contract",
        "label_version",
        "label_cost",
        "missing_assumption",
        "blank_text",
        "horizons",
        "entry_offset",
        "target_offsets",
        "target_definitions",
        "notional",
        "participation",
    ),
)
def test_public_probability_config_rejects_ambiguous_research_contracts(case: str) -> None:
    with pytest.raises(ValueError):
        ProbabilityConfig(**_invalid_probability_config(case))  # type: ignore[arg-type]


def _reseal_authorization_payload(
    artifact: dict[str, object],
) -> dict[str, object]:
    return seal_probability_filter_authorization_artifact(
        cast(Mapping[str, object], artifact["payload"]),
        generated_at=str(artifact["generated_at"]),
    )


@pytest.mark.parametrize(
    "case",
    (
        "schema",
        "payload_version",
        "binding",
        "predictions",
        "candidate_count",
        "duplicate_candidate",
        "selected_binding",
        "missing_selected",
        "invalid_candidate",
        "empty_statistics",
        "duplicate_statistics_date",
        "bh_fdr",
        "short_reference_drift",
        "duplicate_reference_drift",
        "short_current_drift",
        "missing_execution",
        "execution_schema",
    ),
)
def test_public_probability_authorization_rejects_resealed_raw_gate_tamper(
    authorization_boundary: tuple[dict[str, object], dict[str, object]],
    case: str,
) -> None:
    evidence, original = authorization_boundary
    artifact = deepcopy(original)
    payload = cast(dict[str, Any], artifact["payload"])
    if case == "schema":
        artifact["schema_version"] = "probability-filter-authorization-v999"
    elif case == "payload_version":
        payload["version"] = "unregistered"
    elif case == "binding":
        payload["evidence_binding"]["evidence_digest"] = "a" * 64
    elif case == "predictions":
        payload["oos_predictions"][0]["probability"] += 0.01
    elif case == "candidate_count":
        payload["candidate_registry"] = payload["candidate_registry"][:5]
    elif case == "duplicate_candidate":
        payload["candidate_registry"][1]["candidate_id"] = payload["candidate_registry"][0]["candidate_id"]
    elif case == "selected_binding":
        payload["candidate_registry"][0]["evidence_digest"] = "b" * 64
    elif case == "missing_selected":
        payload["selected_candidate_id"] = "not-registered"
    elif case == "invalid_candidate":
        payload["candidate_registry"][1]["evidence_digest"] = "short"
    elif case == "empty_statistics":
        payload["candidate_registry"][1]["session_statistics"] = []
    elif case == "duplicate_statistics_date":
        statistics = payload["candidate_registry"][1]["session_statistics"]
        statistics[1]["session_date"] = statistics[0]["session_date"]
    elif case == "bh_fdr":
        payload["multiple_testing"]["family_size"] += 1
    elif case == "short_reference_drift":
        payload["drift_validation"]["reference_series"] = payload["drift_validation"]["reference_series"][:29]
    elif case == "duplicate_reference_drift":
        rows = payload["drift_validation"]["reference_series"]
        rows[1]["session_date"] = rows[0]["session_date"]
    elif case == "short_current_drift":
        payload["drift_validation"]["current_series"] = payload["drift_validation"]["current_series"][:29]
    elif case == "missing_execution":
        payload["execution_validation"] = None
    else:
        payload["execution_validation"].pop("capacity_coverage")
    if case != "schema":
        artifact = _reseal_authorization_payload(artifact)

    with pytest.raises(ProbabilityReplayError):
        verify_probability_filter_authorization_artifact(artifact, evidence)


@pytest.mark.parametrize(
    "case",
    (
        "top_level_schema",
        "payload_not_object",
        "integrity_not_object",
        "integrity_schema",
        "payload_schema",
        "timestamp_malformed",
        "timestamp_naive",
        "timestamp_future",
        "timestamp_before_evidence",
        "evidence_timestamp_malformed",
    ),
)
def test_public_probability_authorization_rejects_envelope_boundaries(
    authorization_boundary: tuple[dict[str, object], dict[str, object]],
    case: str,
) -> None:
    evidence, original = authorization_boundary
    artifact = deepcopy(original)
    if case == "top_level_schema":
        artifact["unexpected"] = True
    elif case == "payload_not_object":
        artifact["payload"] = []
    elif case == "integrity_not_object":
        artifact["integrity"] = []
    elif case == "integrity_schema":
        cast(dict[str, object], artifact["integrity"]).pop("notice")
    elif case == "payload_schema":
        cast(dict[str, object], artifact["payload"]).pop("execution_validation")
        artifact = _reseal_authorization_payload(artifact)
    elif case == "timestamp_malformed":
        artifact["generated_at"] = "not-a-time"
    elif case == "timestamp_naive":
        artifact["generated_at"] = "2026-08-11T09:00:00"
    elif case == "timestamp_future":
        artifact["generated_at"] = "2099-01-01T00:00:00Z"
    elif case == "timestamp_before_evidence":
        artifact = seal_probability_filter_authorization_artifact(
            cast(Mapping[str, object], artifact["payload"]),
            generated_at="2026-08-10T08:00:00Z",
        )
    else:
        evidence = {**evidence, "generated_at": "not-a-time"}

    with pytest.raises(ProbabilityReplayError):
        verify_probability_filter_authorization_artifact(artifact, evidence)


def test_public_probability_qualification_handles_future_joint_contract_and_bad_metrics(
    fitted_probability_evidence: dict[str, object],
) -> None:
    future = deepcopy(fitted_probability_evidence)
    contract = cast(dict[str, Any], future["contract"])
    contract["label"] = {
        "version": "market-scan-joint-execution-label-v1",
        "target": "joint_execution_action_positive",
        "target_population": "all_fixed_full_market_decisions_including_unfilled_and_unexecutable",
        "observed_components": ["entry_fill", "exit_executable", "net_positive"],
        "selection_probability": "joint_execution_action_probability",
    }
    future["schema_version"] = "market-scan-joint-execution-probability-v1"
    qualification = build_probability_filter_qualification(future)
    assert qualification["passed"] is False
    assert cast(dict[str, object], qualification["gates"])["execution_validation_passed"] is False

    malformed = deepcopy(fitted_probability_evidence)
    calibrated = cast(dict[str, object], cast(dict[str, Any], malformed["calibration_metrics"])["calibrated"])
    calibrated["brier_improvement_vs_reference_ci_95"] = [1.0, -1.0]
    calibrated["unhashable_contract_value"] = object()
    malformed_qualification = build_probability_filter_qualification(malformed)
    assert cast(dict[str, object], malformed_qualification["proper_score_evidence"])[
        "metrics_digest"
    ] is None


def test_public_probability_deployment_envelope_is_sealed_and_fails_closed_without_token(
    fitted_probability_evidence: dict[str, object],
) -> None:
    artifact = seal_probability_deployment_artifact(
        {"contract": "boundary"},
        generated_at="2026-08-11T09:00:00Z",
    )
    assert artifact["schema_version"]
    with pytest.raises(ProbabilityReplayError, match="strict authorization"):
        verify_probability_deployment_artifact(
            artifact,
            evidence=fitted_probability_evidence,
            authorization=object(),
            samples=_signal_samples(43),
        )
    with pytest.raises(ValueError, match="无效"):
        seal_probability_deployment_artifact(
            {},
            generated_at="not-a-time",
        )
    with pytest.raises(TypeError, match="strict verifier"):
        VerifiedProbabilityDeploymentEstimator(
            encoded_payload="{}",
            integrity_digest="a" * 64,
        )


@pytest.mark.parametrize(
    "case",
    (
        "blank_owner",
        "lease_malformed",
        "lease_naive",
        "lease_expired",
        "terminal_status",
        "succeeded_digest",
        "skipped_digest",
        "skipped_reason",
        "lost_finish_lease",
        "retry_time_malformed",
        "retry_time_naive",
        "retry_reason",
        "claim_race",
        "audit_input_digest",
    ),
)
def test_sqlite_probability_capture_outbox_rejects_public_lease_counterexamples(
    tmp_path: Path,
    case: str,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    owner = "release-boundary-owner"
    future = "2099-01-01T00:00:00Z"
    if case == "blank_owner":
        operation = partial(
            cache.claim_probability_source_capture,
            owner=" ",
            lease_expires_at=future,
        )
        expected = ValueError
    elif case == "lease_malformed":
        operation = partial(
            cache.claim_probability_source_capture,
            owner=owner,
            lease_expires_at="not-a-time",
        )
        expected = ValueError
    elif case == "lease_naive":
        operation = partial(
            cache.claim_probability_source_capture,
            owner=owner,
            lease_expires_at="2099-01-01T00:00:00",
        )
        expected = ValueError
    elif case == "lease_expired":
        operation = partial(
            cache.claim_probability_source_capture,
            owner=owner,
            lease_expires_at="2000-01-01T00:00:00Z",
        )
        expected = ValueError
    elif case == "terminal_status":
        operation = partial(
            cache.finish_probability_source_capture,
            run_id,
            owner=owner,
            status="failed",
        )
        expected = ValueError
    elif case == "succeeded_digest":
        operation = partial(
            cache.finish_probability_source_capture,
            run_id,
            owner=owner,
            status="succeeded",
            archive_digest="A" * 64,
        )
        expected = ValueError
    elif case == "skipped_digest":
        operation = partial(
            cache.finish_probability_source_capture,
            run_id,
            owner=owner,
            status="skipped",
            archive_digest="a" * 64,
            message="not eligible",
        )
        expected = ValueError
    elif case == "skipped_reason":
        operation = partial(
            cache.finish_probability_source_capture,
            run_id,
            owner=owner,
            status="skipped",
            message=" ",
        )
        expected = ValueError
    elif case == "lost_finish_lease":
        assert cache.claim_probability_source_capture(
            owner=owner,
            lease_expires_at=future,
        ) is not None
        operation = partial(
            cache.finish_probability_source_capture,
            run_id,
            owner="other-owner",
            status="skipped",
            message="not eligible",
        )
        expected = RuntimeError
    elif case == "retry_time_malformed":
        operation = partial(
            cache.retry_probability_source_capture,
            run_id,
            owner=owner,
            next_attempt_at="not-a-time",
            error="temporary failure",
        )
        expected = ValueError
    elif case == "retry_time_naive":
        operation = partial(
            cache.retry_probability_source_capture,
            run_id,
            owner=owner,
            next_attempt_at="2026-08-13T08:00:00",
            error="temporary failure",
        )
        expected = ValueError
    elif case == "retry_reason":
        operation = partial(
            cache.retry_probability_source_capture,
            run_id,
            owner=owner,
            next_attempt_at="2026-08-13T08:00:00Z",
            error=" ",
        )
        expected = ValueError
    elif case == "claim_race":
        with cache._connect() as conn:  # noqa: SLF001 - SQLite lease race boundary
            conn.execute(
                "UPDATE market_scan_probability_capture_outbox SET next_attempt_at = '2000-01-01T00:00:00Z'"
            )
            conn.execute(
                """
                CREATE TRIGGER release_gate_ignore_capture_claim
                BEFORE UPDATE OF status ON market_scan_probability_capture_outbox
                WHEN OLD.status = 'pending' AND NEW.status = 'processing'
                BEGIN
                    SELECT RAISE(IGNORE);
                END
                """
            )
        assert cache.claim_probability_source_capture(
            owner=owner,
            lease_expires_at=future,
        ) is None
        return
    else:
        operation = partial(
            cache.audit_probability_source_capture_archives,
            {run_id: "A" * 64},
        )
        expected = ValueError

    with pytest.raises(expected):
        operation()


@pytest.mark.parametrize("case", ("matching", "missing", "null_digest", "invalid_digest"))
def test_sqlite_probability_capture_outbox_audits_persisted_terminal_claims(
    tmp_path: Path,
    case: str,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    owner = "release-boundary-owner"
    digest = "a" * 64
    assert cache.claim_probability_source_capture(
        owner=owner,
        lease_expires_at="2099-01-01T00:00:00Z",
    ) is not None
    cache.finish_probability_source_capture(
        run_id,
        owner=owner,
        status="succeeded",
        archive_digest=digest,
    )
    if case in {"null_digest", "invalid_digest"}:
        with cache._connect() as conn:  # noqa: SLF001 - persisted restore corruption boundary
            conn.execute("PRAGMA ignore_check_constraints = ON")
            conn.execute(
                "UPDATE market_scan_probability_capture_outbox SET archive_digest = ? WHERE run_id = ?",
                (None if case == "null_digest" else "A" * 64, run_id),
            )
    archives = {run_id: digest} if case == "matching" else {}
    assert cache.audit_probability_source_capture_archives(archives) == (
        0 if case == "matching" else 1
    )
    with cache._connect() as conn:  # noqa: SLF001 - persisted audit assertion
        status = conn.execute(
            "SELECT status FROM market_scan_probability_capture_outbox WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
    assert status == ("succeeded" if case == "matching" else "pending")


@pytest.mark.parametrize(
    "case",
    ("valid", "run_id", "quote_date", "digest"),
)
def test_public_probability_source_capture_reuses_only_exact_existing_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    run = capture_run(71)
    cache = CaptureFakeCache(tmp_path, run, candidates=[run], items=[object(), object()])
    archive = capture_archive_info(71)
    archive["captured_at"] = "2026-08-11T08:00:00Z"
    if case == "run_id":
        archive["run_id"] = 70
    elif case == "quote_date":
        archive["quote_date"] = "2026-08-10"
    elif case == "digest":
        archive["digest"] = "A" * 64
    monkeypatch.setattr(
        probability_capture,
        "list_probability_source_snapshots",
        lambda *_args, **_kwargs: [archive],
    )

    if case == "valid":
        assert probability_capture.capture_market_scan_probability_source(cache, 71) == archive
    else:
        with pytest.raises(probability_capture.ProbabilitySourceCaptureError):
            probability_capture.capture_market_scan_probability_source(cache, 71)


@pytest.mark.parametrize(
    "case",
    (
        "legacy_seal",
        "quote_date",
        "no_success",
        "not_canonical",
        "incomplete_results",
        "invalid_as_of",
        "captured_at_none",
        "captured_at_datetime",
        "captured_at_invalid",
        "run_changed",
    ),
)
def test_public_probability_source_capture_rejects_run_and_time_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    run = capture_run(71)
    candidates = [run]
    items = [object(), object()]
    captured_at: datetime | str | None = "2026-08-11T16:10:00+08:00"
    if case == "legacy_seal":
        run = run.model_copy(update={"snapshot_seal_origin": "legacy_backfill"})
        candidates = [run]
    elif case == "quote_date":
        run = run.model_copy(update={"quote_date": "2026-08-10"})
        candidates = [run]
    elif case == "no_success":
        run = run.model_copy(update={"success_count": 0})
        candidates = [run]
    elif case == "not_canonical":
        candidates = []
    elif case == "incomplete_results":
        items = [object()]
    elif case == "invalid_as_of":
        run = capture_run(71).model_copy(update={"as_of": "not-a-time"})
        candidates = [run]
    elif case == "captured_at_none":
        captured_at = None
    elif case == "captured_at_datetime":
        captured_at = datetime(2026, 8, 11, 16, 10)
    elif case == "captured_at_invalid":
        captured_at = "not-a-time"

    cache = CaptureFakeCache(tmp_path, run, candidates=candidates, items=items)
    if case == "run_changed":
        changed = run.model_copy(update={"success_count": 1})

        class ChangingCache(CaptureFakeCache):
            reads = 0

            def market_scan_run(self, run_id: int):
                self.reads += 1
                return run if self.reads == 1 else changed

        cache = ChangingCache(tmp_path, run, candidates=[run], items=items)

    monkeypatch.setattr(
        probability_capture,
        "project_probability_source_capture",
        lambda *_args, **_kwargs: {"run": {"run_id": 71}, "records": []},
    )

    def persist(*_args, before_publish, **_kwargs):
        before_publish()
        return capture_archive_info(71)

    monkeypatch.setattr(probability_capture, "capture_source_snapshot", persist)

    if case in {"invalid_as_of", "captured_at_none", "captured_at_datetime"}:
        assert probability_capture.capture_market_scan_probability_source(
            cache,
            71,
            directory=tmp_path / "archive",
            captured_at=captured_at,
        )["run_id"] == 71
    else:
        with pytest.raises(probability_capture.ProbabilitySourceCaptureError):
            probability_capture.capture_market_scan_probability_source(
                cache,
                71,
                directory=tmp_path / "archive",
                captured_at=captured_at,
            )


@pytest.mark.parametrize(
    "case",
    ("newest", "conflict", "invalid_time", "naive_time", "missing_auditor", "missing_path"),
)
def test_public_probability_source_archive_audit_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    class AuditCache:
        path = tmp_path / "runtime.sqlite3"
        observed: dict[int, str] | None = None

        def audit_probability_source_capture_archives(self, archives: dict[int, str]) -> int:
            self.observed = archives
            return len(archives)

    cache: object = AuditCache()
    candidates = [
        {
            "run_id": 71,
            "captured_at": "2026-08-11T08:00:00Z",
            "digest": "a" * 64,
        }
    ]
    if case == "newest":
        candidates.append(
            {
                "run_id": 71,
                "captured_at": "2026-08-11T09:00:00Z",
                "digest": "b" * 64,
            }
        )
    elif case == "conflict":
        candidates.append({**candidates[0], "digest": "b" * 64})
    elif case == "invalid_time":
        candidates[0]["captured_at"] = "not-a-time"
    elif case == "naive_time":
        candidates[0]["captured_at"] = "2026-08-11T08:00:00"
    elif case == "missing_auditor":
        cache = SimpleNamespace(path=tmp_path / "runtime.sqlite3")
    elif case == "missing_path":
        cache = object()
    monkeypatch.setattr(
        probability_capture,
        "list_probability_source_snapshots",
        lambda *_args, **_kwargs: candidates,
    )

    if case == "newest":
        assert probability_capture.audit_market_scan_probability_source_archives(cache) == 1
        assert cast(AuditCache, cache).observed == {71: "b" * 64}
    elif case == "missing_auditor":
        assert probability_capture.audit_market_scan_probability_source_archives(cache) == 0
    else:
        with pytest.raises(probability_capture.ProbabilitySourceCaptureError):
            probability_capture.audit_market_scan_probability_source_archives(cache)


@pytest.mark.parametrize("case", ("failure", "writer_failure", "success", "cancelled"))
def test_public_probability_source_best_effort_monitor_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    class BoundaryCache:
        path = tmp_path / "runtime.sqlite3"

        def market_scan_run(self, _run_id: int):
            raise RuntimeError("archive unavailable")

        def save_monitor_event(self, *_args):
            if case == "writer_failure":
                raise OSError("monitor unavailable")

    cache: object = BoundaryCache() if case != "failure" else SimpleNamespace(
        path=tmp_path / "runtime.sqlite3",
        market_scan_run=lambda _run_id: (_ for _ in ()).throw(RuntimeError("archive unavailable")),
    )
    if case == "success":
        monkeypatch.setattr(
            probability_capture,
            "capture_market_scan_probability_source",
            lambda *_args, **_kwargs: capture_archive_info(71),
        )
    elif case == "cancelled":
        def cancelled(*_args, **_kwargs):
            raise asyncio.CancelledError

        monkeypatch.setattr(
            probability_capture,
            "capture_market_scan_probability_source",
            cancelled,
        )

    if case == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                probability_capture.capture_market_scan_probability_source_best_effort(cache, 71)
            )
    else:
        outcome = asyncio.run(
            probability_capture.capture_market_scan_probability_source_best_effort(cache, 71)
        )
        assert outcome["status"] == ("captured" if case == "success" else "failed")


def test_public_probability_capture_outbox_skips_ineligible_claim() -> None:
    class QueueCache:
        claims = [
            {"run_id": 71, "attempt_count": 1, "captured_at": "2026-08-11T08:00:00Z"},
            None,
        ]
        finished: list[dict[str, object]] = []

        def claim_probability_source_capture(self, **_kwargs):
            return self.claims.pop(0)

        def market_scan_run(self, _run_id: int):
            return capture_run(71).model_copy(update={"status": "failed"})

        def finish_probability_source_capture(self, run_id: int, **kwargs):
            self.finished.append({"run_id": run_id, **kwargs})

        def save_monitor_event(self, *_args):
            return None

    cache = QueueCache()
    summary = asyncio.run(
        probability_capture.process_market_scan_probability_capture_outbox(
            cache,
            owner="release-boundary-owner",
            limit=2,
        )
    )
    assert summary == {"captured": 0, "skipped": 1, "failed": 0}
    assert cache.finished[0]["status"] == "skipped"


@pytest.fixture
def compact_probability_source_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        probability_source,
        "PROBABILITY_SOURCE_MINIMUM_POPULATION",
        {scope: 1 for scope in ("ALL", "SH", "SZ", "BJ")},
    )
    monkeypatch.setattr(
        probability_source,
        "PROBABILITY_SOURCE_MINIMUM_COVERAGE",
        {scope: 0.0 for scope in ("ALL", "SH", "SZ", "BJ")},
    )
    monkeypatch.setattr(
        probability_source,
        "PROBABILITY_SOURCE_MINIMUM_ELIGIBLE_RATIO",
        {scope: 0.0 for scope in ("ALL", "SH", "SZ", "BJ")},
    )


@pytest.mark.parametrize(
    "case",
    ("contract", "scopes", "aggregate", "run_counts"),
)
def test_public_probability_source_coverage_rejects_resealed_counterexamples(
    compact_probability_source_contract: None,
    case: str,
) -> None:
    run = source_contract_run(success_count=1)
    coverage = deepcopy(cast(dict[str, object], run["full_market_coverage"]))
    scopes = cast(dict[str, dict[str, object]], coverage["scopes"])
    total_count = run["total_count"]
    success_count = run["success_count"]
    if case == "contract":
        coverage["contract_version"] = "unsupported"
    elif case == "scopes":
        scopes.pop("BJ")
    elif case == "aggregate":
        scopes["ALL"] = deepcopy(scopes["SH"])
    else:
        total_count = cast(int, total_count) + 1

    with pytest.raises(probability_source.ProbabilitySourceError):
        probability_source.validate_current_full_market_coverage(
            coverage,
            total_count=total_count,
            success_count=success_count,
        )


@pytest.mark.parametrize(
    "case",
    ("missing_success", "missing_progress", "market_set", "progress_counts", "empty_contract", "run_id"),
)
def test_public_probability_source_projection_rejects_population_counterexamples(
    compact_probability_source_contract: None,
    case: str,
) -> None:
    run = source_contract_run(success_count=1)
    results: list[dict[str, object]] = [source_result_item("600519.SH", "SH", "SH_MAIN")]
    if case == "missing_success":
        results = []
    elif case in {"missing_progress", "market_set", "progress_counts"}:
        run.pop("full_market_coverage")
        progress = [
            {
                "market": "SH",
                "total_count": 2,
                "processed_count": 2,
                "success_count": 1,
                "missing_count": 1,
                "skipped_count": 0,
            },
            {
                "market": "SZ",
                "total_count": 1,
                "processed_count": 1,
                "success_count": 0,
                "missing_count": 0,
                "skipped_count": 1,
            },
            {
                "market": "BJ",
                "total_count": 1,
                "processed_count": 1,
                "success_count": 0,
                "missing_count": 0,
                "skipped_count": 1,
            },
        ]
        if case == "missing_progress":
            run["market_progress"] = None
        elif case == "market_set":
            run["market_progress"] = progress[:2]
        else:
            progress[0]["processed_count"] = 1
            run["market_progress"] = progress
    elif case == "empty_contract":
        run = source_contract_run(success_count=0)
        results = []
    else:
        results[0]["run_id"] = 71

    with pytest.raises(probability_source.ProbabilitySourceError):
        probability_source.project_probability_source_capture(
            run,
            results,
            canonical_published=True,
        )


def _reseal_source_artifact(artifact: dict[str, object]) -> None:
    payload = cast(dict[str, object], artifact["payload"])
    cast(dict[str, object], artifact["integrity"])["integrity_digest"] = (
        probability_source.probability_source_payload_digest(payload)
    )


@pytest.mark.parametrize(
    "case",
    (
        "run_mode",
        "score_contract",
        "records_not_array",
        "duplicate_symbol",
        "duplicate_digest",
        "incomplete_records",
        "evidence_contract",
        "feature_names",
        "feature_schema_version",
        "feature_schema_digest",
        "market_coverage",
    ),
)
def test_public_probability_source_verifier_rejects_resealed_storage_counterexamples(
    compact_probability_source_contract: None,
    case: str,
) -> None:
    projection = source_contract_projection(
        ("600519.SH", "SH", "SH_MAIN"),
        ("600520.SH", "SH", "SH_MAIN"),
    )
    artifact = build_current_source(projection)
    payload = cast(dict[str, Any], artifact["payload"])
    run = cast(dict[str, Any], payload["run"])
    records = cast(list[dict[str, Any]], payload["records"])
    feature_schema = cast(dict[str, Any], payload["feature_schema"])
    if case == "run_mode":
        run["mode"] = "intraday"
    elif case == "score_contract":
        run["production_score_spec_hash"] = "0" * 64
    elif case == "records_not_array":
        payload["records"] = None
    elif case == "duplicate_symbol":
        records[1]["symbol"] = records[0]["symbol"]
    elif case == "duplicate_digest":
        records[1]["source_evidence_digest"] = records[0]["source_evidence_digest"]
    elif case == "incomplete_records":
        records.pop()
    elif case == "evidence_contract":
        records[0]["source_evidence_contract_version"] = "unsupported"
    elif case == "feature_names":
        features = cast(dict[str, object], records[0]["features"])
        features.pop(next(iter(features)))
    elif case == "feature_schema_version":
        feature_schema["version"] = "unsupported"
    elif case == "feature_schema_digest":
        feature_schema["digest"] = "0" * 64
    else:
        scopes = cast(dict[str, dict[str, object]], run["full_market_coverage"]["scopes"])
        scopes["SH"], scopes["SZ"] = deepcopy(scopes["SZ"]), deepcopy(scopes["SH"])
    _reseal_source_artifact(artifact)

    with pytest.raises(probability_source.ProbabilitySourceError):
        probability_source.verify_probability_source_snapshot(artifact)


def test_public_probability_source_file_boundary_rejects_noncanonical_and_conflicting_archives(
    tmp_path: Path,
    compact_probability_source_contract: None,
) -> None:
    canonical_directory = tmp_path / "canonical"
    info = capture_current_source(
        canonical_directory,
        source_contract_projection(("600519.SH", "SH", "SH_MAIN")),
    )
    archive = Path(cast(str, info["path"]))
    decoded = gzip.decompress(archive.read_bytes())
    archive.write_bytes(gzip.compress(decoded, compresslevel=9, mtime=1))
    with pytest.raises(probability_source.ProbabilitySourceError, match="规范确定性 gzip"):
        probability_source.load_probability_source_snapshot(archive)

    conflict_directory = tmp_path / "conflict"
    capture_current_source(
        conflict_directory,
        source_contract_projection(("600519.SH", "SH", "SH_MAIN")),
    )
    capture_current_source(
        conflict_directory,
        source_contract_projection(("300750.SZ", "SZ", "CHINEXT")),
    )
    with pytest.raises(probability_source.ProbabilitySourceError, match="同 captured_at"):
        probability_source.load_probability_source_snapshot_for_run(conflict_directory, 70)

    plain_file = tmp_path / "not-a-directory"
    plain_file.write_text("boundary", encoding="utf-8")
    with pytest.raises(probability_source.ProbabilitySourceError, match="路径不是目录"):
        probability_source.list_probability_source_snapshots(plain_file)


@pytest.fixture(scope="module")
def frozen_snapshot(tmp_path_factory: pytest.TempPathFactory) -> tuple[MarketScanRun, list[MarketScanResultItem]]:
    cache, _service, _strategy_id, run_id = _environment(tmp_path_factory.mktemp("release-snapshot"))
    frozen = cache.strategy_execution_repo.frozen_scan(
        run_id=run_id,
        data_date=None,
        mode="official",
    )
    return frozen.run, frozen.items


def _score_spec_hash(spec: Mapping[str, object]) -> str:
    encoded = json.dumps(
        spec,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _cleared_result(item: MarketScanResultItem, *, status: str) -> MarketScanResultItem:
    return item.model_copy(
        update={
            "status": status,
            "rank": None,
            "score": None,
            "raw_score": None,
            "trend_score": None,
            "leader_score": None,
            "data_quality_score": None,
        }
    )


@pytest.mark.parametrize(
    "case",
    (
        "status",
        "mode",
        "scope",
        "invalid_date",
        "noncanonical_date",
        "official_date_mismatch",
        "intraday_date_order",
        "pending",
        "header_counts",
        "total_count",
        "terminal_conservation",
        "foreign_run",
        "blank_symbol",
        "duplicate_symbol",
        "missing_rank",
        "rank_sequence",
        "non_success_rank",
        "result_date",
        "missing_score",
        "non_success_score",
        "no_success",
        "run_rule",
        "score_hash",
        "mixed_score_contract",
        "nonfinite_score_contract",
    ),
)
def test_frozen_snapshot_validator_rejects_every_boundary_counterexample(
    frozen_snapshot: tuple[MarketScanRun, list[MarketScanResultItem]],
    case: str,
) -> None:
    source_run, source_items = frozen_snapshot
    run = source_run.model_copy(deep=True)
    items = [item.model_copy(deep=True) for item in source_items]
    if case == "status":
        run = run.model_copy(update={"status": "running"})
    elif case == "mode":
        run = run.model_copy(update={"mode": "preopen"})
    elif case == "scope":
        run = run.model_copy(update={"scope": "top100_refresh"})
    elif case == "invalid_date":
        run = run.model_copy(update={"quote_date": "not-a-date"})
    elif case == "noncanonical_date":
        run = run.model_copy(update={"quote_date": run.quote_date.replace("-", "")})
    elif case == "official_date_mismatch":
        run = run.model_copy(update={"quote_date": "2026-08-10"})
    elif case == "intraday_date_order":
        previous_date = (date.fromisoformat(run.data_date) - timedelta(days=1)).isoformat()
        run = run.model_copy(update={"mode": "intraday", "quote_date": previous_date})
    elif case == "pending":
        items[0] = items[0].model_copy(update={"status": "pending"})
    elif case == "header_counts":
        items[0] = _cleared_result(items[0], status="missing")
    elif case == "total_count":
        run = run.model_copy(update={"total_count": run.total_count + 1})
    elif case == "terminal_conservation":
        items[0] = _cleared_result(items[0], status="unknown")
        run = run.model_copy(update={"success_count": run.success_count - 1})
    elif case == "foreign_run":
        items[0] = items[0].model_copy(update={"run_id": run.id + 1})
    elif case == "blank_symbol":
        items[0] = items[0].model_copy(update={"symbol": " "})
    elif case == "duplicate_symbol":
        items[1] = items[1].model_copy(update={"symbol": items[0].symbol})
    elif case == "missing_rank":
        items[0] = items[0].model_copy(update={"rank": None})
    elif case == "rank_sequence":
        items[1] = items[1].model_copy(update={"rank": items[0].rank})
    elif case == "non_success_rank":
        items[0] = _cleared_result(items[0], status="missing").model_copy(update={"rank": 4})
        for rank, index in enumerate(range(1, len(items)), start=1):
            items[index] = items[index].model_copy(update={"rank": rank})
        run = run.model_copy(
            update={"success_count": run.success_count - 1, "missing_count": run.missing_count + 1}
        )
    elif case == "result_date":
        items[0] = items[0].model_copy(update={"data_date": "2026-08-10"})
    elif case == "missing_score":
        items[0] = items[0].model_copy(update={"score": None})
    elif case == "non_success_score":
        items[0] = items[0].model_copy(update={"status": "missing", "rank": None})
        for rank, index in enumerate(range(1, len(items)), start=1):
            items[index] = items[index].model_copy(update={"rank": rank})
        run = run.model_copy(
            update={"success_count": run.success_count - 1, "missing_count": run.missing_count + 1}
        )
    elif case == "no_success":
        items = [_cleared_result(item, status="skipped") for item in items]
        run = run.model_copy(
            update={"success_count": 0, "missing_count": 0, "skipped_count": len(items)}
        )
    else:
        details = deepcopy(items[0].score_details)
        if case == "run_rule":
            details["run_rule_version"] = "other-run-rule"
        elif case == "score_hash":
            details["score_spec_hash"] = "a" * 64
        elif case == "mixed_score_contract":
            spec = cast(dict[str, object], details["score_spec"])
            spec["rule_version"] = "other-score-rule"
            details["score_spec_hash"] = _score_spec_hash(spec)
        else:
            cast(dict[str, object], details["score_spec"])["nonfinite"] = float("nan")
        items[0] = items[0].model_copy(update={"score_details": details})

    with pytest.raises(FrozenFullMarketSnapshotIntegrityError):
        validate_frozen_full_market_snapshot(run, items)


@pytest.mark.parametrize(
    "case",
    (
        "digest_missing_run",
        "digest_unpublished",
        "digest_nonfinite",
        "seal_missing_run",
        "seal_unpublished",
        "existing_origin",
        "existing_digest",
        "blank_stamp",
        "provenance_race",
        "digest_race",
        "verify_missing_run",
        "verify_unpublished",
        "verify_missing_digest",
        "verify_bad_digest",
        "verify_bad_origin",
        "verify_missing_stamp",
        "verify_bad_audit_time",
        "verify_future_result",
        "json_not_text",
        "json_malformed",
    ),
)
def test_sqlite_snapshot_seal_fails_closed_for_persisted_counterexamples(
    tmp_path: Path,
    case: str,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    with cache._connect() as conn:  # noqa: SLF001 - real SQLite trust-boundary corruption
        _disable_market_scan_immutability(conn)
        if case == "digest_missing_run":
            operation = partial(market_scan_snapshot_digest, conn, run_id + 999_999)
        elif case == "digest_unpublished":
            conn.execute("UPDATE market_scan_run SET status = 'running' WHERE id = ?", (run_id,))
            operation = partial(market_scan_snapshot_digest, conn, run_id)
        elif case == "digest_nonfinite":
            conn.execute("PRAGMA ignore_check_constraints = ON")
            conn.execute(
                "UPDATE market_scan_result SET raw_score = ? WHERE run_id = ? AND rank = 1",
                (float("inf"), run_id),
            )
            operation = partial(market_scan_snapshot_digest, conn, run_id)
        elif case == "seal_missing_run":
            operation = partial(seal_market_scan_snapshot, conn, run_id + 999_999)
        elif case == "seal_unpublished":
            conn.execute("UPDATE market_scan_run SET status = 'running' WHERE id = ?", (run_id,))
            operation = partial(seal_market_scan_snapshot, conn, run_id)
        elif case == "existing_origin":
            conn.execute(
                "UPDATE market_scan_run SET snapshot_seal_origin = 'legacy_backfill' WHERE id = ?",
                (run_id,),
            )
            operation = partial(seal_market_scan_snapshot, conn, run_id)
        elif case == "existing_digest":
            conn.execute("UPDATE market_scan_run SET snapshot_digest = ? WHERE id = ?", ("a" * 64, run_id))
            operation = partial(seal_market_scan_snapshot, conn, run_id)
        elif case == "blank_stamp":
            conn.execute(
                "UPDATE market_scan_run SET snapshot_digest = NULL, snapshot_seal_origin = NULL, snapshot_sealed_at = NULL WHERE id = ?",
                (run_id,),
            )
            operation = partial(seal_market_scan_snapshot, conn, run_id, sealed_at=" ")
        elif case == "provenance_race":
            conn.execute(
                "UPDATE market_scan_run SET snapshot_digest = NULL, snapshot_seal_origin = 'publication', snapshot_sealed_at = NULL WHERE id = ?",
                (run_id,),
            )
            operation = partial(seal_market_scan_snapshot, conn, run_id)
        elif case == "digest_race":
            conn.execute(
                "UPDATE market_scan_run SET snapshot_digest = NULL, snapshot_seal_origin = NULL, snapshot_sealed_at = NULL WHERE id = ?",
                (run_id,),
            )
            conn.execute(
                f"""
                CREATE TRIGGER release_gate_digest_race
                AFTER UPDATE OF snapshot_seal_origin ON market_scan_run
                WHEN NEW.id = {run_id}
                BEGIN
                    UPDATE market_scan_run SET snapshot_digest = '{'a' * 64}' WHERE id = NEW.id;
                END
                """
            )
            operation = partial(
                seal_market_scan_snapshot,
                conn,
                run_id,
                origin="legacy_backfill",
            )
        elif case == "verify_missing_run":
            operation = partial(verify_market_scan_snapshot, conn, run_id + 999_999)
        elif case == "verify_unpublished":
            conn.execute("UPDATE market_scan_run SET status = 'running' WHERE id = ?", (run_id,))
            operation = partial(verify_market_scan_snapshot, conn, run_id)
        elif case == "verify_missing_digest":
            conn.execute("UPDATE market_scan_run SET snapshot_digest = NULL WHERE id = ?", (run_id,))
            operation = partial(verify_market_scan_snapshot, conn, run_id)
        elif case == "verify_bad_digest":
            conn.execute("PRAGMA ignore_check_constraints = ON")
            conn.execute("UPDATE market_scan_run SET snapshot_digest = ? WHERE id = ?", ("A" * 64, run_id))
            operation = partial(verify_market_scan_snapshot, conn, run_id)
        elif case == "verify_bad_origin":
            conn.execute("PRAGMA ignore_check_constraints = ON")
            conn.execute("UPDATE market_scan_run SET snapshot_seal_origin = 'unknown' WHERE id = ?", (run_id,))
            operation = partial(verify_market_scan_snapshot, conn, run_id)
        elif case == "verify_missing_stamp":
            conn.execute("UPDATE market_scan_run SET snapshot_sealed_at = NULL WHERE id = ?", (run_id,))
            operation = partial(verify_market_scan_snapshot, conn, run_id)
        elif case == "verify_bad_audit_time":
            conn.execute("UPDATE market_scan_run SET finished_at = 'not-a-time' WHERE id = ?", (run_id,))
            operation = partial(verify_market_scan_snapshot, conn, run_id)
        elif case == "verify_future_result":
            conn.execute(
                "UPDATE market_scan_run SET finished_at = ?, updated_at = ?, snapshot_sealed_at = ? WHERE id = ?",
                ("2099-01-01T00:00:00Z", "2099-01-02T00:00:00Z", "2099-01-04T00:00:00Z", run_id),
            )
            conn.execute(
                "UPDATE market_scan_result SET updated_at = ? WHERE run_id = ? AND rank = 1",
                ("2099-01-03T00:00:00Z", run_id),
            )
            operation = partial(verify_market_scan_snapshot, conn, run_id)
        elif case == "json_not_text":
            conn.execute("UPDATE market_scan_result SET tags_json = x'7B7D' WHERE run_id = ? AND rank = 1", (run_id,))
            operation = partial(market_scan_snapshot_digest, conn, run_id)
        else:
            conn.execute("UPDATE market_scan_result SET tags_json = '{' WHERE run_id = ? AND rank = 1", (run_id,))
            operation = partial(market_scan_snapshot_digest, conn, run_id)

        with pytest.raises(MarketScanSnapshotSealError):
            operation()


def test_sqlite_snapshot_digest_accepts_canonical_null_json_and_existing_seal(
    tmp_path: Path,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    with cache._connect() as conn:  # noqa: SLF001 - real SQLite trust-boundary assertion
        expected = seal_market_scan_snapshot(conn, run_id)
        assert expected == verify_market_scan_snapshot(conn, run_id)
        _disable_market_scan_immutability(conn)
        conn.execute("UPDATE market_scan_run SET publication_diagnostics_json = NULL WHERE id = ?", (run_id,))
        assert len(market_scan_snapshot_digest(conn, run_id)) == 64
