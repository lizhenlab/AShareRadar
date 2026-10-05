"""Shared strict value validation for probability fitting and evidence replay.

These checks preserve the same scalar and rejection contract on both sides of
the estimator/evidence boundary; no artifact or orchestration dependency lives
in this module.
"""

from __future__ import annotations

import math


class ProbabilityReplayError(ValueError):
    """Raised when persisted probability evidence is invalid or cannot replay."""


def validated_target(value: int | bool | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in (0, 1):
        return value
    raise ValueError("上涨概率 target 必须是 0、1 或 None")


def require_probability(value: float, label: str) -> None:
    if not 0 <= value <= 1:
        raise ValueError(f"{label} 必须在 [0, 1] 范围内")


def finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 必须是数值")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"{label} 必须是有限数值")
    return numeric


def number_sequence(value: object, label: str) -> list[float]:
    if not isinstance(value, (list, tuple)):
        raise ProbabilityReplayError(f"{label} 必须是数组")
    try:
        return [finite_number(item, label) for item in value]
    except ValueError as exc:
        raise ProbabilityReplayError(str(exc)) from exc
