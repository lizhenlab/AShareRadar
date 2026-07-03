from __future__ import annotations

import pytest

from app.models.schemas import ActionAdvice, AnalysisResult, DataQuality, SignalItem, SignalSnapshot
from app.services.stock_strategy import _quality_signal_level, _quality_strategy_status
from tests.factories import make_kline, make_quote


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("满足", "暂停观察"),
        ("触发", "暂停观察"),
        ("接近触发", "暂停观察"),
        ("仅底仓适用", "暂停做T"),
        ("等待", "暂停"),
    ],
)
def test_strategy_status_pauses_active_states_when_quality_is_severe(status: str, expected: str) -> None:
    assert _quality_strategy_status(status, _analysis(49)) == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("满足", "等待确认"),
        ("触发", "等待确认"),
        ("接近触发", "观察"),
        ("仅底仓适用", "仅底仓适用（降权）"),
        ("等待", "等待"),
    ],
)
def test_strategy_status_downshifts_active_states_when_quality_is_weak(status: str, expected: str) -> None:
    assert _quality_strategy_status(status, _analysis(69)) == expected


def test_strategy_status_and_signal_level_keep_original_values_when_quality_is_good() -> None:
    analysis = _analysis(88)

    assert _quality_strategy_status("满足", analysis) == "满足"
    assert _quality_signal_level("积极", analysis) == "积极"


def test_signal_level_turns_cautious_or_risk_by_quality_band() -> None:
    assert _quality_signal_level("积极", _analysis(69)) == "谨慎"
    assert _quality_signal_level("观察", _analysis(69)) == "谨慎"
    assert _quality_signal_level("谨慎", _analysis(69)) == "谨慎"
    assert _quality_signal_level("积极", _analysis(49)) == "风险"
    assert _quality_signal_level("风险", _analysis(49)) == "风险"


def _analysis(quality_score: int) -> AnalysisResult:
    quote = make_quote()
    return AnalysisResult(
        quote=quote,
        action_advice=ActionAdvice(action="观察", confidence=70, reason="测试"),
        data_quality=DataQuality(level="测试质量", source="测试", quote_time=quote.timestamp, kline_count=30, score=quality_score),
        signal_snapshot=SignalSnapshot(score=55, label="观察", confidence=70, summary="测试信号"),
        trend_score=60,
        trend_label="观察",
        support=1260.0,
        resistance=1320.0,
        ma5=1280.0,
        ma10=1270.0,
        ma20=1265.0,
        risk_level="可控观察",
        beginner_summary="测试摘要",
        buy_points=[SignalItem(title="测试买点", level="观察", reason="测试")],
        sell_points=[SignalItem(title="测试卖点", level="谨慎", reason="测试")],
        t_plan=[SignalItem(title="测试做T", level="观察", reason="测试")],
        strength_tags=[],
        klines=[make_kline()],
    )
