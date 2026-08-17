"""Pure deterministic metrics for full-market probability research.

This module owns calculation-only probability evaluation primitives.  It has no
dependency on fitted models, persisted artifacts, or the market-scan write path.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
import hashlib
import math
import random
from typing import cast


PROBABILITY_BASELINE_VERSION = "shadow-up-probability-empirical-bayes-bins-v1"


def evaluate_probability_predictions(
    probabilities: Sequence[float],
    outcomes: Sequence[int | bool],
    session_dates: Sequence[str],
    *,
    base_rate: float,
    bin_count: int = 5,
    reference_probabilities: Sequence[float] | None = None,
) -> dict[str, object]:
    """Compute proper scoring, discrimination, calibration and monotonicity metrics."""
    rows = validated_metric_rows(probabilities, outcomes, session_dates)
    if not rows:
        raise ValueError("概率评估至少需要一个观测")
    require_probability(base_rate, "base_rate")
    if bin_count < 2:
        raise ValueError("bin_count 不能小于 2")
    references, reference_definition = metric_reference_probabilities(
        reference_probabilities, base_rate, len(rows),
    )
    brier, reference = brier_scores(rows, references)
    bins = calibration_bins(rows, bin_count)
    return {
        "observation_count": len(rows),
        "independent_session_count": len({row[2] for row in rows}),
        "base_rate": base_rate,
        "reference_base_rate_mean": sum(references) / len(references),
        "reference_brier_score": reference,
        "reference_definition": reference_definition,
        "actual_positive_rate": sum(row[1] for row in rows) / len(rows),
        "brier_score": brier,
        "brier_skill_score": None if reference <= 0 else 1.0 - brier / reference,
        "log_loss": log_loss(rows),
        "ece": expected_calibration_error(bins, len(rows)),
        "auc": auc(rows),
        "calibration_bins": bins,
        "bin_monotonic": bins_are_monotonic(bins),
        "highest_bin_above_base_rate": bool(
            bins and finite_number(bins[-1]["actual_rate"], "highest bin actual_rate") > base_rate
        ),
    }


def fit_empirical_bayes_baseline(
    scores: Sequence[float],
    outcomes: Sequence[int | bool],
    *,
    bin_count: int = 10,
    prior_strength: float = 20.0,
) -> dict[str, object]:
    """Fit a deterministic empirical-Bayes binned probability baseline."""
    values, labels = validated_scores_and_labels(scores, outcomes)
    if bin_count < 2 or prior_strength <= 0 or not math.isfinite(prior_strength):
        raise ValueError("经验贝叶斯分箱参数无效")
    base_rate = (sum(labels) + 1.0) / (len(labels) + 2.0)
    boundaries = quantile_boundaries(values, bin_count)
    counts = [0] * (len(boundaries) + 1)
    positives = [0] * len(counts)
    for score, label in zip(values, labels, strict=True):
        index = bisect_right(boundaries, score)
        counts[index] += 1
        positives[index] += label
    probabilities = [
        (positive + prior_strength * base_rate) / (count + prior_strength)
        for count, positive in zip(counts, positives, strict=True)
    ]
    return {
        "version": PROBABILITY_BASELINE_VERSION,
        "boundaries": boundaries,
        "probabilities": probabilities,
        "counts": counts,
        "positives": positives,
        "base_rate": base_rate,
        "prior_strength": prior_strength,
    }


def metric_reference_probabilities(
    values: Sequence[float] | None, base_rate: float, observation_count: int,
) -> tuple[list[float], str]:
    if values is None:
        return [base_rate] * observation_count, "constant_base_rate"
    if len(values) != observation_count:
        raise ValueError("参考概率与概率观测长度必须一致")
    references = [finite_number(value, "reference_probability") for value in values]
    for value in references:
        require_probability(value, "reference_probability")
    return references, "per_observation_calibration_base_rate"


def brier_scores(
    rows: Sequence[tuple[float, int, str]], references: Sequence[float],
) -> tuple[float, float]:
    brier = sum((probability - outcome) ** 2 for probability, outcome, _date in rows) / len(rows)
    reference = sum(
        (reference_probability - outcome) ** 2
        for reference_probability, (_probability, outcome, _date) in zip(references, rows, strict=True)
    ) / len(rows)
    return brier, reference


def expected_calibration_error(
    bins: Sequence[Mapping[str, object]], observation_count: int,
) -> float:
    weighted_errors = (
        integer(item["count"], "calibration bin count")
        * abs(
            finite_number(item["actual_rate"], "calibration actual_rate")
            - finite_number(item["mean_probability"], "calibration mean_probability")
        )
        for item in bins
    )
    return sum(weighted_errors) / observation_count


def validated_metric_rows(
    probabilities: Sequence[float], outcomes: Sequence[int | bool], session_dates: Sequence[str],
) -> list[tuple[float, int, str]]:
    if not (len(probabilities) == len(outcomes) == len(session_dates)):
        raise ValueError("概率、结果和交易日长度必须一致")
    rows: list[tuple[float, int, str]] = []
    for probability, outcome, session_date in zip(probabilities, outcomes, session_dates, strict=True):
        numeric = finite_number(probability, "probability")
        require_probability(numeric, "probability")
        label = validated_target(outcome)
        if label is None:
            raise ValueError("概率评估结果不能为 None")
        rows.append((numeric, label, validated_date(session_date)))
    return rows


def calibration_bins(
    rows: Sequence[tuple[float, int, str]], bin_count: int,
) -> list[dict[str, object]]:
    grouped: list[list[tuple[float, int, str]]] = [[] for _index in range(bin_count)]
    for row in rows:
        index = min(bin_count - 1, int(row[0] * bin_count))
        grouped[index].append(row)
    bins: list[dict[str, object]] = []
    for index, values in enumerate(grouped):
        if not values:
            continue
        bins.append(
            {
                "lower": index / bin_count,
                "upper": (index + 1) / bin_count,
                "count": len(values),
                "independent_session_count": len({item[2] for item in values}),
                "mean_probability": sum(item[0] for item in values) / len(values),
                "actual_rate": sum(item[1] for item in values) / len(values),
            }
        )
    return bins


def bins_are_monotonic(bins: Sequence[Mapping[str, object]]) -> bool:
    rates = [finite_number(item["actual_rate"], "calibration actual_rate") for item in bins]
    return all(right + 1e-12 >= left for left, right in zip(rates, rates[1:], strict=False))


def log_loss(rows: Sequence[tuple[float, int, str]]) -> float:
    total = 0.0
    for probability, outcome, _date in rows:
        clipped = min(1.0 - 1e-15, max(1e-15, probability))
        total -= outcome * math.log(clipped) + (1 - outcome) * math.log(1.0 - clipped)
    return total / len(rows)


def auc(rows: Sequence[tuple[float, int, str]]) -> float | None:
    positive_count = sum(outcome for _probability, outcome, _date in rows)
    negative_count = len(rows) - positive_count
    if not positive_count or not negative_count:
        return None
    ordered = sorted((probability, outcome) for probability, outcome, _date in rows)
    wins = 0.0
    negatives_seen = 0
    index = 0
    while index < len(ordered):
        group_end = index + 1
        while group_end < len(ordered) and ordered[group_end][0] == ordered[index][0]:
            group_end += 1
        group = ordered[index:group_end]
        group_positives = sum(outcome for _probability, outcome in group)
        group_negatives = len(group) - group_positives
        wins += group_positives * negatives_seen + 0.5 * group_positives * group_negatives
        negatives_seen += group_negatives
        index = group_end
    return wins / (positive_count * negative_count)


def date_block_bootstrap_ci(
    rows: Sequence[tuple[str, float]],
    seed_text: str,
    bootstrap_samples: int,
    *,
    block_length_sessions: int = 1,
) -> list[float]:
    """Return a deterministic circular-moving-session-block percentile interval.

    Rows from the same session remain clustered.  Blocks are sampled from the
    chronologically ordered circular session series and concatenated until the original
    number of sessions is reached.  ``block_length_sessions=1`` intentionally
    preserves the former date-cluster bootstrap for callers without overlapping
    forward labels; probability studies pre-register a block at least as long
    as their holding horizon.
    """
    if (
        isinstance(block_length_sessions, bool)
        or not isinstance(block_length_sessions, int)
        or block_length_sessions <= 0
    ):
        raise ValueError("bootstrap block_length_sessions 必须是正整数")
    grouped: dict[str, list[float]] = defaultdict(list)
    for session_date, value in rows:
        grouped[session_date].append(finite_number(value, "bootstrap value"))
    dates = sorted(grouped)
    if not dates:
        raise ValueError("bootstrap 至少需要一个交易日")
    aggregates = {key: (sum(values), len(values)) for key, values in grouped.items()}
    if len(dates) == 1:
        mean = sum(grouped[dates[0]]) / len(grouped[dates[0]])
        return [mean, mean]
    generator = random.Random(int.from_bytes(hashlib.sha256(seed_text.encode()).digest()[:8], "big"))
    block_length = block_length_sessions
    estimates: list[float] = []
    for _sample in range(bootstrap_samples):
        selected: list[str] = []
        while len(selected) < len(dates):
            start = generator.randrange(len(dates))
            selected.extend(
                dates[(start + offset) % len(dates)]
                for offset in range(block_length)
            )
        selected = selected[: len(dates)]
        total = sum(aggregates[selected_date][0] for selected_date in selected)
        count = sum(aggregates[selected_date][1] for selected_date in selected)
        estimates.append(total / count)
    estimates.sort()
    return [percentile(estimates, 0.025), percentile(estimates, 0.975)]


def validated_scores_and_labels(
    scores: Sequence[float], outcomes: Sequence[int | bool],
) -> tuple[list[float], list[int]]:
    if not scores or len(scores) != len(outcomes):
        raise ValueError("经验贝叶斯分箱分数和结果必须非空且长度一致")
    values = [finite_number(value, "score") for value in scores]
    labels = [validated_target(value) for value in outcomes]
    if any(value is None for value in labels):
        raise ValueError("经验贝叶斯结果不能为 None")
    return values, cast(list[int], labels)


def quantile_boundaries(values: Sequence[float], bin_count: int) -> list[float]:
    ordered = sorted(values)
    candidates = [percentile(ordered, index / bin_count) for index in range(1, bin_count)]
    boundaries: list[float] = []
    for value in candidates:
        if not boundaries or value > boundaries[-1]:
            boundaries.append(value)
    return boundaries


def percentile(values: Sequence[float], probability: float) -> float:
    if len(values) == 1:
        return float(values[0])
    position = (len(values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(values[lower])
    fraction = position - lower
    return float(values[lower] * (1.0 - fraction) + values[upper] * fraction)


def validated_target(value: int | bool | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if value not in (0, 1):
        raise ValueError("上涨概率 target 必须是 0/1/None")
    return value


def require_probability(value: float, label: str) -> None:
    if not 0 <= value <= 1:
        raise ValueError(f"{label} 必须在 [0, 1] 范围内")


def validated_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("上涨概率 session_date 必须是 ISO 交易日") from exc
    return parsed.isoformat()


def finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 必须是数值")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"{label} 必须是有限数值")
    return numeric


def integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} 必须是整数")
    return value


__all__ = [
    "PROBABILITY_BASELINE_VERSION",
    "evaluate_probability_predictions",
    "fit_empirical_bayes_baseline",
]
