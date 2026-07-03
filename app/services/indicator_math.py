from __future__ import annotations

from statistics import mean

from app.models.schemas import Kline
from app.utils.market_data import filter_valid_klines, finite_float


def moving_average(klines: list[Kline], window: int) -> float:
    if not klines or window <= 0:
        return 0
    values = [item.close for item in filter_valid_klines(klines)[-window:]]
    if not values:
        return 0
    return round(mean(values), 2)


def average_true_range(klines: list[Kline], window: int = 14) -> float:
    if len(klines) < 2:
        return 0
    valid_rows = filter_valid_klines(klines)
    rows = valid_rows[-(window + 1) :] if window > 0 else valid_rows
    ranges: list[float] = []
    for index in range(1, len(rows)):
        current = rows[index]
        previous = rows[index - 1]
        true_range = max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        if true_range > 0:
            ranges.append(true_range)
    if not ranges:
        return 0
    return round(mean(ranges), 2)


def daily_return_volatility(klines: list[Kline], window: int = 20) -> float:
    valid_rows = filter_valid_klines(klines)
    rows = valid_rows[-(window + 1) :] if window > 0 else valid_rows
    if len(rows) < 2:
        return 0
    returns = [pct_change(rows[index].close, rows[index - 1].close) for index in range(1, len(rows)) if rows[index - 1].close]
    return round(volatility(returns), 2)


def pct_change(new: float, old: float) -> float:
    parsed_new = finite_float(new)
    parsed_old = finite_float(old)
    if parsed_new is None or parsed_old is None or parsed_old == 0:
        return 0
    return (parsed_new - parsed_old) / parsed_old * 100


def max_drawdown(closes: list[float]) -> float:
    valid_closes = [value for value in (finite_float(item) for item in closes) if value is not None]
    if not valid_closes:
        return 0
    peak = valid_closes[0]
    worst = 0.0
    for close in valid_closes:
        peak = max(peak, close)
        drawdown = pct_change(close, peak)
        worst = min(worst, drawdown)
    return worst


def quantile(values: list[float], ratio: float) -> float:
    valid_values = [value for value in (finite_float(item) for item in values) if value is not None]
    if not valid_values:
        return 0
    if len(valid_values) == 1:
        return valid_values[0]
    ratio = max(0, min(1, ratio))
    position = (len(valid_values) - 1) * ratio
    lower = int(position)
    upper = min(lower + 1, len(valid_values) - 1)
    weight = position - lower
    return valid_values[lower] * (1 - weight) + valid_values[upper] * weight


def volatility(returns: list[float]) -> float:
    valid_returns = [value for value in (finite_float(item) for item in returns) if value is not None]
    if not valid_returns:
        return 0
    avg = mean(valid_returns)
    variance = mean([(item - avg) ** 2 for item in valid_returns])
    return variance ** 0.5


def trend_days(rows: list[Kline]) -> int:
    count = 0
    valid_rows = filter_valid_klines(rows)
    for index in range(len(valid_rows)):
        if index < 4:
            continue
        ma5 = mean([item.close for item in valid_rows[index - 4 : index + 1]])
        if valid_rows[index].close >= ma5:
            count += 1
    return count


__all__ = [
    "average_true_range",
    "daily_return_volatility",
    "max_drawdown",
    "moving_average",
    "pct_change",
    "quantile",
    "trend_days",
    "volatility",
]
