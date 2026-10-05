from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from app.models.research import (
    AlphaEvidencePoint,
    FactorLabReport,
    MarketRegimeReport,
    RiskRewardReport,
    TimeframeAlignmentReport,
)
from app.models.analysis import (
    AnalysisResult,
    StockInsightBundle,
)
from app.services.research_factors import (
    _factor_alpha_reason,
    _factor_calibration_impact,
    _factor_score_impact,
)
from app.services.scoring import bounded_int
from app.services.research_factor_aggregation import FACTOR_SHARES


@dataclass(frozen=True)
class FactorAlphaPointContext:
    score_impact: int
    calibration_impact: int


def collect_alpha_points(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    *,
    factor_lab: FactorLabReport | None = None,
    market_regime: MarketRegimeReport | None = None,
    timeframe: TimeframeAlignmentReport | None = None,
    risk_reward: RiskRewardReport | None = None,
) -> list[AlphaEvidencePoint]:
    points: list[AlphaEvidencePoint] = []
    points.extend(trend_points(analysis))
    points.extend(overview_factor_points(insights))
    points.extend(rule_match_points(insights))
    points.extend(abnormal_event_points(insights))
    points.extend(factor_lab_points(factor_lab))
    points.extend(regime_points(market_regime))
    points.extend(timeframe_points(timeframe))
    points.extend(risk_reward_points(risk_reward))
    return points


def trend_points(analysis: AnalysisResult) -> list[AlphaEvidencePoint]:
    return [
        AlphaEvidencePoint(
            source=f"趋势/{item.category}",
            title=item.name,
            impact=item.impact,
            level=item.level,
            reason=item.reason,
        )
        for item in analysis.signal_snapshot.contributions
    ]


def overview_factor_points(insights: StockInsightBundle) -> list[AlphaEvidencePoint]:
    return [
        _overview_factor_alpha_point(factor)
        for factor in insights.overview.factors
        if factor.score_available and factor.participates_in_total_score
        and factor.data_nature != "unavailable"
    ]


def _overview_factor_alpha_point(factor) -> AlphaEvidencePoint:
    impact = round((factor.score - 50) / 2)
    is_constraint = factor.aggregation_role == "risk_constraint"
    if is_constraint:
        impact = min(0, impact)
    return AlphaEvidencePoint(
        source="五维诊断", title=factor.name, impact=impact,
        level=impact_level(impact) if is_constraint else factor.level, reason=factor.summary,
    )


def rule_match_points(insights: StockInsightBundle) -> list[AlphaEvidencePoint]:
    return [
        AlphaEvidencePoint(
            source="规则引擎",
            title=match.name,
            impact=rule_match_impact(match.status, match.level),
            level=match.level,
            reason=match.reason,
        )
        for match in insights.rule_matches.matches[:4]
    ]


def abnormal_event_points(insights: StockInsightBundle) -> list[AlphaEvidencePoint]:
    return [
        AlphaEvidencePoint(
            source="异动识别",
            title=event.title,
            impact=event_impact(event.level),
            level=event.level,
            reason=event.description,
        )
        for event in insights.abnormal_events.events[:3]
    ]


def factor_lab_points(factor_lab: FactorLabReport | None) -> list[AlphaEvidencePoint]:
    if not factor_lab:
        return []
    counts = Counter(factor.id for factor in factor_lab.factors)
    return [_factor_alpha_point(factor) for factor in factor_lab.factors
            if counts[factor.id] == 1 and _factor_has_alpha_signal(factor)]


def _factor_has_alpha_signal(factor) -> bool:
    return bool(
        factor.participates_in_current_score
        and factor.id in FACTOR_SHARES
        and factor.score_usage not in {"excluded", "observation"}
        and factor.aggregation_role != "composite"
        and factor.data_nature != "unavailable"
        and (factor.score >= 62 or factor.score <= 45)
    )


def _factor_alpha_point(factor) -> AlphaEvidencePoint:
    context = _factor_alpha_point_context(factor)
    impact = bounded_int(context.score_impact + context.calibration_impact, -18, 18, round_value=True)
    return AlphaEvidencePoint(
        source="因子实验室",
        title=factor.name,
        impact=impact,
        level=impact_level(impact) if factor.id == "risk_pressure" else factor.level,
        reason=_admitted_factor_alpha_reason(factor),
    )


