from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
import math
from pathlib import Path
import sqlite3
from threading import Event, RLock, Thread
from time import perf_counter
from typing import Any, cast

import pytest

import app.services.market_scan_probability as probability_module
import app.services.market_scan_probability_metrics as probability_metrics
import app.services.market_scan_probability_research as probability_research_module
import app.services.market_scan_probability_store as probability_store_module
from app.config import Settings
from app.db.market_scan_integrity import seal_market_scan_snapshot
from app.services.joint_execution_probability import (
    build_decision_time_joint_execution_probability_evidence,
)
from app.services.cache import SQLiteCache
from app.services.market_scan_probability import (
    LEGACY_PROBABILITY_FEATURE_VERSION,
    PROBABILITY_CALIBRATOR_VERSION,
    PROBABILITY_COST_MODEL_VERSION,
    PROBABILITY_FEATURE_VERSION,
    PROBABILITY_FILTER_AUTHORIZATION_VERSION,
    PROBABILITY_LABEL_VERSION,
    PROBABILITY_ISOTONIC_CALIBRATOR_VERSION,
    PROBABILITY_MODEL_VERSION,
    ProbabilityConfig,
    ProbabilityReplayError,
    ProbabilitySample,
    build_probability_contract,
    build_probability_filter_qualification,
    evaluate_probability_predictions,
    fit_empirical_bayes_baseline,
    fit_probability_deployment_estimator,
    fit_shadow_probability,
    grouped_walk_forward_splits,
    probability_selection_qualified,
    probability_filter_qualified,
    predict_shadow_probability,
    replay_shadow_probability,
    seal_probability_filter_authorization_artifact,
    stable_probability_hash,
    verify_probability_filter_authorization_artifact,
    verify_shadow_probability_evidence,
)
from app.services.market_scan_probability_artifact import (
    PROBABILITY_RESULT_CONTRACT_VERSION,
    ProbabilityArtifactError,
    build_probability_artifact,
    write_probability_artifact,
)
from app.services.market_scan_probability_labels import (
    PROBABILITY_EXECUTION_MODEL,
    ProbabilityLabelConfig,
    ProbabilityLabelOutcome,
    probability_label_contract,
)
from app.services.market_scan_probability_research import (
    ProbabilityResearchRow,
    build_probability_research,
    probability_artifact_payload,
    probability_feature_vector,
)
from app.services.market_scan_probability_store import MarketScanProbabilityStore
from app.services.market_scan_universe import FULL_MARKET_SCOPE
from tests.market_scan_test_support import action_pass_publication_diagnostics


# These store fixtures are frozen historical v4 probability artifacts. Keep
# their scan generation non-current so they do not self-attest the v6 replay
# receipt required for newly published action sources.
_STORE_RULE_VERSION = f"full-market-scan-v5:{'a' * 64}"


def test_probability_metric_primitives_reject_invalid_inputs_without_silent_coercion() -> None:
    with pytest.raises(ValueError, match="至少需要一个观测"):
        probability_metrics.evaluate_probability_predictions([], [], [], base_rate=0.5)
    with pytest.raises(ValueError, match="bin_count"):
        probability_metrics.evaluate_probability_predictions([0.5], [1], ["2026-01-01"], base_rate=0.5, bin_count=1)
    with pytest.raises(ValueError, match="长度必须一致"):
        probability_metrics.evaluate_probability_predictions([0.5], [1], [], base_rate=0.5)
    with pytest.raises(ValueError, match="参考概率"):
        probability_metrics.metric_reference_probabilities([0.5, 0.6], 0.5, 1)
    with pytest.raises(ValueError, match="不能为 None"):
        probability_metrics.validated_metric_rows([0.5], [None], ["2026-01-01"])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="分箱参数"):
        probability_metrics.fit_empirical_bayes_baseline([1.0], [1], bin_count=1)
    with pytest.raises(ValueError, match="非空且长度一致"):
        probability_metrics.validated_scores_and_labels([], [])
    with pytest.raises(ValueError, match="不能为 None"):
        probability_metrics.validated_scores_and_labels([1.0], [None])  # type: ignore[list-item]


def test_probability_metric_scalar_boundaries_and_single_class_outputs() -> None:
    report = probability_metrics.evaluate_probability_predictions(
        [0.0, 0.0],
        [False, False],
        ["2026-01-01", "2026-01-02"],
        base_rate=0.0,
    )
    assert report["brier_skill_score"] is None
    assert report["auc"] is None
    assert report["highest_bin_above_base_rate"] is False
    assert probability_metrics.date_block_bootstrap_ci([("2026-01-01", 2.5)], "single", 10) == [2.5, 2.5]
    assert probability_metrics.percentile([7.0], 0.5) == 7.0
    assert probability_metrics.percentile([1.0, 2.0, 3.0], 0.5) == 2.0
    assert probability_metrics.validated_target(None) is None
    assert probability_metrics.validated_target(True) == 1

    with pytest.raises(ValueError, match="0/1/None"):
        probability_metrics.validated_target(2)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        probability_metrics.require_probability(1.1, "probability")
    with pytest.raises(ValueError, match="ISO 交易日"):
        probability_metrics.validated_date("not-a-date")
    with pytest.raises(ValueError, match="必须是数值"):
        probability_metrics.finite_number(True, "value")
    with pytest.raises(ValueError, match="有限数值"):
        probability_metrics.finite_number(float("inf"), "value")
    with pytest.raises(ValueError, match="必须是整数"):
        probability_metrics.integer(True, "count")
    with pytest.raises(ValueError, match="正整数"):
        probability_metrics.date_block_bootstrap_ci(
            [("2026-01-01", 1.0)], "invalid-block", 10, block_length_sessions=1.5,  # type: ignore[arg-type]
        )


def test_contract_versions_label_cost_model_and_keeps_targets_separate() -> None:
    excess = ProbabilityConfig(horizon=5, target="net_excess_positive")
    absolute = ProbabilityConfig(horizon=5, target="net_return_positive")

    excess_contract = build_probability_contract(excess)
    absolute_contract = build_probability_contract(absolute)

    assert excess_contract["model"]["version"] == PROBABILITY_MODEL_VERSION  # type: ignore[index]
    assert excess_contract["label"]["version"] == PROBABILITY_LABEL_VERSION  # type: ignore[index]
    assert excess_contract["cost"]["version"] == PROBABILITY_COST_MODEL_VERSION  # type: ignore[index]
    assert excess_contract["label"]["target_definition"] == "future_5d_net_excess_return_gt_0_after_costs"  # type: ignore[index]
    assert absolute_contract["label"]["target_definition"] == "future_5d_net_return_gt_0_after_costs"  # type: ignore[index]
    assert stable_probability_hash(excess_contract) != stable_probability_hash(absolute_contract)
    assert stable_probability_hash({"b": 2, "a": 1}) == stable_probability_hash({"a": 1, "b": 2})
    candidates = excess_contract["calibrator"]["candidate_registry"]  # type: ignore[index]
    assert [item["id"] for item in candidates] == ["platt", "isotonic"]
    assert candidates[1]["minimum_calibration_sessions"] == 120
    assert candidates[1]["selection_policy"] == "comparison_only_never_automatic"


def test_grouped_walk_forward_split_has_two_horizon_gaps_and_no_date_leakage() -> None:
    dates = [_day(index) for index in range(40)]
    config = ProbabilityConfig(
        horizon=5,
        minimum_train_sessions=10,
        minimum_calibration_sessions=5,
        minimum_test_sessions=5,
        minimum_bin_sessions=1,
        bootstrap_samples=100,
    )

    folds = grouped_walk_forward_splits([date for date in dates for _duplicate in range(3)], config)

    assert len(folds) == 2
    latest = folds[-1]
    assert len(latest.train_dates) == 15
    assert len(latest.train_gap_dates) == 6
    assert len(latest.calibration_dates) == 5
    assert len(latest.calibration_gap_dates) == 6
    assert len(latest.test_dates) == 5
    partitions = (
        latest.train_dates,
        latest.train_gap_dates,
        latest.calibration_dates,
        latest.calibration_gap_dates,
        latest.test_dates,
    )
    assert len(set().union(*map(set, partitions))) == 37
    assert set(dates[-3:]).isdisjoint(set().union(*map(set, partitions)))
    assert latest.train_dates[-1] < latest.train_gap_dates[0] < latest.calibration_dates[0]
    assert latest.calibration_dates[-1] < latest.calibration_gap_dates[0] < latest.test_dates[0]


@pytest.mark.parametrize(("horizon", "target_offset"), ((1, 2), (5, 6), (20, 21)))
def test_grouped_walk_forward_purge_covers_exact_H_plus_1_label_boundary(
    horizon: int,
    target_offset: int,
) -> None:
    config = ProbabilityConfig(
        horizon=horizon,
        minimum_train_sessions=2,
        minimum_calibration_sessions=2,
        minimum_test_sessions=2,
        minimum_bin_sessions=1,
        bootstrap_samples=100,
    )
    dates = [_day(index) for index in range(2 + 2 * target_offset + 2 + 2)]
    split = grouped_walk_forward_splits(dates, config)[0]
    positions = {value: index for index, value in enumerate(dates)}

    assert config.effective_gap_sessions == target_offset
    assert len(split.train_gap_dates) == target_offset
    assert len(split.calibration_gap_dates) == target_offset
    assert positions[split.train_dates[-1]] + target_offset == positions[split.train_gap_dates[-1]]
    assert positions[split.train_gap_dates[-1]] < positions[split.calibration_dates[0]]
    assert positions[split.calibration_dates[-1]] + target_offset == positions[split.calibration_gap_dates[-1]]
    assert positions[split.calibration_gap_dates[-1]] < positions[split.test_dates[0]]

    with pytest.raises(ValueError, match=r"horizon \+ 1"):
        ProbabilityConfig(horizon=horizon, gap_sessions=horizon, bootstrap_samples=100)


def test_grouped_walk_forward_uses_only_complete_non_overlapping_test_windows() -> None:
    dates = [_day(index) for index in range(43)]
    config = _small_config()

    folds = grouped_walk_forward_splits(
        [session_date for session_date in dates for _stock in range(4)], config,
    )

    assert len(folds) == 3
    test_windows = [set(fold.test_dates) for fold in folds]
    assert all(len(window) == config.minimum_test_sessions for window in test_windows)
    assert all(left.isdisjoint(right) for index, left in enumerate(test_windows) for right in test_windows[index + 1 :])
    assert set(dates[-3:]).isdisjoint(set().union(*test_windows))


def test_calibrated_shadow_fit_is_deterministic_and_replayable() -> None:
    samples = _signal_samples(42)
    config = _small_config()

    first = fit_shadow_probability(samples, config=config, generated_at="2026-08-11T08:00:00Z")
    second = fit_shadow_probability(samples, config=config, generated_at="2026-08-11T08:00:00Z")

    assert first == second
    assert first["status"] == "calibrated_shadow"
    assert first["probability"] is None  # A study is not an individual-stock estimate.
    assert len(str(first["input_digest"])) == 64
    assert len(str(first["model_digest"])) == 64
    assert len(str(first["calibrator_digest"])) == 64
    counts = cast(dict[str, Any], first["counts"])
    assert counts["training_session_count"] >= config.minimum_train_sessions
    assert counts["calibration_session_count"] == config.minimum_calibration_sessions
    assert counts["test_session_count"] == config.minimum_test_sessions
    assert counts["label_coverage"] == 1
    metrics = cast(dict[str, Any], first["calibration_metrics"])["calibrated"]
    assert metrics["brier_skill_score"] > 0
    assert metrics["log_loss"] >= 0
    assert 0 <= metrics["ece"] <= 1
    assert metrics["auc"] > 0.9
    assert metrics["bin_monotonic"] is True
    assert len(metrics["brier_score_ci_95"]) == 2
    assert len(metrics["actual_positive_rate_ci_95"]) == 2
    candidates = {item["id"]: item for item in first["calibration_candidates"]}
    assert candidates["platt"]["status"] == "evaluated_primary"
    assert candidates["platt"]["selected_for_display"] is True
    assert candidates["isotonic"]["status"] == "not_evaluated_insufficient_sessions"
    assert candidates["isotonic"]["parameters"] is None
    assert verify_shadow_probability_evidence(first) is True
    assert verify_shadow_probability_evidence(first, samples) is True
    assert replay_shadow_probability(first, samples) == first


