from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import math
from typing import Any, cast

import pytest

import app.services.market_scan_probability as probability_module
import app.services.market_scan_probability_metrics as probability_metrics
import app.services.market_scan_probability_research as probability_research_module
import app.services.market_scan_probability_store as probability_store_module
from app.services.market_scan_probability import (
    PROBABILITY_CALIBRATOR_VERSION,
    PROBABILITY_COST_MODEL_VERSION,
    PROBABILITY_FEATURE_VERSION,
    PROBABILITY_LABEL_VERSION,
    PROBABILITY_ISOTONIC_CALIBRATOR_VERSION,
    PROBABILITY_MODEL_VERSION,
    ProbabilityConfig,
    ProbabilityReplayError,
    ProbabilitySample,
    build_probability_contract,
    evaluate_probability_predictions,
    fit_empirical_bayes_baseline,
    fit_shadow_probability,
    grouped_walk_forward_splits,
    probability_selection_qualified,
    predict_shadow_probability,
    replay_shadow_probability,
    stable_probability_hash,
    verify_shadow_probability_evidence,
)
from app.services.market_scan_probability_artifact import (
    PROBABILITY_RESULT_CONTRACT_VERSION,
    ProbabilityArtifactError,
    build_probability_artifact,
    write_probability_artifact,
)
from app.services.market_scan_probability_labels import (
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

    assert len(folds) == 3
    latest = folds[-1]
    assert len(latest.train_dates) == 20
    assert len(latest.train_gap_dates) == 5
    assert len(latest.calibration_dates) == 5
    assert len(latest.calibration_gap_dates) == 5
    assert len(latest.test_dates) == 5
    partitions = (
        latest.train_dates,
        latest.train_gap_dates,
        latest.calibration_dates,
        latest.calibration_gap_dates,
        latest.test_dates,
    )
    assert len(set().union(*map(set, partitions))) == 40
    assert latest.train_dates[-1] < latest.train_gap_dates[0] < latest.calibration_dates[0]
    assert latest.calibration_dates[-1] < latest.calibration_gap_dates[0] < latest.test_dates[0]


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
    assert set(dates[-5:]).isdisjoint(set().union(*test_windows))


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
        _signal_samples(26),
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

    assert metrics["bootstrap_method"] == "deterministic_circular_moving_session_block_95pct_v1"
    assert metrics["bootstrap_block_length_sessions"] == 20
    assert evaluation["bootstrap_block_length_sessions"] == 20
    assert evaluation["bootstrap"] == metrics["bootstrap_method"]
    assert verify_shadow_probability_evidence(evidence, _signal_samples(100)) is True


def test_multifold_oos_evidence_replays_each_fold_and_final_fold_drives_future_prediction() -> None:
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
    assert counts["unused_tail_session_count"] == 5
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
    expected_raw = probability_module._model_probability(final_fold["model"], features)
    expected_probability = probability_module._platt_probability(final_fold["calibrator"], expected_raw)
    assert estimate["raw_probability"] == pytest.approx(expected_raw)
    assert estimate["probability"] == pytest.approx(expected_probability)
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


def test_prediction_uses_independent_platt_calibration_and_reports_interval() -> None:
    evidence = fit_shadow_probability(
        _signal_samples(42),
        config=_small_config(),
        generated_at="2026-08-11T08:00:00Z",
    )

    positive = predict_shadow_probability(evidence, {"risk": -0.5, "trend": 1.2}, sample_id="positive")
    negative = predict_shadow_probability(evidence, {"risk": 0.5, "trend": -1.2}, sample_id="negative")

    assert positive["status"] == "calibrated_shadow"
    assert 0 <= positive["probability"] <= 1
    assert 0 <= negative["probability"] <= 1
    assert positive["probability"] > negative["probability"]
    assert len(positive["confidence_interval"]) == 2
    assert positive["confidence_interval_definition"] == "test_session_block_bootstrap_calibration_offset_95pct"
    assert positive["model_version"] == PROBABILITY_MODEL_VERSION
    assert len(positive["prediction_digest"]) == 64


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
    assert estimate["probability"] != estimate["empirical_bayes_probability"]
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
    assert estimate["confidence_interval"] is None
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
    feature_evidence = cast(list[dict[str, Any]], artifact["payload"]["feature_evidence"])  # type: ignore[index]
    assert len(feature_evidence) == len(rows)
    assert all("features" in item and "dimensions" in item for item in feature_evidence)
    assert all("feature_evidence_key" in item["details"] and "dimensions" in item["details"] for item in persisted_records)
    assert all("feature_evidence_reference" not in item["details"] for item in persisted_records)
    assert all(item["details"]["versions"] == studies[0]["versions"] for item in persisted_records)
    assert all("digests" in item["details"] and "base_rate" in item["details"] for item in persisted_records)
    database = tmp_path / "ashare.sqlite3"
    database.write_bytes(b"database-remains-unchanged")
    target = tmp_path / "market-scan-probability" / f"market-scan-probability-{artifact['integrity']['integrity_digest']}.json"  # type: ignore[index]
    write_probability_artifact(target, artifact, database_path=database)
    summary, projected = MarketScanProbabilityStore(target.parent).run_projection(29)
    assert summary["status"] == "insufficient_data"
    assert projected["600001.SH"]["1"]["net_excess_positive"]["probability"] is None  # type: ignore[index]
    assert database.read_bytes() == b"database-remains-unchanged"


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

    first_summary, first_records = store.run_projection(29)
    first_summary["status"] = "caller-mutated"
    first_records.clear()
    second_summary, second_records = store.run_projection(29)

    assert calls["count"] == 1
    assert second_summary["status"] == "insufficient_data"
    assert second_records["600519.SH"]["5"]["net_excess_positive"]["probability"] is None
    assert database.read_bytes() == b"persistent-database"


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
    assert database.read_bytes() == b"persistent-database"


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

    assert tuple(sorted(sparse)) == tuple(sorted(complete))
    assert all(math.isfinite(value) for value in sparse.values())
    assert sparse["market_bj"] == 1
    assert sparse["board_bse"] == 1
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
        "execution_model": "next-session-open,H-holding-session-close,T+1,no-delayed-exit",
        "horizons": [1, 5, 20],
        "target_definitions": [
            "absolute_net_return_positive",
            "equal_weight_market_net_excess_positive",
        ],
        "cost_model_version": PROBABILITY_COST_MODEL_VERSION,
        "cost_profile_id": "test-base-v1",
        "execution_notional": 100_000.0,
        "max_daily_participation_rate": 0.01,
    }


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
                "confidence_interval": (
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
    if not database.exists():
        database.write_bytes(b"persistent-database")
    directory = root / "market-scan-probability"
    target = directory / filename
    if target.exists():
        replacement = directory / f"replacement-{filename}"
        write_probability_artifact(replacement, artifact, database_path=database)
        replacement.replace(target)
    else:
        write_probability_artifact(target, artifact, database_path=database)
    return directory, database, target


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
        "confidence_interval_definition": "test_session_block_bootstrap_calibration_offset_95pct" if calibrated else None,
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
                    sample_id=f"{session_index:03d}-{stock_index}",
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
