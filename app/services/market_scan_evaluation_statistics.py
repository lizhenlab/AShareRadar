"""Deterministic session-level inference helpers for market-scan research.

These helpers deliberately operate on one value per independent scan session.  They
do not turn the much larger cross-sectional stock count into a fictitious time
sample, and they do not claim to calculate PBO or the deflated Sharpe ratio.
"""

from __future__ import annotations

import hashlib
import math
import random
from statistics import fmean
from typing import Sequence


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

    finite = tuple(float(value) for value in values if math.isfinite(float(value)))
    if len(finite) < minimum_count or samples < 100 or block_length < 1:
        return None
    observed = fmean(finite)
    centred = tuple(value - observed for value in finite)
    seed = int.from_bytes(hashlib.sha256(seed_text.encode("utf-8")).digest()[:8], "big")
    generator = random.Random(seed)
    exceedances = 0
    for _sample in range(samples):
        resampled: list[float] = []
        while len(resampled) < len(centred):
            start = generator.randrange(len(centred))
            resampled.extend(
                centred[(start + offset) % len(centred)]
                for offset in range(block_length)
            )
        if fmean(resampled[: len(centred)]) >= observed - 1e-15:
            exceedances += 1
    return (exceedances + 1) / (samples + 1)


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


__all__ = ["benjamini_hochberg", "moving_block_bootstrap_p_value"]
