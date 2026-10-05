"""Pure deterministic probability estimators and fitted-value prediction.

This numerical layer owns the registered optimizer/calibrator versions and
fail-closed convergence behavior. Read-only structural inputs keep it independent
of study configuration, evidence authorization, persisted sources and runtime.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
import math
from typing import Protocol, cast

import numpy as np
from numpy.typing import NDArray

from app.services.market_scan_probability_values import (
    ProbabilityReplayError, finite_number, number_sequence, require_probability,
    validated_target,
)


PROBABILITY_MODEL_VERSION = "shadow-up-probability-logit-l2-v2-convergence-required"
PROBABILITY_CALIBRATOR_VERSION = "shadow-up-probability-platt-v2-convergence-required"
PROBABILITY_ISOTONIC_CALIBRATOR_VERSION = "shadow-up-probability-isotonic-pav-v1"


class ProbabilityModelConvergenceError(ValueError):
    """Fail-closed signal for an optimizer that did not converge."""


class ProbabilityOptimizerConfig(Protocol):
    """Only numerical settings consumed by the registered optimizer."""

    @property
    def l2_strength(self) -> float: ...

    @property
    def maximum_iterations(self) -> int: ...

    @property
    def convergence_tolerance(self) -> float: ...


class ProbabilityObservation(Protocol):
    """Point-in-time features and the already validated eventual label."""

    @property
    def features(self) -> Mapping[str, float]: ...

    @property
    def target(self) -> int | bool | None: ...


def fit_probability_logistic_model(
    samples: Sequence[ProbabilityObservation],
    feature_names: tuple[str, ...],
    config: ProbabilityOptimizerConfig,
) -> dict[str, object]:
    """Fit the registered standardized L2 model without changing sample order."""
    matrix = np.asarray([[float(item.features[name]) for name in feature_names] for item in samples], dtype=np.float64)
    labels = np.asarray([probability_required_label(item) for item in samples], dtype=np.float64)
    means = matrix.mean(axis=0)
    scales = matrix.std(axis=0)
    scales = np.where(scales > 1e-12, scales, 1.0)
    standardized = (matrix - means) / scales
    design = np.column_stack((np.ones(len(samples), dtype=np.float64), standardized))
    weights, iterations = _newton_logistic(
        design,
        labels,
        config.l2_strength,
        config,
        component="model",
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


def fit_probability_platt_calibrator(
    raw_probabilities: Sequence[float],
    labels: Sequence[int],
    config: ProbabilityOptimizerConfig,
) -> dict[str, object]:
    """Fit the registered convergent calibration-only Platt transform."""
    logits = np.asarray([_logit(value) for value in raw_probabilities], dtype=np.float64)
    design = np.column_stack((np.ones(len(logits), dtype=np.float64), logits))
    targets = np.asarray(labels, dtype=np.float64)
    weights, iterations = _newton_logistic(
        design,
        targets,
        1e-6,
        config,
        component="calibrator",
    )
    return {
        "version": PROBABILITY_CALIBRATOR_VERSION,
        "intercept": float(weights[0]),
        "slope": float(weights[1]),
        "iterations": iterations,
        "converged": True,
        "fit_partition": "calibration_only",
    }


def fit_probability_isotonic_calibrator(
    raw_probabilities: Sequence[float],
    labels: Sequence[int],
) -> dict[str, object]:
    """Fit deterministic weighted PAV blocks on the independent calibration partition."""
    grouped: list[list[float]] = []
    for score, label in sorted(zip(raw_probabilities, labels, strict=True)):
        require_probability(score, "isotonic raw probability")
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


def probability_model_probability(model: Mapping[str, object], features: Mapping[str, float]) -> float:
    """Replay one fitted model after strict feature and numeric validation."""
    names = cast(Sequence[str], model.get("feature_names"))
    if tuple(sorted(features)) != tuple(names):
        raise ValueError("上涨概率预测特征集合与模型不一致")
    means = number_sequence(model.get("means"), "model.means")
    scales = number_sequence(model.get("scales"), "model.scales")
    coefficients = number_sequence(model.get("coefficients"), "model.coefficients")
    if not (len(names) == len(means) == len(scales) == len(coefficients)):
        raise ProbabilityReplayError("上涨概率模型维度损坏")
    linear = finite_number(model.get("intercept"), "model.intercept")
    for name, mean, scale, coefficient in zip(names, means, scales, coefficients, strict=True):
        value = finite_number(features[name], f"features.{name}")
        if scale <= 0:
            raise ProbabilityReplayError("上涨概率模型 scale 无效")
        linear += coefficient * (value - mean) / scale
    return _sigmoid(linear)


def probability_platt_probability(calibrator: Mapping[str, object], raw_probability: float) -> float:
    """Replay a fitted Platt calibration with registered logit clipping."""
    intercept = finite_number(calibrator.get("intercept"), "calibrator.intercept")
    slope = finite_number(calibrator.get("slope"), "calibrator.slope")
    return _sigmoid(intercept + slope * _logit(raw_probability))


def probability_isotonic_probability(calibrator: Mapping[str, object], raw_probability: float) -> float:
    """Replay the registered PAV block boundary assignment."""
    require_probability(raw_probability, "isotonic raw probability")
    bounds = number_sequence(calibrator.get("upper_bounds"), "isotonic.upper_bounds")
    probabilities = number_sequence(calibrator.get("probabilities"), "isotonic.probabilities")
    if not bounds or len(bounds) != len(probabilities):
        raise ProbabilityReplayError("上涨概率 Isotonic 校准器维度损坏")
    probability = probabilities[min(len(probabilities) - 1, bisect_left(bounds, raw_probability))]
    require_probability(probability, "isotonic probability")
    return probability


def probability_baseline_probability(baseline: Mapping[str, object], score: float) -> float:
    """Replay the registered empirical-Bayes boundary assignment."""
    boundaries = number_sequence(baseline.get("boundaries"), "baseline.boundaries")
    probabilities = number_sequence(baseline.get("probabilities"), "baseline.probabilities")
    if len(probabilities) != len(boundaries) + 1:
        raise ProbabilityReplayError("上涨概率经验贝叶斯分箱维度损坏")
    probability = probabilities[bisect_right(boundaries, score)]
    require_probability(probability, "baseline probability")
    return probability


def probability_required_label(item: ProbabilityObservation) -> int:
    """Require an observed binary label before fitting or OOS evaluation."""
    label = validated_target(item.target)
    if label is None:
        raise ValueError("上涨概率训练分区缺少 target")
    return label


def _newton_logistic(
    design: NDArray[np.float64],
    labels: NDArray[np.float64],
    l2_strength: float,
    config: ProbabilityOptimizerConfig,
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
            raise ProbabilityModelConvergenceError(f"{component}_singular_hessian") from exc
        weights -= step
        if float(np.max(np.abs(step))) <= config.convergence_tolerance:
            return weights, iteration
    raise ProbabilityModelConvergenceError(f"{component}_nonconvergence")


def _isotonic_rate(block: Sequence[float]) -> float:
    return block[3] / block[2]


def _sigmoid(value: float) -> float:
    bounded = max(-35.0, min(35.0, value))
    return 1.0 / (1.0 + math.exp(-bounded))


def _sigmoid_array(values: NDArray[np.float64]) -> NDArray[np.float64]:
    bounded = np.clip(values, -35.0, 35.0)
    return cast(NDArray[np.float64], 1.0 / (1.0 + np.exp(-bounded)))


def _logit(probability: float) -> float:
    require_probability(probability, "probability")
    clipped = min(1.0 - 1e-12, max(1e-12, probability))
    return math.log(clipped / (1.0 - clipped))
