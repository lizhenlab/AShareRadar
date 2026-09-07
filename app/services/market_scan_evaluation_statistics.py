"""Deterministic session-level inference helpers for market-scan research.

These helpers operate on one value per chronological scan date. Distinct dates
are not independent when forward labels overlap, so inference preserves adjacent
observations in date blocks. Stock count never substitutes for time evidence.
"""

from __future__ import annotations

import hashlib
import math
import random
from statistics import fmean
from typing import Mapping, Sequence


def inference_contract(horizon: int, minimum_count: int) -> dict[str, object]:
    return {
        "method": "deterministic-circular-moving-block-bootstrap",
        "block_length_sessions": horizon,
        "minimum_session_count": max(minimum_count, 20, 2 * horizon),
        "minimum_complete_blocks": 2,
        "order": "quote_date_ascending",
        "distinct_dates_are_not_independent_samples": True,
    }


def gross_date_confidence_intervals(
    run_dates: Mapping[int, str], daily_returns: Mapping[int, float], daily_excess: Mapping[int, float],
    *, horizon: int, minimum_count: int, samples: int, seed_text: str,
) -> dict[str, object]:
    """Gross-return inference keeps all frozen candidate/benchmark dates."""
    ordered = sorted(run_dates, key=lambda run_id: (run_dates[run_id], run_id))
    returns = [daily_returns.get(run_id, math.nan) for run_id in ordered]
    excess = [daily_excess.get(run_id, math.nan) for run_id in ordered]
    valid_returns = sum(math.isfinite(value) for value in returns)
    valid_excess = sum(math.isfinite(value) for value in excess)
    return {
        "session_return_confidence_interval_95": moving_block_bootstrap_confidence_interval(
            returns, seed_text=seed_text + ":return", samples=samples,
            block_length=horizon, minimum_count=max(20, minimum_count),
        ),
        "session_excess_confidence_interval_95": moving_block_bootstrap_confidence_interval(
            excess, seed_text=seed_text + ":excess", samples=samples,
            block_length=horizon, minimum_count=max(20, minimum_count),
        ),
        "confidence_interval_inference": {
            **inference_contract(horizon, minimum_count),
            "target": "signal-close-to-D+H-close-gross-return",
            "eligible_for_promotion": False,
            "expected_session_count": len(ordered),
            "return_valid_session_count": valid_returns,
            "return_missing_session_count": len(ordered) - valid_returns,
            "excess_valid_session_count": valid_excess,
            "excess_missing_session_count": len(ordered) - valid_excess,
        },
    }


def complete_session_count(sessions: Sequence[Mapping[str, object]], metrics: Sequence[str]) -> int:
    return sum(
        all(isinstance(value, int | float) and math.isfinite(value)
            for value in (session.get(metric) for metric in metrics))
        for session in sessions
    )


def horizon_return_drawdown_diagnostics(returns: Sequence[float]) -> dict[str, object]:
    """Keep a synthetic horizon chain separate from unavailable portfolio risk."""
    return {
        "session_maximum_drawdown": None,
        "portfolio_drawdown": {
            "status": "unavailable",
            "maximum_drawdown": None,
            "eligible_for_promotion": False,
            "reason": "per_signal_horizon_returns_are_not_a_daily_portfolio_nav",
            "missing_evidence": [
                "shared_capital_allocation_policy",
                "dated_cash_positions_and_transaction_costs",
                "daily_valuation_for_open_and_blocked_positions",
            ],
        },
        "horizon_return_chain_diagnostic": {
            "maximum_drawdown": synthetic_return_chain_maximum_drawdown(returns),
            "eligible_for_promotion": False,
            "semantics": "synthetic_compounding_of_overlapping_gross_horizon_returns",
        },
    }


def synthetic_return_chain_maximum_drawdown(returns: Sequence[float]) -> float | None:
    """Synthetic chain diagnostic only; no shared-capital portfolio is implied."""
    if not returns or any(not math.isfinite(value) or value < -1 for value in returns):
        return None
    wealth = 1.0
    peak = 1.0
    worst = 0.0
    for value in returns:
        wealth *= 1 + value
        if not math.isfinite(wealth):
            return None
        peak = max(peak, wealth)
        worst = min(worst, wealth / peak - 1)
    return worst


def moving_block_bootstrap_confidence_interval(
    values: Sequence[float],
    *,
    samples: int,
    block_length: int,
    seed_text: str,
    minimum_count: int,
) -> list[float] | None:
    """Percentile 95% interval for a chronologically ordered date-mean series.

    This uses the same circular block resampling as the one-sided test below.
    Missing/non-finite observations invalidate inference instead of compressing
    the time axis. At least two complete blocks and the declared sample floor
    are required; a singleton can never produce a zero-width interval.
    """
    series = _inference_series(values, samples, block_length, minimum_count)
    if series is None:
        return None
    means = sorted(_resampled_means(series, samples, block_length, seed_text))
    return [_percentile(means, 0.025), _percentile(means, 0.975)]


