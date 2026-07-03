from __future__ import annotations

from app.utils.market_data import finite_float


def bounded_int(value: object, low: int, high: int, *, default: int | None = None, round_value: bool = False) -> int:
    lower, upper = sorted((int(low), int(high)))
    parsed = finite_float(value)
    if parsed is None:
        parsed = lower if default is None else finite_float(default)
    if parsed is None:
        parsed = lower
    if round_value:
        parsed = round(parsed)
    return max(lower, min(upper, int(parsed)))


def clamp_score(value: object, *, default: int = 0, round_value: bool = False) -> int:
    return bounded_int(value, 0, 100, default=default, round_value=round_value)


def score_level(score: int) -> str:
    if score >= 80:
        return "强"
    if score >= 65:
        return "偏强"
    if score >= 50:
        return "中性"
    if score >= 35:
        return "偏弱"
    return "弱"
