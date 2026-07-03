from __future__ import annotations

from types import SimpleNamespace

from app.models.schemas import RiskRadarItem
from app.services.research_risk import (
    RISK_RADAR_RULES,
    _risk_radar_overall_level,
    _timeframe_conflict_score,
    build_risk_radar_report,
)


def test_risk_radar_rule_order_and_item_scores_are_explicit() -> None:
    assert [rule.name for rule in RISK_RADAR_RULES] == [
        "trend_break",
        "valuation",
        "abnormal_event",
        "liquidity",
        "market_regime",
        "timeframe_conflict",
        "risk_reward",
        "data_quality",
    ]

    report = build_risk_radar_report(
        _analysis(data_quality_score=80, data_quality_level="良好"),
        _insights(valuation_label="高估", abnormal_level="风险", abnormal_signal="放量下跌"),
        _feature(trend_score=35, valuation_score=30, amount=250_000_000),
        _market_regime(risk_multiplier=1.30, market_label="环境偏冷"),
        _risk_reward(rating="性价比不足", summary="收益风险比不足。"),
        _timeframe(conflict_level="高冲突", summary="短强长弱。"),
    )

    assert [item.name for item in report.items] == ["趋势破位", "估值压力", "事件异动", "流动性", "环境风险", "周期冲突", "性价比", "数据质量"]
    assert [item.score for item in report.items] == [65, 70, 75, 65, 50, 72, 68, 20]
    assert report.top_risks == ["事件异动：放量下跌", "周期冲突：短强长弱。", "估值压力：估值评分 30，高估。"]
    assert report.summary == "中风险：优先处理事件异动、周期冲突、估值压力。"


def test_risk_radar_handles_missing_amount_and_non_risk_event() -> None:
    report = build_risk_radar_report(
        _analysis(),
        _insights(abnormal_level="观察", abnormal_signal="暂无异常"),
        _feature(amount=None),
        _market_regime(),
        _risk_reward(rating="性价比较好"),
        _timeframe(conflict_level="轻微分歧"),
    )

    liquidity = next(item for item in report.items if item.name == "流动性")
    event = next(item for item in report.items if item.name == "事件异动")
    reward = next(item for item in report.items if item.name == "性价比")
    assert liquidity.score == 35
    assert liquidity.reason == "成交额缺失。"
    assert event.score == 35
    assert reward.score == 38


def test_timeframe_conflict_score_boundaries_are_stable() -> None:
    assert _timeframe_conflict_score("高冲突") == 72
    assert _timeframe_conflict_score("多周期偏弱") == 72
    assert _timeframe_conflict_score("中冲突") == 48
    assert _timeframe_conflict_score("多周期顺向") == 30


def test_risk_radar_overall_level_boundaries_are_stable() -> None:
    assert _risk_radar_overall_level([_item(68)]) == "高风险"
    assert _risk_radar_overall_level([_item(45)]) == "中风险"
    assert _risk_radar_overall_level([_item(44)]) == "风险可控"
    assert _risk_radar_overall_level([]) == "风险可控"


def _analysis(*, data_quality_score: int = 90, data_quality_level: str = "优秀"):
    return SimpleNamespace(data_quality=SimpleNamespace(score=data_quality_score, level=data_quality_level))


def _insights(*, valuation_label: str = "估值适中", abnormal_level: str = "观察", abnormal_signal: str = "暂无异常"):
    return SimpleNamespace(
        valuation=SimpleNamespace(valuation_anchor_label=valuation_label),
        abnormal_events=SimpleNamespace(level=abnormal_level, main_signal=abnormal_signal),
    )


def _feature(*, trend_score: int = 60, valuation_score: int = 60, amount: float | None = 1_000_000_000):
    return SimpleNamespace(trend_score=trend_score, ma20=100.0, valuation_score=valuation_score, amount=amount)


def _market_regime(*, risk_multiplier: float = 1.0, market_label: str = "环境中性"):
    return SimpleNamespace(risk_multiplier=risk_multiplier, market_label=market_label)


def _risk_reward(*, rating: str = "性价比一般", summary: str = "收益风险比一般。"):
    return SimpleNamespace(rating=rating, summary=summary)


def _timeframe(*, conflict_level: str = "轻微分歧", summary: str = "周期轻微分歧。"):
    return SimpleNamespace(conflict_level=conflict_level, summary=summary)


def _item(score: int) -> RiskRadarItem:
    return RiskRadarItem(name=f"风险{score}", level="中", score=score, reason="测试", action="测试")
