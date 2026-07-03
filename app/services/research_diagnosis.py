from __future__ import annotations

from app.models.schemas import (
    AlphaEvidenceReport,
    AnalysisResult,
    FactorLabReport,
    FeatureSnapshot,
    MarketRegimeReport,
    RiskRewardReport,
    SignalValidationReport,
    StockDiagnosis,
    StockInsightBundle,
    TimeframeAlignmentReport,
)
from app.services.research_diagnosis_decisions import (
    diagnosis_confidence,
    diagnosis_headline,
    final_diagnosis_action,
)
from app.services.research_diagnosis_sections import (
    build_beginner_summary,
    build_confirmation_signals,
    build_hard_risks,
    build_professional_summary,
    build_watch_focus,
    diagnosis_extra_text,
    diagnosis_factor_regime_text,
    main_conflict_sentence,
)


MAX_DIAGNOSIS_ITEMS = 5


def build_stock_diagnosis(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    alpha: AlphaEvidenceReport,
    factor_lab: FactorLabReport | None = None,
    market_regime: MarketRegimeReport | None = None,
    validation: SignalValidationReport | None = None,
    risk_reward: RiskRewardReport | None = None,
    timeframe: TimeframeAlignmentReport | None = None,
) -> StockDiagnosis:
    final_action = final_diagnosis_action(analysis, alpha, validation, risk_reward, timeframe, market_regime)
    return StockDiagnosis(
        symbol=feature.symbol,
        updated_at=feature.updated_at,
        headline=diagnosis_headline(analysis, feature, alpha, factor_lab, market_regime, validation, risk_reward, timeframe),
        beginner_summary=build_beginner_summary(analysis, final_action),
        professional_summary=build_professional_summary(analysis, insights, feature, alpha, factor_lab, market_regime, validation, risk_reward, timeframe),
        confirmation_signals=build_confirmation_signals(feature, factor_lab, timeframe)[:MAX_DIAGNOSIS_ITEMS],
        hard_risks=build_hard_risks(feature, insights, factor_lab, market_regime, risk_reward, timeframe)[:MAX_DIAGNOSIS_ITEMS],
        watch_focus=build_watch_focus(feature, factor_lab, market_regime, validation, timeframe)[:MAX_DIAGNOSIS_ITEMS],
        action=final_action,
        confidence=diagnosis_confidence(analysis, alpha, factor_lab, market_regime, risk_reward, timeframe),
    )


_diagnosis_headline = diagnosis_headline
_final_diagnosis_action = final_diagnosis_action
_diagnosis_factor_regime_text = diagnosis_factor_regime_text
_diagnosis_extra_text = diagnosis_extra_text
_main_conflict_sentence = main_conflict_sentence