def test_selection_qualification_is_separate_from_fitted_calibrated_display_state() -> None:
    config = ProbabilityConfig(
        horizon=1,
        label_contract=_complete_test_label_contract(),
        minimum_train_sessions=12,
        minimum_calibration_sessions=6,
        minimum_test_sessions=6,
        minimum_bin_sessions=1,
        bootstrap_samples=100,
    )
    one_fold = fit_shadow_probability(
        _signal_samples(28),
        config=config,
        generated_at="2026-08-11T08:00:00Z",
    )
    multiple_folds = fit_shadow_probability(
        _signal_samples(42),
        config=config,
        generated_at="2026-08-11T08:00:00Z",
    )

    assert one_fold["status"] == "calibrated_shadow"
    assert one_fold["fit_status"] == "fitted_oos"
    assert one_fold["selection_qualified"] is False
    assert one_fold["selection_qualification"]["gates"]["multiple_complete_oos_folds"] is False
    assert probability_selection_qualified(one_fold) is False
    assert probability_selection_qualified({"status": "calibrated_shadow"}) is False

    assert multiple_folds["selection_qualified"] is True
    assert multiple_folds["selection_qualification"]["gates"] == {
        "complete_label_contract_bound": True,
        "positive_oos_brier_skill": True,
        "effective_probability_stratification": True,
        "multiple_complete_oos_folds": True,
        "stable_positive_skill_across_complete_oos_folds": True,
    }
    assert probability_selection_qualified(multiple_folds) is True


def test_filter_qualification_requires_bound_promotion_statistics_drift_and_execution() -> None:
    config = ProbabilityConfig(
        horizon=1,
        label_contract=_complete_test_label_contract(),
        minimum_train_sessions=12,
        minimum_calibration_sessions=6,
        minimum_test_sessions=30,
        minimum_bin_sessions=1,
        bootstrap_samples=100,
    )
    evidence = fit_shadow_probability(
        _signal_samples(100), config=config, generated_at="2026-08-11T08:00:00Z",
    )

    missing = build_probability_filter_qualification(evidence)
    assert evidence["selection_qualified"] is True
    assert missing["passed"] is False
    assert missing["gates"]["authorization_bound_to_evidence_digest"] is False
    assert missing["gates"]["multiple_testing_fdr_passed"] is False
    assert probability_filter_qualified(evidence) is False

    authorization_artifact = _filter_authorization(evidence)
    assert probability_filter_qualified(evidence, authorization_artifact) is False
    assert probability_module._joint_execution_estimand_supported(evidence) is False
    assert probability_module._verified_drift_validation(authorization_artifact["payload"])
    with pytest.raises(ProbabilityReplayError, match="原始证据"):
        verify_probability_filter_authorization_artifact(authorization_artifact, evidence)
    with pytest.raises(TypeError, match="strict verifier"):
        probability_module.VerifiedProbabilityFilterAuthorization(
            encoded_payload="{}", integrity_digest="a" * 64,
        )

    future_contract = deepcopy(evidence)
    future_contract["schema_version"] = "market-scan-joint-execution-probability-v1"
    future_contract["contract"]["label"] = {
        "version": "market-scan-joint-execution-label-v1",
        "target": "joint_execution_action_positive",
        "target_population": (
            "all_fixed_full_market_decisions_including_unfilled_and_unexecutable"
        ),
        "observed_components": ["entry_fill", "exit_executable", "net_positive"],
        "selection_probability": "joint_execution_action_probability",
    }
    assert probability_module._joint_execution_estimand_supported(future_contract)
    assert not probability_module._verified_execution_validation(
        authorization_artifact["payload"], future_contract,
    )

    missing_fdr = deepcopy(authorization_artifact)
    del missing_fdr["payload"]["multiple_testing"]
    assert probability_filter_qualified(evidence, missing_fdr) is False
    with pytest.raises(ProbabilityReplayError, match="原始证据"):
        verify_probability_filter_authorization_artifact(missing_fdr, evidence)

    forged_statistics = deepcopy(authorization_artifact)
    forged_statistics["payload"]["candidate_registry"][0]["raw_p_value"] = 1.0
    forged_statistics = seal_probability_filter_authorization_artifact(
        forged_statistics["payload"],
        generated_at=str(forged_statistics["generated_at"]),
    )
    with pytest.raises(ProbabilityReplayError, match="原始证据"):
        verify_probability_filter_authorization_artifact(forged_statistics, evidence)

    forged_ci = deepcopy(authorization_artifact)
    forged_ci["payload"]["calibration_validation"]["brier_improvement_ci_95"] = [0.5, 0.6]
    forged_ci = seal_probability_filter_authorization_artifact(
        forged_ci["payload"], generated_at=str(forged_ci["generated_at"]),
    )
    with pytest.raises(ProbabilityReplayError, match="原始证据"):
        verify_probability_filter_authorization_artifact(forged_ci, evidence)

    forged_drift = deepcopy(authorization_artifact)
    drift = forged_drift["payload"]["drift_validation"]
    current_series = drift["current_series"]
    current_series[0]["probability"] += 0.02
    drift["current_digest"] = stable_probability_hash(current_series)
    drift["statistics"] = probability_module._drift_statistics(
        probability_module._validated_drift_series(drift["reference_series"], "reference"),
        probability_module._validated_drift_series(current_series, "current"),
    )
    forged_drift = seal_probability_filter_authorization_artifact(
        forged_drift["payload"], generated_at=str(forged_drift["generated_at"]),
    )
    assert not probability_module._verified_drift_validation(forged_drift["payload"])

    unavailable_execution = deepcopy(authorization_artifact)
    execution = unavailable_execution["payload"]["execution_validation"]
    joint = execution["joint_execution_evidence"][0]
    unavailable_joint = build_decision_time_joint_execution_probability_evidence(
        sample_id=joint["sample_id"],
        symbol=joint["symbol"],
        signal_session=joint["signal_session"],
        generated_at=joint["generated_at"],
        evidence=joint["evidence"],
    ).model_dump(mode="json")
    execution["joint_execution_evidence"][0] = unavailable_joint
    execution["joint_execution_evidence_digest"] = stable_probability_hash(
        execution["joint_execution_evidence"],
    )
    execution["joint_execution_estimand_digest"] = stable_probability_hash(
        unavailable_joint["estimand"],
    )
    unavailable_execution = seal_probability_filter_authorization_artifact(
        unavailable_execution["payload"],
        generated_at=str(unavailable_execution["generated_at"]),
    )
    assert not probability_module._verified_execution_validation(
        unavailable_execution["payload"], future_contract,
    )

    predated = seal_probability_filter_authorization_artifact(
        authorization_artifact["payload"], generated_at="2026-08-11T07:59:00Z",
    )
    with pytest.raises(ProbabilityReplayError, match="原始证据"):
        verify_probability_filter_authorization_artifact(predated, evidence)

    tampered_evidence = deepcopy(evidence)
    tampered_evidence["limitations"].append("unsigned_mutation")
    with pytest.raises(ProbabilityReplayError, match="原始证据"):
        verify_probability_filter_authorization_artifact(
            authorization_artifact, tampered_evidence,
        )

    superseded = deepcopy(evidence)
    superseded["schema_version"] = "market-scan-shadow-probability-v3"
    assert probability_selection_qualified(superseded) is False
    assert probability_filter_qualified(superseded) is False


