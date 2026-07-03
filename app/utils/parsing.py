from __future__ import annotations

import math


MISSING_NUMERIC_VALUES = (None, "", "-", "--")


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        return required_float(value)
    except ValueError:
        return default


def required_float(value: object, field: str = "数值", *, positive: bool = False) -> float:
    if value in MISSING_NUMERIC_VALUES:
        raise ValueError(f"{field}缺失")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field}不是有效数字") from None
    if not math.isfinite(result):
        raise ValueError(f"{field}不是有限数字")
    if positive and result <= 0:
        raise ValueError(f"{field}应为正数")
    return result
