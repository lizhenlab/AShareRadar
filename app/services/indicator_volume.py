from __future__ import annotations

from statistics import mean

from app.models.schemas import Kline
from app.utils.market_data import filter_valid_klines, finite_float


def recent_volume_ratio(klines: list[Kline], recent_window: int = 5, base_window: int = 20) -> float:
    values = [item.volume for item in filter_valid_klines(klines)]
    return positive_volume_ratio(values, recent_window, base_window, min_count=recent_window + 1, precision=2)


def positive_volume_ratio(
    values: list[float],
    recent_window: int = 5,
    base_window: int = 20,
    *,
    min_count: int | None = None,
    precision: int | None = None,
) -> float:
    if _invalid_volume_ratio_input(values, recent_window, base_window, min_count):
        return 1.0
    recent = _positive_values(values[-recent_window:])
    base = _positive_values(values[-base_window:])
    return _positive_average_ratio(recent, base, precision)


def average_volume(klines: list[Kline], window: int) -> float | None:
    if window <= 0:
        return None
    sample = _positive_values([item.volume for item in filter_valid_klines(klines)[-window:]])
    if not sample:
        return None
    return mean(sample)


def _invalid_volume_ratio_input(values: list[float], recent_window: int, base_window: int, min_count: int | None) -> bool:
    required_count = min_count if min_count is not None else recent_window
    return recent_window <= 0 or base_window <= 0 or len(values) < required_count


def _positive_values(values: list[float]) -> list[float]:
    parsed_values = (finite_float(value) for value in values)
    return [value for value in parsed_values if value is not None and value > 0]


def _positive_average_ratio(recent: list[float], base: list[float], precision: int | None) -> float:
    if not recent or not base:
        return 1.0
    base_avg = mean(base)
    if base_avg <= 0:
        return 1.0
    ratio = mean(recent) / base_avg
    return round(ratio, precision) if precision is not None else ratio


__all__ = ["average_volume", "positive_volume_ratio", "recent_volume_ratio"]
