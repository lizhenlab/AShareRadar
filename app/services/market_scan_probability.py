"""Deterministic, auditable Shadow probabilities for full-market research.

The module deliberately has no dependency on the production ranking write path.
It consumes already point-in-time labelled observations, applies grouped temporal
splits, fits a regularised model and emits JSON-compatible replay evidence.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date
import hashlib
import json
import math
from typing import Literal, cast

import numpy as np
from numpy.typing import NDArray

import app.services.market_scan_probability_metrics as probability_metrics


PROBABILITY_SCHEMA_VERSION = "market-scan-shadow-probability-v3"
PROBABILITY_MODEL_VERSION = "shadow-up-probability-logit-l2-v1"
PROBABILITY_CALIBRATOR_VERSION = "shadow-up-probability-platt-v1"
PROBABILITY_ISOTONIC_CALIBRATOR_VERSION = "shadow-up-probability-isotonic-pav-v1"
PROBABILITY_BASELINE_VERSION = probability_metrics.PROBABILITY_BASELINE_VERSION
PROBABILITY_FEATURE_VERSION = "full-market-point-in-time-features-v2"
PROBABILITY_LABEL_VERSION = "market-scan-upside-label-v2"
PROBABILITY_COST_MODEL_VERSION = "ashare-executable-round-trip-cost-v1"
PROBABILITY_SPLIT_VERSION = "grouped-date-multifold-train-gap-calibration-gap-test-v2"
ProbabilityStatus = Literal["insufficient_data", "calibrated_shadow"]
ProbabilityTarget = Literal["net_excess_positive", "net_return_positive"]
_SUPPORTED_HORIZONS = frozenset({1, 5, 20})
_FORBIDDEN_FEATURE_NAMES = frozenset(
    {"symbol", "stock_code", "ticker", "rank", "final_rank", "ranking", "target", "outcome", "label"}
)
_FORBIDDEN_FEATURE_PREFIXES = ("future_", "forward_", "next_", "realized_", "observed_")

# Compatibility aliases keep both the original public surface and historically
# imported private helpers available while calculation ownership lives in the
# dependency-free metrics module.
evaluate_probability_predictions = probability_metrics.evaluate_probability_predictions
fit_empirical_bayes_baseline = probability_metrics.fit_empirical_bayes_baseline
_metric_reference_probabilities = probability_metrics.metric_reference_probabilities
_brier_scores = probability_metrics.brier_scores
_expected_calibration_error = probability_metrics.expected_calibration_error
_validated_metric_rows = probability_metrics.validated_metric_rows
_calibration_bins = probability_metrics.calibration_bins
_bins_are_monotonic = probability_metrics.bins_are_monotonic
_log_loss = probability_metrics.log_loss
_auc = probability_metrics.auc
_date_block_bootstrap_ci = probability_metrics.date_block_bootstrap_ci
_validated_scores_and_labels = probability_metrics.validated_scores_and_labels
_quantile_boundaries = probability_metrics.quantile_boundaries
_percentile = probability_metrics.percentile


class ProbabilityReplayError(ValueError):
    """Raised when persisted probability evidence is invalid or cannot replay."""


@dataclass(frozen=True)
class ProbabilitySample:
    """One immutable point-in-time feature row and its eventual executable label."""

    sample_id: str
    session_date: str
    features: Mapping[str, float]
    target: int | bool | None
    executable: bool = True
    net_return: float | None = None
    net_excess_return: float | None = None


@dataclass(frozen=True)
class ProbabilityConfig:
    horizon: int = 5
    target: ProbabilityTarget = "net_excess_positive"
    cost_model_version: str = PROBABILITY_COST_MODEL_VERSION
    label_contract: Mapping[str, object] | None = None
    minimum_train_sessions: int = 120
    minimum_calibration_sessions: int = 40
    minimum_test_sessions: int = 60
    minimum_label_coverage: float = 0.95
    minimum_bin_sessions: int = 20
    minimum_selection_folds: int = 2
    gap_sessions: int | None = None
    calibration_bin_count: int = 5
    minimum_isotonic_calibration_sessions: int = 120
    empirical_bayes_bin_count: int = 10
    empirical_bayes_prior_strength: float = 20.0
    l2_strength: float = 1.0
    bootstrap_samples: int = 1_000
    maximum_iterations: int = 100
    convergence_tolerance: float = 1e-10

    def __post_init__(self) -> None:
        _validate_config(self)

    @property
    def effective_gap_sessions(self) -> int:
        return self.horizon if self.gap_sessions is None else self.gap_sessions


@dataclass(frozen=True)
class GroupedWalkForwardSplit:
    train_dates: tuple[str, ...]
    train_gap_dates: tuple[str, ...]
    calibration_dates: tuple[str, ...]
    calibration_gap_dates: tuple[str, ...]
    test_dates: tuple[str, ...]


@dataclass(frozen=True)
class _PreparedStudy:
    samples: tuple[ProbabilitySample, ...]
    eligible: tuple[ProbabilitySample, ...]
    feature_names: tuple[str, ...]
    input_digest: str
    label_coverage: float
    splits: tuple[GroupedWalkForwardSplit, ...]


@dataclass(frozen=True)
class _FittedArtifacts:
    model: dict[str, object]
    calibrator: dict[str, object]
    isotonic_calibrator: dict[str, object] | None
    baseline: dict[str, object]
    base_rate: float


@dataclass(frozen=True)
class _EvaluatedFold:
    fold_id: int
    split: GroupedWalkForwardSplit
    artifacts: _FittedArtifacts
    predictions: tuple[dict[str, object], ...]


def build_probability_contract(config: ProbabilityConfig) -> dict[str, object]:
    """Return the registered, versioned research contract for one horizon/target."""
    bound_label_contract = _bound_label_contract(config)
    return {
        "schema_version": PROBABILITY_SCHEMA_VERSION,
        "feature_version": PROBABILITY_FEATURE_VERSION,
        "label": {
            "version": PROBABILITY_LABEL_VERSION,
            "target": config.target,
            "target_definition": _target_definition(config),
            "entry": "next_tradable_session_open_after_scan",
            "exit": f"effective_trading_session_{config.horizon}_close",
            "unfilled_policy": "explicitly_non_executable_never_assume_fill",
            "point_in_time_required": True,
        },
        "cost": {
            "version": config.cost_model_version,
            "components": ["commission", "stamp_tax", "transfer_fee", "slippage"],
            "deduct_before_label": True,
            "label_contract": bound_label_contract,
            "label_contract_digest": stable_probability_hash(bound_label_contract),
            "label_contract_binding": (
                "complete" if config.label_contract is not None else "legacy_version_only"
            ),
        },
        "model": {
            "version": PROBABILITY_MODEL_VERSION,
            "algorithm": "standardized_l2_logistic_regression_newton",
            "l2_strength": config.l2_strength,
            "maximum_iterations": config.maximum_iterations,
            "convergence_tolerance": config.convergence_tolerance,
        },
        "calibrator": {
            "version": PROBABILITY_CALIBRATOR_VERSION,
            "algorithm": "independent_platt_sigmoid",
            "primary": True,
            "candidate_registry": [
                {
                    "id": "platt",
                    "version": PROBABILITY_CALIBRATOR_VERSION,
                    "algorithm": "independent_platt_sigmoid",
                    "minimum_calibration_sessions": config.minimum_calibration_sessions,
                },
                {
                    "id": "isotonic",
                    "version": PROBABILITY_ISOTONIC_CALIBRATOR_VERSION,
                    "algorithm": "weighted_pool_adjacent_violators",
                    "minimum_calibration_sessions": config.minimum_isotonic_calibration_sessions,
                    "selection_policy": "comparison_only_never_automatic",
                },
            ],
        },
        "baseline": {
            "version": PROBABILITY_BASELINE_VERSION,
            "bin_count": config.empirical_bayes_bin_count,
            "prior_strength": config.empirical_bayes_prior_strength,
        },
        "split": _split_contract(config),
        "evaluation": _evaluation_contract(config),
    }


def grouped_walk_forward_splits(
    session_dates: Sequence[str],
    config: ProbabilityConfig,
) -> tuple[GroupedWalkForwardSplit, ...]:
    """Build expanding grouped folds; all rows from one date remain in one partition."""
    dates = tuple(sorted({_validated_date(value) for value in session_dates}))
    gap = config.effective_gap_sessions
    required = (
        config.minimum_train_sessions
        + gap
        + config.minimum_calibration_sessions
        + gap
        + config.minimum_test_sessions
    )
    if len(dates) < required:
        return ()
    # A trailing remainder is deliberately excluded. Appending the final date
    # would overlap the preceding test window and would make a partial window
    # look like a complete out-of-sample fold.
    endpoints = list(range(required, len(dates) + 1, config.minimum_test_sessions))
    return tuple(_split_at_endpoint(dates, endpoint, config) for endpoint in endpoints)


def fit_shadow_probability(
    samples: Sequence[ProbabilitySample],
    *,
    config: ProbabilityConfig | None = None,
    generated_at: str,
) -> dict[str, object]:
    """Fit and evaluate one Shadow probability contract without production writes."""
    config = config or ProbabilityConfig()
    if not generated_at.strip():
        raise ValueError("generated_at 不能为空")
    prepared = _prepare_study(samples, config)
    initial_reasons = _initial_insufficiency_reasons(prepared, config)
    if initial_reasons:
        return _insufficient_evidence(prepared, config, generated_at, initial_reasons)
    evaluated_folds: list[_EvaluatedFold] = []
    for fold_id, split in enumerate(prepared.splits, start=1):
        partitions = _partition_samples(prepared.eligible, split)
        diversity_reasons = _class_diversity_reasons(partitions)
        if diversity_reasons:
            tagged = [f"fold_{fold_id}_{reason}" for reason in diversity_reasons]
            return _insufficient_evidence(prepared, config, generated_at, tagged, split=split)
        artifacts = _fit_artifacts(partitions, prepared.feature_names, config)
        fold_predictions = tuple(
            _test_predictions(
                partitions["test"], artifacts, prepared.feature_names, fold_id=fold_id,
            )
        )
        evaluated_folds.append(
            _EvaluatedFold(
                fold_id=fold_id,
                split=split,
                artifacts=artifacts,
                predictions=fold_predictions,
            )
        )
    all_predictions = [item for fold in evaluated_folds for item in fold.predictions]
    metrics = _prediction_metrics(all_predictions, config, prepared.input_digest)
    reasons = _metric_insufficiency_reasons(metrics, config)
    return _complete_evidence(
        prepared, config, generated_at, evaluated_folds, all_predictions, metrics, reasons,
    )


def predict_shadow_probability(
    evidence: Mapping[str, object],
    features: Mapping[str, float],
    *,
    sample_id: str = "research-current",
) -> dict[str, object]:
    """Apply a calibrated artifact; insufficient studies return a null probability."""
    verify_shadow_probability_evidence(evidence)
    if evidence.get("status") != "calibrated_shadow":
        return _null_estimate(evidence, sample_id)
    model = _object_mapping(evidence.get("model"), "model")
    calibrator = _object_mapping(evidence.get("calibrator"), "calibrator")
    baseline = _object_mapping(evidence.get("empirical_bayes_baseline"), "empirical_bayes_baseline")
    raw = _model_probability(model, features)
    probability = _platt_probability(calibrator, raw)
    estimate = _estimate_payload(evidence, sample_id, probability, raw, _baseline_probability(baseline, raw))
    estimate["prediction_digest"] = stable_probability_hash(estimate)
    return estimate


def probability_selection_qualified(evidence: Mapping[str, object]) -> bool:
    """Fail closed unless new evidence explicitly passed selection-use gates.

    Legacy calibrated artifacts remain displayable, but lack this independently
    verified qualification and therefore cannot silently become filter inputs.
    """
    qualification = evidence.get("selection_qualification")
    return bool(
        evidence.get("status") == "calibrated_shadow"
        and evidence.get("selection_qualified") is True
        and isinstance(qualification, Mapping)
        and qualification.get("passed") is True
    )


def verify_shadow_probability_evidence(
    evidence: Mapping[str, object],
    samples: Sequence[ProbabilitySample] | None = None,
) -> bool:
    """Verify registered contracts, hashes, predictions and optionally full refit replay."""
    try:
        _verify_evidence_digest(evidence)
        config = _config_from_evidence(evidence)
        _verify_registered_evidence(evidence, config)
        _verify_artifact_digests(evidence)
        _verify_fold_artifacts(evidence, config)
        _verify_calibrator_candidate_records(evidence, config)
        _verify_persisted_predictions(evidence)
        _verify_persisted_metrics(evidence, config)
        _verify_selection_qualification(evidence, config)
        if samples is not None:
            rebuilt = fit_shadow_probability(samples, config=config, generated_at=str(evidence.get("generated_at") or ""))
            if rebuilt != dict(evidence):
                raise ProbabilityReplayError("上涨概率完整输入重放不一致")
    except ProbabilityReplayError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ProbabilityReplayError("上涨概率证据结构损坏") from exc
    return True


def replay_shadow_probability(
    evidence: Mapping[str, object],
    samples: Sequence[ProbabilitySample],
) -> dict[str, object]:
    """Deterministically refit from full inputs and return the verified evidence."""
    verify_shadow_probability_evidence(evidence, samples)
    return dict(evidence)


def stable_probability_hash(value: object) -> str:
    """Hash a finite canonical JSON representation with stable key ordering."""
    canonical = _canonical_json_value(value)
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_config(config: ProbabilityConfig) -> None:
    if config.horizon not in _SUPPORTED_HORIZONS:
        raise ValueError("上涨概率 horizon 仅支持 1、5、20")
    if config.target not in ("net_excess_positive", "net_return_positive"):
        raise ValueError("上涨概率 target 不受支持")
    if not config.cost_model_version.strip():
        raise ValueError("上涨概率 cost_model_version 不能为空")
    positive_ints = (
        config.minimum_train_sessions,
        config.minimum_calibration_sessions,
        config.minimum_test_sessions,
        config.minimum_bin_sessions,
        config.minimum_selection_folds,
        config.minimum_isotonic_calibration_sessions,
        config.calibration_bin_count,
        config.empirical_bayes_bin_count,
        config.maximum_iterations,
    )
    if any(value <= 0 for value in positive_ints):
        raise ValueError("上涨概率会话、分箱和迭代门槛必须为正整数")
    if config.effective_gap_sessions < config.horizon:
        raise ValueError("gap_sessions 不能小于预测 horizon")
    if not 0 < config.minimum_label_coverage <= 1:
        raise ValueError("minimum_label_coverage 必须在 (0, 1] 范围内")
    positive_floats = (config.empirical_bayes_prior_strength, config.l2_strength, config.convergence_tolerance)
    if any(value <= 0 or not math.isfinite(value) for value in positive_floats):
        raise ValueError("上涨概率正则、先验及收敛参数必须为有限正数")
    if config.bootstrap_samples < 100:
        raise ValueError("bootstrap_samples 不能小于 100")
    _bound_label_contract(config)


def _bound_label_contract(config: ProbabilityConfig) -> dict[str, object]:
    """Canonical label/execution assumptions bound into model identity."""
    if config.label_contract is None:
        return {
            "label_version": PROBABILITY_LABEL_VERSION,
            "cost_model_version": config.cost_model_version,
        }
    contract = _canonical_json_value(dict(config.label_contract))
    if not isinstance(contract, dict) or not contract:
        raise ValueError("上涨概率 label_contract 必须是非空有限 JSON object")
    _validate_complete_label_contract(contract, config)
    return contract


def _validate_complete_label_contract(
    contract: Mapping[str, object], config: ProbabilityConfig,
) -> None:
    label_version = contract.get("label_version")
    cost_version = contract.get("cost_model_version")
    if label_version != PROBABILITY_LABEL_VERSION:
        raise ValueError("上涨概率 label_contract label_version 不一致")
    if cost_version != config.cost_model_version:
        raise ValueError("上涨概率 label_contract cost_model_version 不一致")
    required = (
        "execution_model",
        "horizons",
        "target_definitions",
        "cost_profile_id",
        "execution_notional",
        "max_daily_participation_rate",
    )
    if any(contract.get(name) is None for name in required):
        raise ValueError("上涨概率 label_contract 缺少完整执行或成本假设")
    _validate_label_contract_semantics(contract, config)


def _validate_label_contract_semantics(
    contract: Mapping[str, object], config: ProbabilityConfig,
) -> None:
    if not isinstance(contract["execution_model"], str) or not contract["execution_model"].strip():
        raise ValueError("上涨概率 label_contract execution_model 无效")
    horizons = contract["horizons"]
    if (
        not isinstance(horizons, list)
        or config.horizon not in horizons
        or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in horizons)
        or len(horizons) != len(set(horizons))
    ):
        raise ValueError("上涨概率 label_contract horizons 无效")
    targets = contract["target_definitions"]
    if not isinstance(targets, list) or not targets or any(
        not isinstance(value, str) or not value.strip() for value in targets
    ):
        raise ValueError("上涨概率 label_contract target_definitions 无效")
    if not isinstance(contract["cost_profile_id"], str) or not contract["cost_profile_id"].strip():
        raise ValueError("上涨概率 label_contract cost_profile_id 无效")
    _validate_label_contract_capacity(contract)


def _validate_label_contract_capacity(contract: Mapping[str, object]) -> None:
    notional = contract["execution_notional"]
    participation = contract["max_daily_participation_rate"]
    if (
        isinstance(notional, bool)
        or not isinstance(notional, int | float)
        or not math.isfinite(float(notional))
        or float(notional) <= 0
    ):
        raise ValueError("上涨概率 label_contract execution_notional 无效")
    if (
        isinstance(participation, bool)
        or not isinstance(participation, int | float)
        or not math.isfinite(float(participation))
        or not 0 < float(participation) <= 1
    ):
        raise ValueError("上涨概率 label_contract max_daily_participation_rate 无效")


def _split_contract(config: ProbabilityConfig) -> dict[str, object]:
    return {
        "version": PROBABILITY_SPLIT_VERSION,
        "group": "session_date",
        "random_split_forbidden": True,
        "minimum_train_sessions": config.minimum_train_sessions,
        "minimum_calibration_sessions": config.minimum_calibration_sessions,
        "minimum_test_sessions": config.minimum_test_sessions,
        "gap_sessions": config.effective_gap_sessions,
        "walk_forward": "expanding_train_rolling_calibration_and_test",
    }


def _evaluation_contract(config: ProbabilityConfig) -> dict[str, object]:
    return {
        "minimum_label_coverage": config.minimum_label_coverage,
        "minimum_bin_sessions": config.minimum_bin_sessions,
        "calibration_bin_count": config.calibration_bin_count,
        "minimum_isotonic_calibration_sessions": config.minimum_isotonic_calibration_sessions,
        "bootstrap": "deterministic_circular_moving_session_block_95pct_v1",
        "bootstrap_block_length_sessions": max(1, config.horizon),
        "bootstrap_samples": config.bootstrap_samples,
        "minimum_selection_folds": config.minimum_selection_folds,
        "selection_qualification": {
            "requires_complete_label_contract_binding": True,
            "requires_positive_oos_brier_skill": True,
            "requires_effective_probability_stratification": True,
            "requires_multiple_complete_oos_folds": True,
            "requires_positive_skill_in_every_complete_oos_fold": True,
        },
        "probability_when_insufficient": None,
        "production_ranking_effect": "none",
        "automatic_promotion": False,
    }


def _target_definition(config: ProbabilityConfig) -> str:
    return (
        f"future_{config.horizon}d_net_excess_return_gt_0_after_costs"
        if config.target == "net_excess_positive"
        else f"future_{config.horizon}d_net_return_gt_0_after_costs"
    )


def _split_at_endpoint(
    dates: tuple[str, ...], endpoint: int, config: ProbabilityConfig,
) -> GroupedWalkForwardSplit:
    gap = config.effective_gap_sessions
    test_start = endpoint - config.minimum_test_sessions
    second_gap_start = test_start - gap
    calibration_start = second_gap_start - config.minimum_calibration_sessions
    first_gap_start = calibration_start - gap
    return GroupedWalkForwardSplit(
        train_dates=dates[:first_gap_start],
        train_gap_dates=dates[first_gap_start:calibration_start],
        calibration_dates=dates[calibration_start:second_gap_start],
        calibration_gap_dates=dates[second_gap_start:test_start],
        test_dates=dates[test_start:endpoint],
    )


def _prepare_study(samples: Sequence[ProbabilitySample], config: ProbabilityConfig) -> _PreparedStudy:
    values = tuple(samples)
    feature_names = _validate_samples(values)
    eligible = tuple(item for item in values if item.executable and item.target is not None)
    covered = sum(item.target is not None for item in values)
    coverage = covered / len(values) if values else 0.0
    splits = grouped_walk_forward_splits([item.session_date for item in eligible], config)
    return _PreparedStudy(
        samples=values,
        eligible=eligible,
        feature_names=feature_names,
        input_digest=stable_probability_hash([_sample_payload(item) for item in values]),
        label_coverage=coverage,
        splits=splits,
    )


def _validate_samples(samples: tuple[ProbabilitySample, ...]) -> tuple[str, ...]:
    identifiers: set[str] = set()
    expected: tuple[str, ...] | None = None
    for item in samples:
        if not item.sample_id or item.sample_id in identifiers:
            raise ValueError("上涨概率 sample_id 不能为空或重复")
        identifiers.add(item.sample_id)
        _validated_date(item.session_date)
        if not isinstance(item.executable, bool):
            raise ValueError("上涨概率 executable 必须是布尔值")
        names = tuple(sorted(item.features))
        _validate_feature_names(names)
        if expected is None:
            expected = names
        if not names or names != expected:
            raise ValueError("上涨概率所有样本必须具有相同且非空的特征集合")
        for name in names:
            _finite_number(item.features[name], f"features.{name}")
        _validated_target(item.target)
        _validate_optional_return(item.net_return, "net_return")
        _validate_optional_return(item.net_excess_return, "net_excess_return")
    return expected or ()


def _validate_feature_names(names: Sequence[str]) -> None:
    for name in names:
        normalized = name.strip().lower()
        if (
            not normalized
            or normalized in _FORBIDDEN_FEATURE_NAMES
            or normalized.startswith(_FORBIDDEN_FEATURE_PREFIXES)
        ):
            raise ValueError(f"上涨概率包含禁止或无效特征：{name}")


def _validated_target(value: int | bool | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in (0, 1):
        return value
    raise ValueError("上涨概率 target 必须是 0、1 或 None")


def _validate_optional_return(value: float | None, label: str) -> None:
    if value is not None:
        _finite_number(value, label)


def _sample_payload(item: ProbabilitySample) -> dict[str, object]:
    return {
        "sample_id": item.sample_id,
        "session_date": _validated_date(item.session_date),
        "features": {name: float(item.features[name]) for name in sorted(item.features)},
        "target": _validated_target(item.target),
        "executable": item.executable,
        "net_return": item.net_return,
        "net_excess_return": item.net_excess_return,
    }


def _initial_insufficiency_reasons(prepared: _PreparedStudy, config: ProbabilityConfig) -> list[str]:
    reasons: list[str] = []
    if not prepared.samples:
        reasons.append("no_observations")
    if prepared.label_coverage < config.minimum_label_coverage:
        reasons.append("minimum_label_coverage")
    if not prepared.splits:
        reasons.append("minimum_independent_sessions")
    return reasons


def _partition_samples(
    samples: Sequence[ProbabilitySample], split: GroupedWalkForwardSplit,
) -> dict[str, tuple[ProbabilitySample, ...]]:
    date_sets = {
        "train": frozenset(split.train_dates),
        "calibration": frozenset(split.calibration_dates),
        "test": frozenset(split.test_dates),
    }
    return {
        name: tuple(item for item in samples if item.session_date in dates)
        for name, dates in date_sets.items()
    }


def _class_diversity_reasons(
    partitions: Mapping[str, Sequence[ProbabilitySample]],
) -> list[str]:
    reasons: list[str] = []
    for name in ("train", "calibration"):
        labels = {_validated_target(item.target) for item in partitions[name]}
        if labels != {0, 1}:
            reasons.append(f"{name}_class_diversity")
    return reasons


def _fit_artifacts(
    partitions: Mapping[str, Sequence[ProbabilitySample]],
    feature_names: tuple[str, ...],
    config: ProbabilityConfig,
) -> _FittedArtifacts:
    model = _fit_logistic_model(partitions["train"], feature_names, config)
    raw_calibration = [_model_probability(model, item.features) for item in partitions["calibration"]]
    labels = [_required_label(item) for item in partitions["calibration"]]
    calibrator = _fit_platt_calibrator(raw_calibration, labels, config)
    calibration_sessions = len({item.session_date for item in partitions["calibration"]})
    isotonic = (
        _fit_isotonic_calibrator(raw_calibration, labels)
        if calibration_sessions >= config.minimum_isotonic_calibration_sessions
        else None
    )
    baseline = fit_empirical_bayes_baseline(
        raw_calibration,
        labels,
        bin_count=config.empirical_bayes_bin_count,
        prior_strength=config.empirical_bayes_prior_strength,
    )
    return _FittedArtifacts(
        model=model,
        calibrator=calibrator,
        isotonic_calibrator=isotonic,
        baseline=baseline,
        base_rate=sum(labels) / len(labels),
    )


def _fit_logistic_model(
    samples: Sequence[ProbabilitySample], feature_names: tuple[str, ...], config: ProbabilityConfig,
) -> dict[str, object]:
    matrix = np.asarray([[float(item.features[name]) for name in feature_names] for item in samples], dtype=np.float64)
    labels = np.asarray([_required_label(item) for item in samples], dtype=np.float64)
    means = matrix.mean(axis=0)
    scales = matrix.std(axis=0)
    scales = np.where(scales > 1e-12, scales, 1.0)
    standardized = (matrix - means) / scales
    design = np.column_stack((np.ones(len(samples), dtype=np.float64), standardized))
    weights, iterations = _newton_logistic(design, labels, config.l2_strength, config)
    return {
        "version": PROBABILITY_MODEL_VERSION,
        "feature_names": list(feature_names),
        "means": means.tolist(),
        "scales": scales.tolist(),
        "intercept": float(weights[0]),
        "coefficients": weights[1:].tolist(),
        "l2_strength": config.l2_strength,
        "iterations": iterations,
    }


def _newton_logistic(
    design: NDArray[np.float64], labels: NDArray[np.float64], l2_strength: float, config: ProbabilityConfig,
) -> tuple[NDArray[np.float64], int]:
    base_rate = (float(labels.sum()) + 0.5) / (len(labels) + 1.0)
    weights = np.zeros(design.shape[1], dtype=np.float64)
    weights[0] = math.log(base_rate / (1.0 - base_rate))
    regularizer = np.eye(design.shape[1], dtype=np.float64) * (l2_strength / len(labels))
    regularizer[0, 0] = 1e-12
    for iteration in range(1, config.maximum_iterations + 1):
        probabilities = _sigmoid_array(design @ weights)
        gradient = design.T @ (probabilities - labels) / len(labels) + regularizer @ weights
        variance = np.maximum(probabilities * (1.0 - probabilities), 1e-9)
        hessian = design.T @ (design * variance[:, None]) / len(labels) + regularizer
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError as exc:
            raise ValueError("上涨概率逻辑回归矩阵不可解") from exc
        weights -= step
        if float(np.max(np.abs(step))) <= config.convergence_tolerance:
            return weights, iteration
    return weights, config.maximum_iterations


def _fit_platt_calibrator(
    raw_probabilities: Sequence[float], labels: Sequence[int], config: ProbabilityConfig,
) -> dict[str, object]:
    logits = np.asarray([_logit(value) for value in raw_probabilities], dtype=np.float64)
    design = np.column_stack((np.ones(len(logits), dtype=np.float64), logits))
    targets = np.asarray(labels, dtype=np.float64)
    weights, iterations = _newton_logistic(design, targets, 1e-6, config)
    return {
        "version": PROBABILITY_CALIBRATOR_VERSION,
        "intercept": float(weights[0]),
        "slope": float(weights[1]),
        "iterations": iterations,
        "fit_partition": "calibration_only",
    }


def _fit_isotonic_calibrator(
    raw_probabilities: Sequence[float], labels: Sequence[int],
) -> dict[str, object]:
    """Fit deterministic weighted PAV blocks on the independent calibration partition."""
    grouped: list[list[float]] = []
    for score, label in sorted(zip(raw_probabilities, labels, strict=True)):
        _require_probability(score, "isotonic raw probability")
        if grouped and math.isclose(grouped[-1][1], score, rel_tol=0, abs_tol=1e-15):
            grouped[-1][2] += 1.0
            grouped[-1][3] += float(label)
        else:
            grouped.append([score, score, 1.0, float(label)])
    blocks: list[list[float]] = []
    for group in grouped:
        blocks.append(group)
        while len(blocks) >= 2 and _isotonic_rate(blocks[-2]) > _isotonic_rate(blocks[-1]):
            right = blocks.pop()
            left = blocks.pop()
            blocks.append([left[0], right[1], left[2] + right[2], left[3] + right[3]])
    return {
        "version": PROBABILITY_ISOTONIC_CALIBRATOR_VERSION,
        "algorithm": "weighted_pool_adjacent_violators",
        "upper_bounds": [block[1] for block in blocks],
        "probabilities": [_isotonic_rate(block) for block in blocks],
        "counts": [int(block[2]) for block in blocks],
        "fit_partition": "calibration_only",
    }


def _isotonic_rate(block: Sequence[float]) -> float:
    return block[3] / block[2]


def _test_predictions(
    samples: Sequence[ProbabilitySample],
    artifacts: _FittedArtifacts,
    feature_names: tuple[str, ...],
    *,
    fold_id: int,
) -> list[dict[str, object]]:
    predictions: list[dict[str, object]] = []
    for item in sorted(samples, key=lambda value: (value.session_date, value.sample_id)):
        raw = _model_probability(artifacts.model, item.features)
        predictions.append(
            {
                "sample_id": item.sample_id,
                "session_date": item.session_date,
                "fold_id": fold_id,
                "features": {name: float(item.features[name]) for name in feature_names},
                "outcome": _required_label(item),
                "reference_base_rate": artifacts.base_rate,
                "raw_probability": raw,
                "probability": _platt_probability(artifacts.calibrator, raw),
                "isotonic_probability": (
                    _isotonic_probability(artifacts.isotonic_calibrator, raw)
                    if artifacts.isotonic_calibrator is not None
                    else None
                ),
                "baseline_probability": _baseline_probability(artifacts.baseline, raw),
                "net_return": item.net_return,
                "net_excess_return": item.net_excess_return,
            }
        )
    return predictions


def _prediction_metrics(
    predictions: Sequence[Mapping[str, object]], config: ProbabilityConfig, seed: str,
) -> dict[str, object]:
    probabilities = [_finite_number(item["probability"], "probability") for item in predictions]
    baseline = [_finite_number(item["baseline_probability"], "baseline_probability") for item in predictions]
    outcomes = [_integer(item["outcome"], "outcome") for item in predictions]
    dates = [str(item["session_date"]) for item in predictions]
    references = [
        _finite_number(item["reference_base_rate"], "reference_base_rate")
        for item in predictions
    ]
    base_rate = sum(references) / len(references)
    calibrated = evaluate_probability_predictions(
        probabilities,
        outcomes,
        dates,
        base_rate=base_rate,
        bin_count=config.calibration_bin_count,
        reference_probabilities=references,
    )
    baseline_metrics = evaluate_probability_predictions(
        baseline,
        outcomes,
        dates,
        base_rate=base_rate,
        bin_count=config.calibration_bin_count,
        reference_probabilities=references,
    )
    residuals = [(day, outcome - probability) for day, outcome, probability in zip(dates, outcomes, probabilities, strict=True)]
    losses = [(day, (outcome - probability) ** 2) for day, outcome, probability in zip(dates, outcomes, probabilities, strict=True)]
    targets = list(zip(dates, [float(value) for value in outcomes], strict=True))
    block_length = max(1, config.horizon)
    calibrated["calibration_offset_ci_95"] = _date_block_bootstrap_ci(
        residuals, seed + ":offset", config.bootstrap_samples,
        block_length_sessions=block_length,
    )
    calibrated["brier_score_ci_95"] = _date_block_bootstrap_ci(
        losses, seed + ":brier", config.bootstrap_samples,
        block_length_sessions=block_length,
    )
    calibrated["actual_positive_rate_ci_95"] = _date_block_bootstrap_ci(
        targets, seed + ":rate", config.bootstrap_samples,
        block_length_sessions=block_length,
    )
    calibrated["bootstrap_samples"] = config.bootstrap_samples
    calibrated["bootstrap_method"] = "deterministic_circular_moving_session_block_95pct_v1"
    calibrated["bootstrap_block_length_sessions"] = block_length
    isotonic_metrics = _optional_candidate_metrics(
        predictions, outcomes, dates, base_rate, references, config,
    )
    return {
        "calibrated": calibrated,
        "empirical_bayes_baseline": baseline_metrics,
        "isotonic_candidate": isotonic_metrics,
        "fold_stability": _fold_selection_stability(predictions),
    }


def _fold_selection_stability(
    predictions: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    grouped: dict[int, list[Mapping[str, object]]] = defaultdict(list)
    for item in predictions:
        grouped[_integer(item.get("fold_id"), "fold_id")].append(item)
    folds: list[dict[str, object]] = []
    for fold_id, rows in sorted(grouped.items()):
        losses = [
            (_integer(item["outcome"], "outcome") - _finite_number(item["probability"], "probability")) ** 2
            for item in rows
        ]
        references = [
            (_integer(item["outcome"], "outcome") - _finite_number(
                item["reference_base_rate"], "reference_base_rate",
            )) ** 2
            for item in rows
        ]
        brier = sum(losses) / len(losses)
        reference = sum(references) / len(references)
        skill = None if reference <= 0 else 1.0 - brier / reference
        folds.append(
            {
                "fold_id": fold_id,
                "observation_count": len(rows),
                "independent_session_count": len({str(item["session_date"]) for item in rows}),
                "brier_score": brier,
                "reference_brier_score": reference,
                "brier_skill_score": skill,
                "positive_brier_skill": skill is not None and skill > 0,
            }
        )
    return {
        "version": "complete-oos-fold-brier-stability-v1",
        "fold_count": len(folds),
        "all_folds_positive_brier_skill": bool(folds) and all(
            item["positive_brier_skill"] is True for item in folds
        ),
        "folds": folds,
    }


def _optional_candidate_metrics(
    predictions: Sequence[Mapping[str, object]],
    outcomes: Sequence[int],
    dates: Sequence[str],
    base_rate: float,
    reference_probabilities: Sequence[float],
    config: ProbabilityConfig,
) -> dict[str, object] | None:
    values = [item.get("isotonic_probability") for item in predictions]
    if not values or any(value is None for value in values):
        return None
    return evaluate_probability_predictions(
        [_finite_number(value, "isotonic probability") for value in values],
        outcomes,
        dates,
        base_rate=base_rate,
        bin_count=config.calibration_bin_count,
        reference_probabilities=reference_probabilities,
    )


def _metric_insufficiency_reasons(metrics: Mapping[str, object], config: ProbabilityConfig) -> list[str]:
    calibrated = _object_mapping(metrics.get("calibrated"), "metrics.calibrated")
    bins = cast(Sequence[Mapping[str, object]], calibrated.get("calibration_bins"))
    if any(
        _integer(item["independent_session_count"], "independent_session_count") < config.minimum_bin_sessions
        for item in bins
    ):
        return ["minimum_probability_bin_sessions"]
    return []


def _selection_qualification(
    metrics: Mapping[str, object], fold_count: int, config: ProbabilityConfig,
) -> dict[str, object]:
    calibrated = _object_mapping(metrics.get("calibrated"), "metrics.calibrated")
    bins = cast(Sequence[Mapping[str, object]], calibrated.get("calibration_bins"))
    brier_skill = calibrated.get("brier_skill_score")
    positive_skill = (
        not isinstance(brier_skill, bool)
        and isinstance(brier_skill, int | float)
        and math.isfinite(float(brier_skill))
        and float(brier_skill) > 0
    )
    effective_stratification = bool(
        len(bins) >= 2
        and calibrated.get("bin_monotonic") is True
        and calibrated.get("highest_bin_above_base_rate") is True
        and all(
            _integer(item["independent_session_count"], "independent_session_count")
            >= config.minimum_bin_sessions
            for item in bins
        )
    )
    stability = _object_mapping(metrics.get("fold_stability"), "metrics.fold_stability")
    stable_across_folds = bool(
        fold_count >= config.minimum_selection_folds
        and stability.get("fold_count") == fold_count
        and stability.get("all_folds_positive_brier_skill") is True
    )
    gates = {
        "complete_label_contract_bound": config.label_contract is not None,
        "positive_oos_brier_skill": positive_skill,
        "effective_probability_stratification": effective_stratification,
        "multiple_complete_oos_folds": fold_count >= config.minimum_selection_folds,
        "stable_positive_skill_across_complete_oos_folds": stable_across_folds,
    }
    return {
        "version": "market-scan-probability-selection-gates-v1",
        "passed": all(gates.values()),
        "gates": gates,
        "minimum_complete_oos_folds": config.minimum_selection_folds,
        "evaluated_complete_oos_folds": fold_count,
    }


def _complete_evidence(
    prepared: _PreparedStudy,
    config: ProbabilityConfig,
    generated_at: str,
    folds: Sequence[_EvaluatedFold],
    predictions: list[dict[str, object]],
    metrics: dict[str, object],
    reasons: list[str],
) -> dict[str, object]:
    if not folds:
        raise ValueError("完整概率证据至少需要一个完成折")
    final_fold = folds[-1]
    split = final_fold.split
    artifacts = final_fold.artifacts
    status: ProbabilityStatus = "insufficient_data" if reasons else "calibrated_shadow"
    calibrated = _object_mapping(metrics["calibrated"], "metrics.calibrated")
    selection = _selection_qualification(metrics, len(folds), config)
    evidence = _base_evidence(prepared, config, generated_at, reasons)
    evidence.update(
        {
            "status": status,
            "fit_status": "fitted_oos",
            "selection_qualified": status == "calibrated_shadow" and selection["passed"] is True,
            "selection_qualification": selection,
            "base_rate": artifacts.base_rate,
            "confidence_interval": calibrated.get("actual_positive_rate_ci_95"),
            "split": _split_payload(split),
            "counts": _partition_counts(
                prepared, split, evaluated_folds=folds, predictions=predictions,
            ),
            "training_cutoff": split.train_dates[-1],
            "model": artifacts.model,
            "calibrator": artifacts.calibrator,
            "isotonic_calibrator": artifacts.isotonic_calibrator,
            "empirical_bayes_baseline": artifacts.baseline,
            "calibration_metrics": metrics,
            "calibration_candidates": _calibrator_candidate_records(
                artifacts, metrics, len(split.calibration_dates), config,
            ),
            "folds": [_fold_payload(fold) for fold in folds],
            "predictions": predictions,
            "model_digest": stable_probability_hash(artifacts.model),
            "calibrator_digest": stable_probability_hash(artifacts.calibrator),
            "isotonic_calibrator_digest": (
                stable_probability_hash(artifacts.isotonic_calibrator)
                if artifacts.isotonic_calibrator is not None
                else None
            ),
            "baseline_digest": stable_probability_hash(artifacts.baseline),
        }
    )
    return _with_evidence_digest(evidence)


def _insufficient_evidence(
    prepared: _PreparedStudy,
    config: ProbabilityConfig,
    generated_at: str,
    reasons: Sequence[str],
    *,
    split: GroupedWalkForwardSplit | None = None,
) -> dict[str, object]:
    evidence = _base_evidence(prepared, config, generated_at, reasons)
    evidence.update(
        {
            "status": "insufficient_data",
            "fit_status": "not_fitted",
            "selection_qualified": False,
            "selection_qualification": None,
            "split": _split_payload(split) if split else None,
            "counts": _partition_counts(prepared, split),
            "training_cutoff": split.train_dates[-1] if split else None,
            "model": None,
            "calibrator": None,
            "isotonic_calibrator": None,
            "empirical_bayes_baseline": None,
            "calibration_metrics": None,
            "calibration_candidates": _unfitted_calibrator_candidates(config),
            "folds": [],
            "predictions": [],
            "model_digest": None,
            "calibrator_digest": None,
            "isotonic_calibrator_digest": None,
            "baseline_digest": None,
        }
    )
    return _with_evidence_digest(evidence)


def _calibrator_candidate_records(
    artifacts: _FittedArtifacts,
    metrics: Mapping[str, object],
    calibration_sessions: int,
    config: ProbabilityConfig,
) -> list[dict[str, object]]:
    isotonic = artifacts.isotonic_calibrator
    return [
        {
            "id": "platt",
            "version": PROBABILITY_CALIBRATOR_VERSION,
            "status": "evaluated_primary",
            "selected_for_display": True,
            "parameters": dict(artifacts.calibrator),
            "metrics": metrics.get("calibrated"),
        },
        {
            "id": "isotonic",
            "version": PROBABILITY_ISOTONIC_CALIBRATOR_VERSION,
            "status": "evaluated_shadow_candidate" if isotonic is not None else "not_evaluated_insufficient_sessions",
            "selected_for_display": False,
            "eligibility": {
                "calibration_session_count": calibration_sessions,
                "minimum_calibration_session_count": config.minimum_isotonic_calibration_sessions,
            },
            "parameters": dict(isotonic) if isotonic is not None else None,
            "metrics": metrics.get("isotonic_candidate"),
        },
    ]


def _unfitted_calibrator_candidates(config: ProbabilityConfig) -> list[dict[str, object]]:
    return [
        {
            "id": "platt",
            "version": PROBABILITY_CALIBRATOR_VERSION,
            "status": "not_evaluated_study_insufficient",
            "selected_for_display": True,
            "parameters": None,
            "metrics": None,
        },
        {
            "id": "isotonic",
            "version": PROBABILITY_ISOTONIC_CALIBRATOR_VERSION,
            "status": "not_evaluated_study_insufficient",
            "selected_for_display": False,
            "eligibility": {
                "calibration_session_count": 0,
                "minimum_calibration_session_count": config.minimum_isotonic_calibration_sessions,
            },
            "parameters": None,
            "metrics": None,
        },
    ]


def _base_evidence(
    prepared: _PreparedStudy, config: ProbabilityConfig, generated_at: str, reasons: Sequence[str],
) -> dict[str, object]:
    return {
        "schema_version": PROBABILITY_SCHEMA_VERSION,
        "status": "insufficient_data",
        "fit_status": "not_fitted",
        "selection_qualified": False,
        "selection_qualification": None,
        "probability": None,
        "horizon": config.horizon,
        "target_definition": _target_definition(config),
        "base_rate": None,
        "confidence_interval": None,
        "model_version": PROBABILITY_MODEL_VERSION,
        "feature_version": PROBABILITY_FEATURE_VERSION,
        "label_version": PROBABILITY_LABEL_VERSION,
        "cost_model_version": config.cost_model_version,
        "label_contract_digest": stable_probability_hash(_bound_label_contract(config)),
        "label_contract_binding": (
            "complete" if config.label_contract is not None else "legacy_version_only"
        ),
        "generated_at": generated_at,
        "input_digest": prepared.input_digest,
        "contract": build_probability_contract(config),
        "limitations": list(dict.fromkeys([*reasons, "shadow_only_no_production_ranking_effect"])),
    }


def _partition_counts(
    prepared: _PreparedStudy,
    split: GroupedWalkForwardSplit | None,
    *,
    evaluated_folds: Sequence[_EvaluatedFold] = (),
    predictions: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    if split is None:
        train_count = calibration_count = test_count = 0
    else:
        train_count = len(split.train_dates)
        calibration_count = len(split.calibration_dates)
        test_count = len(split.test_dates)
    oos_dates = {str(item.get("session_date") or "") for item in predictions}
    available_dates = {item.session_date for item in prepared.eligible}
    final_test_date = evaluated_folds[-1].split.test_dates[-1] if evaluated_folds else None
    unused_tail_count = (
        sum(value > final_test_date for value in available_dates)
        if final_test_date is not None
        else len(available_dates)
    )
    return {
        "training_session_count": train_count,
        "calibration_session_count": calibration_count,
        "test_session_count": test_count,
        "available_independent_session_count": len({item.session_date for item in prepared.eligible}),
        "observation_count": len(prepared.samples),
        "eligible_observation_count": len(prepared.eligible),
        "label_coverage": prepared.label_coverage,
        "walk_forward_fold_count": len(prepared.splits),
        "evaluated_fold_count": len(evaluated_folds),
        "out_of_sample_session_count": len(oos_dates),
        "out_of_sample_observation_count": len(predictions),
        "unused_tail_session_count": unused_tail_count,
    }


def _fold_payload(fold: _EvaluatedFold) -> dict[str, object]:
    artifacts = fold.artifacts
    payload: dict[str, object] = {
        "fold_id": fold.fold_id,
        "split": _split_payload(fold.split),
        "training_cutoff": fold.split.train_dates[-1],
        "base_rate": artifacts.base_rate,
        "model": artifacts.model,
        "calibrator": artifacts.calibrator,
        "isotonic_calibrator": artifacts.isotonic_calibrator,
        "empirical_bayes_baseline": artifacts.baseline,
        "model_digest": stable_probability_hash(artifacts.model),
        "calibrator_digest": stable_probability_hash(artifacts.calibrator),
        "isotonic_calibrator_digest": (
            stable_probability_hash(artifacts.isotonic_calibrator)
            if artifacts.isotonic_calibrator is not None
            else None
        ),
        "baseline_digest": stable_probability_hash(artifacts.baseline),
        "prediction_count": len(fold.predictions),
        "test_session_count": len(fold.split.test_dates),
    }
    payload["fold_digest"] = stable_probability_hash(payload)
    return payload


def _split_payload(split: GroupedWalkForwardSplit | None) -> dict[str, object] | None:
    if split is None:
        return None
    return {
        "train_dates": list(split.train_dates),
        "train_gap_dates": list(split.train_gap_dates),
        "calibration_dates": list(split.calibration_dates),
        "calibration_gap_dates": list(split.calibration_gap_dates),
        "test_dates": list(split.test_dates),
    }


def _with_evidence_digest(evidence: dict[str, object]) -> dict[str, object]:
    output = dict(evidence)
    output["evidence_digest"] = stable_probability_hash(output)
    return output


def _model_probability(model: Mapping[str, object], features: Mapping[str, float]) -> float:
    names = cast(Sequence[str], model.get("feature_names"))
    if tuple(sorted(features)) != tuple(names):
        raise ValueError("上涨概率预测特征集合与模型不一致")
    means = _number_sequence(model.get("means"), "model.means")
    scales = _number_sequence(model.get("scales"), "model.scales")
    coefficients = _number_sequence(model.get("coefficients"), "model.coefficients")
    if not (len(names) == len(means) == len(scales) == len(coefficients)):
        raise ProbabilityReplayError("上涨概率模型维度损坏")
    linear = _finite_number(model.get("intercept"), "model.intercept")
    for name, mean, scale, coefficient in zip(names, means, scales, coefficients, strict=True):
        value = _finite_number(features[name], f"features.{name}")
        if scale <= 0:
            raise ProbabilityReplayError("上涨概率模型 scale 无效")
        linear += coefficient * (value - mean) / scale
    return _sigmoid(linear)


def _platt_probability(calibrator: Mapping[str, object], raw_probability: float) -> float:
    intercept = _finite_number(calibrator.get("intercept"), "calibrator.intercept")
    slope = _finite_number(calibrator.get("slope"), "calibrator.slope")
    return _sigmoid(intercept + slope * _logit(raw_probability))


def _isotonic_probability(calibrator: Mapping[str, object], raw_probability: float) -> float:
    _require_probability(raw_probability, "isotonic raw probability")
    bounds = _number_sequence(calibrator.get("upper_bounds"), "isotonic.upper_bounds")
    probabilities = _number_sequence(calibrator.get("probabilities"), "isotonic.probabilities")
    if not bounds or len(bounds) != len(probabilities):
        raise ProbabilityReplayError("上涨概率 Isotonic 校准器维度损坏")
    probability = probabilities[min(len(probabilities) - 1, bisect_left(bounds, raw_probability))]
    _require_probability(probability, "isotonic probability")
    return probability


def _baseline_probability(baseline: Mapping[str, object], score: float) -> float:
    boundaries = _number_sequence(baseline.get("boundaries"), "baseline.boundaries")
    probabilities = _number_sequence(baseline.get("probabilities"), "baseline.probabilities")
    if len(probabilities) != len(boundaries) + 1:
        raise ProbabilityReplayError("上涨概率经验贝叶斯分箱维度损坏")
    probability = probabilities[bisect_right(boundaries, score)]
    _require_probability(probability, "baseline probability")
    return probability


def _estimate_payload(
    evidence: Mapping[str, object], sample_id: str, probability: float, raw: float, baseline: float,
) -> dict[str, object]:
    metrics = _object_mapping(evidence.get("calibration_metrics"), "calibration_metrics")
    calibrated = _object_mapping(metrics.get("calibrated"), "calibration_metrics.calibrated")
    offset = _number_sequence(calibrated.get("calibration_offset_ci_95"), "calibration_offset_ci_95")
    interval = [_clamp_probability(probability + offset[0]), _clamp_probability(probability + offset[1])]
    return {
        "status": "calibrated_shadow",
        "sample_id": sample_id,
        "probability": probability,
        "raw_probability": raw,
        "empirical_bayes_probability": baseline,
        "confidence_interval": interval,
        "confidence_interval_definition": "test_session_block_bootstrap_calibration_offset_95pct",
        "horizon": evidence.get("horizon"),
        "target_definition": evidence.get("target_definition"),
        "base_rate": evidence.get("base_rate"),
        "counts": evidence.get("counts"),
        "calibration_metrics": evidence.get("calibration_metrics"),
        "model_version": evidence.get("model_version"),
        "feature_version": evidence.get("feature_version"),
        "label_version": evidence.get("label_version"),
        "cost_model_version": evidence.get("cost_model_version"),
        "label_contract_digest": evidence.get("label_contract_digest"),
        "selection_qualified": evidence.get("selection_qualified") is True,
        "selection_qualification": evidence.get("selection_qualification"),
        "training_cutoff": evidence.get("training_cutoff"),
        "model_digest": evidence.get("model_digest"),
        "calibrator_digest": evidence.get("calibrator_digest"),
        "input_digest": evidence.get("input_digest"),
        "limitations": [
            "shadow_only_no_production_ranking_effect",
            "confidence_interval_is_calibration_uncertainty_not_individual_outcome_certainty",
        ],
        "generated_at": evidence.get("generated_at"),
    }


def _null_estimate(evidence: Mapping[str, object], sample_id: str) -> dict[str, object]:
    return {
        "status": "insufficient_data",
        "sample_id": sample_id,
        "probability": None,
        "confidence_interval": None,
        "horizon": evidence.get("horizon"),
        "target_definition": evidence.get("target_definition"),
        "base_rate": evidence.get("base_rate"),
        "counts": evidence.get("counts"),
        "model_version": evidence.get("model_version"),
        "feature_version": evidence.get("feature_version"),
        "label_version": evidence.get("label_version"),
        "cost_model_version": evidence.get("cost_model_version"),
        "label_contract_digest": evidence.get("label_contract_digest"),
        "selection_qualified": False,
        "selection_qualification": evidence.get("selection_qualification"),
        "training_cutoff": evidence.get("training_cutoff"),
        "limitations": evidence.get("limitations"),
        "generated_at": evidence.get("generated_at"),
    }


def _verify_evidence_digest(evidence: Mapping[str, object]) -> None:
    expected = evidence.get("evidence_digest")
    unsigned = {key: value for key, value in evidence.items() if key != "evidence_digest"}
    if not isinstance(expected, str) or expected != stable_probability_hash(unsigned):
        raise ProbabilityReplayError("上涨概率 evidence_digest 不一致")


def _verify_registered_evidence(evidence: Mapping[str, object], config: ProbabilityConfig) -> None:
    contract = _object_mapping(evidence.get("contract"), "contract")
    if dict(contract) != build_probability_contract(config):
        raise ProbabilityReplayError("上涨概率契约不是已注册版本")
    expected = {
        "schema_version": PROBABILITY_SCHEMA_VERSION,
        "model_version": PROBABILITY_MODEL_VERSION,
        "feature_version": PROBABILITY_FEATURE_VERSION,
        "label_version": PROBABILITY_LABEL_VERSION,
        "cost_model_version": config.cost_model_version,
        "label_contract_digest": stable_probability_hash(_bound_label_contract(config)),
        "label_contract_binding": (
            "complete" if config.label_contract is not None else "legacy_version_only"
        ),
        "target_definition": _target_definition(config),
    }
    if any(evidence.get(name) != value for name, value in expected.items()):
        raise ProbabilityReplayError("上涨概率顶层版本或标签口径不受支持")
    if evidence.get("status") not in ("insufficient_data", "calibrated_shadow"):
        raise ProbabilityReplayError("上涨概率状态不受支持")
    if evidence.get("status") == "calibrated_shadow" and evidence.get("model") is None:
        raise ProbabilityReplayError("上涨概率 calibrated_shadow 缺少已拟合模型")
    if evidence.get("probability") is not None:
        raise ProbabilityReplayError("上涨概率研究批次不能伪装成个股概率")
    if not isinstance(evidence.get("generated_at"), str) or not str(evidence["generated_at"]).strip():
        raise ProbabilityReplayError("上涨概率 generated_at 无效")


def _verify_artifact_digests(evidence: Mapping[str, object]) -> None:
    pairs = (
        ("model", "model_digest"),
        ("calibrator", "calibrator_digest"),
        ("isotonic_calibrator", "isotonic_calibrator_digest"),
        ("empirical_bayes_baseline", "baseline_digest"),
    )
    for payload_name, digest_name in pairs:
        payload, digest = evidence.get(payload_name), evidence.get(digest_name)
        if payload is None and digest is None:
            continue
        if not isinstance(payload, Mapping) or digest != stable_probability_hash(payload):
            raise ProbabilityReplayError(f"上涨概率 {digest_name} 不一致")
    _verify_artifact_versions(evidence)


def _verify_artifact_versions(evidence: Mapping[str, object]) -> None:
    expected = (
        ("model", PROBABILITY_MODEL_VERSION),
        ("calibrator", PROBABILITY_CALIBRATOR_VERSION),
        ("isotonic_calibrator", PROBABILITY_ISOTONIC_CALIBRATOR_VERSION),
        ("empirical_bayes_baseline", PROBABILITY_BASELINE_VERSION),
    )
    for name, version in expected:
        payload = evidence.get(name)
        if payload is not None and _object_mapping(payload, name).get("version") != version:
            raise ProbabilityReplayError(f"上涨概率 {name} 版本不受支持")


def _verify_fold_artifacts(
    evidence: Mapping[str, object], config: ProbabilityConfig,
) -> None:
    value = evidence.get("folds")
    if not isinstance(value, list):
        raise ProbabilityReplayError("上涨概率 folds 必须是数组")
    counts = _object_mapping(evidence.get("counts"), "counts")
    if evidence.get("model") is None:
        _verify_unfitted_folds(value, counts)
        return
    if not value:
        raise ProbabilityReplayError("上涨概率已拟合研究缺少逐折证据")
    folds = [_object_mapping(item, "fold") for item in value]
    _verify_fold_counts(folds, counts)
    splits = [
        _verify_one_fold(fold, expected_fold_id, config)
        for expected_fold_id, fold in enumerate(folds, start=1)
    ]
    _verify_split_sequence(splits, config)
    _verify_active_fold(evidence, folds[-1])


def _verify_unfitted_folds(
    folds: Sequence[object], counts: Mapping[str, object],
) -> None:
    evaluated = _integer(counts.get("evaluated_fold_count"), "evaluated_fold_count")
    if folds or evaluated != 0:
        raise ProbabilityReplayError("上涨概率未拟合研究不应持久化完成折")


def _verify_fold_counts(
    folds: Sequence[Mapping[str, object]], counts: Mapping[str, object],
) -> None:
    evaluated = _integer(counts.get("evaluated_fold_count"), "evaluated_fold_count")
    available = _integer(counts.get("walk_forward_fold_count"), "walk_forward_fold_count")
    if len(folds) != evaluated:
        raise ProbabilityReplayError("上涨概率完成折数量与计数不一致")
    if len(folds) != available:
        raise ProbabilityReplayError("上涨概率 Walk-forward 折未全部评估")


def _verify_one_fold(
    fold: Mapping[str, object], expected_fold_id: int, config: ProbabilityConfig,
) -> GroupedWalkForwardSplit:
    if _integer(fold.get("fold_id"), "fold_id") != expected_fold_id:
        raise ProbabilityReplayError("上涨概率 fold_id 必须连续且从 1 开始")
    _verify_fold_digest(fold)
    split = _split_from_payload(fold.get("split"))
    _verify_complete_split(split, config)
    if fold.get("training_cutoff") != split.train_dates[-1]:
        raise ProbabilityReplayError("上涨概率逐折训练截止日不一致")
    if _integer(fold.get("test_session_count"), "test_session_count") != len(split.test_dates):
        raise ProbabilityReplayError("上涨概率逐折测试日期计数不一致")
    _require_probability(_finite_number(fold.get("base_rate"), "fold.base_rate"), "fold.base_rate")
    _verify_fold_artifact_digests(fold)
    return split


def _verify_split_sequence(
    splits: Sequence[GroupedWalkForwardSplit], config: ProbabilityConfig,
) -> None:
    all_dates = sorted({value for split in splits for value in _all_split_dates(split)})
    if tuple(splits) != grouped_walk_forward_splits(all_dates, config):
        raise ProbabilityReplayError("上涨概率逐折窗口并非完整且无重叠的 grouped-date Walk-forward")
    test_dates = [date_value for split in splits for date_value in split.test_dates]
    if len(test_dates) != len(set(test_dates)):
        raise ProbabilityReplayError("上涨概率逐折测试窗口重叠")


def _verify_active_fold(
    evidence: Mapping[str, object], final: Mapping[str, object],
) -> None:
    top_level_pairs = (
        ("split", "split"),
        ("training_cutoff", "training_cutoff"),
        ("base_rate", "base_rate"),
        ("model", "model"),
        ("calibrator", "calibrator"),
        ("isotonic_calibrator", "isotonic_calibrator"),
        ("empirical_bayes_baseline", "empirical_bayes_baseline"),
        ("model_digest", "model_digest"),
        ("calibrator_digest", "calibrator_digest"),
        ("isotonic_calibrator_digest", "isotonic_calibrator_digest"),
        ("baseline_digest", "baseline_digest"),
    )
    if any(evidence.get(top_name) != final.get(fold_name) for top_name, fold_name in top_level_pairs):
        raise ProbabilityReplayError("上涨概率主展示模型必须等于最后一个完整折")


def _verify_fold_digest(fold: Mapping[str, object]) -> None:
    digest = fold.get("fold_digest")
    unsigned = {key: value for key, value in fold.items() if key != "fold_digest"}
    if not isinstance(digest, str) or digest != stable_probability_hash(unsigned):
        raise ProbabilityReplayError("上涨概率 fold_digest 不一致")


def _verify_fold_artifact_digests(fold: Mapping[str, object]) -> None:
    pairs = (
        ("model", "model_digest", PROBABILITY_MODEL_VERSION),
        ("calibrator", "calibrator_digest", PROBABILITY_CALIBRATOR_VERSION),
        ("isotonic_calibrator", "isotonic_calibrator_digest", PROBABILITY_ISOTONIC_CALIBRATOR_VERSION),
        ("empirical_bayes_baseline", "baseline_digest", PROBABILITY_BASELINE_VERSION),
    )
    for payload_name, digest_name, version in pairs:
        payload, digest = fold.get(payload_name), fold.get(digest_name)
        if payload is None and digest is None and payload_name == "isotonic_calibrator":
            continue
        mapping = _object_mapping(payload, f"fold.{payload_name}")
        if digest != stable_probability_hash(mapping):
            raise ProbabilityReplayError(f"上涨概率逐折 {digest_name} 不一致")
        if mapping.get("version") != version:
            raise ProbabilityReplayError(f"上涨概率逐折 {payload_name} 版本不受支持")


def _split_from_payload(value: object) -> GroupedWalkForwardSplit:
    payload = _object_mapping(value, "fold.split")

    def dates(name: str) -> tuple[str, ...]:
        items = payload.get(name)
        if not isinstance(items, list):
            raise ProbabilityReplayError(f"上涨概率 {name} 必须是日期数组")
        result = tuple(_validated_date(str(item)) for item in items)
        if result != tuple(sorted(set(result))):
            raise ProbabilityReplayError(f"上涨概率 {name} 日期必须唯一且升序")
        return result

    return GroupedWalkForwardSplit(
        train_dates=dates("train_dates"),
        train_gap_dates=dates("train_gap_dates"),
        calibration_dates=dates("calibration_dates"),
        calibration_gap_dates=dates("calibration_gap_dates"),
        test_dates=dates("test_dates"),
    )


def _all_split_dates(split: GroupedWalkForwardSplit) -> tuple[str, ...]:
    return (
        *split.train_dates,
        *split.train_gap_dates,
        *split.calibration_dates,
        *split.calibration_gap_dates,
        *split.test_dates,
    )


def _verify_complete_split(split: GroupedWalkForwardSplit, config: ProbabilityConfig) -> None:
    if len(split.train_dates) < config.minimum_train_sessions:
        raise ProbabilityReplayError("上涨概率逐折训练日期不足")
    expected_lengths = (
        (split.train_gap_dates, config.effective_gap_sessions),
        (split.calibration_dates, config.minimum_calibration_sessions),
        (split.calibration_gap_dates, config.effective_gap_sessions),
        (split.test_dates, config.minimum_test_sessions),
    )
    if any(len(values) != expected for values, expected in expected_lengths):
        raise ProbabilityReplayError("上涨概率逐折 gap、校准或测试窗口不完整")
    dates = _all_split_dates(split)
    if dates != tuple(sorted(set(dates))):
        raise ProbabilityReplayError("上涨概率逐折日期分区重叠或乱序")


def _verify_calibrator_candidate_records(
    evidence: Mapping[str, object], config: ProbabilityConfig,
) -> None:
    records = evidence.get("calibration_candidates")
    if not isinstance(records, list) or len(records) != 2:
        raise ProbabilityReplayError("上涨概率校准候选记录不完整")
    by_id = {
        str(record.get("id")): record
        for record in records
        if isinstance(record, Mapping)
    }
    if set(by_id) != {"platt", "isotonic"}:
        raise ProbabilityReplayError("上涨概率校准候选标识不受支持")
    if evidence.get("model") is None:
        if records != _unfitted_calibrator_candidates(config):
            raise ProbabilityReplayError("上涨概率未拟合候选记录不一致")
        return
    _verify_fitted_calibrator_candidate_records(evidence, config, by_id)


def _verify_fitted_calibrator_candidate_records(
    evidence: Mapping[str, object],
    config: ProbabilityConfig,
    records: Mapping[str, Mapping[str, object]],
) -> None:
    metrics = _object_mapping(evidence.get("calibration_metrics"), "calibration_metrics")
    counts = _object_mapping(evidence.get("counts"), "counts")
    isotonic = evidence.get("isotonic_calibrator")
    platt_record, isotonic_record = records["platt"], records["isotonic"]
    expected_status = "evaluated_shadow_candidate" if isotonic is not None else "not_evaluated_insufficient_sessions"
    eligibility = {
        "calibration_session_count": _integer(counts.get("calibration_session_count"), "calibration_session_count"),
        "minimum_calibration_session_count": config.minimum_isotonic_calibration_sessions,
    }
    checks = (
        platt_record.get("version") == PROBABILITY_CALIBRATOR_VERSION,
        platt_record.get("status") == "evaluated_primary",
        platt_record.get("selected_for_display") is True,
        platt_record.get("parameters") == evidence.get("calibrator"),
        platt_record.get("metrics") == metrics.get("calibrated"),
        isotonic_record.get("version") == PROBABILITY_ISOTONIC_CALIBRATOR_VERSION,
        isotonic_record.get("selected_for_display") is False,
        isotonic_record.get("status") == expected_status,
        isotonic_record.get("eligibility") == eligibility,
        isotonic_record.get("parameters") == isotonic,
        isotonic_record.get("metrics") == metrics.get("isotonic_candidate"),
    )
    if not all(checks):
        raise ProbabilityReplayError("上涨概率校准候选参数或指标不一致")


def _verify_persisted_predictions(evidence: Mapping[str, object]) -> None:
    predictions = evidence.get("predictions")
    if not isinstance(predictions, list):
        raise ProbabilityReplayError("上涨概率 predictions 必须是数组")
    if not predictions:
        counts = _object_mapping(evidence.get("counts"), "counts")
        persisted_count = _integer(
            counts.get("out_of_sample_observation_count"), "out_of_sample_observation_count",
        )
        if evidence.get("model") is not None or persisted_count != 0:
            raise ProbabilityReplayError("上涨概率已拟合研究不能缺少 OOS 预测")
        return
    fold_items = evidence.get("folds")
    if not isinstance(fold_items, list):
        raise ProbabilityReplayError("上涨概率缺少逐折预测模型")
    folds = {
        _integer(fold.get("fold_id"), "fold_id"): fold
        for value in fold_items
        for fold in [_object_mapping(value, "fold")]
    }
    rows = [_object_mapping(item, "prediction") for item in predictions]
    _verify_unique_prediction_ids(rows)
    assignments = [_verify_one_prediction(row, folds) for row in rows]
    _verify_prediction_assignments(assignments, folds)
    session_folds = {session_date for _fold_id, session_date in assignments}
    counts = _object_mapping(evidence.get("counts"), "counts")
    if len(predictions) != _integer(counts.get("out_of_sample_observation_count"), "out_of_sample_observation_count"):
        raise ProbabilityReplayError("上涨概率 OOS 观测数量不一致")
    if len(session_folds) != _integer(counts.get("out_of_sample_session_count"), "out_of_sample_session_count"):
        raise ProbabilityReplayError("上涨概率 OOS 独立日期数量不一致")


def _verify_unique_prediction_ids(rows: Sequence[Mapping[str, object]]) -> None:
    sample_ids = [_nonempty_text(row.get("sample_id"), "prediction.sample_id") for row in rows]
    if len(sample_ids) != len(set(sample_ids)):
        raise ProbabilityReplayError("上涨概率 OOS 预测 sample_id 重复")


def _verify_one_prediction(
    row: Mapping[str, object], folds: Mapping[int, Mapping[str, object]],
) -> tuple[int, str]:
    fold_id = _integer(row.get("fold_id"), "prediction.fold_id")
    fold = folds.get(fold_id)
    if fold is None:
        raise ProbabilityReplayError("上涨概率预测引用未知 fold_id")
    split = _split_from_payload(fold.get("split"))
    session_date = _validated_date(str(row.get("session_date") or ""))
    if session_date not in split.test_dates:
        raise ProbabilityReplayError("上涨概率预测不属于所标记折的测试窗口")
    model = _object_mapping(fold.get("model"), "fold.model")
    calibrator = _object_mapping(fold.get("calibrator"), "fold.calibrator")
    baseline = _object_mapping(fold.get("empirical_bayes_baseline"), "fold.empirical_bayes_baseline")
    features = _object_mapping(row.get("features"), "prediction.features")
    raw = _model_probability(model, cast(Mapping[str, float], features))
    expected = (raw, _platt_probability(calibrator, raw), _baseline_probability(baseline, raw))
    _verify_prediction_values(row, expected)
    _verify_prediction_reference(row, fold)
    isotonic_value = fold.get("isotonic_calibrator")
    isotonic = _object_mapping(isotonic_value, "fold.isotonic_calibrator") if isotonic_value is not None else None
    _verify_isotonic_prediction(row, isotonic, raw)
    return fold_id, session_date


def _verify_prediction_values(
    row: Mapping[str, object], expected: Sequence[float],
) -> None:
    persisted = tuple(
        _finite_number(row.get(name), name)
        for name in ("raw_probability", "probability", "baseline_probability")
    )
    differences = zip(expected, persisted, strict=True)
    if any(not math.isclose(left, right, rel_tol=0, abs_tol=1e-12) for left, right in differences):
        raise ProbabilityReplayError("上涨概率预测无法从模型重放")


def _verify_prediction_reference(
    row: Mapping[str, object], fold: Mapping[str, object],
) -> None:
    reference = _finite_number(row.get("reference_base_rate"), "reference_base_rate")
    base_rate = _finite_number(fold.get("base_rate"), "fold.base_rate")
    if not math.isclose(reference, base_rate, rel_tol=0, abs_tol=1e-12):
        raise ProbabilityReplayError("上涨概率 Brier Skill 参考率不是该折校准期基准率")


def _verify_prediction_assignments(
    assignments: Sequence[tuple[int, str]], folds: Mapping[int, Mapping[str, object]],
) -> None:
    by_session: dict[str, set[int]] = defaultdict(set)
    prediction_counts: dict[int, int] = defaultdict(int)
    for fold_id, session_date in assignments:
        by_session[session_date].add(fold_id)
        prediction_counts[fold_id] += 1
    if any(len(values) != 1 for values in by_session.values()):
        raise ProbabilityReplayError("上涨概率同一交易日股票被分到不同折")
    for fold_id, fold in folds.items():
        expected = _integer(fold.get("prediction_count"), "prediction_count")
        if prediction_counts[fold_id] != expected:
            raise ProbabilityReplayError("上涨概率逐折预测数量不一致")


def _verify_isotonic_prediction(
    row: Mapping[str, object], calibrator: Mapping[str, object] | None, raw: float,
) -> None:
    persisted = row.get("isotonic_probability")
    if calibrator is None:
        if persisted is not None:
            raise ProbabilityReplayError("上涨概率未注册 Isotonic 候选却持久化了预测")
        return
    expected = _isotonic_probability(calibrator, raw)
    if not math.isclose(expected, _finite_number(persisted, "isotonic_probability"), rel_tol=0, abs_tol=1e-12):
        raise ProbabilityReplayError("上涨概率 Isotonic 预测无法重放")


def _verify_persisted_metrics(evidence: Mapping[str, object], config: ProbabilityConfig) -> None:
    predictions = evidence.get("predictions")
    if not isinstance(predictions, list):
        raise ProbabilityReplayError("上涨概率 predictions 必须是数组")
    if not predictions:
        if evidence.get("calibration_metrics") is not None:
            raise ProbabilityReplayError("上涨概率空预测不应带有校准指标")
        return
    rows = [_object_mapping(item, "prediction") for item in predictions]
    seed = evidence.get("input_digest")
    if not isinstance(seed, str) or len(seed) != 64:
        raise ProbabilityReplayError("上涨概率 input_digest 无效")
    expected = _prediction_metrics(rows, config, seed)
    if expected != evidence.get("calibration_metrics"):
        raise ProbabilityReplayError("上涨概率校准指标无法从测试观测重放")


def _verify_selection_qualification(
    evidence: Mapping[str, object], config: ProbabilityConfig,
) -> None:
    metrics = evidence.get("calibration_metrics")
    folds = evidence.get("folds")
    if metrics is None:
        if (
            evidence.get("fit_status") != "not_fitted"
            or evidence.get("selection_qualified") is not False
            or evidence.get("selection_qualification") is not None
        ):
            raise ProbabilityReplayError("上涨概率未拟合证据的选择资格无效")
        return
    if not isinstance(metrics, Mapping) or not isinstance(folds, list):
        raise ProbabilityReplayError("上涨概率选择资格缺少指标或逐折证据")
    expected = _selection_qualification(metrics, len(folds), config)
    if evidence.get("fit_status") != "fitted_oos" or evidence.get("selection_qualification") != expected:
        raise ProbabilityReplayError("上涨概率选择资格无法从样本外指标重放")
    qualified = evidence.get("status") == "calibrated_shadow" and expected["passed"] is True
    if evidence.get("selection_qualified") is not qualified:
        raise ProbabilityReplayError("上涨概率 selection_qualified 与门禁不一致")


def _config_from_evidence(evidence: Mapping[str, object]) -> ProbabilityConfig:
    contract = _object_mapping(evidence.get("contract"), "contract")
    label = _object_mapping(contract.get("label"), "contract.label")
    cost = _object_mapping(contract.get("cost"), "contract.cost")
    model = _object_mapping(contract.get("model"), "contract.model")
    baseline = _object_mapping(contract.get("baseline"), "contract.baseline")
    split = _object_mapping(contract.get("split"), "contract.split")
    evaluation = _object_mapping(contract.get("evaluation"), "contract.evaluation")
    bound_label = _object_mapping(cost.get("label_contract"), "cost.label_contract")
    label_contract = (
        None
        if set(bound_label) == {"label_version", "cost_model_version"}
        else dict(bound_label)
    )
    return ProbabilityConfig(
        horizon=_integer(evidence.get("horizon"), "horizon"),
        target=cast(ProbabilityTarget, label.get("target")),
        cost_model_version=_nonempty_text(cost.get("version"), "cost.version"),
        label_contract=label_contract,
        minimum_train_sessions=_integer(split.get("minimum_train_sessions"), "minimum_train_sessions"),
        minimum_calibration_sessions=_integer(split.get("minimum_calibration_sessions"), "minimum_calibration_sessions"),
        minimum_test_sessions=_integer(split.get("minimum_test_sessions"), "minimum_test_sessions"),
        minimum_label_coverage=_finite_number(evaluation.get("minimum_label_coverage"), "minimum_label_coverage"),
        minimum_bin_sessions=_integer(evaluation.get("minimum_bin_sessions"), "minimum_bin_sessions"),
        minimum_selection_folds=_integer(
            evaluation.get("minimum_selection_folds"), "minimum_selection_folds",
        ),
        minimum_isotonic_calibration_sessions=_integer(
            evaluation.get("minimum_isotonic_calibration_sessions"),
            "minimum_isotonic_calibration_sessions",
        ),
        gap_sessions=_integer(split.get("gap_sessions"), "gap_sessions"),
        calibration_bin_count=_integer(evaluation.get("calibration_bin_count"), "calibration_bin_count"),
        empirical_bayes_bin_count=_integer(baseline.get("bin_count"), "empirical_bayes_bin_count"),
        empirical_bayes_prior_strength=_finite_number(baseline.get("prior_strength"), "prior_strength"),
        l2_strength=_finite_number(model.get("l2_strength"), "l2_strength"),
        bootstrap_samples=_integer(evaluation.get("bootstrap_samples"), "bootstrap_samples"),
        maximum_iterations=_integer(model.get("maximum_iterations"), "maximum_iterations"),
        convergence_tolerance=_finite_number(model.get("convergence_tolerance"), "convergence_tolerance"),
    )


def _required_label(item: ProbabilitySample) -> int:
    label = _validated_target(item.target)
    if label is None:
        raise ValueError("上涨概率训练分区缺少 target")
    return label


def _sigmoid(value: float) -> float:
    bounded = max(-35.0, min(35.0, value))
    return 1.0 / (1.0 + math.exp(-bounded))


def _sigmoid_array(values: NDArray[np.float64]) -> NDArray[np.float64]:
    bounded = np.clip(values, -35.0, 35.0)
    return cast(NDArray[np.float64], 1.0 / (1.0 + np.exp(-bounded)))


def _logit(probability: float) -> float:
    _require_probability(probability, "probability")
    clipped = min(1.0 - 1e-12, max(1e-12, probability))
    return math.log(clipped / (1.0 - clipped))


def _clamp_probability(value: float) -> float:
    return max(0.0, min(1.0, value))


def _require_probability(value: float, label: str) -> None:
    if not 0 <= value <= 1:
        raise ValueError(f"{label} 必须在 [0, 1] 范围内")


def _validated_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("上涨概率 session_date 必须是 ISO 交易日") from exc
    return parsed.isoformat()


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 必须是数值")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"{label} 必须是有限数值")
    return numeric


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} 必须是整数")
    return value


def _nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProbabilityReplayError(f"{label} 必须是非空字符串")
    return value


def _number_sequence(value: object, label: str) -> list[float]:
    if not isinstance(value, (list, tuple)):
        raise ProbabilityReplayError(f"{label} 必须是数组")
    try:
        return [_finite_number(item, label) for item in value]
    except ValueError as exc:
        raise ProbabilityReplayError(str(exc)) from exc


def _object_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProbabilityReplayError(f"{label} 必须是对象")
    return cast(Mapping[str, object], value)


def _canonical_json_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical_json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _canonical_json_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    if isinstance(value, date):
        return value.isoformat()
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("上涨概率证据不能包含非有限数值")
        return value
    raise ValueError(f"上涨概率证据包含不可序列化类型：{type(value).__name__}")


__all__ = [
    "PROBABILITY_BASELINE_VERSION",
    "PROBABILITY_CALIBRATOR_VERSION",
    "PROBABILITY_COST_MODEL_VERSION",
    "PROBABILITY_FEATURE_VERSION",
    "PROBABILITY_ISOTONIC_CALIBRATOR_VERSION",
    "PROBABILITY_LABEL_VERSION",
    "PROBABILITY_MODEL_VERSION",
    "PROBABILITY_SCHEMA_VERSION",
    "GroupedWalkForwardSplit",
    "ProbabilityConfig",
    "ProbabilityReplayError",
    "ProbabilitySample",
    "build_probability_contract",
    "evaluate_probability_predictions",
    "fit_empirical_bayes_baseline",
    "fit_shadow_probability",
    "grouped_walk_forward_splits",
    "probability_selection_qualified",
    "predict_shadow_probability",
    "replay_shadow_probability",
    "stable_probability_hash",
    "verify_shadow_probability_evidence",
]
