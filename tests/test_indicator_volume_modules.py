from __future__ import annotations

from app.services.indicator_volume import average_volume, positive_volume_ratio, recent_volume_ratio
from tests.factories import make_kline


def test_positive_volume_ratio_uses_positive_values_only() -> None:
    values = [100, 0, 100, 200, 200]

    assert positive_volume_ratio(values, recent_window=2, base_window=5, precision=2) == 1.33


def test_positive_volume_ratio_returns_neutral_for_invalid_or_empty_windows() -> None:
    assert positive_volume_ratio([100, 200], recent_window=3, base_window=5, min_count=3) == 1.0
    assert positive_volume_ratio([0, 0, 0], recent_window=2, base_window=3) == 1.0
    assert positive_volume_ratio([100, 200], recent_window=0, base_window=3) == 1.0


def test_recent_volume_ratio_keeps_legacy_minimum_sample_guard() -> None:
    klines = [make_kline(volume=100 + index * 10) for index in range(5)]

    assert recent_volume_ratio(klines, recent_window=5, base_window=20) == 1.0


def test_average_volume_ignores_non_positive_values() -> None:
    klines = [make_kline(volume=0), make_kline(volume=100), make_kline(volume=300)]

    assert average_volume(klines, 3) == 200
