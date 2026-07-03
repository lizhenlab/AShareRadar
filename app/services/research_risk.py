from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.models.schemas import (
    AnalysisResult,
    FeatureSnapshot,
    MarketRegimeReport,
    RiskRadarItem,
    RiskRadarReport,
    RiskRewardReport,
    StockInsightBundle,
    TimeframeAlignmentReport,
)
from app.services.scoring import clamp_score


@dataclass(frozen=True)
class RiskRadarContext:
    analysis: AnalysisResult
    insights: StockInsightBundle
    feature: FeatureSnapshot
    market_regime: MarketRegimeReport
    risk_reward: RiskRewardReport
    timeframe: TimeframeAlignmentReport


@dataclass(frozen=True)
class RiskRadarRule:
    name: str
    build: Callable[[RiskRadarContext], RiskRadarItem]


def build_risk_radar_report(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    market_regime: MarketRegimeReport,
    risk_reward: RiskRewardReport,
    timeframe: TimeframeAlignmentReport,
) -> RiskRadarReport:
    context = RiskRadarContext(analysis, insights, feature, market_regime, risk_reward, timeframe)
    items = _risk_radar_items(context)
    top = _top_risk_items(items)
    overall_level = _risk_radar_overall_level(items)
    return RiskRadarReport(
        overall_level=overall_level,
        summary=f"{overall_level}：优先处理" + "、".join(item.name for item in top) + "。",
        items=items,
        top_risks=[f"{item.name}：{item.reason}" for item in top],
    )


def _risk_radar_item(name: str, raw_score: float, reason: str, action: str) -> RiskRadarItem:
    score = clamp_score(raw_score, round_value=True)
    level = "高" if score >= 68 else "中" if score >= 42 else "低"
    return RiskRadarItem(name=name, level=level, score=score, reason=reason, action=action)


def _risk_radar_items(context: RiskRadarContext) -> list[RiskRadarItem]:
    return [rule.build(context) for rule in RISK_RADAR_RULES]


def _top_risk_items(items: list[RiskRadarItem]) -> list[RiskRadarItem]:
    return sorted(items, key=lambda item: item.score, reverse=True)[:3]


def _risk_radar_overall_level(items: list[RiskRadarItem]) -> str:
    overall_score = round(sum(item.score for item in items) / len(items)) if items else 0
    if overall_score >= 68:
        return "高风险"
    if overall_score >= 45:
        return "中风险"
    return "风险可控"


def _trend_break_risk(context: RiskRadarContext) -> RiskRadarItem:
    feature = context.feature
    return _risk_radar_item(
        "趋势破位",
        100 - feature.trend_score,
        f"趋势评分 {feature.trend_score}，20日线 {feature.ma20:.2f}。",
        "跌破关键均线先降级。",
    )


def _valuation_risk(context: RiskRadarContext) -> RiskRadarItem:
    return _risk_radar_item(
        "估值压力",
        100 - context.feature.valuation_score,
        f"估值评分 {context.feature.valuation_score}，{context.insights.valuation.valuation_anchor_label}。",
        "估值高位时追高必须等确认。",
    )


def _abnormal_event_risk(context: RiskRadarContext) -> RiskRadarItem:
    abnormal_events = context.insights.abnormal_events
    return _risk_radar_item(
        "事件异动",
        75 if abnormal_events.level == "风险" else 35,
        abnormal_events.main_signal,
        "事件风险解除前降低仓位冲动。",
    )


def _liquidity_risk(context: RiskRadarContext) -> RiskRadarItem:
    amount = context.feature.amount
    return _risk_radar_item(
        "流动性",
        65 if amount and amount < 300_000_000 else 35,
        f"成交额 {amount / 100000000:.1f} 亿。" if amount else "成交额缺失。",
        "低流动性信号容易失真。",
    )


def _market_regime_risk(context: RiskRadarContext) -> RiskRadarItem:
    market_regime = context.market_regime
    return _risk_radar_item(
        "环境风险",
        round((market_regime.risk_multiplier - 0.8) * 100),
        f"{market_regime.market_label}，风险倍率 {market_regime.risk_multiplier:.2f}。",
        "环境偏冷时降低信号权重。",
    )


def _timeframe_conflict_risk(context: RiskRadarContext) -> RiskRadarItem:
    timeframe = context.timeframe
    return _risk_radar_item(
        "周期冲突",
        _timeframe_conflict_score(timeframe.conflict_level),
        timeframe.summary,
        "周期冲突时等待主周期修复。",
    )


def _timeframe_conflict_score(conflict_level: str) -> int:
    if conflict_level in {"高冲突", "多周期偏弱"}:
        return 72
    if conflict_level == "中冲突":
        return 48
    return 30


def _risk_reward_risk(context: RiskRadarContext) -> RiskRadarItem:
    risk_reward = context.risk_reward
    return _risk_radar_item(
        "性价比",
        68 if risk_reward.rating in {"风险优先", "周期冲突", "性价比不足"} else 38,
        risk_reward.summary,
        "收益风险比不足时不主动提高积极度。",
    )


def _data_quality_risk(context: RiskRadarContext) -> RiskRadarItem:
    data_quality = context.analysis.data_quality
    return _risk_radar_item(
        "数据质量",
        100 - data_quality.score,
        f"数据质量 {data_quality.level} {data_quality.score} 分。",
        "数据差时所有买卖点降权。",
    )


RISK_RADAR_RULES = (
    RiskRadarRule("trend_break", _trend_break_risk),
    RiskRadarRule("valuation", _valuation_risk),
    RiskRadarRule("abnormal_event", _abnormal_event_risk),
    RiskRadarRule("liquidity", _liquidity_risk),
    RiskRadarRule("market_regime", _market_regime_risk),
    RiskRadarRule("timeframe_conflict", _timeframe_conflict_risk),
    RiskRadarRule("risk_reward", _risk_reward_risk),
    RiskRadarRule("data_quality", _data_quality_risk),
)