def _factor_alpha_point_context(factor) -> FactorAlphaPointContext:
    current_impact = round((factor.score - 50) / 2)
    if factor.id == "risk_pressure":
        return FactorAlphaPointContext(score_impact=min(0, current_impact), calibration_impact=0)
    return FactorAlphaPointContext(
        score_impact=_factor_score_impact(factor) if _has_eligible_factor_calibration(factor) else current_impact,
        calibration_impact=_eligible_factor_calibration_impact(factor),
    )


def _eligible_factor_calibration_impact(factor) -> int:
    if _has_eligible_factor_calibration(factor) and factor.calibration.sample_count >= 5:
        return _factor_calibration_impact(factor.calibration)
    return 0


def _has_eligible_factor_calibration(factor) -> bool:
    calibration = factor.calibration
    version = factor.score_rule_version
    return bool(
        calibration
        and calibration.availability == "available"
        and calibration.participates_in_historical_aggregate
        and isinstance(version, str) and version.strip()
        and calibration.score_rule_version == version
    )


def _admitted_factor_alpha_reason(factor) -> str:
    if factor.id == "risk_pressure":
        return f"{factor.value}；风险约束仅保留扣分，不作为看多证据。"
    if _has_eligible_factor_calibration(factor):
        return _factor_alpha_reason(factor)
    return f"{factor.value}；仅使用当前观测，历史校准未参与加权。"


def regime_points(market_regime: MarketRegimeReport | None) -> list[AlphaEvidencePoint]:
    if not market_regime:
        return []
    return [
        AlphaEvidencePoint(
            source="市场环境",
            title=market_regime.stock_state,
            impact=market_regime.confidence_adjustment,
            level=impact_level(market_regime.confidence_adjustment, positive_threshold=3, negative_threshold=-3),
            reason=f"{market_regime.market_label}，{market_regime.industry_label}，风险倍率 {market_regime.risk_multiplier:.2f}。",
        )
    ]


def timeframe_points(timeframe: TimeframeAlignmentReport | None) -> list[AlphaEvidencePoint]:
    if not timeframe:
        return []
    impact = timeframe_impact(timeframe)
    return [
        AlphaEvidencePoint(
            source="多周期",
            title=timeframe.alignment_label,
            impact=impact,
            level=impact_level(impact),
            reason=timeframe.summary,
        )
    ]


def risk_reward_points(risk_reward: RiskRewardReport | None) -> list[AlphaEvidencePoint]:
    if not risk_reward:
        return []
    impact = risk_reward_impact(risk_reward.rating)
    if impact > 0 and risk_reward.ratio_available is not True:
        impact = 0
    return [
        AlphaEvidencePoint(
            source="风险收益",
            title=risk_reward.rating,
            impact=impact,
            level=impact_level(impact, positive_threshold=4),
            reason=risk_reward.summary,
        )
    ]


def rule_match_impact(status: str, level: str) -> int:
    if status == "命中" and level == "积极":
        return 16
    if status == "命中" and level == "风险":
        return -18
    if status == "接近" and level == "积极":
        return 6
    return 0


def event_impact(level: str) -> int:
    if level == "风险":
        return -14
    if level == "积极":
        return 10
    return 0


def timeframe_impact(timeframe: TimeframeAlignmentReport) -> int:
    if timeframe.conflict_level == "多周期顺向" and timeframe.alignment_score >= 65:
        return 12
    if timeframe.conflict_level in {"高冲突", "中冲突", "多周期偏弱"} or timeframe.alignment_label == "多周期偏弱":
        return -14
    return 0


def risk_reward_impact(rating: str) -> int:
    if rating == "性价比较好":
        return 10
    if rating in {"风险优先", "周期冲突", "性价比不足"}:
        return -12
    if rating == "性价比一般":
        return 2
    return 0


def impact_level(impact: int, positive_threshold: int = 0, negative_threshold: int = 0) -> str:
    if impact > positive_threshold:
        return "积极"
    if impact < negative_threshold:
        return "风险"
    return "观察"


__all__ = [
    "abnormal_event_points",
    "bounded_int",
    "collect_alpha_points",
    "event_impact",
    "factor_lab_points",
    "impact_level",
    "overview_factor_points",
    "regime_points",
    "risk_reward_impact",
    "risk_reward_points",
    "rule_match_impact",
    "rule_match_points",
    "timeframe_impact",
    "timeframe_points",
    "trend_points",
]
