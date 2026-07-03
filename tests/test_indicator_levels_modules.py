from __future__ import annotations

from app.services.indicator_levels import _ordered_levels, support_resistance
from tests.factories import make_kline


def test_support_resistance_ignores_inverted_high_low_rows() -> None:
    rows = [make_kline(close=100, high=103, low=98, volume=1000) for _ in range(20)]
    rows[-1] = make_kline(close=100, high=90, low=120, volume=5000)

    support, resistance = support_resistance(rows)

    assert support == 98
    assert resistance == 103


def test_support_resistance_uses_last_valid_close_when_latest_close_is_invalid() -> None:
    rows = [make_kline(close=100, high=103, low=98, volume=1000) for _ in range(18)]
    rows.append(make_kline(close=120, high=121, low=118, volume=2000))
    rows.append(make_kline(close=0, high=115, low=99, volume=2000))

    _support, resistance = support_resistance(rows)

    assert resistance == 121


def test_support_resistance_uses_realtime_price_for_breakdown_with_valid_recent_low() -> None:
    rows = [make_kline(close=100, high=103, low=98, volume=1000) for _ in range(19)]
    rows.append(make_kline(close=100, high=102, low=94, volume=1800))

    support, _resistance = support_resistance(rows, current_price=90)

    assert support == 94


def test_ordered_levels_keeps_support_below_resistance_on_noisy_data() -> None:
    assert _ordered_levels(120, 100) == (100, 120)
    assert _ordered_levels(98, 103) == (98, 103)
