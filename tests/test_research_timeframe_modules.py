from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.schemas import TimeframeTrend
from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.research_features import build_feature_snapshot
from app.services.research_timeframe import (
    _timeframe_alignment_label,
    _timeframe_conflict_level,
    _timeframe_trend,
    build_timeframe_alignment_report,
)
from app.services.stock_insights import build_stock_insight_bundle
from tests.factories import make_kline, make_quote


def test_timeframe_trend_scores_short_term_with_feature_trend_blend() -> None:
    analysis, feature = _timeframe_inputs([100, 102, 104, 106, 108], latest=110)
    feature = feature.model_copy(update={"price": 110, "trend_score": 60})

    trend = _timeframe_trend(analysis, feature, "短线", 20)

    assert trend.score == 72
    assert trend.return_pct == 10
    assert trend.above_ma is True
    assert trend.ma_value == 104
    assert any("高于" in item for item in trend.evidence)


def test_timeframe_trend_scores_downside_without_short_term_blend() -> None:
    analysis, feature = _timeframe_inputs([100, 98, 96, 94, 90], latest=88)
    feature = feature.model_copy(update={"price": 88, "trend_score": 60})

    trend = _timeframe_trend(analysis, feature, "波段", 20)

    assert trend.score == 22
    assert trend.return_pct == -12
    assert trend.max_drawdown_pct == -10
    assert trend.above_ma is False


def test_timeframe_trend_uses_neutral_result_when_sample_is_too_small() -> None:
    analysis, feature = _timeframe_inputs([100, 101, 102, 103], latest=104)

    trend = _timeframe_trend(analysis, feature, "短线", 20)

    assert trend.score == 50
    assert trend.label == "样本不足"
    assert trend.window_days == 20
    assert trend.evidence == ["K线样本不足，暂按中性处理。"]


def test_timeframe_trend_falls_back_to_last_kline_when_feature_price_is_invalid() -> None:
    analysis, feature = _timeframe_inputs([100, 102, 104, 106, 108], latest=108)
    feature = feature.model_copy(update={"price": 0, "trend_score": 60})

    trend = _timeframe_trend(analysis, feature, "波段", 20)

    assert trend.return_pct == 8
    assert trend.above_ma is True
    assert trend.score > 50


def test_timeframe_trend_treats_non_positive_window_as_insufficient() -> None:
    analysis, feature = _timeframe_inputs([100, 102, 104, 106, 108], latest=108)

    trend = _timeframe_trend(analysis, feature, "短线", 0)

    assert trend.window_days == 0
    assert trend.label == "样本不足"


def test_timeframe_conflict_level_boundaries_are_stable() -> None:
    assert _timeframe_conflict_level([_trend("短线", 80)]) == "待确认"
    assert _timeframe_conflict_level([_trend("短线", 80), _trend("波段", 44)]) == "高冲突"
    assert _timeframe_conflict_level([_trend("短线", 62), _trend("波段", 45)]) == "中冲突"
    assert _timeframe_conflict_level([_trend("短线", 55), _trend("波段", 60)]) == "多周期顺向"
    assert _timeframe_conflict_level([_trend("短线", 48), _trend("波段", 42)]) == "多周期偏弱"
    assert _timeframe_conflict_level([_trend("短线", 52), _trend("波段", 58)]) == "轻微分歧"


def test_timeframe_conflict_does_not_override_same_direction_with_large_spread() -> None:
    assert _timeframe_conflict_level([_trend("短线", 55), _trend("波段", 92)]) == "多周期顺向"
    assert _timeframe_conflict_level([_trend("短线", 12), _trend("波段", 48)]) == "多周期偏弱"


def test_timeframe_alignment_label_follows_explicit_directional_conflict_levels() -> None:
    assert _timeframe_alignment_label(58, "多周期顺向") == "多周期顺向"
    assert _timeframe_alignment_label(52, "多周期偏弱") == "多周期偏弱"


def test_timeframe_alignment_has_no_valid_period_below_20_rows() -> None:
    closes = [100 + index * 0.2 for index in range(19)]
    analysis, feature = _timeframe_inputs(closes, latest=closes[-1])

    report = build_timeframe_alignment_report(analysis, feature, SimpleNamespace(total_score=50))

    assert report.timeframes == []
    assert report.alignment_score == 50
    assert report.conflict_level == "待确认"
    assert report.alignment_label == "周期仍需确认"


@pytest.mark.parametrize(
    ("row_count", "expected_frames"),
    [
        (20, [("短线", 20)]),
        (59, [("短线", 20)]),
        (60, [("短线", 20), ("波段", 60)]),
        (119, [("短线", 20), ("波段", 60)]),
        (120, [("短线", 20), ("波段", 60), ("中期", 120)]),
    ],
)
def test_timeframe_alignment_only_includes_completed_requested_windows(
    row_count: int,
    expected_frames: list[tuple[str, int]],
) -> None:
    closes = [100 + index * 0.2 for index in range(row_count)]
    analysis, feature = _timeframe_inputs(closes, latest=closes[-1])

    report = build_timeframe_alignment_report(analysis, feature, SimpleNamespace(total_score=50))

    assert [(item.name, item.window_days) for item in report.timeframes] == expected_frames
    if row_count < 60:
        assert report.conflict_level == "待确认"
        assert report.alignment_label == "周期仍需确认"
        assert "多周期共振" not in report.summary


def _timeframe_inputs(closes: list[float], *, latest: float):
    start = date(2026, 1, 1)
    klines = [
        make_kline(
            date=(start + timedelta(days=index)).isoformat(),
            close=close,
            high=close + 1,
            low=close - 1,
        )
        for index, close in enumerate(closes)
    ]
    quote = make_quote(price=latest, prev_close=closes[-1], high=latest + 1, low=latest - 1, change_pct=0.0)
    quality = build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0))
    analysis = build_analysis(quote, klines, data_quality=quality)
    feature = build_feature_snapshot(analysis, build_stock_insight_bundle(analysis))
    return analysis, feature


def _trend(name: str, score: int) -> TimeframeTrend:
    return TimeframeTrend(
        name=name,
        window_days=20,
        score=score,
        label="测试",
        return_pct=0,
        max_drawdown_pct=0,
        above_ma=True,
        ma_value=100,
        evidence=[],
    )
