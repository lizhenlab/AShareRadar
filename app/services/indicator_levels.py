from __future__ import annotations

from dataclasses import dataclass

from app.models.schemas import Kline
from app.services.indicator_math import quantile
from app.utils.market_data import finite_float, valid_kline


LEVEL_LOOKBACK_DAYS = 20
RECENT_BREAK_WINDOW = 5
SUPPORT_QUANTILE = 0.18
RESISTANCE_QUANTILE = 0.82


@dataclass(frozen=True)
class LevelWindow:
    rows: list[Kline]
    lows: list[float]
    highs: list[float]
    support_base: float
    resistance_base: float


def support_resistance(klines: list[Kline], current_price: float | None = None) -> tuple[float, float]:
    window = _level_window(klines)
    if window is None:
        return 0, 0
    price = _effective_current_price(current_price, window.rows)
    support = _support_level(window, price)
    resistance = _resistance_level(window, price)
    support, resistance = _ordered_levels(support, resistance)
    return round(support, 2), round(resistance, 2)


def _level_window(klines: list[Kline]) -> LevelWindow | None:
    rows = _valid_level_rows(klines[-LEVEL_LOOKBACK_DAYS:])
    lows = sorted(item.low for item in rows)
    highs = sorted(item.high for item in rows)
    if not rows or not lows or not highs:
        return None
    return LevelWindow(
        rows=rows,
        lows=lows,
        highs=highs,
        support_base=quantile(lows, SUPPORT_QUANTILE),
        resistance_base=quantile(highs, RESISTANCE_QUANTILE),
    )


def _valid_level_rows(rows: list[Kline]) -> list[Kline]:
    return [item for item in rows if _valid_level_row(item)]


def _valid_level_row(row: Kline) -> bool:
    return valid_kline(row)


def _effective_current_price(current_price: float | None, rows: list[Kline]) -> float:
    price = finite_float(current_price)
    if price and price > 0:
        return price
    closes = [item.close for item in rows if item.close > 0]
    return closes[-1] if closes else 0


def _support_level(window: LevelWindow, current_price: float) -> float:
    if current_price > 0 and current_price < window.support_base:
        return _recent_valid_low(window.rows, fallback=window.support_base)
    return window.support_base


def _resistance_level(window: LevelWindow, current_price: float) -> float:
    if current_price > window.resistance_base:
        return _recent_valid_high(window.rows, fallback=window.resistance_base)
    return window.resistance_base


def _recent_valid_low(rows: list[Kline], *, fallback: float) -> float:
    values = [item.low for item in rows[-RECENT_BREAK_WINDOW:] if _valid_level_row(item)]
    return min(values) if values else fallback


def _recent_valid_high(rows: list[Kline], *, fallback: float) -> float:
    values = [item.high for item in rows[-RECENT_BREAK_WINDOW:] if _valid_level_row(item)]
    return max(values) if values else fallback


def _ordered_levels(support: float, resistance: float) -> tuple[float, float]:
    if support > 0 and resistance > 0 and support > resistance:
        return resistance, support
    return support, resistance


__all__ = ["support_resistance"]
