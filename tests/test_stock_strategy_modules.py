from __future__ import annotations

import pytest

from app.models.schemas import ActionAdvice, AnalysisResult, DataQuality, SignalItem, SignalSnapshot
from app.services.stock_insights import build_stock_insight_bundle
from app.services.stock_strategy import _quality_signal_level, _quality_strategy_status, build_strategy_cards
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


def test_unavailable_structural_placeholders_do_not_change_strategy_cards() -> None:
    analysis = _analysis(88)
    bundle = build_stock_insight_bundle(analysis)
    first = analysis.model_copy(
        update={
            "support": 1.11,
            "resistance": 2.22,
            "ma20": 3.33,
            "support_available": False,
            "resistance_available": False,
            "ma20_available": False,
        }
    )
    second = first.model_copy(update={"support": 911.11, "resistance": 922.22, "ma20": 933.33})

    first_cards = build_strategy_cards(first, bundle.fund_flow, bundle.order_pressure)
    second_cards = build_strategy_cards(second, bundle.fund_flow, bundle.order_pressure)

    assert [item.model_dump() for item in first_cards] == [item.model_dump() for item in second_cards]
    rendered = str([item.model_dump() for item in first_cards])
    assert "1.11" not in rendered
    assert "2.22" not in rendered
    assert "3.33" not in rendered
    assert first_cards[1].status == "等待"
    assert first_cards[2].status == "等待"
    assert first_cards[3].status == "等待"


def test_available_structural_levels_keep_strategy_reference_prices() -> None:
    analysis = _analysis(88)
    bundle = build_stock_insight_bundle(analysis)

    cards = build_strategy_cards(analysis, bundle.fund_flow, bundle.order_pressure)

    assert cards[1].reference_price == "压力位 1320.00"
    assert cards[2].reference_price == "支撑位 1260.00"
    assert cards[3].reference_price == "1260.00 - 1320.00"
    assert cards[4].reference_price == "20日线 1265.00"


def test_unavailable_fund_score_perturbations_do_not_change_breakout_strategy() -> None:
    analysis = _analysis(88)
    analysis = analysis.model_copy(update={"quote": analysis.quote.model_copy(update={"price": 1310.0})})
    bundle = build_stock_insight_bundle(analysis)
    flows = [
        bundle.fund_flow.model_copy(update={"available": True, "data_nature": "unavailable", "overall_score": score})
        for score in (0, 50, 100)
    ]

    cards = [build_strategy_cards(analysis, item, bundle.order_pressure)[1] for item in flows]

    assert cards[0].model_dump() == cards[1].model_dump() == cards[2].model_dump()
    assert cards[0].level == "观察"
    assert cards[0].status == "等待"
    assert "证据当前不可用" in cards[0].current_evidence[0]


def test_unavailable_order_pressure_text_cannot_change_t_range_strategy() -> None:
    analysis = _analysis(88)
    bundle = build_stock_insight_bundle(analysis)
    pressures = [
        bundle.order_pressure.model_copy(
            update={
                "available": False,
                "data_nature": "unavailable",
                "pressure_level": pressure,
                "summary": f"不可信占位：{pressure}",
            }
        )
        for pressure in ("订单压力不可用", "主动卖压持续增强", "强买盘占优")
    ]

    cards = [build_strategy_cards(analysis, bundle.fund_flow, item)[3] for item in pressures]

    assert cards[0].model_dump() == cards[1].model_dump() == cards[2].model_dump()
    assert "盘口证据不可用" in " ".join(cards[0].current_evidence)
    assert "不可信占位" not in " ".join(cards[0].current_evidence)


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
        support_available=True,
        resistance_available=True,
        ma5=1280.0,
        ma10=1270.0,
        ma20=1265.0,
        ma20_available=True,
        risk_level="可控观察",
        beginner_summary="测试摘要",
        buy_points=[SignalItem(title="测试买点", level="观察", reason="测试")],
        sell_points=[SignalItem(title="测试卖点", level="谨慎", reason="测试")],
        t_plan=[SignalItem(title="测试做T", level="观察", reason="测试")],
        strength_tags=[],
        klines=[make_kline()],
    )