def moving_block_bootstrap_p_value(
    values: Sequence[float],
    *,
    samples: int,
    block_length: int,
    seed_text: str,
    minimum_count: int,
) -> float | None:
    """Return a one-sided p-value for H0: session mean <= 0.

    A circular moving-block bootstrap is applied to the null-centred session
    series.  The block length should normally match the forward-return horizon so
    overlapping labels are not treated as independent.  ``None`` is an explicit
    insufficient-data result, never a zero or an inferred rejection.
    """

    series = _inference_series(values, samples, block_length, minimum_count)
    if series is None:
        return None
    observed = fmean(series)
    centred = tuple(value - observed for value in series)
    exceedances = sum(
        value >= observed - 1e-15
        for value in _resampled_means(centred, samples, block_length, seed_text)
    )
    return (exceedances + 1) / (samples + 1)


def _inference_series(
    values: Sequence[float], samples: int, block_length: int, minimum_count: int,
) -> tuple[float, ...] | None:
    if any(type(value) is not int for value in (samples, block_length, minimum_count)):
        return None
    if samples < 100 or block_length < 1 or minimum_count < 1:
        return None
    if len(values) < max(minimum_count, 2 * block_length):
        return None
    series = tuple(float(value) for value in values)
    return series if all(math.isfinite(value) for value in series) else None


def _resampled_means(
    series: Sequence[float], samples: int, block_length: int, seed_text: str,
) -> list[float]:
    seed = int.from_bytes(hashlib.sha256(seed_text.encode("utf-8")).digest()[:8], "big")
    generator = random.Random(seed)
    means: list[float] = []
    for _sample in range(samples):
        resampled: list[float] = []
        while len(resampled) < len(series):
            start = generator.randrange(len(series))
            resampled.extend(
                series[(start + offset) % len(series)]
                for offset in range(block_length)
            )
        means.append(fmean(resampled[: len(series)]))
    return means


def _percentile(values: Sequence[float], probability: float) -> float:
    position = (len(values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def benjamini_hochberg(
    p_values: Sequence[float | None],
    *,
    alpha: float,
) -> tuple[tuple[float | None, ...], tuple[bool | None, ...]]:
    """Adjust a preregistered family of p-values with BH-FDR.

    Missing hypotheses remain explicit ``None`` values.  They still count toward
    the family size, which is equivalent to conservatively assigning them p=1 for
    adjustment without pretending that a test was run.
    """

    if not 0 < alpha < 1:
        raise ValueError("alpha must be in (0, 1)")
    family_size = len(p_values)
    if family_size == 0:
        return (), ()
    available: list[tuple[int, float]] = []
    for index, value in enumerate(p_values):
        if value is None:
            continue
        parsed = float(value)
        if not math.isfinite(parsed) or not 0 <= parsed <= 1:
            raise ValueError("p-values must be finite values in [0, 1] or None")
        available.append((index, parsed))
    ordered = sorted(available, key=lambda item: (item[1], item[0]))
    adjusted_by_index: dict[int, float] = {}
    running = 1.0
    for rank_index in range(len(ordered) - 1, -1, -1):
        original_index, raw = ordered[rank_index]
        rank = rank_index + 1
        running = min(running, raw * family_size / rank)
        adjusted_by_index[original_index] = min(1.0, running)
    adjusted = tuple(adjusted_by_index.get(index) for index in range(family_size))
    rejected = tuple(value <= alpha if value is not None else None for value in adjusted)
    return adjusted, rejected


def benjamini_yekutieli(
    p_values: Sequence[float | None],
    *,
    alpha: float,
) -> tuple[tuple[float | None, ...], tuple[bool | None, ...]]:
    """Conservatively adjust a declared family under arbitrary dependence.

    BY multiplies the BH adjusted values by the fixed harmonic factor
    H_m = sum(1 / rank for rank in 1..m), then caps them at one. Both ``m``
    and ``H_m`` include missing hypotheses, which retain ``None`` outputs.
    Correlated or duplicate candidates are never deduplicated after inspection.

    The dependence guarantee requires valid marginal p-values; this correction
    cannot establish their calibration or prove completeness of the trial list.
    Reference: Benjamini and Yekutieli (2001), doi:10.1214/aos/1013699998.
    """

    bh_adjusted, _ = benjamini_hochberg(p_values, alpha=alpha)
    harmonic_factor = math.fsum(1.0 / rank for rank in range(1, len(p_values) + 1))
    adjusted = tuple(None if value is None else min(1.0, value * harmonic_factor) for value in bh_adjusted)
    rejected = tuple(value <= alpha if value is not None else None for value in adjusted)
    return adjusted, rejected


__all__ = [
    "benjamini_hochberg",
    "benjamini_yekutieli",
    "complete_session_count",
    "gross_date_confidence_intervals",
    "horizon_return_drawdown_diagnostics",
    "inference_contract",
    "moving_block_bootstrap_confidence_interval",
    "moving_block_bootstrap_p_value",
    "synthetic_return_chain_maximum_drawdown",
]