def test_deployment_refit_stays_unavailable_for_conditional_label_evidence() -> None:
    config = ProbabilityConfig(
        horizon=1,
        label_contract=_complete_test_label_contract(),
        minimum_train_sessions=12,
        minimum_calibration_sessions=6,
        minimum_test_sessions=30,
        minimum_bin_sessions=1,
        bootstrap_samples=100,
    )
    start = date(2026, 5, 3)
    samples = [
        replace(item, session_date=(start + timedelta(days=index // 4)).isoformat())
        for index, item in enumerate(_signal_samples(100))
    ]
    evidence = fit_shadow_probability(
        samples, config=config, generated_at="2026-08-11T08:00:00Z",
    )
    authorization_artifact = _filter_authorization(evidence)
    with pytest.raises(ProbabilityReplayError, match="原始证据"):
        verify_probability_filter_authorization_artifact(authorization_artifact, evidence)

    with pytest.raises(ProbabilityReplayError, match="strict verified authorization"):
        fit_probability_deployment_estimator(
            samples,
            evidence=evidence,
            authorization=authorization_artifact,
            generated_at="2026-08-11T09:00:00Z",
        )
    estimate = predict_shadow_probability(
        evidence,
        {"risk": -0.4, "trend": 1.0},
        sample_id="conditional-label-current",
    )
    assert estimate["deployment_status"] == "deployment_model_not_fitted"
    assert estimate["probability"] is None


def test_overlapping_horizon_uses_preregistered_circular_moving_block_ci() -> None:
    config = ProbabilityConfig(
        horizon=20,
        minimum_train_sessions=20,
        minimum_calibration_sessions=10,
        minimum_test_sessions=20,
        minimum_bin_sessions=1,
        bootstrap_samples=100,
    )
    evidence = fit_shadow_probability(
        _signal_samples(100),
        config=config,
        generated_at="2026-08-11T08:00:00Z",
    )
    metrics = evidence["calibration_metrics"]["calibrated"]
    evaluation = evidence["contract"]["evaluation"]

    assert metrics["bootstrap_method"] == "deterministic_circular_moving_target_offset_block_95pct_v2"
    assert metrics["bootstrap_block_length_sessions"] == 21
    assert evaluation["bootstrap_block_length_sessions"] == 21
    assert evaluation["bootstrap"] == metrics["bootstrap_method"]
    assert verify_shadow_probability_evidence(evidence, _signal_samples(100)) is True


def test_multifold_oos_evidence_replays_each_fold_but_forbids_future_prediction() -> None:
    samples = _signal_samples(43)
    config = _small_config()
    evidence = fit_shadow_probability(
        samples, config=config, generated_at="2026-08-11T08:00:00Z",
    )

    folds = cast(list[dict[str, Any]], evidence["folds"])
    predictions = cast(list[dict[str, Any]], evidence["predictions"])
    counts = cast(dict[str, Any], evidence["counts"])
    assert len(folds) == counts["evaluated_fold_count"] == counts["walk_forward_fold_count"] == 3
    assert counts["out_of_sample_session_count"] == 18
    assert counts["out_of_sample_observation_count"] == len(predictions) == 72
    assert counts["unused_tail_session_count"] == 3
    assert {item["fold_id"] for item in predictions} == {1, 2, 3}
    for fold in folds:
        fold_dates = set(fold["split"]["test_dates"])
        fold_predictions = [item for item in predictions if item["fold_id"] == fold["fold_id"]]
        assert {item["session_date"] for item in fold_predictions} == fold_dates
        assert all(item["reference_base_rate"] == fold["base_rate"] for item in fold_predictions)

    references = [float(item["reference_base_rate"]) for item in predictions]
    outcomes = [int(item["outcome"]) for item in predictions]
    expected_reference_brier = sum(
        (reference - outcome) ** 2
        for reference, outcome in zip(references, outcomes, strict=True)
    ) / len(predictions)
    metrics = evidence["calibration_metrics"]["calibrated"]
    assert metrics["reference_brier_score"] == pytest.approx(expected_reference_brier)
    assert metrics["reference_definition"] == "per_observation_calibration_base_rate"

    final_fold = folds[-1]
    assert evidence["model"] == final_fold["model"]
    assert evidence["calibrator"] == final_fold["calibrator"]
    features = {"risk": -0.5, "trend": 1.2}
    estimate = predict_shadow_probability(evidence, features, sample_id="future")
    assert estimate["status"] == "insufficient_data"
    assert estimate["probability"] is None
    assert estimate["deployment_status"] == "deployment_model_not_fitted"
    assert "oos_evaluation_fold_forbidden_for_new_prediction" in estimate["limitations"]
    assert verify_shadow_probability_evidence(evidence, samples) is True


def test_multifold_evidence_rejects_deleted_or_changed_fold_after_top_level_reseal() -> None:
    evidence = fit_shadow_probability(
        _signal_samples(43), config=_small_config(), generated_at="2026-08-11T08:00:00Z",
    )

    deleted = deepcopy(evidence)
    del deleted["folds"][1]
    deleted["evidence_digest"] = stable_probability_hash(
        {key: value for key, value in deleted.items() if key != "evidence_digest"},
    )
    with pytest.raises(ProbabilityReplayError, match="完成折数量"):
        verify_shadow_probability_evidence(deleted)

    changed = deepcopy(evidence)
    changed["folds"][0]["split"]["test_dates"][0] = _day(42)
    changed["evidence_digest"] = stable_probability_hash(
        {key: value for key, value in changed.items() if key != "evidence_digest"},
    )
    with pytest.raises(ProbabilityReplayError, match="fold_digest"):
        verify_shadow_probability_evidence(changed)


def test_prediction_never_uses_oos_platt_fold_as_deployment_model() -> None:
    evidence = fit_shadow_probability(
        _signal_samples(42),
        config=_small_config(),
        generated_at="2026-08-11T08:00:00Z",
    )

    positive = predict_shadow_probability(evidence, {"risk": -0.5, "trend": 1.2}, sample_id="positive")
    negative = predict_shadow_probability(evidence, {"risk": 0.5, "trend": -1.2}, sample_id="negative")

    assert positive["status"] == negative["status"] == "insufficient_data"
    assert positive["probability"] is negative["probability"] is None
    assert "confidence_interval" not in positive
    assert positive["deployment_status"] == "deployment_model_not_fitted"
    assert positive["model_version"] == PROBABILITY_MODEL_VERSION


def test_non_default_label_cost_version_is_native_to_evidence_and_full_replay() -> None:
    samples = _signal_samples(42)
    config = ProbabilityConfig(
        horizon=1,
        cost_model_version="paper-trading-cost-actual-v9",
        minimum_train_sessions=12,
        minimum_calibration_sessions=6,
        minimum_test_sessions=6,
        minimum_bin_sessions=1,
        bootstrap_samples=100,
    )

    evidence = fit_shadow_probability(
        samples, config=config, generated_at="2026-08-11T08:00:00Z",
    )

    assert evidence["cost_model_version"] == config.cost_model_version
    assert evidence["contract"]["cost"]["version"] == config.cost_model_version
    assert verify_shadow_probability_evidence(evidence, samples) is True


def test_complete_label_execution_contract_is_bound_to_model_identity() -> None:
    first_label_contract = probability_label_contract(
        ProbabilityLabelConfig(execution_notional=100_000.0, max_daily_participation_rate=0.01),
    )
    second_label_contract = probability_label_contract(
        ProbabilityLabelConfig(execution_notional=200_000.0, max_daily_participation_rate=0.005),
    )
    base = dict(
        horizon=1,
        minimum_train_sessions=12,
        minimum_calibration_sessions=6,
        minimum_test_sessions=6,
        minimum_bin_sessions=1,
        bootstrap_samples=100,
    )
    first = fit_shadow_probability(
        _signal_samples(42),
        config=ProbabilityConfig(
            **base,
            cost_model_version=str(first_label_contract["cost_model_version"]),
            label_contract=first_label_contract,
        ),
        generated_at="2026-08-11T08:00:00Z",
    )
    second = fit_shadow_probability(
        _signal_samples(42),
        config=ProbabilityConfig(
            **base,
            cost_model_version=str(second_label_contract["cost_model_version"]),
            label_contract=second_label_contract,
        ),
        generated_at="2026-08-11T08:00:00Z",
    )

    assert first["cost_model_version"] == second["cost_model_version"]
    assert first["label_contract_digest"] == stable_probability_hash(first_label_contract)
    assert second["label_contract_digest"] == stable_probability_hash(second_label_contract)
    assert first["label_contract_digest"] != second["label_contract_digest"]
    assert first["contract"]["cost"]["label_contract"] == first_label_contract
    assert first["contract"]["cost"]["label_contract_digest"] == first["label_contract_digest"]

    tampered = deepcopy(first)
    tampered["contract"]["cost"]["label_contract"]["execution_notional"] = 200_000.0
    tampered["evidence_digest"] = stable_probability_hash(
        {key: value for key, value in tampered.items() if key != "evidence_digest"},
    )
    with pytest.raises(ProbabilityReplayError, match="结构损坏|label_contract|契约"):
        verify_shadow_probability_evidence(tampered)


def test_isotonic_candidate_requires_registered_session_floor_and_never_replaces_platt() -> None:
    config = ProbabilityConfig(
        horizon=1,
        minimum_train_sessions=12,
        minimum_calibration_sessions=6,
        minimum_test_sessions=6,
        minimum_bin_sessions=1,
        minimum_isotonic_calibration_sessions=6,
        bootstrap_samples=100,
    )
    evidence = fit_shadow_probability(
        _signal_samples(42), config=config, generated_at="2026-08-11T08:00:00Z",
    )

    candidates = {item["id"]: item for item in evidence["calibration_candidates"]}
    isotonic = candidates["isotonic"]
    assert isotonic["version"] == PROBABILITY_ISOTONIC_CALIBRATOR_VERSION
    assert isotonic["status"] == "evaluated_shadow_candidate"
    assert isotonic["selected_for_display"] is False
    assert isotonic["eligibility"] == {
        "calibration_session_count": 6,
        "minimum_calibration_session_count": 6,
    }
    assert isotonic["parameters"]["algorithm"] == "weighted_pool_adjacent_violators"
    assert isotonic["metrics"] == evidence["calibration_metrics"]["isotonic_candidate"]
    estimate = predict_shadow_probability(evidence, {"risk": -0.5, "trend": 1.2})
    assert estimate["probability"] is None
    assert estimate["empirical_bayes_probability"] is None
    assert evidence["calibrator"]["version"] != evidence["isotonic_calibrator"]["version"]
    assert verify_shadow_probability_evidence(evidence, _signal_samples(42)) is True


def test_insufficient_sessions_returns_null_instead_of_placeholder_probability() -> None:
    config = ProbabilityConfig(horizon=1, bootstrap_samples=100)
    evidence = fit_shadow_probability(
        _signal_samples(20),
        config=config,
        generated_at="2026-08-11T08:00:00Z",
    )

    assert evidence["status"] == "insufficient_data"
    assert evidence["probability"] is None
    assert "minimum_independent_sessions" in evidence["limitations"]
    estimate = predict_shadow_probability(evidence, {"risk": 0.0, "trend": 0.0})
    assert estimate["status"] == "insufficient_data"
    assert estimate["probability"] is None
    assert "confidence_interval" not in estimate
    assert verify_shadow_probability_evidence(evidence) is True


def test_label_coverage_and_probability_bin_session_gates_are_enforced() -> None:
    incomplete = _signal_samples(42)
    incomplete[0] = ProbabilitySample(
        sample_id=incomplete[0].sample_id,
        session_date=incomplete[0].session_date,
        features=incomplete[0].features,
        target=None,
        executable=False,
    )
    strict_coverage = ProbabilityConfig(
        horizon=1,
        minimum_train_sessions=12,
        minimum_calibration_sessions=6,
        minimum_test_sessions=6,
        minimum_label_coverage=1.0,
        minimum_bin_sessions=1,
        bootstrap_samples=100,
    )
    coverage_evidence = fit_shadow_probability(
        incomplete,
        config=strict_coverage,
        generated_at="2026-08-11T08:00:00Z",
    )
    assert coverage_evidence["status"] == "insufficient_data"
    assert "minimum_label_coverage" in coverage_evidence["limitations"]

    strict_bins = ProbabilityConfig(
        horizon=1,
        minimum_train_sessions=12,
        minimum_calibration_sessions=6,
        minimum_test_sessions=6,
        minimum_bin_sessions=19,
        bootstrap_samples=100,
    )
    bin_evidence = fit_shadow_probability(
        _signal_samples(42),
        config=strict_bins,
        generated_at="2026-08-11T08:00:00Z",
    )
    assert bin_evidence["status"] == "insufficient_data"
    assert bin_evidence["probability"] is None
    assert "minimum_probability_bin_sessions" in bin_evidence["limitations"]


def test_non_executable_target_and_optimizer_nonconvergence_fail_closed() -> None:
    invalid = _signal_samples(1)[0]
    with pytest.raises(ValueError, match="不可执行样本的 target 必须为 None"):
        fit_shadow_probability(
            [
                ProbabilitySample(
                    sample_id=invalid.sample_id,
                    session_date=invalid.session_date,
                    features=invalid.features,
                    target=invalid.target,
                    executable=False,
                )
            ],
            config=ProbabilityConfig(horizon=1, bootstrap_samples=100),
            generated_at="2026-08-11T08:00:00Z",
        )

    config = ProbabilityConfig(
        horizon=1,
        minimum_train_sessions=12,
        minimum_calibration_sessions=6,
        minimum_test_sessions=6,
        minimum_bin_sessions=1,
        maximum_iterations=1,
        bootstrap_samples=100,
    )
    evidence = fit_shadow_probability(
        _signal_samples(42), config=config, generated_at="2026-08-11T08:00:00Z",
    )
    assert evidence["status"] == "insufficient_data"
    assert evidence["fit_status"] == "not_fitted"
    assert evidence["model"] is None
    assert evidence["selection_qualified"] is False
    assert any("nonconvergence" in value for value in evidence["limitations"])


def test_metrics_report_proper_scores_ece_auc_and_monotonic_bins() -> None:
    report = evaluate_probability_predictions(
        [0.1, 0.4, 0.6, 0.9],
        [0, 0, 1, 1],
        [_day(index) for index in range(4)],
        base_rate=0.5,
        bin_count=2,
    )

    assert report["brier_score"] == pytest.approx(0.085)
    assert report["brier_skill_score"] == pytest.approx(0.66)
    assert report["log_loss"] == pytest.approx(-math.log(0.9 * 0.6 * 0.6 * 0.9) / 4)
    assert report["ece"] == pytest.approx(0.25)
    assert report["auc"] == 1
    assert report["bin_monotonic"] is True
    assert report["highest_bin_above_base_rate"] is True


def test_empirical_bayes_bins_are_shrunk_and_deterministic() -> None:
    first = fit_empirical_bayes_baseline(
        [0.1, 0.2, 0.8, 0.9],
        [0, 0, 1, 1],
        bin_count=2,
        prior_strength=4,
    )
    second = fit_empirical_bayes_baseline(
        [0.1, 0.2, 0.8, 0.9],
        [0, 0, 1, 1],
        bin_count=2,
        prior_strength=4,
    )

    assert first == second
    probabilities = first["probabilities"]
    assert 0 < probabilities[0] < 0.5  # type: ignore[index]
    assert 0.5 < probabilities[-1] < 1  # type: ignore[index]
    assert sum(first["counts"]) == 4  # type: ignore[arg-type]


def test_evidence_hash_and_prediction_replay_detect_tampering() -> None:
    evidence = fit_shadow_probability(
        _signal_samples(42),
        config=_small_config(),
        generated_at="2026-08-11T08:00:00Z",
    )
    changed_prediction = deepcopy(evidence)
    changed_prediction["predictions"][0]["probability"] = 0.5  # type: ignore[index]
    with pytest.raises(ProbabilityReplayError, match="evidence_digest"):
        verify_shadow_probability_evidence(changed_prediction)

    changed_model = deepcopy(evidence)
    changed_model["model"]["intercept"] += 0.1  # type: ignore[index,operator]
    unsigned = {key: value for key, value in changed_model.items() if key != "evidence_digest"}
    changed_model["evidence_digest"] = stable_probability_hash(unsigned)
    with pytest.raises(ProbabilityReplayError, match="model_digest"):
        verify_shadow_probability_evidence(changed_model)

    changed_metrics = deepcopy(evidence)
    changed_metrics["calibration_metrics"]["calibrated"]["brier_score"] = 0.99  # type: ignore[index]
    unsigned_metrics = {key: value for key, value in changed_metrics.items() if key != "evidence_digest"}
    changed_metrics["evidence_digest"] = stable_probability_hash(unsigned_metrics)
    with pytest.raises(ProbabilityReplayError, match="校准指标"):
        verify_shadow_probability_evidence(changed_metrics)

    changed_candidate = deepcopy(evidence)
    changed_candidate["calibration_candidates"][0]["selected_for_display"] = False
    unsigned_candidate = {key: value for key, value in changed_candidate.items() if key != "evidence_digest"}
    changed_candidate["evidence_digest"] = stable_probability_hash(unsigned_candidate)
    with pytest.raises(ProbabilityReplayError, match="候选参数或指标"):
        verify_shadow_probability_evidence(changed_candidate)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_features_are_rejected(bad_value: float) -> None:
    sample = ProbabilitySample("bad", _day(0), {"trend": bad_value}, 1)

    with pytest.raises(ValueError, match="有限数值"):
        fit_shadow_probability([sample], config=_small_config(), generated_at="2026-08-11T08:00:00Z")
    with pytest.raises(ValueError, match="非有限"):
        stable_probability_hash({"value": bad_value})


def test_post_hoc_and_inconsistent_feature_sets_are_rejected() -> None:
    for feature_name in (
        "symbol", "future_return", "forward_close", "next_open", "realized_pnl", "observed_label",
    ):
        with pytest.raises(ValueError, match="禁止"):
            fit_shadow_probability(
                [ProbabilitySample("bad", _day(0), {feature_name: 1.0}, 1)],
                config=_small_config(),
                generated_at="2026-08-11T08:00:00Z",
            )
    with pytest.raises(ValueError, match="相同"):
        fit_shadow_probability(
            [
                ProbabilitySample("one", _day(0), {"trend": 1.0}, 1),
                ProbabilitySample("two", _day(1), {"risk": 1.0}, 0),
            ],
            config=_small_config(),
            generated_at="2026-08-11T08:00:00Z",
        )
    legitimate = fit_shadow_probability(
        [ProbabilitySample("valid", _day(0), {"return_1d_pct": 1.0, "rank_refinement": 0.5}, 1)],
        config=_small_config(),
        generated_at="2026-08-11T08:00:00Z",
    )
    assert legitimate["status"] == "insufficient_data"


def test_research_persists_every_run_symbol_target_horizon_as_null_when_insufficient(tmp_path) -> None:
    features = probability_feature_vector(
        {"raw_score": 80.0, "trend_score": 75.0, "amount": 1_000_000.0},
        market="SH", board="SH_MAIN", liquidity="high", regime="strong",
    )
    rows = [
        ProbabilityResearchRow(
            run_id=29,
            symbol=symbol,
            session_date="2026-01-05",
            features=features,
            labels={1: ProbabilityLabelOutcome(1, "modelled", "target_close", net_return=net_return)},
            mature_horizons=frozenset({1}),
            dimensions={"market": "SH", "board": "SH_MAIN", "industry": "测试", "liquidity": "high", "regime": "strong"},
            source_evidence_digest="a" * 64,
            source_integrity_digest="c" * 64,
        )
        for symbol, net_return in (("600001.SH", 0.02), ("600002.SH", -0.01))
    ]

    label_contract = probability_label_contract(ProbabilityLabelConfig(cost_profile="stress"))
    research = build_probability_research(
        rows,
        generated_at="2026-08-11T08:00:00Z",
        bootstrap_samples=100,
        label_contract=label_contract,
    )

    assert research["status"] == "insufficient_data"
    assert research["label_contract"] == label_contract
    assert research["record_count"] == 12
    one_day_summary = research["horizons"]["1"]["net_excess_positive"]
    assert one_day_summary["cost_model_version"] == label_contract["cost_model_version"]
    assert one_day_summary["contract"]["cost"]["version"] == label_contract["cost_model_version"]
    assert one_day_summary["deterministic_replay_verified"] is True
    records = cast(list[dict[str, Any]], research["records"])
    assert all(item["probability"] is None and item["status"] == "insufficient_data" for item in records)
    one_day = [item for item in records if item["horizon"] == 1 and item["target"] == "net_excess_positive"]
    assert {item["observed_label"] for item in one_day} == {0, 1}
    by_symbol = {item["symbol"]: item for item in one_day}
    assert by_symbol["600001.SH"]["market_benchmark_net_return"] == pytest.approx(0.005)
    assert by_symbol["600002.SH"]["market_benchmark_net_return"] == pytest.approx(0.005)
    assert by_symbol["600001.SH"]["net_excess_return"] == pytest.approx(0.015)
    assert by_symbol["600002.SH"]["net_excess_return"] == pytest.approx(-0.015)
    canonical = [
        item for item in records
        if item["horizon"] == 1 and item["target"] == "net_excess_positive"
    ]
    assert all("feature_values" in item and "dimensions" in item for item in canonical)
    assert all(
        item.get("feature_evidence_reference") == "1/net_excess_positive"
        for item in records
        if item not in canonical
    )
    artifact = build_probability_artifact(
        probability_artifact_payload(research),
        generated_at="2026-08-11T08:00:00Z",
    )
    studies = cast(list[dict[str, Any]], artifact["payload"]["studies"])  # type: ignore[index]
    persisted_records = cast(list[dict[str, Any]], artifact["payload"]["records"])  # type: ignore[index]
    assert {item["versions"]["cost_model"] for item in studies} == {label_contract["cost_model_version"]}
    expected_label_contract_digest = stable_probability_hash(label_contract)
    assert {item["digests"]["label_contract"] for item in studies} == {expected_label_contract_digest}
    assert {
        item["metadata"]["label_contract_digest"] for item in studies
    } == {expected_label_contract_digest}
    assert {item["metadata"]["source_integrity_digest"] for item in studies} == {"c" * 64}
    feature_evidence = cast(list[dict[str, Any]], artifact["payload"]["feature_evidence"])  # type: ignore[index]
    assert len(feature_evidence) == len(rows)
    assert all("features" in item and "dimensions" in item for item in feature_evidence)
    assert all("feature_evidence_key" in item["details"] and "dimensions" in item["details"] for item in persisted_records)
    assert all("feature_evidence_reference" not in item["details"] for item in persisted_records)
    assert all(item["details"]["versions"] == studies[0]["versions"] for item in persisted_records)
    assert all("digests" in item["details"] and "base_rate" in item["details"] for item in persisted_records)
    database = tmp_path / "ashare.sqlite3"
    _ensure_probability_artifact_database(database, run_ids=(29,))
    target = tmp_path / "market-scan-probability" / f"market-scan-probability-{artifact['integrity']['integrity_digest']}.json"  # type: ignore[index]
    database_before_write = database.read_bytes()
    write_probability_artifact(target, artifact, database_path=database)
    assert database.read_bytes() == database_before_write
    summary, projected = MarketScanProbabilityStore(target.parent).run_projection(29)
    assert summary["status"] == "insufficient_data"
    assert projected["600001.SH"]["1"]["net_excess_positive"]["probability"] is None  # type: ignore[index]
    assert database.read_bytes() == database_before_write


def test_research_isolates_mode_scope_and_rule_cohorts_without_borrowing_dates() -> None:
    features = probability_feature_vector(
        {"raw_score": 70.0},
        market="SH",
        board="SH_MAIN",
        liquidity="high",
        regime="neutral",
    )
    contracts = (
        ("official", "full-market", "rule-v1", (0, 1)),
        ("intraday", "top100-refresh", "rule-v2", (1, 2)),
    )
    rows = [
        ProbabilityResearchRow(
            run_id=100 + cohort_index * 10 + date_index,
            symbol=f"600{cohort_index}{date_index:02d}.SH",
            session_date=_day(day_index),
            features=features,
            labels={
                1: ProbabilityLabelOutcome(
                    1,
                    "modelled",
                    "target_close",
                    net_return=0.01 if date_index % 2 == 0 else -0.01,
                )
            },
            mature_horizons=frozenset({1}),
            dimensions={"market": "SH", "board": "SH_MAIN"},
            source_evidence_digest=f"{cohort_index + 1}" * 64,
            mode=mode,
            scope=scope,
            rule_version=rule_version,
        )
        for cohort_index, (mode, scope, rule_version, day_indexes) in enumerate(contracts)
        for date_index, day_index in enumerate(day_indexes)
    ]

    research = build_probability_research(
        rows,
        generated_at="2026-08-11T08:00:00Z",
        bootstrap_samples=100,
    )
    cohorts = cast(list[dict[str, Any]], research["cohorts"])

    assert research["cohort_count"] == 2
    assert research["horizons"]["1"]["net_excess_positive"]["cohort_count"] == 2
    assert [
        cohort["horizons"]["1"]["net_excess_positive"]["counts"][
            "available_independent_session_count"
        ]
        for cohort in cohorts
    ] == [2, 2]
    assert len({
        cohort["horizons"]["1"]["net_excess_positive"]["input_digest"]
        for cohort in cohorts
    }) == 2
    assert all(item["probability"] is None for item in research["records"])
    artifact_payload = probability_artifact_payload(research)
    studies = cast(list[dict[str, Any]], artifact_payload["studies"])
    assert {
        tuple(sorted(item["metadata"]["cohort_contract"].items()))
        for item in studies
    } == {
        tuple(sorted({"mode": mode, "scope": scope, "rule_version": rule}.items()))
        for mode, scope, rule, _dates in contracts
    }


def test_research_rejects_two_runs_for_one_cohort_session() -> None:
    features = probability_feature_vector(
        {}, market="SH", board="SH_MAIN", liquidity="high", regime="neutral",
    )
    rows = [
        ProbabilityResearchRow(
            run_id=run_id,
            symbol=f"60000{run_id}.SH",
            session_date="2026-01-05",
            features=features,
            labels={},
            mature_horizons=frozenset(),
            dimensions={},
            mode="official",
            scope="full-market",
            rule_version="rule-v1",
        )
        for run_id in (1, 2)
    ]

    with pytest.raises(ValueError, match="multiple runs for one session date"):
        build_probability_research(
            rows,
            generated_at="2026-08-11T08:00:00Z",
            bootstrap_samples=100,
        )


def test_artifact_digest_registry_keeps_fitted_evidence_for_non_oos_runs() -> None:
    evidence = {
        "input_digest": "a" * 64,
        "model": {"version": PROBABILITY_MODEL_VERSION},
        "model_digest": "b" * 64,
        "calibrator_digest": "c" * 64,
        "isotonic_calibrator_digest": "d" * 64,
        "baseline_digest": "e" * 64,
        "evidence_digest": "f" * 64,
    }

    assert probability_research_module._artifact_digests(evidence) == {
        "input": "a" * 64,
        "model": "b" * 64,
        "calibrator": "c" * 64,
        "isotonic_calibrator": "d" * 64,
        "baseline": "e" * 64,
        "evidence": "f" * 64,
    }


def test_probability_store_cache_hits_without_sharing_mutable_projection(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, database, _target = _write_store_artifact(
        tmp_path,
        filename="market-scan-probability-cache.json",
        generated_at="2026-08-11T08:00:00Z",
        status="insufficient_data",
    )
    calls = _count_store_loads(monkeypatch)
    store = MarketScanProbabilityStore(directory)
    database_before_read = database.read_bytes()

    first_summary, first_records = store.run_projection(29)
    first_summary["status"] = "caller-mutated"
    first_records.clear()
    second_summary, second_records = store.run_projection(29)
    _selected_summary, selected_records = store.run_projection(
        29,
        symbols=("not-present.SH",),
    )

    assert calls["count"] == 1
    assert second_summary["status"] == "insufficient_data"
    assert second_records["600519.SH"]["5"]["net_excess_positive"]["probability"] is None
    assert selected_records == {}
    assert database.read_bytes() == database_before_read


def test_probability_store_rejects_symlink_root_and_keeps_missing_root_empty(tmp_path) -> None:
    directory, _database, _target = _write_store_artifact(
        tmp_path,
        filename="market-scan-probability-root.json",
        generated_at="2026-08-11T08:00:00Z",
        status="insufficient_data",
    )
    alias = tmp_path / "probability-root-alias"
    alias.symlink_to(directory, target_is_directory=True)

    with pytest.raises(ProbabilityArtifactError, match="不是普通目录"):
        MarketScanProbabilityStore(alias).research_projection(29)

    loop = tmp_path / "probability-root-loop"
    loop.symlink_to(loop, target_is_directory=True)
    with pytest.raises(ProbabilityArtifactError, match="目录无法读取"):
        MarketScanProbabilityStore(loop / "nested").research_projection(29)

    assert MarketScanProbabilityStore(directory).research_projection(29)["status"] == "insufficient_data"
    missing = MarketScanProbabilityStore(tmp_path / "missing-probability-root")
    assert missing.research_projection(29)["status"] == "not_generated"

    plain_file = tmp_path / "probability-root-file"
    plain_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ProbabilityArtifactError, match="不是普通目录"):
        MarketScanProbabilityStore(plain_file).research_projection(29)


def test_probability_store_rejects_non_regular_artifact_entry(tmp_path) -> None:
    directory, _database, target = _write_store_artifact(
        tmp_path,
        filename="market-scan-probability-regular.json",
        generated_at="2026-08-11T08:00:00Z",
        status="insufficient_data",
    )
    linked = directory / "market-scan-probability-linked.json"
    linked.symlink_to(target)

    with pytest.raises(ProbabilityArtifactError, match="不是普通文件"):
        MarketScanProbabilityStore(directory).research_projection(29)


@pytest.mark.parametrize("failure", ("glob", "artifact_lstat"))
def test_probability_store_fails_closed_when_directory_cannot_be_fully_scanned(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    directory, _database, target = _write_store_artifact(
        tmp_path,
        filename="market-scan-probability-io-error.json",
        generated_at="2026-08-11T08:00:00Z",
        status="insufficient_data",
    )
    if failure == "glob":
        original_glob = Path.glob

        def blocked_glob(path: Path, pattern: str):
            if path == directory:
                raise OSError("simulated complete-scan failure")
            return original_glob(path, pattern)

        monkeypatch.setattr(Path, "glob", blocked_glob)
        message = "无法完整扫描"
    else:
        original_lstat = Path.lstat

        def blocked_lstat(path: Path):
            if path == target:
                raise OSError("simulated artifact stat failure")
            return original_lstat(path)

        monkeypatch.setattr(Path, "lstat", blocked_lstat)
        message = "artifact 无法读取"

    with pytest.raises(ProbabilityArtifactError, match=message):
        MarketScanProbabilityStore(directory).research_projection(29)


def test_probability_store_rejects_artifact_changed_after_deep_verification(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, _database, target = _write_store_artifact(
        tmp_path,
        filename="market-scan-probability-toctou.json",
        generated_at="2026-08-11T08:00:00Z",
        status="insufficient_data",
    )
    original = probability_store_module.load_probability_artifact

    def mutating_load(path: Path):
        artifact = original(path)
        path.write_bytes(path.read_bytes() + b"\n")
        return artifact

    monkeypatch.setattr(probability_store_module, "load_probability_artifact", mutating_load)

    with pytest.raises(ProbabilityArtifactError, match="校验期间发生变化"):
        MarketScanProbabilityStore(directory).research_projection(29)

    assert target.read_bytes().endswith(b"\n")


def test_probability_store_invalidates_changed_file_and_fails_closed_on_corruption(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, database, target = _write_store_artifact(
        tmp_path,
        filename="market-scan-probability-current.json",
        generated_at="2026-08-11T08:00:00Z",
        status="insufficient_data",
    )
    calls = _count_store_loads(monkeypatch)
    store = MarketScanProbabilityStore(directory)
    database_before_read = database.read_bytes()
    assert store.run_projection(29)[0]["status"] == "insufficient_data"

    _write_store_artifact(
        tmp_path,
        filename=target.name,
        generated_at="2026-08-11T09:00:00Z",
        status="calibrated_shadow",
        probability=0.64,
    )
    summary, records = store.run_projection(29)
    assert summary["status"] == "calibrated_shadow"
    assert records["600519.SH"]["5"]["net_excess_positive"]["probability"] == 0.64
    assert calls["count"] == 2

    target.write_text("{}", encoding="utf-8")
    with pytest.raises(ProbabilityArtifactError):
        store.run_projection(29)
    assert calls["count"] == 3
    assert database.read_bytes() == database_before_read


def test_probability_store_directory_changes_reuse_unchanged_files_and_survive_restart(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, _database, _older = _write_store_artifact(
        tmp_path,
        filename="market-scan-probability-older.json",
        generated_at="2026-08-11T08:00:00Z",
        status="insufficient_data",
    )
    calls = _count_store_loads(monkeypatch)
    store = MarketScanProbabilityStore(directory)
    assert store.run_projection(29)[0]["status"] == "insufficient_data"

    _directory, _database, newer = _write_store_artifact(
        tmp_path,
        filename="market-scan-probability-newer.json",
        generated_at="2026-08-11T09:00:00Z",
        status="calibrated_shadow",
        probability=0.67,
    )
    assert store.run_projection(29)[0]["status"] == "calibrated_shadow"
    assert calls["count"] == 2  # The unchanged older artifact was not reparsed.

    newer.unlink()
    assert store.run_projection(29)[0]["status"] == "insufficient_data"
    assert calls["count"] == 2

    restarted = MarketScanProbabilityStore(directory)
    restarted_summary, restarted_records = restarted.run_projection(29)
    assert restarted_summary["status"] == "insufficient_data"
    assert restarted_records["600519.SH"]["5"]["net_excess_positive"]["status"] == "insufficient_data"
    assert calls["count"] == 3


def test_probability_store_lazily_loads_only_the_requested_run_artifact(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest29, digest30 = "a" * 64, "b" * 64
    directory, _database, _first = _write_store_artifact(
        tmp_path,
        filename=f"market-scan-probability-run-29-{digest29}.json",
        generated_at="2026-08-11T08:00:00Z",
        status="insufficient_data",
        run_id=29,
    )
    _write_store_artifact(
        tmp_path,
        filename=f"market-scan-probability-run-30-{digest30}.json",
        generated_at="2026-08-11T08:00:00Z",
        status="insufficient_data",
        run_id=30,
    )
    calls = _count_store_loads(monkeypatch)
    store = MarketScanProbabilityStore(directory)

    assert store.research_projection(29)["run_id"] == 29
    assert calls["count"] == 1
    assert store.research_projection(29)["run_id"] == 29
    assert calls["count"] == 1
    assert store.research_projection(30)["run_id"] == 30
    assert calls["count"] == 2


def test_probability_store_rejects_oversized_legacy_before_interactive_deep_read(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = "a" * 64
    directory, _database, artifact = _write_store_artifact(
        tmp_path,
        filename=f"market-scan-probability-run-29-{digest}.json",
        generated_at="2026-08-11T08:00:00Z",
        status="calibrated_shadow",
        probability=0.64,
        run_id=29,
    )
    monkeypatch.setattr(
        probability_store_module,
        "PROBABILITY_INTERACTIVE_ARTIFACT_MAX_BYTES",
        artifact.stat().st_size - 1,
    )

    def forbidden_deep_read(_path):
        raise AssertionError("oversized interactive artifact must not be parsed")

    monkeypatch.setattr(
        probability_store_module,
        "load_probability_artifact",
        forbidden_deep_read,
    )
    started = perf_counter()
    research, records = MarketScanProbabilityStore(directory).run_projection(29)
    elapsed = perf_counter() - started

    assert elapsed < 1.0
    assert research["status"] == "not_generated"
    assert research["availability"] == "legacy_artifact_exceeds_interactive_budget"
    assert records == {}


def test_probability_store_keeps_warm_snapshot_readable_during_cold_verification(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest29, digest30 = "a" * 64, "b" * 64
    directory, _database, _first = _write_store_artifact(
        tmp_path,
        filename=f"market-scan-probability-run-29-{digest29}.json",
        generated_at="2026-08-11T08:00:00Z",
        status="insufficient_data",
        run_id=29,
    )
    store = MarketScanProbabilityStore(directory)
    assert store.research_projection(29)["status"] == "insufficient_data"
    _directory, _database, cold_path = _write_store_artifact(
        tmp_path,
        filename=f"market-scan-probability-run-30-{digest30}.json",
        generated_at="2026-08-11T09:00:00Z",
        status="insufficient_data",
        run_id=30,
    )
    original = probability_store_module.load_probability_artifact
    cold_entered, allow_cold, warm_done = Event(), Event(), Event()
    failures: list[BaseException] = []

    def blocked_load(path):
        if path == cold_path:
            cold_entered.set()
            assert allow_cold.wait(timeout=2)
        return original(path)

    def project(run_id: int, done: Event | None = None) -> None:
        try:
            store.research_projection(run_id)
        except BaseException as exc:
            failures.append(exc)
        finally:
            if done is not None:
                done.set()

    monkeypatch.setattr(probability_store_module, "load_probability_artifact", blocked_load)
    cold = Thread(target=project, args=(30,), daemon=True)
    cold.start()
    assert cold_entered.wait(timeout=2)
    warm = Thread(target=project, args=(29, warm_done), daemon=True)
    warm.start()
    warm_returned_before_cold_publish = warm_done.wait(timeout=0.5)
    allow_cold.set()
    cold.join(timeout=2)
    warm.join(timeout=2)

    assert warm_returned_before_cold_publish
    assert failures == []
    assert not cold.is_alive()


def test_probability_store_serializes_two_cold_readers_and_publishes_once(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = "a" * 64
    directory, _database, target = _write_store_artifact(
        tmp_path,
        filename=f"market-scan-probability-run-29-{digest}.json",
        generated_at="2026-08-11T08:00:00Z",
        status="insufficient_data",
        run_id=29,
    )
    store = MarketScanProbabilityStore(directory)
    original_load = probability_store_module.load_probability_artifact
    entered, release, blocking_wait = Event(), Event(), Event()
    loads: list[Path] = []
    failures: list[BaseException] = []
    statuses: list[object] = []

    class TrackingLock:
        def __init__(self) -> None:
            self.inner = RLock()

        def acquire(self, blocking: bool = True) -> bool:
            if blocking:
                blocking_wait.set()
            return self.inner.acquire(blocking=blocking)

        def release(self) -> None:
            self.inner.release()

    store._refresh_lock = TrackingLock()  # type: ignore[assignment]  # noqa: SLF001

    def blocked_load(path: Path):
        loads.append(path)
        if path == target:
            entered.set()
            assert release.wait(timeout=2)
        return original_load(path)

    def project() -> None:
        try:
            statuses.append(store.research_projection(29)["status"])
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    monkeypatch.setattr(probability_store_module, "load_probability_artifact", blocked_load)
    first = Thread(target=project, daemon=True)
    second = Thread(target=project, daemon=True)
    first.start()
    assert entered.wait(timeout=2)
    second.start()
    assert blocking_wait.wait(timeout=2)
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert failures == []
    assert statuses == ["insufficient_data", "insufficient_data"]
    assert loads == [target]
    assert not first.is_alive() and not second.is_alive()


def test_probability_store_fails_closed_after_three_directory_snapshot_races(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MarketScanProbabilityStore(tmp_path / "probability")
    calls = 0

    def changing_snapshot(_directory: Path):
        nonlocal calls
        calls += 1
        return (calls, calls, calls, calls), ()

    monkeypatch.setattr(probability_store_module, "_directory_snapshot", changing_snapshot)

    with pytest.raises(ProbabilityArtifactError, match="读取期间发生变化"):
        store.research_projection(29)

    assert calls == 7


@pytest.mark.parametrize(
    ("generated_at", "message"),
    (
        ("not-a-date", "generated_at 无效"),
        ("2026-08-11T10:00:00", "必须包含时区"),
    ),
)
def test_probability_store_generated_at_order_requires_aware_iso_timestamp(
    generated_at: str,
    message: str,
) -> None:
    with pytest.raises(ProbabilityArtifactError, match=message):
        probability_store_module._generated_at_order({"generated_at": generated_at})  # noqa: SLF001


def test_probability_store_never_uses_unsealed_legacy_time_as_latest() -> None:
    sealed = {
        "generated_at": "2026-08-11T08:00:00Z",
        "integrity": {"scope": "generated_at+payload"},
        "payload": {"studies": [{"run_id": 29}]},
    }
    later_legacy = {
        "generated_at": "2026-08-12T08:00:00Z",
        "integrity": {"scope": "payload"},
        "payload": {"studies": [{"run_id": 29}]},
    }

    selected = probability_store_module._newest_artifact_for_run(  # noqa: SLF001
        [(cast(Any, None), later_legacy), (cast(Any, None), sealed)], 29,
    )

    assert selected is sealed
    with pytest.raises(ProbabilityArtifactError, match="不能信任未封印 generated_at"):
        probability_store_module._newest_artifact_for_run(  # noqa: SLF001
            [(cast(Any, None), later_legacy), (cast(Any, None), deepcopy(later_legacy))],
            29,
        )


def test_probability_store_legacy_projection_joins_study_evidence_without_fabrication() -> None:
    records = [
        {
            "run_id": 29,
            "symbol": "600519.SH",
            "target": "net_excess_positive",
            "horizon": 5,
            "status": "insufficient_data",
            "probability": None,
            "confidence_interval": None,
            "details": {"limitations": ["record-local"]},
        }
    ]
    studies = [
        {
            "run_id": 29,
            "target": "net_excess_positive",
            "horizon": 5,
            "metadata": {
                "base_rate": 0.51,
                "training_cutoff": "2026-08-08",
                "target_definition": "future_5d_net_excess_return_gt_0_after_costs",
                "generated_at": "2026-08-11T08:00:00Z",
            },
            "limitations": ["study-shared", "record-local"],
        }
    ]

    joined = probability_store_module._record_projection(  # noqa: SLF001
        records,
        studies,
        merge_legacy_study=True,
    )
    without_study = probability_store_module._record_projection(  # noqa: SLF001
        records,
        [],
        merge_legacy_study=True,
    )
    details = joined["600519.SH"]["5"]["net_excess_positive"]
    unbound = without_study["600519.SH"]["5"]["net_excess_positive"]

    assert details["base_rate"] == pytest.approx(0.51)
    assert details["training_cutoff"] == "2026-08-08"
    assert details["limitations"] == [
        "study-shared",
        "record-local",
        "legacy_record_requires_study_join",
    ]
    assert details["calibration_bias_interval"] is None
    assert "base_rate" not in unbound
    assert probability_store_module._artifact_run_quote_dates(  # noqa: SLF001
        {"records": [{"run_id": 29, "details": {"quote_date": "2026-08-11"}}]},
        29,
    ) == {"2026-08-11"}


def test_probability_store_orders_aware_generated_instants_and_rejects_ties(tmp_path) -> None:
    directory, _database, _first = _write_store_artifact(
        tmp_path,
        filename="market-scan-probability-offset-older.json",
        generated_at="2026-08-11T17:00:00+08:00",
        status="insufficient_data",
    )
    _write_store_artifact(
        tmp_path,
        filename="market-scan-probability-offset-newer.json",
        generated_at="2026-08-11T10:00:00+00:00",
        status="calibrated_shadow",
        probability=0.66,
    )
    summary, records = MarketScanProbabilityStore(directory).run_projection(29)
    assert summary["status"] == "calibrated_shadow"
    binding = cast(dict[str, object], summary["run_binding"])
    assert binding["binding_status"] == "verified"
    assert binding["scan_rule_hash"] == "a" * 64
    assert binding["production_score_rule_version"] == "full-market-score-v4"
    assert binding["production_score_spec_hash"] == "b" * 64
    assert binding["source_integrity_digest"] == "c" * 64
    record = records["600519.SH"]["5"]["net_excess_positive"]
    assert record["probability"] == 0.66
    assert record["holding_period_sessions"] == 5
    assert record["target_session_offset"] == 6
    assert "confidence_interval" not in record
    assert record["calibration_bias_interval"] == {
        "level": 0.95,
        "lower": pytest.approx(-0.05),
        "upper": pytest.approx(0.05),
        "method": "date_block_bootstrap_signed_calibration_bias",
        "semantics": "signed_observed_rate_minus_probability_bias",
    }
    assert record["calibration_adjusted_probability_interval"] == {
        "level": 0.95,
        "lower": pytest.approx(0.61),
        "upper": pytest.approx(0.71),
        "method": "date_block_bootstrap_calibration_offset",
        "semantics": "calibration_adjusted_probability_interval_not_individual_outcome_interval",
    }

    _write_store_artifact(
        tmp_path,
        filename="market-scan-probability-offset-tie.json",
        generated_at="2026-08-11T18:00:00+08:00",
        status="insufficient_data",
    )
    with pytest.raises(ProbabilityArtifactError, match="同 generated_at"):
        MarketScanProbabilityStore(directory).research_projection(29)


def test_probability_feature_vector_has_fixed_finite_registered_shape() -> None:
    sparse = probability_feature_vector({}, market="BJ", board="BSE", liquidity="low", regime="weak")
    complete = probability_feature_vector(
        {
            "raw_score": 90,
            "leader_base": 50,
            "leader_trend_delta": 8,
            "leader_unclamped": 58,
            "leader_score": 58,
            "final_base": 58,
            "final_quality_penalty": 3,
            "final_rank_discount": 0.02,
            "final_raw": 54.98,
            "final_rounded": 55,
            "final_score": 55,
            "rank_refinement": 0.8,
            "alpha_5d": 4,
            "risk": 20,
            "amount": 5_000_000,
            "feature_return_1d_pct": 19,
            "feature_atr20_pct": 4.2,
            "feature_downside_volatility_20d_pct": 2.1,
            "feature_max_drawdown_60d_pct": -15,
        },
        market="SZ", board="CHINEXT", liquidity="high", regime="strong",
        industry="信息技术", segment="new", market_strength=64,
        board_relative_strength=3, industry_relative_strength=1.5,
    )
    lower_limit = probability_feature_vector(
        {"feature_return_1d_pct": -18},
        market="SZ", board="CHINEXT", liquidity="high", regime="weak",
    )
    intraday_limit = probability_feature_vector(
        {"change_pct": 5, "feature_return_1d_pct": 19},
        market="SZ", board="CHINEXT", liquidity="high", regime="strong",
    )
    current_medium = probability_feature_vector(
        {}, market="SH", board="SH_MAIN", liquidity="medium", regime="neutral",
    )
    legacy_medium = probability_feature_vector(
        {},
        market="SH",
        board="SH_MAIN",
        liquidity="medium",
        regime="neutral",
        feature_version=LEGACY_PROBABILITY_FEATURE_VERSION,
    )

    assert tuple(sorted(sparse)) == tuple(sorted(complete))
    assert all(math.isfinite(value) for value in sparse.values())
    assert sparse["market_bj"] == 1
    assert sparse["board_bse"] == 1
    assert current_medium["liquidity_mid"] == 1
    assert legacy_medium["liquidity_mid"] == 0
    assert PROBABILITY_FEATURE_VERSION != LEGACY_PROBABILITY_FEATURE_VERSION
    assert complete["leader_base"] == 50
    assert complete["leader_trend_delta"] == 8
    assert complete["leader_unclamped"] == 58
    assert complete["leader_score"] == 58
    assert complete["final_base"] == 58
    assert complete["quality_penalty"] == 3
    assert complete["final_rank_discount"] == pytest.approx(0.02)
    assert complete["final_raw"] == pytest.approx(54.98)
    assert complete["final_rounded"] == 55
    assert complete["final_score"] == 55
    assert complete["atr20_pct"] == pytest.approx(4.2)
    assert complete["downside_volatility_20d_pct"] == pytest.approx(2.1)
    assert complete["max_drawdown_60d_pct"] == pytest.approx(-15)
    assert complete["market_strength"] == 64
    assert complete["board_relative_strength"] == 3
    assert complete["industry_relative_strength"] == 1.5
    assert complete["is_new"] == 1
    assert complete["upper_price_limit_proximity"] == pytest.approx(0.95)
    assert complete["lower_price_limit_proximity"] == 0
    assert complete["price_limit_profile_verified"] == 0
    assert complete["price_limit_profile_uncertain"] == 1
    assert lower_limit["upper_price_limit_proximity"] == 0
    assert lower_limit["lower_price_limit_proximity"] == pytest.approx(0.9)
    assert intraday_limit["upper_price_limit_proximity"] == pytest.approx(0.25)
    assert intraday_limit["lower_price_limit_proximity"] == 0
    assert sum(complete[f"industry_bucket_{index:02d}"] for index in range(16)) == 1


def test_top100_probability_outcomes_exclude_lower_ranked_large_loser() -> None:
    predictions = [
        {
            "sample_id": f"29:{index:06d}.SH:5:net_excess_positive",
            "session_date": _day(day),
            "probability": 0.99 - index / 1_000,
            "net_return": 0.01,
            "net_excess_return": 0.005,
        }
        for day in range(2)
        for index in range(100)
    ]
    predictions.extend(
        {
            "sample_id": "29:999999.SH:5:net_excess_positive",
            "session_date": _day(day),
            "probability": 0.01,
            "net_return": -0.99,
            "net_excess_return": -0.99,
        }
        for day in range(2)
    )

    metrics = probability_research_module._portfolio_outcome_metrics({"predictions": predictions})

    assert metrics["observation_count"] == 200
    assert metrics["independent_session_count"] == 2
    assert metrics["mean_net_return"] == pytest.approx(0.01)
    assert metrics["mean_net_excess_return"] == pytest.approx(0.005)
    assert metrics["mean_top100_turnover"] == 0


def test_probability_bins_report_cost_aware_outcomes_and_independent_dates() -> None:
    predictions = [
        {
            "sample_id": f"29:{symbol}:5:net_excess_positive",
            "session_date": _day(day),
            "probability": probability,
            "outcome": outcome,
            "net_return": net_return,
            "net_excess_return": net_excess,
        }
        for day in range(40)
        for symbol, probability, outcome, net_return, net_excess in (
            ("LOW.SH", 0.1, 0, -0.01, -0.015),
            ("HIGH.SH", 0.9, 1, 0.02, 0.015),
        )
    ]
    evidence = {
        "predictions": predictions,
        "calibration_metrics": {
            "calibrated": {
                "calibration_bins": [
                    {"lower": 0.0, "upper": 0.2},
                    {"lower": 0.8, "upper": 1.0},
                ]
            }
        },
    }

    outcomes = probability_research_module._probability_bin_outcome_metrics(evidence)

    assert [item["independent_session_count"] for item in outcomes] == [40, 40]
    assert all(item["status"] == "ok" for item in outcomes)
    assert outcomes[0]["mean_net_excess_return"] == pytest.approx(-0.015)
    assert outcomes[1]["mean_net_excess_return"] == pytest.approx(0.015)
    assert outcomes[1]["mean_turnover"] == 0
    assert outcomes[1]["maximum_net_excess_drawdown"] == 0


def test_stability_major_strata_and_replay_are_explicit_promotion_gates() -> None:
    predictions = [
        {
            "sample_id": f"29:{symbol}:5:net_excess_positive",
            "session_date": _day(day),
            "probability": probability,
            "outcome": outcome,
            "net_return": 0.01 if outcome else -0.01,
            "net_excess_return": 0.005 if outcome else -0.005,
        }
        for day in range(40)
        for symbol, probability, outcome in (("LOW.SH", 0.02, 0), ("HIGH.SH", 0.98, 1))
    ]
    stability = probability_research_module._temporal_stability_metrics(
        {"predictions": predictions, "base_rate": 0.5},
    )
    stratified = {
        "market": [
            {"value": "SH", "observation_count": 80, "status": "ok", "brier_skill_score": 0.2, "ece": 0.03},
            {"value": "BJ", "observation_count": 5, "status": "insufficient_data", "brier_skill_score": -1, "ece": 1},
        ],
        "board": [
            {"value": "SH_MAIN", "observation_count": 80, "status": "ok", "brier_skill_score": 0.2, "ece": 0.03},
        ],
    }
    major = probability_research_module._major_strata_calibration_summary(stratified)
    evidence = {
        "status": "calibrated_shadow",
        "selection_qualified": True,
        "counts": {"label_coverage": 1.0},
        "calibration_metrics": {
            "calibrated": {
                "brier_skill_score": 0.2,
                "ece": 0.03,
                "auc": 0.7,
                "bin_monotonic": True,
                "highest_bin_above_base_rate": True,
            }
        },
    }
    outcomes = {
        "independent_session_count": 60,
        "mean_net_excess_return": 0.01,
        "maximum_drawdown": -0.10,
        "mean_top100_turnover": 0.50,
    }
    bins = [{"independent_session_count": 20}]

    gates = probability_research_module._promotion_gates(
        evidence, outcomes, {"coverage": 1.0}, bins, major, stability, True,
    )
    failed_replay = probability_research_module._promotion_gates(
        evidence, outcomes, {"coverage": 1.0}, bins, major, stability, False,
    )

    assert stability["passed"] is True
    assert major["market"]["passed"] is True
    assert major["board"]["passed"] is True
    assert gates["passed"] is True
    assert gates["gates"]["selection_qualified"] is True
    assert gates["gates"]["deterministic_replay_verified"] is True
    assert failed_replay["passed"] is False


def test_research_excludes_unverified_point_in_time_features_from_model_evidence() -> None:
    row = ProbabilityResearchRow(
        run_id=30,
        symbol="600001.SH",
        session_date="2026-01-05",
        features=probability_feature_vector({}, market="SH", board="SH_MAIN", liquidity="high", regime="strong"),
        labels={1: ProbabilityLabelOutcome(1, "modelled", "target_close", net_return=0.02)},
        mature_horizons=frozenset({1}),
        dimensions={"market": "SH", "board": "SH_MAIN", "industry": "测试", "liquidity": "high", "regime": "strong"},
        source_evidence_digest="not-a-verified-sha256",
    )

    research = build_probability_research(
        [row],
        generated_at="2026-08-11T08:00:00Z",
        bootstrap_samples=100,
    )
    one_day = research["horizons"]["1"]["net_excess_positive"]  # type: ignore[index]
    record = next(
        item
        for item in research["records"]  # type: ignore[union-attr]
        if item["horizon"] == 1 and item["target"] == "net_excess_positive"
    )

    assert one_day["counts"]["eligible_observation_count"] == 0
    assert one_day["point_in_time_evidence"]["coverage"] == 0
    assert one_day["promotion_gates"]["gates"]["point_in_time_evidence_at_least_95pct"] is False
    assert record["probability"] is None
    assert "point_in_time_source_digest_unavailable" in record["limitations"]


def test_research_excludes_modelled_returns_with_unverified_trade_rule_profile() -> None:
    row = ProbabilityResearchRow(
        run_id=31,
        symbol="600001.SH",
        session_date="2026-01-05",
        features=probability_feature_vector(
            {}, market="SH", board="SH_MAIN", liquidity="high", regime="strong",
        ),
        labels={
            1: ProbabilityLabelOutcome(
                1,
                "modelled",
                "target_close",
                net_return=0.02,
                rule_profile_verified=False,
            )
        },
        mature_horizons=frozenset({1}),
        dimensions={"market": "SH", "board": "SH_MAIN"},
        source_evidence_digest="a" * 64,
    )

    research = build_probability_research(
        [row],
        generated_at="2026-08-11T08:00:00Z",
        bootstrap_samples=100,
    )
    evidence = research["horizons"]["1"]["net_excess_positive"]  # type: ignore[index]
    record = next(
        item
        for item in research["records"]  # type: ignore[union-attr]
        if item["horizon"] == 1 and item["target"] == "net_excess_positive"
    )

    assert evidence["counts"]["eligible_observation_count"] == 0
    assert record["label_rule_profile_verified"] is False
    assert record["net_return"] is None
    assert record["observed_label"] is None
    assert "label_rule_profile_unverified" in record["limitations"]


def _small_config() -> ProbabilityConfig:
    return ProbabilityConfig(
        horizon=1,
        minimum_train_sessions=12,
        minimum_calibration_sessions=6,
        minimum_test_sessions=6,
        minimum_bin_sessions=1,
        bootstrap_samples=100,
    )


def _complete_test_label_contract() -> dict[str, object]:
    return {
        "label_version": PROBABILITY_LABEL_VERSION,
        "execution_model": PROBABILITY_EXECUTION_MODEL,
        "horizons": [1, 5, 20],
        "entry_session_offset": 1,
        "target_session_offsets": {"1": 2, "5": 6, "20": 21},
        "target_definitions": [
            "absolute_net_return_positive",
            "equal_weight_market_net_excess_positive",
        ],
        "cost_model_version": PROBABILITY_COST_MODEL_VERSION,
        "cost_profile_id": "test-base-v1",
        "execution_notional": 100_000.0,
        "max_daily_participation_rate": 0.01,
    }


def _qualified_joint_execution_probability(
    sample_id: str = "71:600519.SH:1:net_excess_positive",
    session: str = "2025-01-01",
) -> dict[str, object]:
    horizon = int(sample_id.split(":")[2])

    def bar(role: str, session: str, price: float) -> dict[str, object]:
        return {
            "role": role, "session_date": session,
            "session_offset_from_signal": 1 if role == "entry" else horizon + 1,
            "source_kind": "official_exchange_daily_ohlcv_amount",
            "adjustment_mode": "none", "open": price, "high": price * 1.02,
            "low": price * 0.98, "close": price * 1.01, "volume": 1_000_000.0,
            "amount": 20_000_000.0, "source_dataset_digest": "a" * 64,
        }

    def rules(role: str, session: str) -> dict[str, object]:
        return {
            "role": role, "session_date": session,
            "source_kind": "official_effective_dated", "effective_date": session,
            "board": "main", "is_st": False, "listing_status": "listed",
            "board_rule_id": "main-v1", "st_rule_id": "st-v1",
            "delisting_rule_id": "delisting-v1", "ruleset_digest": "b" * 64,
        }

    def reference(role: str, session: str, price: float) -> dict[str, object]:
        return {
            "role": role, "session_date": session,
            "basis": "official_unadjusted_reference_with_effective_corporate_action",
            "previous_close": price, "reference_price": price,
            "corporate_action_status": "none", "reference_price_rule_id": "ref-v1",
            "source_dataset_digest": "c" * 64,
        }

    signal_day = date.fromisoformat(session)
    entry_session = (signal_day + timedelta(days=1)).isoformat()
    exit_session = (signal_day + timedelta(days=horizon + 1)).isoformat()
    symbol = sample_id.split(":")[1]
    evidence = {
        "entry_bar": bar("entry", entry_session, 10.0),
        "exit_bar": bar("exit", exit_session, 10.2),
        "entry_rules": rules("entry", entry_session),
        "exit_rules": rules("exit", exit_session),
        "entry_reference": reference("entry", entry_session, 9.9),
        "exit_reference": reference("exit", exit_session, 10.1),
        "participation": {
            "basis": "entry_and_exit_same_session_amount",
            "entry_order_notional": 100_000.0, "entry_session_amount": 20_000_000.0,
            "entry_participation_rate": 0.005, "exit_order_notional": 102_000.0,
            "exit_session_amount": 20_000_000.0, "exit_participation_rate": 0.0051,
            "maximum_participation_rate": 0.01, "evidence_digest": "d" * 64,
        },
        "benchmark": {
            "universe_basis": "fixed_full_market_at_signal", "outcome_population": "all_decisions",
            "benchmark_method": "fixed_universe_leave_one_out", "universe_frozen_before_outcomes": True,
            "benchmark_predeclared": True, "subject_excluded": True,
            "universe_definition_digest": "e" * 64, "universe_membership_digest": "f" * 64,
            "decision_cohort_digest": "1" * 64, "benchmark_series_digest": "2" * 64,
        },
        "calibration": {
            "estimator_contract": "three_component_joint_chain", "training_cutoff": "2024-12-31",
            "prediction_generated_at": "2025-01-01T15:05:00+08:00",
            "entry_model_digest": "3" * 64, "exit_model_digest": "4" * 64,
            "net_model_digest": "5" * 64, "calibrator_digest": "6" * 64,
            "feature_schema_digest": "7" * 64, "decision_information_digest": "8" * 64,
            "out_of_sample_assessment_digest": "9" * 64,
            "out_of_sample_verified": True, "calibration_verified": True,
            "selection_qualified": True,
        },
    }
    report = build_decision_time_joint_execution_probability_evidence(
        sample_id=sample_id, symbol=symbol, signal_session=session,
        generated_at=f"{(signal_day + timedelta(days=3)).isoformat()}T09:00:00+08:00",
        evidence=evidence,
        probabilities={
            "entry_fill_probability": 0.8,
            "exit_executable_given_entry_probability": 0.9,
            "net_positive_given_entry_and_exit_probability": 0.6,
            "joint_net_positive_probability": 0.432,
            "action_probability": 0.432,
        },
    )
    return report.model_dump(mode="json")


def _filter_authorization(evidence: Mapping[str, object]) -> dict[str, object]:
    evidence_digest = str(evidence["evidence_digest"])
    predictions = cast(list[Mapping[str, object]], evidence["predictions"])
    selected_statistics = probability_module._selected_candidate_session_statistics(predictions)
    registry: list[dict[str, object]] = []
    for index in range(6):
        candidate_id = "selected" if index == 0 else f"registered-{index}"
        statistics = selected_statistics if index == 0 else [
            (day, max(value, 0.01)) for day, value in selected_statistics
        ]
        registry.append({
            "candidate_id": candidate_id,
            "evidence_digest": evidence_digest if index == 0 else stable_probability_hash(candidate_id),
            "session_statistics": [
                {"session_date": day, "proper_score_improvement": value}
                for day, value in statistics
            ],
            "raw_p_value": probability_module._one_sided_sign_test_p_value(
                [value for _day, value in statistics],
            ),
        })
    adjusted = probability_module._benjamini_hochberg_adjusted({
        str(item["candidate_id"]): float(item["raw_p_value"])
        for item in registry
    })
    calibrated = cast(Mapping[str, object], cast(Mapping[str, object], evidence["calibration_metrics"])["calibrated"])
    reference_drift = [
        {
            "session_date": (date(2024, 1, 1) + timedelta(days=index)).isoformat(),
            "feature_statistic": 0.10 + index / 10_000,
            "probability": 0.55 + index / 10_000,
            "performance": 0.02 + index / 100_000,
        }
        for index in range(30)
    ]
    current_drift = [
        {
            "session_date": day,
            "feature_statistic": feature,
            "probability": probability,
            "performance": performance,
        }
        for day, feature, probability, performance
        in probability_module._oos_current_drift_series(predictions)
    ]
    for index, row in enumerate(reference_drift):
        current = current_drift[index]
        row["feature_statistic"] = float(current["feature_statistic"]) - 0.01
        row["probability"] = max(0.0, float(current["probability"]) - 0.01)
        row["performance"] = float(current["performance"]) - 0.001
    drift_statistics = probability_module._drift_statistics(
        probability_module._validated_drift_series(reference_drift, "reference"),
        probability_module._validated_drift_series(current_drift, "current"),
    )
    joint_corpus = [
        _qualified_joint_execution_probability(
            str(prediction["sample_id"]), str(prediction["session_date"]),
        )
        for prediction in predictions
    ]
    session_economics = probability_module._execution_session_economics(
        predictions, joint_corpus,
    )
    execution_metrics = probability_module._execution_metrics(session_economics)
    joint_assessments = [
        {
            "sample_id": report["sample_id"],
            "assessment_digest": report["evidence"]["calibration"][
                "out_of_sample_assessment_digest"
            ],
        }
        for report in joint_corpus
    ]
    joint_estimand_digest = stable_probability_hash(joint_corpus[0]["estimand"])
    payload = {
        "version": PROBABILITY_FILTER_AUTHORIZATION_VERSION,
        "evidence_binding": {
            "evidence_digest": evidence_digest,
            "metrics_digest": stable_probability_hash(evidence["calibration_metrics"]),
            "input_digest": evidence["input_digest"],
            "horizon": evidence["horizon"],
            "target_definition": evidence["target_definition"],
        },
        "oos_predictions": predictions,
        "candidate_registry": registry,
        "selected_candidate_id": "selected",
        "multiple_testing": {
            "method": "benjamini_hochberg_fdr",
            "alpha": 0.05,
            "family_size": len(registry),
            "adjusted_p_value": adjusted["selected"],
        },
        "calibration_validation": {
            "independent_session_count": calibrated["independent_session_count"],
            "brier_improvement_ci_95": calibrated["brier_improvement_vs_reference_ci_95"],
            "log_loss_improvement_ci_95": calibrated["log_loss_improvement_vs_reference_ci_95"],
            "ece": calibrated["ece"],
        },
        "drift_validation": {
            "independent_session_count": 60,
            "reference_series": reference_drift,
            "current_series": current_drift,
            "reference_digest": stable_probability_hash(reference_drift),
            "current_digest": stable_probability_hash(current_drift),
            "statistics": drift_statistics,
            "thresholds": {
                "maximum_feature_mean_shift": 0.10,
                "maximum_probability_mean_shift": 0.10,
                "maximum_performance_mean_shift": 0.10,
            },
        },
        "execution_validation": {
            "observation_count": len(predictions),
            "independent_session_count": len(session_economics),
            "prediction_digest": stable_probability_hash(predictions),
            "joint_execution_evidence": joint_corpus,
            "joint_execution_evidence_digest": stable_probability_hash(joint_corpus),
            "joint_execution_assessment_digest": stable_probability_hash(joint_assessments),
            "joint_execution_estimand_digest": joint_estimand_digest,
            "session_economics": session_economics,
            "session_economics_digest": stable_probability_hash(session_economics),
            **execution_metrics,
            "thresholds": {
                "minimum_mean_net_excess_return": 0.0,
                "minimum_maximum_drawdown": -0.20,
                "maximum_mean_top100_turnover": 0.50,
                "minimum_capacity_coverage": 0.95,
            },
        },
    }
    return seal_probability_filter_authorization_artifact(
        payload, generated_at="2026-08-11T08:30:00Z",
    )


def _write_store_artifact(
    root,
    *,
    filename: str,
    generated_at: str,
    status: str,
    probability: float | None = None,
    run_id: int = 29,
):
    calibrated = status == "calibrated_shadow"
    persisted_probability = float(probability) if calibrated and probability is not None else None
    model = _store_model(persisted_probability) if persisted_probability is not None else None
    calibrator = _store_calibrator() if calibrated else None
    baseline = _store_baseline(persisted_probability) if persisted_probability is not None else None
    versions = {
        "model": PROBABILITY_MODEL_VERSION,
        "calibrator": PROBABILITY_CALIBRATOR_VERSION,
        "feature": PROBABILITY_FEATURE_VERSION,
        "label": PROBABILITY_LABEL_VERSION,
        "cost_model": PROBABILITY_COST_MODEL_VERSION,
    }
    digests = {
        "input": "a" * 64,
        "model": stable_probability_hash(model) if model is not None else None,
        "calibrator": stable_probability_hash(calibrator) if calibrator is not None else None,
        "isotonic_calibrator": None,
        "baseline": stable_probability_hash(baseline) if baseline is not None else None,
        "evidence": None,
    }
    limitations = ["shadow_only_no_production_ranking_effect"]
    counts = {"training_session_count": 120 if calibrated else 0}
    contract = {"schema_version": "probability-contract-v1"}
    split = {
        "train_dates": ["2026-07-01"],
        "train_gap_dates": [],
        "calibration_dates": ["2026-07-15"],
        "calibration_gap_dates": [],
        "test_dates": ["2026-07-31"],
    } if calibrated else None
    folds = [{
        "fold_id": 1,
        "split": split,
        "training_cutoff": "2026-07-31",
        "base_rate": 0.52,
        "model": model,
        "calibrator": calibrator,
        "isotonic_calibrator": None,
        "empirical_bayes_baseline": baseline,
        "model_digest": digests["model"],
        "calibrator_digest": digests["calibrator"],
        "isotonic_calibrator_digest": None,
        "baseline_digest": digests["baseline"],
        "prediction_count": 1,
        "test_session_count": 1,
    }] if calibrated else []
    payload = {
        "record_contract_version": PROBABILITY_RESULT_CONTRACT_VERSION,
        "feature_evidence": [
            {
                "run_id": run_id,
                "symbol": "600519.SH",
                "quote_date": "2026-07-31",
                "features": {"trend": 0.0},
                "feature_names": ["trend"],
                "feature_vector_digest": stable_probability_hash({"trend": 0.0}),
                "dimensions": {"market": "SH", "board": "SH_MAIN"},
                "source_evidence_digest": "e" * 64,
            }
        ],
        "studies": [
            {
                "run_id": run_id,
                "target": "net_excess_positive",
                "horizon": 5,
                "status": status,
                "versions": versions,
                "digests": digests,
                "limitations": limitations,
                "metadata": {
                    "base_rate": 0.52 if calibrated else None,
                    "training_cutoff": "2026-07-31" if calibrated else None,
                    "split": split,
                    "target_definition": "future_5d_net_excess_return_gt_0_after_costs",
                    "counts": counts,
                    "contract": contract,
                    "cohort_contract": {
                        "mode": "official",
                        "scope": FULL_MARKET_SCOPE,
                        "rule_version": _STORE_RULE_VERSION,
                    },
                    "production_score_contract": {
                        "production_score_rule_version": "full-market-score-v4",
                        "production_score_spec_hash": "b" * 64,
                    },
                    "source_integrity_digest": "c" * 64,
                    "generated_at": generated_at,
                    "input_digest": digests["input"],
                    "model": model,
                    "calibrator": calibrator,
                    "isotonic_calibrator": None,
                    "empirical_bayes_baseline": baseline,
                    "model_digest": digests["model"],
                    "calibrator_digest": digests["calibrator"],
                    "isotonic_calibrator_digest": None,
                    "baseline_digest": digests["baseline"],
                    "calibration_metrics": _store_calibration_metrics(calibrated),
                    "folds": folds,
                },
            }
        ],
        "records": [
            {
                "run_id": run_id,
                "symbol": "600519.SH",
                "target": "net_excess_positive",
                "horizon": 5,
                "status": status,
                "probability": persisted_probability,
                "calibration_bias_interval": (
                    [-0.05, 0.05] if persisted_probability is not None else None
                ),
                "calibration_adjusted_probability_interval": (
                    [persisted_probability - 0.05, persisted_probability + 0.05]
                    if persisted_probability is not None else None
                ),
                "details": _store_record_details(
                    run_id=run_id,
                    generated_at=generated_at,
                    calibrated=calibrated,
                    probability=persisted_probability,
                    versions=versions,
                    digests=digests,
                    limitations=limitations,
                    counts=counts,
                    contract=contract,
                ),
            }
        ],
    }
    artifact = build_probability_artifact(payload, generated_at=generated_at)
    database = root / "ashare.sqlite3"
    _ensure_probability_artifact_database(database, run_ids=(run_id,))
    directory = root / "market-scan-probability"
    target = directory / filename
    if target.exists():
        replacement = directory / f"replacement-{filename}"
        database_before_write = database.read_bytes()
        write_probability_artifact(replacement, artifact, database_path=database)
        assert database.read_bytes() == database_before_write
        replacement.replace(target)
    else:
        database_before_write = database.read_bytes()
        write_probability_artifact(target, artifact, database_path=database)
        assert database.read_bytes() == database_before_write
    return directory, database, target


def _ensure_probability_artifact_database(
    database: Path,
    *,
    run_ids: tuple[int, ...],
) -> None:
    if not database.exists():
        SQLiteCache(database, settings=Settings(cache_path=database))
    with sqlite3.connect(database) as connection:
        for run_id in sorted(set(run_ids)):
            if connection.execute(
                "SELECT 1 FROM market_scan_run WHERE id = ?",
                (run_id,),
            ).fetchone() is not None:
                continue
            _insert_probability_artifact_snapshot(connection, run_id)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _insert_probability_artifact_snapshot(
    connection: sqlite3.Connection,
    run_id: int,
) -> None:
    session_day = 1 + run_id % 20
    session_date = f"2026-08-{session_day:02d}"
    created_at = f"{session_date}T16:00:00+08:00"
    updated_at = f"{session_date}T16:31:00+08:00"
    connection.execute(
        """
        INSERT INTO market_scan_run (
            id, status, trigger, mode, rule_version, as_of, data_date,
            quote_date, scope, total_count, processed_count, success_count,
            publication_diagnostics_json, created_at, updated_at, started_at,
            finished_at, duration_ms
        ) VALUES (
            ?, 'success', 'manual', 'official', ?, ?, ?, ?, ?, 1, 1, 1,
            ?, ?, ?, ?, ?, 60000
        )
        """,
        (
            run_id,
            _STORE_RULE_VERSION,
            f"{session_date} 16:30:00",
            session_date,
            session_date,
            FULL_MARKET_SCOPE,
            action_pass_publication_diagnostics().model_dump_json(),
            created_at,
            updated_at,
            created_at,
            f"{session_date}T16:30:00+08:00",
        ),
    )
    connection.execute(
        """
        INSERT INTO market_scan_result (
            run_id, symbol, code, market, name, status, rank, score, raw_score,
            trend_score, leader_score, data_quality_score, price, data_date,
            quote_timestamp, quote_observed_at, quote_source, kline_source,
            adjustment_mode, updated_at
        ) VALUES (?, '600519.SH', '600519', 'SH', '贵州茅台', 'success',
                  1, 80, 80, 80, 80, 100, 10, ?, ?, ?, 'test', 'test', 'qfq', ?)
        """,
        (
            run_id,
            session_date,
            f"{session_date} 15:00:00",
            f"{session_date}T15:00:01+08:00",
            f"{session_date}T16:29:00+08:00",
        ),
    )
    seal_market_scan_snapshot(
        connection,
        run_id,
        origin="publication",
        sealed_at=f"{session_date}T16:32:00+08:00",
    )


def _store_model(probability: float) -> dict[str, object]:
    return {
        "version": PROBABILITY_MODEL_VERSION,
        "feature_names": ["trend"],
        "means": [0.0],
        "scales": [1.0],
        "intercept": math.log(probability / (1.0 - probability)),
        "coefficients": [0.0],
    }


def _store_calibrator() -> dict[str, object]:
    return {"version": PROBABILITY_CALIBRATOR_VERSION, "intercept": 0.0, "slope": 1.0}


def _store_baseline(probability: float) -> dict[str, object]:
    return {"version": "empirical-bayes-v1", "boundaries": [], "probabilities": [probability]}


def _store_calibration_metrics(calibrated: bool) -> dict[str, object] | None:
    if not calibrated:
        return None
    return {
        "calibrated": {
            "observation_count": 100,
            "independent_session_count": 60,
            "brier_score": 0.2,
            "brier_skill_score": 0.1,
            "log_loss": 0.6,
            "ece": 0.03,
            "auc": 0.62,
            "bin_monotonic": True,
            "highest_bin_above_base_rate": True,
            "brier_score_ci_95": [0.18, 0.22],
            "actual_positive_rate_ci_95": [0.48, 0.58],
            "calibration_offset_ci_95": [-0.05, 0.05],
            "bootstrap_samples": 1000,
            "calibration_bins": [{"independent_session_count": 20}],
        }
    }


def _store_calibration_summary(calibrated: bool) -> dict[str, object]:
    metrics = _store_calibration_metrics(calibrated)
    values = metrics["calibrated"] if metrics is not None else {}
    names = (
        "observation_count", "independent_session_count", "brier_score", "brier_skill_score",
        "reference_base_rate_mean", "reference_brier_score", "reference_definition",
        "log_loss", "ece", "auc", "bin_monotonic", "highest_bin_above_base_rate",
        "brier_score_ci_95", "actual_positive_rate_ci_95", "calibration_offset_ci_95", "bootstrap_samples",
    )
    return {
        **{name: values.get(name) for name in names},
        "calibration_bin_count": 1 if calibrated else None,
        "minimum_bin_independent_session_count": 20 if calibrated else None,
    }


def _store_record_details(
    *,
    run_id: int,
    generated_at: str,
    calibrated: bool,
    probability: float | None,
    versions: dict[str, object],
    digests: dict[str, object],
    limitations: list[str],
    counts: dict[str, object],
    contract: dict[str, object],
) -> dict[str, object]:
    return {
        "record_contract_version": PROBABILITY_RESULT_CONTRACT_VERSION,
        "sample_id": f"{run_id}:600519.SH:5:net_excess_positive",
        "quote_date": "2026-07-31",
        "feature_evidence_key": f"{run_id}:600519.SH",
        "dimensions": {"market": "SH", "board": "SH_MAIN"},
        "feature_vector_digest": stable_probability_hash({"trend": 0.0}),
        "source_evidence_digest": "e" * 64,
        "mature_horizon": calibrated,
        "executable": calibrated,
        "model_target": 1 if calibrated else None,
        "fold_id": 1 if calibrated else None,
        "observed_label": 1 if calibrated else None,
        "label_status": "modelled" if calibrated else "data_unavailable",
        "label_reason": "target_close" if calibrated else "label_missing",
        "net_return": 0.02 if calibrated else None,
        "market_benchmark_net_return": 0.01 if calibrated else None,
        "net_excess_return": 0.01 if calibrated else None,
        "entry_date": "2026-08-03" if calibrated else None,
        "exit_date": "2026-08-10" if calibrated else None,
        "raw_probability": probability,
        "empirical_bayes_probability": probability,
        "calibration_adjusted_probability_interval_definition": (
            "test_session_block_bootstrap_calibration_offset_95pct" if calibrated else None
        ),
        "versions": versions,
        "digests": digests,
        "base_rate": 0.52 if calibrated else None,
        "training_cutoff": "2026-07-31" if calibrated else None,
        "target_definition": "future_5d_net_excess_return_gt_0_after_costs",
        "counts": counts,
        "contract": contract,
        "calibration_summary": _store_calibration_summary(calibrated),
        "calibration_offset_ci_95": [-0.05, 0.05] if calibrated else None,
        "limitations": limitations,
        "generated_at": generated_at,
        "automatic_promotion": False,
    }


def _count_store_loads(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    original = probability_store_module.load_probability_artifact
    calls = {"count": 0}

    def counted(path):
        calls["count"] += 1
        return original(path)

    monkeypatch.setattr(probability_store_module, "load_probability_artifact", counted)
    return calls


def _signal_samples(session_count: int) -> list[ProbabilitySample]:
    samples: list[ProbabilitySample] = []
    signals = (-1.2, -0.4, 0.4, 1.2)
    for session_index in range(session_count):
        shift = ((session_index % 5) - 2) * 0.08
        for stock_index, signal in enumerate(signals):
            outcome = int(signal + shift > 0)
            samples.append(
                ProbabilitySample(
                    sample_id=(
                        f"{session_index}:600{stock_index:03d}.SH:"
                        "1:net_excess_positive"
                    ),
                    session_date=_day(session_index),
                    features={"trend": signal, "risk": -signal * 0.4 + shift},
                    target=outcome,
                    net_return=0.01 if outcome else -0.01,
                    net_excess_return=0.005 if outcome else -0.005,
                )
            )
    return samples


def _day(index: int) -> str:
    return (date(2025, 1, 1) + timedelta(days=index)).isoformat()
