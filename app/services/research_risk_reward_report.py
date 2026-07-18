from __future__ import annotations

from app.models.schemas import (
    AnalysisResult,
    FactorLabReport,
    FeatureSnapshot,
    MarketRegimeReport,
    RiskRewardReport,
    SignalValidationReport,
    TimeframeAlignmentReport,
)
from app.services.research_risk_reward_contracts import RiskRewardReportParts
from app.services.research_risk_reward_metrics import _risk_reward_metrics
from app.services.research_risk_reward_rating import _risk_reward_notes, _risk_reward_rating, _risk_reward_summary
from app.services.research_risk_reward_scenarios import _scenario_plans
from app.services.research_risk_reward_values import _text_or_default


def build_risk_reward_report(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport,
    market_regime: MarketRegimeReport,
    validation: SignalValidationReport,
    timeframe: TimeframeAlignmentReport | None = None,
) -> RiskRewardReport:
    parts = _risk_reward_report_parts(analysis, feature, factor_lab, market_regime, validation, timeframe)
    return _risk_reward_report_from_parts(feature, parts)


def _risk_reward_report_parts(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport,
    market_regime: MarketRegimeReport,
    validation: SignalValidationReport,
    timeframe: TimeframeAlignmentReport | None,
) -> RiskRewardReportParts:
    metrics = _risk_reward_metrics(feature, factor_lab, market_regime)
    rating = _risk_reward_rating(metrics.ratio, factor_lab, market_regime, validation, timeframe)
    scenarios = _scenario_plans(
        analysis,
        feature,
        factor_lab,
        market_regime,
        validation,
        metrics.upside_target,
        metrics.downside_stop,
        rating=rating,
        timeframe=timeframe,
    )
    summary = _risk_reward_summary(
        rating,
        metrics.ratio,
        metrics.upside_pct,
        metrics.downside_pct,
        market_regime,
        timeframe,
        feature,
        metrics.upside_target,
        metrics.downside_stop,
    )
    return RiskRewardReportParts(
        metrics=metrics,
        rating=rating,
        summary=summary,
        scenarios=scenarios,
        notes=_risk_reward_notes(metrics),
    )


def _risk_reward_report_from_parts(feature: FeatureSnapshot, parts: RiskRewardReportParts) -> RiskRewardReport:
    metrics = parts.metrics
    return RiskRewardReport(
        symbol=_text_or_default(getattr(feature, "symbol", None), ""),
        updated_at=_text_or_default(getattr(feature, "updated_at", None), ""),
        current_price=round(metrics.price, 2),
        upside_target=round(metrics.upside_target, 2),
        downside_stop=round(metrics.downside_stop, 2),
        upside_pct=round(metrics.upside_pct, 2),
        downside_pct=round(metrics.downside_pct, 2),
        reward_risk_ratio=metrics.ratio,
        atr14=round(metrics.atr14, 2),
        atr_pct=round(metrics.atr_pct, 2),
        volatility_pct=round(metrics.volatility_pct, 2),
        rating=parts.rating,
        summary=parts.summary,
        scenarios=parts.scenarios,
        notes=parts.notes,
    )
