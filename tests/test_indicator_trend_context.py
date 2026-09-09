from __future__ import annotations

from datetime import date, timedelta
import math
from statistics import mean

import pytest

from app.services.indicator_math import moving_average
from app.services.indicator_trend import (
    TREND_SCORE_ALGORITHM_VERSION, TREND_SCORE_LEGACY_ALGORITHM_VERSION, trend_score_snapshot,
)
from app.services.indicator_trend_components import build_trend_context
from app.services.indicator_volume import recent_volume_ratio
from app.utils import market_data
from tests.factories import make_kline, make_quote


def _rows(count: int):
    return [
        make_kline(
            date=(date(2026, 1, 1) + timedelta(days=index)).isoformat(),
            close=10.005 + (index % 13) * .017 + index * .0001,
            volume=1000 + index * 7,
        )
        for index in range(count)
    ]


@pytest.mark.parametrize("count", [0, 1, 4, 5, 9, 10, 19, 20, 24, 25, 61, 240])
@pytest.mark.parametrize("algorithm", [TREND_SCORE_ALGORITHM_VERSION, TREND_SCORE_LEGACY_ALGORITHM_VERSION])
def test_trend_context_preserves_raw_and_frozen_rounded_windows(count: int, algorithm: str) -> None:
    rows = _rows(count)
    quote = make_quote()
    average = moving_average if algorithm == TREND_SCORE_LEGACY_ALGORITHM_VERSION else _unrounded_average
    expected_ma5 = average(rows, 5)
    expected_ma20 = average(rows, 20)

    for mode in ("official", "intraday", "preopen"):
        context = build_trend_context(quote, rows, mode=mode, algorithm_version=algorithm)

        assert context.ma5 == expected_ma5
        assert context.ma10 == average(rows, 10)
        assert context.ma20 == expected_ma20
        assert context.prev_ma5 == (average(rows[:-5], 5) if count >= 10 else expected_ma5)
        assert context.prev_ma20 == (average(rows[:-5], 20) if count >= 25 else expected_ma20)
        assert context.recent_high == max((row.high for row in rows[-20:]), default=0)
        assert context.recent_low == min((row.low for row in rows[-20:]), default=0)
        assert context.volume_ratio == recent_volume_ratio(rows)
        assert context.volume_confirmation_enabled == (mode == "official")


def test_trend_context_drops_bad_bars_before_defining_previous_windows() -> None:
    clean = _rows(25)
    dirty = [
        *clean[:7], clean[7].model_copy(update={"high": math.inf}),
        *clean[7:15], clean[15].model_copy(update={"volume": -1}),
        *clean[15:], clean[-1].model_copy(update={"open": 1000}),
    ]
    quote = make_quote()

    for mode in ("official", "intraday", "preopen"):
        assert build_trend_context(quote, dirty, mode=mode) == build_trend_context(quote, clean, mode=mode)
        assert trend_score_snapshot(quote, dirty, mode=mode) == trend_score_snapshot(quote, clean, mode=mode)


def test_trend_context_keeps_input_order_and_revalidates_mutated_rows() -> None:
    rows = _rows(30)
    rows = rows[::2] + rows[1::2]
    first = build_trend_context(make_quote(), rows)
    assert first.klines == rows
    assert first.ma5 == _unrounded_average(rows, 5)

    rows[-1].high = math.inf
    second = build_trend_context(make_quote(), rows)

    assert len(second.klines) == 29
    assert second.ma5 == _unrounded_average(rows, 5)
    assert second.ma5 != first.ma5
    assert second.prev_ma20 == _unrounded_average(rows[:-1][:-5], 20)


def _unrounded_average(rows, window):
    values = [row.close for row in market_data.filter_valid_klines(rows)][-window:]
    return mean(values) if values else 0


def test_trend_context_does_not_rescan_all_bars_for_each_mean(monkeypatch) -> None:
    rows = _rows(240)
    original = market_data.valid_kline
    inspections = 0

    def counted(row):
        nonlocal inspections
        inspections += 1
        return original(row)

    monkeypatch.setattr(market_data, "valid_kline", counted)

    context = build_trend_context(make_quote(), rows)

    assert len(context.klines) == 240
    assert inspections <= 2 * len(rows), "Context and volume admission may scan; each mean must use the validated window."
