from __future__ import annotations

from app.models.schemas import FactorLabReport, FeatureSnapshot, MarketRegimeReport
from app.services.research_risk_reward_contracts import (
    CONFIRMING_VALIDATION_STATUSES,
    DOWNSIDE_HIGH_RISK_BASE_LOSS_PCT,
    DOWNSIDE_HIGH_RISK_MULTIPLIER,
    DOWNSIDE_MIN_LOSS_PCT,
    DOWNSIDE_NORMAL_BASE_LOSS_PCT,
    SCENARIO_CYCLE_CONFLICT_POSITIVE_CAP,
    SCENARIO_FACTOR_WEIGHT,
    SCENARIO_MIN_NEUTRAL_PROBABILITY,
    SCENARIO_POSITIVE_BASE,
    SCENARIO_RISK_BASE,
    SCENARIO_RISK_MULTIPLIER_WEIGHT,
    SCENARIO_RISK_PRIORITY_POSITIVE_CAP,
    SCENARIO_WAIT_CONFIRM_POSITIVE_CAP,
    STRUCTURAL_STOP_MAX_DISTANCE_PCT,
    TIMEFRAME_WAIT_LEVELS,
    UPSIDE_TARGET_ATR_PCT_CAP,
    UPSIDE_TARGET_MAX_CAP_PCT,
    UPSIDE_TARGET_MIN_CAP_PCT,
    UPSIDE_TARGET_VOLATILITY_PCT_CAP,
    DownsideStopAdjustmentRule,
    DownsideStopContext,
    RiskRewardLevelAvailability,
    RiskRewardMetrics,
    RiskRewardRatingContext,
    RiskRewardRatingRule,
    RiskRewardReportParts,
    ScenarioPlanContext,
    ScenarioProbabilities,
)
from app.services.research_risk_reward_metrics import (
    DOWNSIDE_STOP_ADJUSTMENT_RULES as DOWNSIDE_STOP_ADJUSTMENT_RULES,
    _downside_distance_pct as _downside_distance_pct,
    _downside_stop as _downside_stop,
    _reward_risk_ratio as _reward_risk_ratio,
    _risk_reward_metrics as _calculate_risk_reward_metrics,
    _upside_distance_pct as _upside_distance_pct,
    _upside_target as _upside_target,
)
from app.services.research_risk_reward_rating import (
    RISK_REWARD_RATING_RULES as RISK_REWARD_RATING_RULES,
    _risk_reward_rating as _risk_reward_rating,
    _risk_reward_summary as _risk_reward_summary,
)
from app.services.research_risk_reward_report import build_risk_reward_report as build_risk_reward_report
from app.services.research_risk_reward_scenarios import (
    _normalize_scenario_probabilities as _normalize_scenario_probabilities,
    _scenario_plans as _scenario_plans,
    _scenario_probabilities as _scenario_probabilities,
)


_COMPAT_REEXPORTS = (
    CONFIRMING_VALIDATION_STATUSES,
    DOWNSIDE_HIGH_RISK_BASE_LOSS_PCT,
    DOWNSIDE_HIGH_RISK_MULTIPLIER,
    DOWNSIDE_MIN_LOSS_PCT,
    DOWNSIDE_NORMAL_BASE_LOSS_PCT,
    DOWNSIDE_STOP_ADJUSTMENT_RULES,
    RISK_REWARD_RATING_RULES,
    SCENARIO_CYCLE_CONFLICT_POSITIVE_CAP,
    SCENARIO_FACTOR_WEIGHT,
    SCENARIO_MIN_NEUTRAL_PROBABILITY,
    SCENARIO_POSITIVE_BASE,
    SCENARIO_RISK_BASE,
    SCENARIO_RISK_MULTIPLIER_WEIGHT,
    SCENARIO_RISK_PRIORITY_POSITIVE_CAP,
    SCENARIO_WAIT_CONFIRM_POSITIVE_CAP,
    STRUCTURAL_STOP_MAX_DISTANCE_PCT,
    TIMEFRAME_WAIT_LEVELS,
    UPSIDE_TARGET_ATR_PCT_CAP,
    UPSIDE_TARGET_MAX_CAP_PCT,
    UPSIDE_TARGET_MIN_CAP_PCT,
    UPSIDE_TARGET_VOLATILITY_PCT_CAP,
    DownsideStopAdjustmentRule,
    DownsideStopContext,
    RiskRewardLevelAvailability,
    RiskRewardMetrics,
    RiskRewardRatingContext,
    RiskRewardRatingRule,
    RiskRewardReportParts,
    ScenarioPlanContext,
    ScenarioProbabilities,
    _downside_distance_pct,
    _normalize_scenario_probabilities,
    _reward_risk_ratio,
    _risk_reward_rating,
    _risk_reward_summary,
    _scenario_plans,
    _scenario_probabilities,
    _upside_distance_pct,
    build_risk_reward_report,
)


def _risk_reward_metrics(
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport,
    market_regime: MarketRegimeReport,
) -> RiskRewardMetrics:
    return _calculate_risk_reward_metrics(
        feature,
        factor_lab,
        market_regime,
        upside_target_builder=_upside_target,
        downside_stop_builder=_downside_stop,
    )
