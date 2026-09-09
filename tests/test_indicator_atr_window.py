from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import mean

import pytest

from app.models.market import Kline
from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.indicator_math import average_true_range
from app.services.research_features import build_feature_snapshot
from app.services.stock_insights import build_stock_insight_bundle
from tests.factories import make_kline, make_quote


@pytest.mark.parametrize("active_count", [1, 2, 7, 13, 14])
def test_average_true_range_counts_valid_zero_ranges_in_the_declared_window(active_count: int) -> None:
    ranges = [0.0] * (14 - active_count) + [0.4] * active_count
    rows = _rows([0.0, *ranges])

    assert average_true_range(rows, 14) == round(mean(ranges), 2)


def test_zero_range_sessions_dilute_an_earlier_move_without_extending_the_window() -> None:
    rows = _rows([0.0, 0.4])
    values = [average_true_range(rows, 14)]
    for index in range(1, 15):
        rows.append(rows[-1].model_copy(update={
            "date": (date(2026, 9, 7) + timedelta(days=index)).isoformat(),
            "high": 10.0,
        }))
        values.append(average_true_range(rows, 14))

    assert values[0] == 0.4
    assert values[1] == 0.2
    assert values[-2] == 0.03
    assert values[-1] == 0
    assert all(right <= left for left, right in zip(values[:-1], values[1:], strict=True))


def test_true_range_retains_gaps_even_when_daily_high_equals_low() -> None:
    rows = _rows([0.0] * 15)
    rows[-1] = rows[-1].model_copy(update={"open": 11.0, "close": 11.0, "high": 11.0, "low": 11.0})

    assert average_true_range(rows, 14) == round(1 / 14, 2)


def test_invalid_bars_do_not_become_zero_range_observations() -> None:
    rows = _rows([0.0, 0.4])
    invalid = rows[-1].model_copy(update={"high": 0.0})

    assert average_true_range([*rows, invalid], 14) == 0.4


@pytest.mark.parametrize("last_range", [0.0, 0.4])
def test_public_analysis_feature_chain_preserves_zero_sessions_and_metric_availability(last_range: float) -> None:
    rows = _rows([0.0] * 79 + [last_range])
    quote = make_quote(
        price=10, prev_close=10, high=10 + last_range, low=10, change_pct=0,
        timestamp="2026-09-07 15:15:00",
    ).model_copy(update={"open": 10.0})
    quality = build_data_quality(quote, rows, now=datetime(2026, 9, 7, 15, 16))
    analysis = build_analysis(quote, rows, data_quality=quality)
    feature = build_feature_snapshot(analysis, build_stock_insight_bundle(analysis))
    expected = round(last_range / 14, 2)

    assert quality.score == 100
    assert feature.atr14_available is True
    assert feature.atr14 == expected
    assert feature.atr_pct == round(expected / quote.price * 100, 2)


def _rows(ranges: list[float]) -> list[Kline]:
    dates: list[str] = []
    day = date(2026, 9, 7)
    while len(dates) < len(ranges):
        if day.weekday() < 5:
            dates.append(day.isoformat())
        day -= timedelta(days=1)
    return [
        make_kline(close=10, high=10 + value, low=10, date=day, source="测试行情").model_copy(update={"open": 10.0})
        for day, value in zip(reversed(dates), ranges, strict=True)
    ]
