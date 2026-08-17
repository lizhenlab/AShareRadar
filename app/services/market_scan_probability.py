"""Deterministic, auditable Shadow probabilities for full-market research.

The module deliberately has no dependency on the production ranking write path.
It consumes already point-in-time labelled observations, applies grouped temporal
splits, fits a regularised model and emits JSON-compatible replay evidence.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from typing import Literal, cast

import numpy as np
from numpy.typing import NDArray

import app.services.market_scan_probability_metrics as probability_metrics
from app.services.joint_execution_probability import (
    joint_execution_probability_action_qualified,
    verify_joint_execution_probability_evidence,
)
from app.utils.clock import utc_now


PROBABILITY_SCHEMA_VERSION = "market-scan-shadow-probability-v4"
PROBABILITY_MODEL_VERSION = "shadow-up-probability-logit-l2-v2-convergence-required"
PROBABILITY_CALIBRATOR_VERSION = "shadow-up-probability-platt-v2-convergence-required"
PROBABILITY_ISOTONIC_CALIBRATOR_VERSION = "shadow-up-probability-isotonic-pav-v1"
PROBABILITY_BASELINE_VERSION = probability_metrics.PROBABILITY_BASELINE_VERSION
PROBABILITY_FEATURE_VERSION = "full-market-point-in-time-features-v3-liquidity-medium"
PROBABILITY_LABEL_VERSION = "market-scan-upside-label-v3-explicit-target-offset"
PROBABILITY_COST_MODEL_VERSION = "ashare-executable-round-trip-cost-v1"
PROBABILITY_SPLIT_VERSION = "grouped-date-multifold-target-offset-purge-v3"
PROBABILITY_FILTER_QUALIFICATION_VERSION = "market-scan-probability-filter-qualification-v1"
PROBABILITY_FILTER_AUTHORIZATION_VERSION = (
    "market-scan-probability-filter-authorization-v3-raw-drift-joint-execution"
)
PROBABILITY_FILTER_AUTHORIZATION_SCHEMA_VERSION = (
    "market-scan-probability-filter-authorization-artifact-v1"
)
PROBABILITY_FILTER_AUTHORIZATION_INTEGRITY_NOTICE = (
    "content_address_only_not_signature_load_from_trusted_research_store"
)
PROBABILITY_DEPLOYMENT_ARTIFACT_SCHEMA_VERSION = (
    "market-scan-probability-deployment-estimator-artifact-v1"
)
PROBABILITY_DEPLOYMENT_CONTRACT_VERSION = (
    "market-scan-probability-deployment-refit-v1"
)
PROBABILITY_DEPLOYMENT_MAXIMUM_AGE_HOURS = 36
SUPERSEDED_PROBABILITY_SCHEMA_VERSIONS = ("market-scan-shadow-probability-v3",)
SUPERSEDED_PROBABILITY_FEATURE_VERSIONS = ("full-market-point-in-time-features-v2",)
SUPERSEDED_PROBABILITY_LABEL_VERSIONS = ("market-scan-upside-label-v2",)
SUPERSEDED_PROBABILITY_SPLIT_VERSIONS = (
    "grouped-date-multifold-train-gap-calibration-gap-test-v2",
)
LEGACY_PROBABILITY_FEATURE_VERSION = SUPERSEDED_PROBABILITY_FEATURE_VERSIONS[0]
ProbabilityStatus = Literal["insufficient_data", "calibrated_shadow"]
ProbabilityTarget = Literal["net_excess_positive", "net_return_positive"]
# The generic grouped-date estimator is also reused by the isolated individual-
# stock D+2/D+3/D+4 research namespace.  Full-market routes and artifacts keep
# their separately validated public 1/5/20 contract.
_SUPPORTED_HORIZONS = frozenset({1, 2, 3, 5, 20})
_FORBIDDEN_FEATURE_NAMES = frozenset(
    {"symbol", "stock_code", "ticker", "rank", "final_rank", "ranking", "target", "outcome", "label"}
)
_FORBIDDEN_FEATURE_PREFIXES = ("future_", "forward_", "next_", "realized_", "observed_")
_EVIDENCE_DIGEST_FIELDS = frozenset(
    {
        "schema_version", "status", "fit_status", "selection_qualified", "selection_qualification",
        "probability", "horizon", "target_definition", "base_rate",
        "actual_positive_rate_interval",
        "model_version", "feature_version", "label_version", "cost_model_version",
        "label_contract_digest", "label_contract_binding", "generated_at", "input_digest",
        "contract", "limitations", "split", "counts", "training_cutoff", "model", "calibrator",
        "isotonic_calibrator", "empirical_bayes_baseline", "calibration_metrics",
        "calibration_candidates", "folds", "predictions", "model_digest", "calibrator_digest",
        "isotonic_calibrator_digest", "baseline_digest",
    }
)

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


class _ProbabilityModelConvergenceError(ValueError):
    """Internal fail-closed signal for an optimizer that did not converge."""


_VERIFIED_AUTHORIZATION_SEAL = object()
_VERIFIED_DEPLOYMENT_SEAL = object()


class VerifiedProbabilityFilterAuthorization(Mapping[str, object]):
    """Opaque result of strict raw-evidence authorization verification.

    Callers cannot authorize filtering by passing an ordinary JSON mapping.  A
    mapping first has to pass the exact-schema, content-address and statistical
    replay checks in :func:`verify_probability_filter_authorization_artifact`.
    """

    __slots__ = ("_encoded_payload", "generated_at", "integrity_digest")

    def __init__(
        self,
        *,
        encoded_payload: str,
        generated_at: str = "",
        integrity_digest: str,
        _seal: object | None = None,
    ) -> None:
        if _seal is not _VERIFIED_AUTHORIZATION_SEAL:
            raise TypeError("authorization token 只能由 strict verifier 创建")
        self._encoded_payload = encoded_payload
        self.generated_at = generated_at
        self.integrity_digest = integrity_digest

    @property
    def payload(self) -> Mapping[str, object]:
        decoded = json.loads(self._encoded_payload)
        if not isinstance(decoded, dict):  # pragma: no cover - verifier invariant
            raise TypeError("verified authorization payload is not an object")
        return cast(Mapping[str, object], decoded)

    def __getitem__(self, key: str) -> object:
        return self.payload[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.payload)

    def __len__(self) -> int:
        return len(self.payload)


class VerifiedProbabilityDeploymentEstimator(Mapping[str, object]):
    """Opaque, replay-verified deployment refit accepted by new-row prediction."""

    __slots__ = ("_encoded_payload", "integrity_digest")

    def __init__(
        self,
        *,
        encoded_payload: str,
        integrity_digest: str,
        _seal: object | None = None,
    ) -> None:
        if _seal is not _VERIFIED_DEPLOYMENT_SEAL:
            raise TypeError("deployment estimator 只能由 strict verifier 创建")
        self._encoded_payload = encoded_payload
        self.integrity_digest = integrity_digest

    @property
    def payload(self) -> Mapping[str, object]:
        decoded = json.loads(self._encoded_payload)
        if not isinstance(decoded, dict):  # pragma: no cover - verifier invariant
            raise TypeError("verified deployment payload is not an object")
        return cast(Mapping[str, object], decoded)

    def __getitem__(self, key: str) -> object:
        return self.payload[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.payload)

    def __len__(self) -> int:
        return len(self.payload)


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
        return self.target_session_offset if self.gap_sessions is None else self.gap_sessions

    @property
    def target_session_offset(self) -> int:
        """Signal-date offset of the close used by the H-session label.

        Entry occurs at D+1 open and a holding horizon of H exits at D+H+1
        close.  Purging only H signal dates therefore leaks the first date of
        the following partition into the preceding partition's final label.
        """
        return self.horizon + 1


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


def _probability_label_contract(config: ProbabilityConfig) -> dict[str, object]:
    return {
        "version": PROBABILITY_LABEL_VERSION,
        "target": config.target,
        "target_definition": _target_definition(config),
        "entry": "next_tradable_session_open_after_scan",
        "holding_sessions": config.horizon,
        "entry_session_offset": 1,
        "target_session_offset": config.target_session_offset,
        "exit": f"signal_plus_{config.target_session_offset}_trading_session_close",
        "unfilled_policy": "explicitly_non_executable_never_assume_fill",
        "point_in_time_required": True,
    }


def _probability_cost_contract(
    config: ProbabilityConfig, bound_label_contract: Mapping[str, object],
) -> dict[str, object]:
    return {
        "version": config.cost_model_version,
        "components": ["commission", "stamp_tax", "transfer_fee", "slippage"],
        "deduct_before_label": True,
        "label_contract": dict(bound_label_contract),
        "label_contract_digest": stable_probability_hash(bound_label_contract),
        "label_contract_binding": (
            "complete" if config.label_contract is not None else "legacy_version_only"
        ),
    }


def _probability_model_contract(config: ProbabilityConfig) -> dict[str, object]:
    return {
        "version": PROBABILITY_MODEL_VERSION,
        "algorithm": "standardized_l2_logistic_regression_newton",
        "l2_strength": config.l2_strength,
        "maximum_iterations": config.maximum_iterations,
        "convergence_tolerance": config.convergence_tolerance,
    }


def _probability_calibrator_contract(config: ProbabilityConfig) -> dict[str, object]:
    return {
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
    }


def _probability_compatibility_contract() -> dict[str, object]:
    return {
        "legacy_artifact_policy": "replayable_read_only_never_filter_qualified",
        "qualification_policy": "exact_current_contract_versions_only",
        "superseded_schema_versions": list(SUPERSEDED_PROBABILITY_SCHEMA_VERSIONS),
        "superseded_feature_versions": list(SUPERSEDED_PROBABILITY_FEATURE_VERSIONS),
        "superseded_label_versions": list(SUPERSEDED_PROBABILITY_LABEL_VERSIONS),
        "superseded_split_versions": list(SUPERSEDED_PROBABILITY_SPLIT_VERSIONS),
        "supersession_reason": "target_exit_is_H_plus_1_sessions_from_signal_and_v2_purged_only_H",
    }


def build_probability_contract(config: ProbabilityConfig) -> dict[str, object]:
    """Return the registered, versioned research contract for one horizon/target."""
    bound_label_contract = _bound_label_contract(config)
    return {
        "schema_version": PROBABILITY_SCHEMA_VERSION,
        "feature_version": PROBABILITY_FEATURE_VERSION,
        "label": _probability_label_contract(config),
        "cost": _probability_cost_contract(config, bound_label_contract),
        "model": _probability_model_contract(config),
        "calibrator": _probability_calibrator_contract(config),
        "baseline": {
            "version": PROBABILITY_BASELINE_VERSION,
            "bin_count": config.empirical_bayes_bin_count,
            "prior_strength": config.empirical_bayes_prior_strength,
        },
        "split": _split_contract(config),
        "evaluation": _evaluation_contract(config),
        "compatibility": _probability_compatibility_contract(),
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
        try:
            artifacts = _fit_artifacts(partitions, prepared.feature_names, config)
        except _ProbabilityModelConvergenceError as exc:
            return _insufficient_evidence(
                prepared,
                config,
                generated_at,
                [f"fold_{fold_id}_{exc}"],
                split=split,
            )
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
    deployment: VerifiedProbabilityDeploymentEstimator | object | None = None,
    as_of: str | None = None,
) -> dict[str, object]:
    """Predict only from a fresh, separately refitted deployment estimator.

    The model embedded in research evidence is the final OOS evaluation fold. It
    is intentionally not a deployment model and must never be extrapolated to a
    new row. Persisted OOS rows are projected directly from ``predictions`` by
    the research/artifact layer instead of calling this function.
    """
    verify_shadow_probability_evidence(evidence)
    _validate_prediction_features_only(evidence, features)
    if isinstance(deployment, VerifiedProbabilityDeploymentEstimator):
        return _deployment_estimate(evidence, deployment, features, sample_id, as_of)
    estimate = _null_estimate(evidence, sample_id)
    estimate["deployment_status"] = "deployment_model_not_fitted"
    estimate["limitations"] = list(dict.fromkeys([
        *cast(Sequence[str], evidence.get("limitations") or ()),
        "oos_evaluation_fold_forbidden_for_new_prediction",
        "deployment_model_and_fresh_calibrator_not_available",
    ]))
    return estimate


def _deployment_estimate(
    evidence: Mapping[str, object],
    deployment: VerifiedProbabilityDeploymentEstimator,
    features: Mapping[str, float],
    sample_id: str,
    as_of: str | None,
) -> dict[str, object]:
    if not _joint_execution_estimand_supported(evidence):
        estimate = _null_estimate(evidence, sample_id)
        estimate["deployment_status"] = "joint_execution_estimand_not_supported"
        estimate["limitations"] = ["all_decisions_joint_label_corpus_not_available"]
        return estimate
    payload = deployment.payload
    if not _deployment_binding_matches(payload, evidence):
        raise ProbabilityReplayError("deployment estimator 与研究证据绑定冲突")
    if not _deployment_is_fresh(payload, as_of):
        estimate = _null_estimate(evidence, sample_id)
        estimate["deployment_status"] = "deployment_estimator_stale"
        estimate["limitations"] = ["deployment_estimator_stale_or_future_skew"]
        return estimate
    model = _object_mapping(payload.get("model"), "deployment.model")
    calibrator = _object_mapping(payload.get("calibrator"), "deployment.calibrator")
    baseline = _object_mapping(payload.get("empirical_bayes_baseline"), "deployment.baseline")
    raw = _model_probability(model, features)
    probability = _platt_probability(calibrator, raw)
    estimate = _deployment_estimate_payload(
        evidence, payload, sample_id, probability, raw, _baseline_probability(baseline, raw),
    )
    estimate["deployment_artifact_digest"] = deployment.integrity_digest
    return estimate


def _deployment_estimate_payload(
    evidence: Mapping[str, object],
    deployment: Mapping[str, object],
    sample_id: str,
    probability: float,
    raw: float,
    baseline: float,
) -> dict[str, object]:
    offset = _number_sequence(
        deployment.get("calibration_offset_ci_95"),
        "deployment.calibration_offset_ci_95",
    )
    adjusted = [_clamp_probability(probability + value) for value in offset]
    return {
        **_estimate_payload(evidence, sample_id, probability, raw, baseline),
        "calibration_bias_interval": offset,
        "calibration_adjusted_probability_interval": adjusted,
        "deployment_status": "fresh_verified_deployment_estimator",
        "training_cutoff": deployment.get("training_cutoff"),
        "calibration_cutoff": deployment.get("calibration_cutoff"),
        "deployment_generated_at": deployment.get("generated_at"),
        "model_digest": deployment.get("model_digest"),
        "calibrator_digest": deployment.get("calibrator_digest"),
        "input_digest": deployment.get("corpus_digest"),
    }


def _validate_prediction_features_only(
    evidence: Mapping[str, object], features: Mapping[str, float],
) -> None:
    model = evidence.get("model")
    if not isinstance(model, Mapping):
        return
    names = model.get("feature_names")
    if not isinstance(names, list) or sorted(features) != names:
        raise ProbabilityReplayError("上涨概率新样本 feature schema 与研究模型不一致")
    for name in names:
        _finite_number(features[str(name)], f"features.{name}")


def probability_selection_qualified(evidence: Mapping[str, object]) -> bool:
    """Fail closed unless new evidence explicitly passed selection-use gates.

    Legacy calibrated artifacts remain displayable, but lack this independently
    verified qualification and therefore cannot silently become filter inputs.
    """
    qualification = evidence.get("selection_qualification")
    return bool(
        _has_current_probability_contract(evidence)
        and evidence.get("status") == "calibrated_shadow"
        and evidence.get("selection_qualified") is True
        and isinstance(qualification, Mapping)
        and qualification.get("passed") is True
    )


def build_probability_filter_qualification(
    evidence: Mapping[str, object],
    authorization: VerifiedProbabilityFilterAuthorization | object | None = None,
) -> dict[str, object]:
    """Build the only contract that may authorize probability-based filtering.

    Core selection evidence is deliberately insufficient.  A separately
    persisted authorization must bind the exact evidence digest and must carry
    promotion, multiple-testing, calibration, drift, and executable-portfolio
    evidence.  Missing or malformed sections fail closed rather than raising.
    """
    external = authorization.payload if isinstance(
        authorization, VerifiedProbabilityFilterAuthorization,
    ) else {}
    proper_score = _proper_score_filter_gate(evidence)
    binding = _filter_authorization_binding(evidence, external)
    gates = {
        "current_contract_not_superseded": _has_current_probability_contract(evidence),
        "selection_qualified": probability_selection_qualified(evidence),
        "positive_brier_improvement_ci_95": proper_score["positive_brier_improvement_ci_95"],
        "positive_log_loss_improvement_ci_95": proper_score["positive_log_loss_improvement_ci_95"],
        "ece_at_most_5pct": proper_score["ece_at_most_5pct"],
        "authorization_bound_to_evidence_digest": binding,
        "promotion_gates_passed": _verified_promotion_gates(evidence, external),
        "multiple_testing_fdr_passed": _verified_multiple_testing(external),
        "calibration_validation_passed": _verified_calibration_validation(evidence, external),
        "temporal_drift_validation_passed": _verified_drift_validation(external),
        "execution_validation_passed": _verified_execution_validation(external, evidence),
    }
    return {
        "version": PROBABILITY_FILTER_QUALIFICATION_VERSION,
        "passed": all(value is True for value in gates.values()),
        "gates": gates,
        "evidence_digest": evidence.get("evidence_digest"),
        "authorization_digest": (
            authorization.integrity_digest
            if isinstance(authorization, VerifiedProbabilityFilterAuthorization)
            else None
        ),
        "proper_score_evidence": proper_score,
        "required_external_sections": [
            "promotion_gates",
            "multiple_testing",
            "calibration",
            "drift",
            "execution",
        ],
        "automatic_promotion": False,
    }


def probability_filter_qualified(
    evidence: Mapping[str, object],
    authorization: VerifiedProbabilityFilterAuthorization | object | None = None,
) -> bool:
    """Return whether exact, fully bound evidence may be used as a filter."""
    return build_probability_filter_qualification(evidence, authorization)["passed"] is True


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


def _has_current_probability_contract(evidence: Mapping[str, object]) -> bool:
    contract = evidence.get("contract")
    split = contract.get("split") if isinstance(contract, Mapping) else None
    label = contract.get("label") if isinstance(contract, Mapping) else None
    return bool(
        evidence.get("schema_version") == PROBABILITY_SCHEMA_VERSION
        and evidence.get("model_version") == PROBABILITY_MODEL_VERSION
        and evidence.get("feature_version") == PROBABILITY_FEATURE_VERSION
        and evidence.get("label_version") == PROBABILITY_LABEL_VERSION
        and isinstance(split, Mapping)
        and split.get("version") == PROBABILITY_SPLIT_VERSION
        and isinstance(label, Mapping)
        and label.get("target_session_offset") == _safe_positive_integer(evidence.get("horizon")) + 1
    )


def _proper_score_filter_gate(evidence: Mapping[str, object]) -> dict[str, object]:
    metrics = evidence.get("calibration_metrics")
    calibrated = metrics.get("calibrated") if isinstance(metrics, Mapping) else None
    values: Mapping[str, object] = calibrated if isinstance(calibrated, Mapping) else {}
    brier_ci = _safe_interval(values.get("brier_improvement_vs_reference_ci_95"))
    log_loss_ci = _safe_interval(values.get("log_loss_improvement_vs_reference_ci_95"))
    ece = _safe_finite_number(values.get("ece"))
    return {
        "metrics_digest": _safe_probability_hash(metrics) if isinstance(metrics, Mapping) else None,
        "brier_improvement_vs_reference_ci_95": brier_ci,
        "log_loss_improvement_vs_reference_ci_95": log_loss_ci,
        "ece": ece,
        "positive_brier_improvement_ci_95": brier_ci is not None and brier_ci[0] > 0,
        "positive_log_loss_improvement_ci_95": log_loss_ci is not None and log_loss_ci[0] > 0,
        "ece_at_most_5pct": ece is not None and ece <= 0.05,
    }


def _filter_authorization_binding(
    evidence: Mapping[str, object], authorization: Mapping[str, object],
) -> bool:
    """Recheck the exact evidence identity even for an opaque verified object."""

    binding = authorization.get("evidence_binding")
    if not isinstance(binding, Mapping):
        return False
    evidence_digest = evidence.get("evidence_digest")
    metrics = evidence.get("calibration_metrics")
    return bool(
        authorization.get("version") == PROBABILITY_FILTER_AUTHORIZATION_VERSION
        and isinstance(evidence_digest, str)
        and binding.get("evidence_digest") == evidence_digest
        and isinstance(metrics, Mapping)
        and binding.get("metrics_digest") == _safe_probability_hash(metrics)
        and binding.get("input_digest") == evidence.get("input_digest")
        and binding.get("horizon") == evidence.get("horizon")
        and binding.get("target_definition") == evidence.get("target_definition")
    )


def seal_probability_filter_authorization_artifact(
    payload: Mapping[str, object], *, generated_at: str,
) -> dict[str, object]:
    """Seal a candidate authorization; sealing alone never authorizes filtering."""

    normalized = _canonical_json_value(dict(payload))
    if not isinstance(normalized, dict):
        raise ValueError("概率筛选 authorization payload 必须是 object")
    _validated_aware_timestamp(generated_at, "authorization.generated_at")
    identity = {"generated_at": generated_at, "payload": normalized}
    return {
        "schema_version": PROBABILITY_FILTER_AUTHORIZATION_SCHEMA_VERSION,
        "generated_at": generated_at,
        "payload": normalized,
        "integrity": {
            "algorithm": "sha256",
            "scope": "generated_at+payload",
            "notice": PROBABILITY_FILTER_AUTHORIZATION_INTEGRITY_NOTICE,
            "integrity_digest": stable_probability_hash(identity),
        },
    }


def verify_probability_filter_authorization_artifact(
    artifact: Mapping[str, object], evidence: Mapping[str, object],
) -> VerifiedProbabilityFilterAuthorization:
    """Strictly replay raw authorization evidence and return an opaque token."""

    try:
        _require_exact_mapping_keys(
            artifact,
            {"schema_version", "generated_at", "payload", "integrity"},
            "authorization",
        )
        if artifact.get("schema_version") != PROBABILITY_FILTER_AUTHORIZATION_SCHEMA_VERSION:
            raise ValueError("authorization schema_version 不受支持")
        generated_at = str(artifact.get("generated_at") or "")
        authorization_time = _validated_aware_timestamp(
            generated_at, "authorization.generated_at",
        )
        evidence_time = _validated_aware_timestamp(
            str(evidence.get("generated_at") or ""), "evidence.generated_at",
        )
        if authorization_time < evidence_time:
            raise ValueError("authorization 生成时间早于绑定 OOS evidence")
        payload = _strict_mapping(artifact.get("payload"), "authorization.payload")
        integrity = _strict_mapping(artifact.get("integrity"), "authorization.integrity")
        _require_exact_mapping_keys(
            integrity, {"algorithm", "scope", "notice", "integrity_digest"}, "authorization.integrity",
        )
        digest = str(integrity.get("integrity_digest") or "")
        if (
            integrity.get("algorithm") != "sha256"
            or integrity.get("scope") != "generated_at+payload"
            or integrity.get("notice") != PROBABILITY_FILTER_AUTHORIZATION_INTEGRITY_NOTICE
            or digest != stable_probability_hash({"generated_at": generated_at, "payload": payload})
        ):
            raise ValueError("authorization content address 不一致")
        _verify_filter_authorization_payload(payload, evidence)
    except ProbabilityReplayError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ProbabilityReplayError("概率筛选 authorization 原始证据无效") from exc
    encoded_payload = json.dumps(
        _canonical_json_value(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return VerifiedProbabilityFilterAuthorization(
        encoded_payload=encoded_payload,
        generated_at=generated_at,
        integrity_digest=digest,
        _seal=_VERIFIED_AUTHORIZATION_SEAL,
    )


def fit_probability_deployment_estimator(
    samples: Sequence[ProbabilitySample],
    *,
    evidence: Mapping[str, object],
    authorization: VerifiedProbabilityFilterAuthorization | object,
    generated_at: str,
) -> dict[str, object]:
    """Refit a standalone estimator only after strict OOS/filter authorization."""

    if not isinstance(authorization, VerifiedProbabilityFilterAuthorization):
        raise ProbabilityReplayError("deployment refit 需要 strict verified authorization")
    verify_shadow_probability_evidence(evidence, samples)
    if not probability_filter_qualified(evidence, authorization):
        raise ProbabilityReplayError("deployment refit 的 OOS/filter gates 未通过")
    generated = _validated_aware_timestamp(generated_at, "deployment.generated_at")
    evidence_generated = _validated_aware_timestamp(
        str(evidence.get("generated_at") or ""), "evidence.generated_at",
    )
    authorization_generated = _validated_aware_timestamp(
        authorization.generated_at, "authorization.generated_at",
    )
    if generated < max(evidence_generated, authorization_generated):
        raise ProbabilityReplayError("deployment generated_at 早于研究或授权证据")
    config = _config_from_evidence(evidence)
    prepared = _prepare_study(samples, config)
    split = _deployment_refit_split(prepared.eligible, config)
    generated_market_date = generated.astimezone(timezone(timedelta(hours=8))).date()
    if date.fromisoformat(split.calibration_dates[-1]) > generated_market_date:
        raise ProbabilityReplayError("deployment calibration 尚未成熟")
    partitions = _partition_samples(prepared.eligible, split)
    reasons = _class_diversity_reasons(partitions)
    if reasons:
        raise ProbabilityReplayError(f"deployment refit 数据不足：{','.join(reasons)}")
    artifacts = _fit_artifacts(partitions, prepared.feature_names, config)
    calibration_predictions = _deployment_calibration_predictions(
        partitions["calibration"], artifacts, prepared.feature_names,
    )
    offset = _deployment_calibration_offset_ci(
        calibration_predictions, config, prepared.input_digest,
    )
    payload = _deployment_payload(
        evidence=evidence,
        authorization=authorization,
        prepared=prepared,
        config=config,
        split=split,
        artifacts=artifacts,
        calibration_predictions=calibration_predictions,
        calibration_offset_ci_95=offset,
        generated_at=generated_at,
    )
    return seal_probability_deployment_artifact(payload, generated_at=generated_at)


def seal_probability_deployment_artifact(
    payload: Mapping[str, object], *, generated_at: str,
) -> dict[str, object]:
    normalized = _canonical_json_value(dict(payload))
    if not isinstance(normalized, dict):
        raise ValueError("deployment payload 必须是 object")
    _validated_aware_timestamp(generated_at, "deployment.generated_at")
    return {
        "schema_version": PROBABILITY_DEPLOYMENT_ARTIFACT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "payload": normalized,
        "integrity": {
            "algorithm": "sha256",
            "scope": "generated_at+payload",
            "integrity_digest": stable_probability_hash(
                {"generated_at": generated_at, "payload": normalized},
            ),
            "notice": "content_address_only_not_signature_strict_replay_required",
        },
    }


def verify_probability_deployment_artifact(
    artifact: Mapping[str, object],
    *,
    evidence: Mapping[str, object],
    authorization: VerifiedProbabilityFilterAuthorization | object,
    samples: Sequence[ProbabilitySample],
    as_of: str | None = None,
) -> VerifiedProbabilityDeploymentEstimator:
    """Full-refit replay and freshness verification for one deployment artifact."""

    if not isinstance(authorization, VerifiedProbabilityFilterAuthorization):
        raise ProbabilityReplayError("deployment verifier 缺少 strict authorization")
    try:
        generated_at, payload, digest = _verified_deployment_envelope(artifact)
        rebuilt = fit_probability_deployment_estimator(
            samples,
            evidence=evidence,
            authorization=authorization,
            generated_at=generated_at,
        )
        if artifact != rebuilt:
            raise ValueError("deployment artifact 无法由完整 corpus 确定性重放")
        if not _deployment_is_fresh(payload, as_of):
            raise ValueError("deployment artifact 已过期或存在未来时间偏差")
    except ProbabilityReplayError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ProbabilityReplayError("deployment estimator artifact 无效") from exc
    encoded = json.dumps(
        _canonical_json_value(payload), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )
    return VerifiedProbabilityDeploymentEstimator(
        encoded_payload=encoded,
        integrity_digest=digest,
        _seal=_VERIFIED_DEPLOYMENT_SEAL,
    )


def _verified_deployment_envelope(
    artifact: Mapping[str, object],
) -> tuple[str, Mapping[str, object], str]:
    _require_exact_mapping_keys(
        artifact, {"schema_version", "generated_at", "payload", "integrity"}, "deployment",
    )
    if artifact.get("schema_version") != PROBABILITY_DEPLOYMENT_ARTIFACT_SCHEMA_VERSION:
        raise ValueError("deployment schema_version 不受支持")
    generated_at = str(artifact.get("generated_at") or "")
    _validated_aware_timestamp(generated_at, "deployment.generated_at")
    payload = _strict_mapping(artifact.get("payload"), "deployment.payload")
    integrity = _strict_mapping(artifact.get("integrity"), "deployment.integrity")
    _require_exact_mapping_keys(
        integrity, {"algorithm", "scope", "integrity_digest", "notice"}, "deployment.integrity",
    )
    digest = str(integrity.get("integrity_digest") or "")
    expected = stable_probability_hash({"generated_at": generated_at, "payload": payload})
    if (
        integrity.get("algorithm") != "sha256"
        or integrity.get("scope") != "generated_at+payload"
        or integrity.get("notice") != "content_address_only_not_signature_strict_replay_required"
        or digest != expected
    ):
        raise ValueError("deployment content address 不一致")
    return generated_at, payload, digest


def _verify_filter_authorization_payload(
    payload: Mapping[str, object], evidence: Mapping[str, object],
) -> None:
    _require_exact_mapping_keys(
        payload,
        {
            "version", "evidence_binding", "oos_predictions", "candidate_registry",
            "selected_candidate_id", "multiple_testing", "calibration_validation",
            "drift_validation", "execution_validation",
        },
        "authorization.payload",
    )
    if payload.get("version") != PROBABILITY_FILTER_AUTHORIZATION_VERSION:
        raise ValueError("authorization version 不受支持")
    binding = _strict_mapping(payload.get("evidence_binding"), "evidence_binding")
    _require_exact_mapping_keys(
        binding,
        {"evidence_digest", "metrics_digest", "input_digest", "horizon", "target_definition"},
        "evidence_binding",
    )
    if not _filter_authorization_binding(evidence, payload):
        raise ValueError("authorization 未绑定 exact evidence")
    predictions = payload.get("oos_predictions")
    if not isinstance(predictions, list) or not predictions or not _predictions_bind_evidence(
        predictions, evidence,
    ):
        raise ValueError("authorization OOS predictions 未绑定完整 evidence")
    _verify_authorization_calibration(evidence, payload)
    _verify_authorization_candidates(evidence, payload)
    if (
        not _verified_promotion_gates(evidence, payload)
        or not _verified_multiple_testing(payload)
        or not _verified_calibration_validation(evidence, payload)
        or not _verified_drift_validation(payload)
        or not _verified_execution_validation(payload, evidence)
    ):
        raise ValueError("authorization drift/execution 原始门禁未通过")


def _predictions_bind_evidence(
    predictions: list[object], evidence: Mapping[str, object],
) -> bool:
    persisted = evidence.get("predictions")
    if isinstance(persisted, list):
        if predictions != persisted:
            return False
        try:
            return verify_shadow_probability_evidence(evidence)
        except ProbabilityReplayError:
            return False
    if not _EVIDENCE_DIGEST_FIELDS - {"predictions"} <= evidence.keys():
        return False
    unsigned = {
        name: predictions if name == "predictions" else evidence[name]
        for name in _EVIDENCE_DIGEST_FIELDS
    }
    return stable_probability_hash(unsigned) == evidence.get("evidence_digest")


def _verify_authorization_calibration(
    evidence: Mapping[str, object], payload: Mapping[str, object],
) -> None:
    section = _strict_mapping(payload.get("calibration_validation"), "calibration_validation")
    _require_exact_mapping_keys(
        section,
        {
            "independent_session_count", "brier_improvement_ci_95",
            "log_loss_improvement_ci_95", "ece",
        },
        "calibration_validation",
    )
    predictions = cast(list[Mapping[str, object]], payload["oos_predictions"])
    config = _config_from_evidence(evidence)
    inputs = _prediction_metric_inputs(predictions)
    series = _prediction_bootstrap_series(inputs)
    seed = str(evidence.get("input_digest") or "")
    expected_brier = _date_block_bootstrap_ci(
        series["brier_improvement_vs_reference"], seed + ":brier-improvement",
        config.bootstrap_samples, block_length_sessions=config.target_session_offset,
    )
    expected_log = _date_block_bootstrap_ci(
        series["log_loss_improvement_vs_reference"], seed + ":log-loss-improvement",
        config.bootstrap_samples, block_length_sessions=config.target_session_offset,
    )
    probabilities, _baseline, outcomes, dates, references = inputs
    metrics = evaluate_probability_predictions(
        probabilities, outcomes, dates, base_rate=sum(references) / len(references),
        bin_count=config.calibration_bin_count, reference_probabilities=references,
    )
    expected_sessions = len(set(dates))
    if (
        section.get("independent_session_count") != expected_sessions
        or expected_sessions < 60
        or not _same_interval(section.get("brier_improvement_ci_95"), expected_brier)
        or not _same_interval(section.get("log_loss_improvement_ci_95"), expected_log)
        or not _same_number(section.get("ece"), metrics.get("ece"))
    ):
        raise ValueError("authorization proper-score/calibration replay 不一致")


def _verify_authorization_candidates(
    evidence: Mapping[str, object], payload: Mapping[str, object],
) -> None:
    registry = _authorization_candidate_registry(payload.get("candidate_registry"))
    selected = str(payload.get("selected_candidate_id") or "")
    selected_statistics = _selected_candidate_session_statistics(
        cast(list[Mapping[str, object]], payload["oos_predictions"]),
    )
    raw_p_values = _candidate_p_values(
        registry,
        selected=selected,
        selected_evidence_digest=str(evidence.get("evidence_digest") or ""),
        selected_statistics=selected_statistics,
    )
    _verify_bh_authorization(payload.get("multiple_testing"), raw_p_values, selected)


def _authorization_candidate_registry(value: object) -> list[object]:
    if not isinstance(value, list) or len(value) < 6:
        raise ValueError("authorization candidate family 少于 6")
    return value


def _candidate_p_values(
    registry: Sequence[object],
    *,
    selected: str,
    selected_evidence_digest: str,
    selected_statistics: list[tuple[str, float]],
) -> dict[str, float]:
    raw_p_values: dict[str, float] = {}
    for index, raw in enumerate(registry):
        candidate_id, digest, statistics, raw_p = _validated_authorization_candidate(raw, index)
        if candidate_id in raw_p_values:
            raise ValueError("authorization candidate identity 无效")
        if candidate_id == selected and (
            digest != selected_evidence_digest or statistics != selected_statistics
        ):
            raise ValueError("authorization selected candidate 未绑定 OOS evidence")
        raw_p_values[candidate_id] = raw_p
    if selected not in raw_p_values:
        raise ValueError("authorization selected candidate 不在注册 family")
    return raw_p_values


def _validated_authorization_candidate(
    raw: object, index: int,
) -> tuple[str, str, list[tuple[str, float]], float]:
    path = f"candidate_registry[{index}]"
    candidate = _strict_mapping(raw, path)
    _require_exact_mapping_keys(
        candidate, {"candidate_id", "evidence_digest", "session_statistics", "raw_p_value"},
        path,
    )
    candidate_id = str(candidate.get("candidate_id") or "")
    digest = str(candidate.get("evidence_digest") or "")
    if not candidate_id or len(digest) != 64:
        raise ValueError("authorization candidate identity 无效")
    statistics = _validated_candidate_statistics(candidate.get("session_statistics"))
    raw_p = _one_sided_sign_test_p_value([value for _day, value in statistics])
    if not _same_number(candidate.get("raw_p_value"), raw_p):
        raise ValueError("authorization raw p-value 无法由会话统计重算")
    return candidate_id, digest, statistics, raw_p


def _verify_bh_authorization(
    value: object, raw_p_values: Mapping[str, float], selected: str,
) -> None:
    section = _strict_mapping(value, "multiple_testing")
    _require_exact_mapping_keys(
        section, {"method", "alpha", "family_size", "adjusted_p_value"}, "multiple_testing",
    )
    alpha = _safe_finite_number(section.get("alpha"))
    adjusted = _benjamini_hochberg_adjusted(raw_p_values)[selected]
    if (
        section.get("method") != "benjamini_hochberg_fdr"
        or alpha is None or not 0 < alpha <= 0.10
        or section.get("family_size") != len(raw_p_values)
        or not _same_number(section.get("adjusted_p_value"), adjusted)
        or adjusted > alpha
    ):
        raise ValueError("authorization BH-FDR 重算未通过")


def _selected_candidate_session_statistics(
    predictions: Sequence[Mapping[str, object]],
) -> list[tuple[str, float]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for raw in predictions:
        row = _strict_mapping(raw, "prediction")
        outcome = _integer(row.get("outcome"), "prediction.outcome")
        probability = _finite_number(row.get("probability"), "prediction.probability")
        reference = _finite_number(row.get("reference_base_rate"), "prediction.reference_base_rate")
        grouped[str(row.get("session_date") or "")].append(
            (outcome - reference) ** 2 - (outcome - probability) ** 2
        )
    return [(day, sum(values) / len(values)) for day, values in sorted(grouped.items())]


def _deployment_refit_split(
    samples: Sequence[ProbabilitySample], config: ProbabilityConfig,
) -> GroupedWalkForwardSplit:
    dates = tuple(sorted({item.session_date for item in samples}))
    gap = config.effective_gap_sessions
    calibration_start = len(dates) - config.minimum_calibration_sessions
    train_end = calibration_start - gap
    if train_end < config.minimum_train_sessions or calibration_start >= len(dates):
        raise ProbabilityReplayError("deployment refit 缺少独立后置 calibration block")
    split = GroupedWalkForwardSplit(
        train_dates=dates[:train_end],
        train_gap_dates=dates[train_end:calibration_start],
        calibration_dates=dates[calibration_start:],
        calibration_gap_dates=(),
        test_dates=(),
    )
    if split.train_dates[-1] >= split.calibration_dates[0]:
        raise ProbabilityReplayError("deployment calibration 与 training overlap")
    return split


def _deployment_calibration_predictions(
    samples: Sequence[ProbabilitySample],
    artifacts: _FittedArtifacts,
    feature_names: tuple[str, ...],
) -> list[dict[str, object]]:
    return [
        {
            "sample_id": item.sample_id,
            "session_date": item.session_date,
            "outcome": _required_label(item),
            "raw_probability": (raw := _model_probability(artifacts.model, item.features)),
            "probability": _platt_probability(artifacts.calibrator, raw),
            "baseline_probability": _baseline_probability(artifacts.baseline, raw),
            "feature_vector_digest": stable_probability_hash(
                {name: float(item.features[name]) for name in feature_names},
            ),
        }
        for item in samples
    ]


def _deployment_calibration_offset_ci(
    predictions: Sequence[Mapping[str, object]],
    config: ProbabilityConfig,
    seed: str,
) -> list[float]:
    series = [
        (
            str(item["session_date"]),
            _integer(item["outcome"], "outcome")
            - _finite_number(item["probability"], "probability"),
        )
        for item in predictions
    ]
    return _date_block_bootstrap_ci(
        series,
        seed + ":deployment-calibration-offset",
        config.bootstrap_samples,
        block_length_sessions=config.target_session_offset,
    )


def _deployment_payload(
    *,
    evidence: Mapping[str, object],
    authorization: VerifiedProbabilityFilterAuthorization,
    prepared: _PreparedStudy,
    config: ProbabilityConfig,
    split: GroupedWalkForwardSplit,
    artifacts: _FittedArtifacts,
    calibration_predictions: Sequence[Mapping[str, object]],
    calibration_offset_ci_95: Sequence[float],
    generated_at: str,
) -> dict[str, object]:
    model_digest = stable_probability_hash(artifacts.model)
    final_fold = cast(Sequence[Mapping[str, object]], evidence["folds"])[-1]
    payload = {
        "contract_version": PROBABILITY_DEPLOYMENT_CONTRACT_VERSION,
        "generated_at": generated_at,
        "evidence_digest": evidence["evidence_digest"],
        "oos_predictions_digest": stable_probability_hash(evidence["predictions"]),
        "authorization_digest": authorization.integrity_digest,
        "authorization_generated_at": authorization.generated_at,
        **_deployment_joint_bindings(authorization),
        "corpus_digest": prepared.input_digest,
        "config_digest": stable_probability_hash(build_probability_contract(config)),
        "feature_version": PROBABILITY_FEATURE_VERSION,
        "feature_names": list(prepared.feature_names),
        "label_version": PROBABILITY_LABEL_VERSION,
        "label_contract_digest": evidence["label_contract_digest"],
        "split_version": PROBABILITY_SPLIT_VERSION,
        "purge_sessions": config.effective_gap_sessions,
        "training_dates": list(split.train_dates),
        "purge_dates": list(split.train_gap_dates),
        "calibration_dates": list(split.calibration_dates),
        "training_cutoff": split.train_dates[-1],
        "calibration_start": split.calibration_dates[0],
        "calibration_cutoff": split.calibration_dates[-1],
        "model": artifacts.model,
        "model_digest": model_digest,
        "calibrator": artifacts.calibrator,
        "calibrator_digest": stable_probability_hash(artifacts.calibrator),
        "empirical_bayes_baseline": artifacts.baseline,
        "baseline_digest": stable_probability_hash(artifacts.baseline),
        "calibration_predictions": list(calibration_predictions),
        "calibration_predictions_digest": stable_probability_hash(calibration_predictions),
        "calibration_offset_ci_95": list(calibration_offset_ci_95),
        "freshness": {
            "maximum_age_hours": PROBABILITY_DEPLOYMENT_MAXIMUM_AGE_HOURS,
            "policy": "generated_at_and_latest_mature_calibration_cutoff",
        },
        "oos_final_fold_model_digest": final_fold["model_digest"],
        "oos_final_fold_reuse_forbidden": True,
    }
    if model_digest == final_fold["model_digest"]:
        raise ProbabilityReplayError("deployment estimator 不得复用 final OOS fold 参数")
    return payload


def _deployment_joint_bindings(
    authorization: Mapping[str, object],
) -> dict[str, str]:
    execution = _strict_mapping(
        authorization.get("execution_validation"), "execution_validation",
    )
    bindings = {
        name: str(execution.get(name) or "")
        for name in (
            "joint_execution_evidence_digest",
            "joint_execution_assessment_digest",
            "joint_execution_estimand_digest",
        )
    }
    if any(len(value) != 64 for value in bindings.values()):
        raise ProbabilityReplayError("deployment 缺少成熟 joint execution 评估绑定")
    return bindings


def _deployment_binding_matches(
    payload: Mapping[str, object], evidence: Mapping[str, object],
) -> bool:
    return bool(
        payload.get("contract_version") == PROBABILITY_DEPLOYMENT_CONTRACT_VERSION
        and payload.get("evidence_digest") == evidence.get("evidence_digest")
        and payload.get("feature_version") == evidence.get("feature_version")
        and payload.get("label_version") == evidence.get("label_version")
        and payload.get("label_contract_digest") == evidence.get("label_contract_digest")
        and payload.get("split_version") == PROBABILITY_SPLIT_VERSION
        and payload.get("oos_final_fold_reuse_forbidden") is True
    )


def _deployment_is_fresh(payload: Mapping[str, object], as_of: str | None) -> bool:
    try:
        generated = _validated_aware_timestamp(
            str(payload.get("generated_at") or ""), "deployment.generated_at",
        ).astimezone(timezone.utc)
        reference = (
            _validated_aware_timestamp(as_of, "deployment.as_of").astimezone(timezone.utc)
            if as_of is not None else utc_now()
        )
        calibration_cutoff = date.fromisoformat(str(payload.get("calibration_cutoff") or ""))
    except (TypeError, ValueError):
        return False
    age = reference - generated
    reference_market_date = reference.astimezone(timezone(timedelta(hours=8))).date()
    return bool(
        timedelta(0) <= age <= timedelta(hours=PROBABILITY_DEPLOYMENT_MAXIMUM_AGE_HOURS)
        and calibration_cutoff <= reference_market_date
        and (reference_market_date - calibration_cutoff).days <= 4
    )


def _validated_candidate_statistics(value: object) -> list[tuple[str, float]]:
    if not isinstance(value, list) or not value:
        raise ValueError("candidate session_statistics 必须非空")
    output: list[tuple[str, float]] = []
    for index, raw in enumerate(value):
        row = _strict_mapping(raw, f"session_statistics[{index}]")
        _require_exact_mapping_keys(
            row, {"session_date", "proper_score_improvement"}, f"session_statistics[{index}]",
        )
        day = _validated_date(str(row.get("session_date") or ""))
        output.append((day, _finite_number(row.get("proper_score_improvement"), "proper_score_improvement")))
    if output != sorted(output) or len({day for day, _value in output}) != len(output):
        raise ValueError("candidate session statistics 必须按唯一日期排序")
    return output


def _one_sided_sign_test_p_value(values: Sequence[float]) -> float:
    nonzero = [value for value in values if not math.isclose(value, 0.0, rel_tol=0, abs_tol=1e-15)]
    if not nonzero:
        return 1.0
    positives = sum(value > 0 for value in nonzero)
    denominator = 2 ** len(nonzero)
    return min(1.0, sum(math.comb(len(nonzero), count) for count in range(positives, len(nonzero) + 1)) / denominator)


def _benjamini_hochberg_adjusted(values: Mapping[str, float]) -> dict[str, float]:
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    adjusted: dict[str, float] = {}
    running = 1.0
    family_size = len(ordered)
    for rank in range(family_size, 0, -1):
        candidate_id, raw = ordered[rank - 1]
        running = min(running, raw * family_size / rank)
        adjusted[candidate_id] = min(1.0, running)
    return adjusted


def _verified_promotion_gates(
    evidence: Mapping[str, object], authorization: Mapping[str, object],
) -> bool:
    return bool(
        authorization
        and evidence.get("status") == "calibrated_shadow"
        and probability_selection_qualified(evidence)
        and _filter_authorization_binding(evidence, authorization)
    )


def _verified_multiple_testing(authorization: Mapping[str, object]) -> bool:
    section = authorization.get("multiple_testing")
    if not isinstance(section, Mapping):
        return False
    alpha = _safe_finite_number(section.get("alpha"))
    adjusted = _safe_finite_number(section.get("adjusted_p_value"))
    return bool(
        section.get("method") == "benjamini_hochberg_fdr"
        and alpha is not None and 0 < alpha <= 0.10
        and adjusted is not None and 0 <= adjusted <= alpha
        and section.get("family_size") == len(cast(list[object], authorization.get("candidate_registry") or []))
    )


def _verified_calibration_validation(
    evidence: Mapping[str, object], authorization: Mapping[str, object],
) -> bool:
    section = authorization.get("calibration_validation")
    proper = _proper_score_filter_gate(evidence)
    return bool(
        isinstance(section, Mapping)
        and _safe_positive_integer(section.get("independent_session_count")) >= 60
        and proper["positive_brier_improvement_ci_95"] is True
        and proper["positive_log_loss_improvement_ci_95"] is True
        and proper["ece_at_most_5pct"] is True
        and _same_interval(section.get("brier_improvement_ci_95"), proper["brier_improvement_vs_reference_ci_95"])
        and _same_interval(section.get("log_loss_improvement_ci_95"), proper["log_loss_improvement_vs_reference_ci_95"])
        and _same_number(section.get("ece"), proper["ece"])
    )


def _verified_drift_validation(authorization: Mapping[str, object]) -> bool:
    section = authorization.get("drift_validation")
    if not isinstance(section, Mapping):
        return False
    required = {
        "independent_session_count", "reference_series", "current_series",
        "reference_digest", "current_digest", "statistics", "thresholds",
    }
    if set(section) != required:
        return False
    try:
        reference = _validated_drift_series(section.get("reference_series"), "reference")
        current = _validated_drift_series(section.get("current_series"), "current")
        if current != _oos_current_drift_series(authorization.get("oos_predictions")):
            return False
        statistics = _strict_mapping(section.get("statistics"), "drift.statistics")
        thresholds = _strict_mapping(section.get("thresholds"), "drift.thresholds")
        return _drift_replay_passes(section, reference, current, statistics, thresholds)
    except (TypeError, ValueError):
        return False


def _validated_drift_series(
    value: object, path: str,
) -> list[tuple[str, float, float, float]]:
    if not isinstance(value, list) or len(value) < 30:
        raise ValueError(f"drift {path} series 少于 30 会话")
    rows: list[tuple[str, float, float, float]] = []
    for index, raw in enumerate(value):
        row = _strict_mapping(raw, f"drift.{path}[{index}]")
        _require_exact_mapping_keys(
            row, {"session_date", "feature_statistic", "probability", "performance"},
            f"drift.{path}[{index}]",
        )
        probability = _finite_number(row.get("probability"), "drift.probability")
        _require_probability(probability, "drift.probability")
        rows.append((
            _validated_date(str(row.get("session_date") or "")),
            _finite_number(row.get("feature_statistic"), "drift.feature_statistic"),
            probability,
            _finite_number(row.get("performance"), "drift.performance"),
        ))
    if rows != sorted(rows) or len({row[0] for row in rows}) != len(rows):
        raise ValueError("drift series 日期必须严格递增唯一")
    return rows


def _oos_current_drift_series(value: object) -> list[tuple[str, float, float, float]]:
    if not isinstance(value, list):
        raise ValueError("drift 缺少绑定 OOS predictions")
    grouped: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    for raw in value:
        row = _strict_mapping(raw, "oos_predictions[]")
        performance = (
            row.get("net_excess_return")
            if row.get("net_excess_return") is not None
            else row.get("net_return")
        )
        net = _finite_number(performance, "prediction.performance")
        grouped[_validated_date(str(row.get("session_date") or ""))].append((
            _finite_number(row.get("raw_probability"), "prediction.raw_probability"),
            _finite_number(row.get("probability"), "prediction.probability"),
            net,
        ))
    output = [
        (
            day,
            sum(row[0] for row in rows) / len(rows),
            sum(row[1] for row in rows) / len(rows),
            sum(row[2] for row in rows) / len(rows),
        )
        for day, rows in sorted(grouped.items())
    ]
    if len(output) < 30:
        raise ValueError("drift OOS current series 少于 30 会话")
    return output[-30:]


def _drift_replay_passes(
    section: Mapping[str, object],
    reference: Sequence[tuple[str, float, float, float]],
    current: Sequence[tuple[str, float, float, float]],
    statistics: Mapping[str, object],
    thresholds: Mapping[str, object],
) -> bool:
    statistic_names = {"feature_mean_shift", "probability_mean_shift", "performance_mean_shift"}
    threshold_names = {"maximum_feature_mean_shift", "maximum_probability_mean_shift", "maximum_performance_mean_shift"}
    if set(statistics) != statistic_names or set(thresholds) != threshold_names:
        return False
    replayed = _drift_statistics(reference, current)
    limits = {
        name: _safe_finite_number(thresholds.get(name))
        for name in threshold_names
    }
    return bool(
        reference[-1][0] < current[0][0]
        and section.get("independent_session_count") == len(reference) + len(current)
        and section.get("reference_digest") == stable_probability_hash(section["reference_series"])
        and section.get("current_digest") == stable_probability_hash(section["current_series"])
        and all(_same_number(statistics.get(name), value) for name, value in replayed.items())
        and all(value is not None and value >= 0 for value in limits.values())
        and replayed["feature_mean_shift"] <= cast(float, limits["maximum_feature_mean_shift"])
        and replayed["probability_mean_shift"] <= cast(float, limits["maximum_probability_mean_shift"])
        and replayed["performance_mean_shift"] <= cast(float, limits["maximum_performance_mean_shift"])
    )


def _drift_statistics(
    reference: Sequence[tuple[str, float, float, float]],
    current: Sequence[tuple[str, float, float, float]],
) -> dict[str, float]:
    def mean(rows: Sequence[tuple[str, float, float, float]], index: Literal[1, 2, 3]) -> float:
        total = 0.0
        for row in rows:
            total += row[index]
        return total / len(rows)

    def shift(index: Literal[1, 2, 3]) -> float:
        return abs(mean(reference, index) - mean(current, index))

    return {
        "feature_mean_shift": shift(1),
        "probability_mean_shift": shift(2),
        "performance_mean_shift": shift(3),
    }


def _verified_execution_validation(
    authorization: Mapping[str, object], evidence: Mapping[str, object],
) -> bool:
    if not _joint_execution_estimand_supported(evidence):
        return False
    section = authorization.get("execution_validation")
    if not isinstance(section, Mapping):
        return False
    required = {
        "observation_count", "independent_session_count", "prediction_digest",
        "joint_execution_evidence", "joint_execution_evidence_digest",
        "joint_execution_assessment_digest", "joint_execution_estimand_digest",
        "session_economics", "session_economics_digest", "mean_net_excess_return",
        "maximum_drawdown", "mean_top100_turnover", "capacity_coverage", "thresholds",
    }
    if set(section) != required:
        return False
    try:
        predictions = _execution_predictions(authorization.get("oos_predictions"))
        reports = _verified_joint_execution_corpus(
            section.get("joint_execution_evidence"), predictions,
        )
        economics = _execution_session_economics(predictions, reports)
        metrics = _execution_metrics(economics)
        return _execution_replay_passes(section, predictions, reports, economics, metrics)
    except (TypeError, ValueError):
        return False


def _joint_execution_estimand_supported(evidence: Mapping[str, object]) -> bool:
    """Fail closed until OOS evidence carries observed all-decision joint labels.

    Current v4 research predicts a conditional up/down label only after a row is
    deemed executable.  Joint-execution v2 reports bind matured market evidence
    but do not contain the observed entry-fill, exit-executable, and net-positive
    components needed to assess the all-decisions estimand.  A future probability
    evidence contract must add those observed components before this gate can be
    intentionally versioned open.
    """
    contract = evidence.get("contract")
    label = contract.get("label") if isinstance(contract, Mapping) else None
    return bool(
        isinstance(label, Mapping)
        and label.get("version") == "market-scan-joint-execution-label-v1"
        and label.get("target") == "joint_execution_action_positive"
        and label.get("target_population")
        == "all_fixed_full_market_decisions_including_unfilled_and_unexecutable"
        and label.get("observed_components")
        == ["entry_fill", "exit_executable", "net_positive"]
        and label.get("selection_probability") == "joint_execution_action_probability"
        and evidence.get("schema_version") == "market-scan-joint-execution-probability-v1"
    )


def _execution_predictions(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not value:
        raise ValueError("execution 缺少 OOS predictions")
    return [_strict_mapping(item, "execution.prediction") for item in value]


def _verified_joint_execution_corpus(
    value: object, predictions: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) != len(predictions):
        raise ValueError("joint execution corpus 未与 OOS predictions 全覆盖绑定")
    reports = [
        verify_joint_execution_probability_evidence(
            _strict_mapping(item, f"joint_execution_evidence[{index}]"),
        )
        for index, item in enumerate(value)
    ]
    if len({report.sample_id for report in reports}) != len(reports):
        raise ValueError("joint execution corpus sample_id 重复")
    for prediction, report in zip(predictions, reports, strict=True):
        sample_id = str(prediction.get("sample_id") or "")
        symbol = _probability_sample_symbol(sample_id)
        if (
            report.sample_id != sample_id
            or report.symbol != symbol
            or report.signal_session != str(prediction.get("session_date") or "")
            or not joint_execution_probability_action_qualified(report)
        ):
            raise ValueError("joint execution corpus 与 OOS prediction identity 冲突")
    return [report.model_dump(mode="json") for report in reports]


def _probability_sample_symbol(sample_id: str) -> str:
    parts = sample_id.split(":")
    if len(parts) != 4 or not parts[0].isdigit() or not parts[2].isdigit():
        raise ValueError("prediction sample_id 不是 production 格式")
    symbol, horizon, target = parts[1], int(parts[2]), parts[3]
    valid_symbol = (
        len(symbol) == 9 and symbol[:6].isdigit()
        and symbol[6:] in {".SH", ".SZ", ".BJ"}
    )
    if not valid_symbol or horizon not in {1, 5, 20} or target not in {
        "net_excess_positive", "net_return_positive",
    }:
        raise ValueError("prediction sample_id identity 无效")
    return symbol


def _execution_session_economics(
    predictions: Sequence[Mapping[str, object]],
    reports: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    report_by_id = {str(report["sample_id"]): report for report in reports}
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for prediction in predictions:
        grouped[str(prediction["session_date"])].append(prediction)
    previous_symbols: set[str] | None = None
    output: list[dict[str, object]] = []
    for session_date, candidates in sorted(grouped.items()):
        selected = sorted(
            candidates,
            key=lambda row: (-_finite_number(row["probability"], "probability"), str(row["sample_id"])),
        )[:100]
        symbols = {_probability_sample_symbol(str(row["sample_id"])) for row in selected}
        output.append(_execution_session_row(
            session_date, selected, report_by_id, symbols, previous_symbols,
        ))
        previous_symbols = symbols
    return output


def _execution_session_row(
    session_date: str,
    selected: Sequence[Mapping[str, object]],
    report_by_id: Mapping[str, Mapping[str, object]],
    symbols: set[str],
    previous_symbols: set[str] | None,
) -> dict[str, object]:
    net_returns = [_finite_number(row.get("net_return"), "net_return") for row in selected]
    excess_returns = [
        _finite_number(row.get("net_excess_return"), "net_excess_return")
        for row in selected
    ]
    selected_reports = [report_by_id[str(row["sample_id"])] for row in selected]
    capacity_count = sum(_joint_report_within_capacity(report) for report in selected_reports)
    turnover = 0.0 if previous_symbols is None else (
        1.0 - len(symbols & previous_symbols) / max(1, len(symbols), len(previous_symbols))
    )
    return {
        "session_date": session_date,
        "decision_count": len(selected),
        "portfolio_net_return": sum(net_returns) / len(net_returns),
        "benchmark_return": sum(
            net - excess for net, excess in zip(net_returns, excess_returns, strict=True)
        ) / len(selected),
        "net_excess_return": sum(excess_returns) / len(excess_returns),
        "top100_turnover": turnover,
        "capacity_eligible_count": capacity_count,
        "joint_evidence_digest": stable_probability_hash(
            [report["canonical_digest"] for report in selected_reports],
        ),
    }


def _joint_report_within_capacity(report: Mapping[str, object]) -> bool:
    evidence = _strict_mapping(report.get("evidence"), "joint.evidence")
    participation = _strict_mapping(evidence.get("participation"), "joint.participation")
    maximum = _finite_number(
        participation.get("maximum_participation_rate"), "maximum_participation_rate",
    )
    rates = [
        _safe_finite_number(participation.get(name))
        for name in ("entry_participation_rate", "exit_participation_rate")
    ]
    return bool(all(rate is not None and rate <= maximum for rate in rates))


def _execution_metrics(economics: Sequence[Mapping[str, object]]) -> dict[str, float]:
    net_excess = [
        _finite_number(row["net_excess_return"], "net_excess_return")
        for row in economics
    ]
    decision_count = sum(_integer(row["decision_count"], "decision_count") for row in economics)
    capacity_count = sum(
        _integer(row["capacity_eligible_count"], "capacity_eligible_count")
        for row in economics
    )
    return {
        "mean_net_excess_return": sum(net_excess) / len(net_excess),
        "maximum_drawdown": _execution_maximum_drawdown(economics),
        "mean_top100_turnover": sum(
            _finite_number(row["top100_turnover"], "top100_turnover") for row in economics
        ) / len(economics),
        "capacity_coverage": capacity_count / decision_count,
    }


def _execution_maximum_drawdown(economics: Sequence[Mapping[str, object]]) -> float:
    wealth = peak = 1.0
    drawdown = 0.0
    for row in economics:
        wealth *= 1.0 + _finite_number(row["portfolio_net_return"], "portfolio_net_return")
        peak = max(peak, wealth)
        drawdown = min(drawdown, wealth / peak - 1.0)
    return drawdown


def _execution_replay_passes(
    section: Mapping[str, object],
    predictions: Sequence[Mapping[str, object]],
    reports: Sequence[Mapping[str, object]],
    economics: Sequence[Mapping[str, object]],
    metrics: Mapping[str, float],
) -> bool:
    thresholds = section.get("thresholds")
    if not isinstance(thresholds, Mapping) or set(thresholds) != {
        "minimum_mean_net_excess_return", "minimum_maximum_drawdown",
        "maximum_mean_top100_turnover", "minimum_capacity_coverage",
    }:
        return False
    report_assessments = [
        {
            "sample_id": report["sample_id"],
            "assessment_digest": _strict_mapping(
                _strict_mapping(report["evidence"], "joint.evidence")["calibration"],
                "joint.calibration",
            )["out_of_sample_assessment_digest"],
        }
        for report in reports
    ]
    estimands = {stable_probability_hash(report["estimand"]) for report in reports}
    limits = {name: _safe_finite_number(thresholds.get(name)) for name in thresholds}
    return bool(
        section.get("observation_count") == len(predictions) == len(reports)
        and section.get("independent_session_count") == len(economics) >= 60
        and section.get("prediction_digest") == stable_probability_hash(predictions)
        and section.get("joint_execution_evidence_digest") == stable_probability_hash(reports)
        and section.get("joint_execution_assessment_digest") == stable_probability_hash(report_assessments)
        and len(estimands) == 1
        and section.get("joint_execution_estimand_digest") == next(iter(estimands))
        and section.get("session_economics") == list(economics)
        and section.get("session_economics_digest") == stable_probability_hash(economics)
        and all(_same_number(section.get(name), value) for name, value in metrics.items())
        and all(value is not None for value in limits.values())
        and metrics["mean_net_excess_return"] > cast(float, limits["minimum_mean_net_excess_return"])
        and metrics["maximum_drawdown"] >= cast(float, limits["minimum_maximum_drawdown"])
        and metrics["mean_top100_turnover"] <= cast(float, limits["maximum_mean_top100_turnover"])
        and metrics["capacity_coverage"] >= cast(float, limits["minimum_capacity_coverage"])
    )


def _strict_mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} 必须是 object")
    return cast(Mapping[str, object], value)


def _require_exact_mapping_keys(
    value: Mapping[str, object], expected: set[str], path: str,
) -> None:
    if set(value) != expected:
        raise ValueError(f"{path} 字段不符合 exact schema")


def _same_number(left: object, right: object) -> bool:
    left_value, right_value = _safe_finite_number(left), _safe_finite_number(right)
    return bool(
        left_value is not None and right_value is not None
        and math.isclose(left_value, right_value, rel_tol=0, abs_tol=1e-12)
    )


def _same_interval(left: object, right: object) -> bool:
    left_value, right_value = _safe_interval(left), _safe_interval(right)
    return bool(
        left_value is not None and right_value is not None
        and all(_same_number(a, b) for a, b in zip(left_value, right_value, strict=True))
    )


def _validated_aware_timestamp(value: str, path: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{path} 无效") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{path} 必须包含时区")
    if parsed.astimezone(timezone.utc) > utc_now() + timedelta(minutes=5):
        raise ValueError(f"{path} 不能晚于当前时间")
    return parsed


def _safe_interval(value: object) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    lower, upper = (_safe_finite_number(item) for item in value)
    if lower is None or upper is None or lower > upper:
        return None
    return [lower, upper]


def _safe_finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value)):
        return None
    return float(value)


def _safe_probability_hash(value: object) -> str | None:
    try:
        return stable_probability_hash(value)
    except (TypeError, ValueError):
        return None


def _safe_positive_integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else -1


def _validate_config(config: ProbabilityConfig) -> None:
    if config.horizon not in _SUPPORTED_HORIZONS:
        raise ValueError("上涨概率 estimator horizon 仅支持 1、2、3、5、20")
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
    if config.effective_gap_sessions < config.target_session_offset:
        raise ValueError("gap_sessions 不能小于标签 target_session_offset（horizon + 1）")
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
        "entry_session_offset",
        "target_session_offsets",
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
    _validate_label_contract_text(contract, "execution_model")
    horizons = _validated_label_contract_horizons(contract, config)
    _validate_label_contract_offsets(contract, horizons)
    _validate_label_contract_targets(contract)
    _validate_label_contract_text(contract, "cost_profile_id")
    _validate_label_contract_capacity(contract)


def _validate_label_contract_text(contract: Mapping[str, object], name: str) -> None:
    value = contract[name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"上涨概率 label_contract {name} 无效")


def _validated_label_contract_horizons(
    contract: Mapping[str, object], config: ProbabilityConfig,
) -> list[int]:
    horizons = contract["horizons"]
    if (
        not isinstance(horizons, list)
        or config.horizon not in horizons
        or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in horizons)
        or len(horizons) != len(set(horizons))
    ):
        raise ValueError("上涨概率 label_contract horizons 无效")
    return cast(list[int], horizons)


def _validate_label_contract_offsets(
    contract: Mapping[str, object], horizons: Sequence[int],
) -> None:
    if contract["entry_session_offset"] != 1:
        raise ValueError("上涨概率 label_contract entry_session_offset 必须为 1")
    offsets = contract["target_session_offsets"]
    expected_offsets = {str(value): value + 1 for value in horizons}
    if not isinstance(offsets, Mapping) or dict(offsets) != expected_offsets:
        raise ValueError("上涨概率 label_contract target_session_offsets 必须等于 horizon + 1")


def _validate_label_contract_targets(contract: Mapping[str, object]) -> None:
    targets = contract["target_definitions"]
    if not isinstance(targets, list) or not targets or any(
        not isinstance(value, str) or not value.strip() for value in targets
    ):
        raise ValueError("上涨概率 label_contract target_definitions 无效")


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
        "target_session_offset": config.target_session_offset,
        "minimum_safe_gap_sessions": config.target_session_offset,
        "purge_rule": "all_prior_partition_labels_mature_strictly_before_next_partition_signal",
        "minimum_fit_independent_sessions": _minimum_fit_session_count(config),
        "minimum_selection_independent_sessions": _minimum_selection_session_count(config),
        "walk_forward": "expanding_train_rolling_calibration_and_test",
    }


def _evaluation_contract(config: ProbabilityConfig) -> dict[str, object]:
    return {
        "minimum_label_coverage": config.minimum_label_coverage,
        "minimum_bin_sessions": config.minimum_bin_sessions,
        "calibration_bin_count": config.calibration_bin_count,
        "minimum_isotonic_calibration_sessions": config.minimum_isotonic_calibration_sessions,
        "bootstrap": "deterministic_circular_moving_target_offset_block_95pct_v2",
        "bootstrap_block_length_sessions": config.target_session_offset,
        "bootstrap_samples": config.bootstrap_samples,
        "minimum_selection_folds": config.minimum_selection_folds,
        "selection_qualification": {
            "requires_complete_label_contract_binding": True,
            "requires_positive_oos_brier_skill": True,
            "requires_effective_probability_stratification": True,
            "requires_multiple_complete_oos_folds": True,
            "requires_positive_skill_in_every_complete_oos_fold": True,
        },
        "filter_qualification": {
            "version": PROBABILITY_FILTER_QUALIFICATION_VERSION,
            "requires_exact_evidence_digest_binding": True,
            "requires_positive_brier_improvement_ci_95": True,
            "requires_positive_log_loss_improvement_ci_95": True,
            "requires_multiple_testing_fdr_evidence": True,
            "requires_calibration_validation": True,
            "requires_temporal_drift_validation": True,
            "requires_execution_validation": True,
            "missing_evidence_policy": "fail_closed",
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


def _minimum_fit_session_count(config: ProbabilityConfig) -> int:
    return (
        config.minimum_train_sessions
        + config.minimum_calibration_sessions
        + config.minimum_test_sessions
        + 2 * config.effective_gap_sessions
    )


def _minimum_selection_session_count(config: ProbabilityConfig) -> int:
    return _minimum_fit_session_count(config) + (
        config.minimum_selection_folds - 1
    ) * config.minimum_test_sessions


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
    covered = sum(item.executable and item.target is not None for item in values)
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
        if not item.executable and item.target is not None:
            raise ValueError("上涨概率不可执行样本的 target 必须为 None")
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
    weights, iterations = _newton_logistic(
        design, labels, config.l2_strength, config, component="model",
    )
    return {
        "version": PROBABILITY_MODEL_VERSION,
        "feature_names": list(feature_names),
        "means": means.tolist(),
        "scales": scales.tolist(),
        "intercept": float(weights[0]),
        "coefficients": weights[1:].tolist(),
        "l2_strength": config.l2_strength,
        "iterations": iterations,
        "converged": True,
    }


def _newton_logistic(
    design: NDArray[np.float64],
    labels: NDArray[np.float64],
    l2_strength: float,
    config: ProbabilityConfig,
    *,
    component: str,
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
            raise _ProbabilityModelConvergenceError(f"{component}_singular_hessian") from exc
        weights -= step
        if float(np.max(np.abs(step))) <= config.convergence_tolerance:
            return weights, iteration
    raise _ProbabilityModelConvergenceError(f"{component}_nonconvergence")


def _fit_platt_calibrator(
    raw_probabilities: Sequence[float], labels: Sequence[int], config: ProbabilityConfig,
) -> dict[str, object]:
    logits = np.asarray([_logit(value) for value in raw_probabilities], dtype=np.float64)
    design = np.column_stack((np.ones(len(logits), dtype=np.float64), logits))
    targets = np.asarray(labels, dtype=np.float64)
    weights, iterations = _newton_logistic(
        design, targets, 1e-6, config, component="calibrator",
    )
    return {
        "version": PROBABILITY_CALIBRATOR_VERSION,
        "intercept": float(weights[0]),
        "slope": float(weights[1]),
        "iterations": iterations,
        "converged": True,
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


_PredictionMetricInputs = tuple[
    list[float], list[float], list[int], list[str], list[float],
]
_DatedMetricSeries = dict[str, list[tuple[str, float]]]


def _prediction_metric_inputs(
    predictions: Sequence[Mapping[str, object]],
) -> _PredictionMetricInputs:
    probabilities = [_finite_number(item["probability"], "probability") for item in predictions]
    baseline = [_finite_number(item["baseline_probability"], "baseline_probability") for item in predictions]
    outcomes = [_integer(item["outcome"], "outcome") for item in predictions]
    dates = [str(item["session_date"]) for item in predictions]
    references = [
        _finite_number(item["reference_base_rate"], "reference_base_rate")
        for item in predictions
    ]
    return probabilities, baseline, outcomes, dates, references


def _core_prediction_metrics(
    inputs: _PredictionMetricInputs, config: ProbabilityConfig,
) -> tuple[dict[str, object], dict[str, object], float]:
    probabilities, baseline, outcomes, dates, references = inputs
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
    return calibrated, baseline_metrics, base_rate


def _prediction_bootstrap_series(inputs: _PredictionMetricInputs) -> _DatedMetricSeries:
    probabilities, _baseline, outcomes, dates, references = inputs
    return {
        "calibration_offset": [
            (day, outcome - probability)
            for day, outcome, probability in zip(dates, outcomes, probabilities, strict=True)
        ],
        "brier_score": [
            (day, (outcome - probability) ** 2)
            for day, outcome, probability in zip(dates, outcomes, probabilities, strict=True)
        ],
        "actual_positive_rate": list(
            zip(dates, [float(value) for value in outcomes], strict=True)
        ),
        "brier_improvement_vs_reference": [
            (day, (outcome - reference) ** 2 - (outcome - probability) ** 2)
            for day, outcome, probability, reference in zip(
                dates, outcomes, probabilities, references, strict=True,
            )
        ],
        "log_loss_improvement_vs_reference": [
            (
                day,
                _binary_log_loss(outcome, reference) - _binary_log_loss(outcome, probability),
            )
            for day, outcome, probability, reference in zip(
                dates, outcomes, probabilities, references, strict=True,
            )
        ],
    }


def _attach_prediction_bootstrap_metrics(
    calibrated: dict[str, object],
    series: _DatedMetricSeries,
    config: ProbabilityConfig,
    seed: str,
) -> None:
    block_length = config.target_session_offset
    bootstrap_specs = (
        ("calibration_offset_ci_95", "calibration_offset", ":offset"),
        ("brier_score_ci_95", "brier_score", ":brier"),
        ("actual_positive_rate_ci_95", "actual_positive_rate", ":rate"),
        (
            "brier_improvement_vs_reference_ci_95",
            "brier_improvement_vs_reference",
            ":brier-improvement",
        ),
        (
            "log_loss_improvement_vs_reference_ci_95",
            "log_loss_improvement_vs_reference",
            ":log-loss-improvement",
        ),
    )
    for output_name, series_name, seed_suffix in bootstrap_specs:
        calibrated[output_name] = _date_block_bootstrap_ci(
            series[series_name], seed + seed_suffix, config.bootstrap_samples,
            block_length_sessions=block_length,
        )
    for metric_name in (
        "brier_improvement_vs_reference", "log_loss_improvement_vs_reference",
    ):
        calibrated[metric_name] = sum(value for _day, value in series[metric_name]) / len(
            series[metric_name]
        )
    calibrated["bootstrap_samples"] = config.bootstrap_samples
    calibrated["bootstrap_method"] = "deterministic_circular_moving_target_offset_block_95pct_v2"
    calibrated["bootstrap_block_length_sessions"] = block_length


def _prediction_metrics(
    predictions: Sequence[Mapping[str, object]], config: ProbabilityConfig, seed: str,
) -> dict[str, object]:
    inputs = _prediction_metric_inputs(predictions)
    calibrated, baseline_metrics, base_rate = _core_prediction_metrics(inputs, config)
    _attach_prediction_bootstrap_metrics(
        calibrated, _prediction_bootstrap_series(inputs), config, seed,
    )
    _probabilities, _baseline, outcomes, dates, references = inputs
    isotonic_metrics = _optional_candidate_metrics(
        predictions, outcomes, dates, base_rate, references, config,
    )
    return {
        "calibrated": calibrated,
        "empirical_bayes_baseline": baseline_metrics,
        "isotonic_candidate": isotonic_metrics,
        "fold_stability": _fold_selection_stability(predictions),
    }


def _binary_log_loss(outcome: int, probability: float) -> float:
    clipped = min(1.0 - 1e-15, max(1e-15, probability))
    return -(outcome * math.log(clipped) + (1 - outcome) * math.log(1.0 - clipped))


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
            "actual_positive_rate_interval": calibrated.get("actual_positive_rate_ci_95"),
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
        "actual_positive_rate_interval": None,
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
        "calibration_bias_interval": offset,
        "calibration_adjusted_probability_interval": interval,
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
            "calibration_adjusted_interval_is_not_individual_outcome_certainty",
        ],
        "generated_at": evidence.get("generated_at"),
    }


def _null_estimate(evidence: Mapping[str, object], sample_id: str) -> dict[str, object]:
    return {
        "status": "insufficient_data",
        "sample_id": sample_id,
        "probability": None,
        "raw_probability": None,
        "empirical_bayes_probability": None,
        "calibration_bias_interval": None,
        "calibration_adjusted_probability_interval": None,
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
    for name in ("model", "calibrator"):
        payload = evidence.get(name)
        if payload is not None:
            _verify_optimizer_status(_object_mapping(payload, name), name)


def _verify_optimizer_status(payload: Mapping[str, object], name: str) -> None:
    if payload.get("converged") is not True:
        raise ProbabilityReplayError(f"上涨概率 {name} 未证明优化器收敛")
    if _integer(payload.get("iterations"), f"{name}.iterations") <= 0:
        raise ProbabilityReplayError(f"上涨概率 {name} iterations 无效")


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
        if payload_name in {"model", "calibrator"}:
            _verify_optimizer_status(mapping, f"fold.{payload_name}")


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
    "PROBABILITY_DEPLOYMENT_ARTIFACT_SCHEMA_VERSION",
    "PROBABILITY_DEPLOYMENT_CONTRACT_VERSION",
    "PROBABILITY_FEATURE_VERSION",
    "PROBABILITY_FILTER_AUTHORIZATION_VERSION",
    "PROBABILITY_FILTER_AUTHORIZATION_SCHEMA_VERSION",
    "PROBABILITY_FILTER_QUALIFICATION_VERSION",
    "PROBABILITY_ISOTONIC_CALIBRATOR_VERSION",
    "PROBABILITY_LABEL_VERSION",
    "PROBABILITY_MODEL_VERSION",
    "PROBABILITY_SCHEMA_VERSION",
    "GroupedWalkForwardSplit",
    "ProbabilityConfig",
    "ProbabilityReplayError",
    "ProbabilitySample",
    "VerifiedProbabilityFilterAuthorization",
    "VerifiedProbabilityDeploymentEstimator",
    "build_probability_contract",
    "build_probability_filter_qualification",
    "evaluate_probability_predictions",
    "fit_empirical_bayes_baseline",
    "fit_probability_deployment_estimator",
    "fit_shadow_probability",
    "grouped_walk_forward_splits",
    "probability_selection_qualified",
    "probability_filter_qualified",
    "predict_shadow_probability",
    "replay_shadow_probability",
    "seal_probability_filter_authorization_artifact",
    "seal_probability_deployment_artifact",
    "stable_probability_hash",
    "verify_shadow_probability_evidence",
    "verify_probability_filter_authorization_artifact",
    "verify_probability_deployment_artifact",
]
