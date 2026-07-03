from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.models.schemas import (
    AlphaEvidenceReport,
    AnalysisResult,
    FactorLabReport,
    FeatureSnapshot,
    MarketRegimeReport,
    RiskRewardReport,
    SignalValidationReport,
    TimeframeAlignmentReport,
)
from app.services.scoring import clamp_score as _clamp


RISK_REWARD_CONTROL_RATINGS = {"风险优先", "周期冲突"}
TIMEFRAME_CONTROL_LEVELS = {"高冲突"}
TIMEFRAME_WAIT_LEVELS = {"中冲突"}
TIMEFRAME_WEAK_LEVELS = {"多周期偏弱"}


@dataclass(frozen=True)
class DiagnosisDecisionContext:
    analysis: AnalysisResult
    alpha: AlphaEvidenceReport
    validation: SignalValidationReport | None = None
    risk_reward: RiskRewardReport | None = None
    timeframe: TimeframeAlignmentReport | None = None
    market_regime: MarketRegimeReport | None = None
    feature: FeatureSnapshot | None = None
    factor_lab: FactorLabReport | None = None


@dataclass(frozen=True)
class HeadlineRule:
    name: str
    applies: Callable[[DiagnosisDecisionContext], bool]
    headline: str


@dataclass(frozen=True)
class ActionRule:
    name: str
    applies: Callable[[DiagnosisDecisionContext], bool]
    action: Callable[[DiagnosisDecisionContext], str]


def diagnosis_headline(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    alpha: AlphaEvidenceReport,
    factor_lab: FactorLabReport | None = None,
    market_regime: MarketRegimeReport | None = None,
    validation: SignalValidationReport | None = None,
    risk_reward: RiskRewardReport | None = None,
    timeframe: TimeframeAlignmentReport | None = None,
) -> str:
    context = DiagnosisDecisionContext(
        analysis=analysis,
        alpha=alpha,
        validation=validation,
        risk_reward=risk_reward,
        timeframe=timeframe,
        market_regime=market_regime,
        feature=feature,
        factor_lab=factor_lab,
    )
    return _headline_for_context(context)


def final_diagnosis_action(
    analysis: AnalysisResult,
    alpha: AlphaEvidenceReport,
    validation: SignalValidationReport | None,
    risk_reward: RiskRewardReport | None,
    timeframe: TimeframeAlignmentReport | None,
    market_regime: MarketRegimeReport | None,
) -> str:
    context = DiagnosisDecisionContext(
        analysis=analysis,
        alpha=alpha,
        validation=validation,
        risk_reward=risk_reward,
        timeframe=timeframe,
        market_regime=market_regime,
    )
    return _action_for_context(context)


def diagnosis_confidence(
    analysis: AnalysisResult,
    alpha: AlphaEvidenceReport,
    factor_lab: FactorLabReport | None = None,
    market_regime: MarketRegimeReport | None = None,
    risk_reward: RiskRewardReport | None = None,
    timeframe: TimeframeAlignmentReport | None = None,
) -> int:
    confidence = min(analysis.action_advice.confidence, alpha.confidence)
    if factor_lab:
        confidence = min(confidence, max(35, factor_lab.calibrated_confidence + 8))
    if market_regime:
        confidence = _clamp(confidence + market_regime.confidence_adjustment)
    if timeframe and timeframe.conflict_level in {"高冲突", "中冲突", "多周期偏弱"}:
        confidence = max(30, confidence - 10)
    if _risk_reward_rating_in(risk_reward, RISK_REWARD_CONTROL_RATINGS):
        confidence = max(28, confidence - 8)
    return confidence


def _headline_for_context(context: DiagnosisDecisionContext) -> str:
    for rule in HEADLINE_RULES:
        if rule.applies(context):
            return rule.headline
    return "信号仍需确认，按关键价位观察"


def _action_for_context(context: DiagnosisDecisionContext) -> str:
    for rule in ACTION_RULES:
        if rule.applies(context):
            return rule.action(context)
    return context.analysis.action_advice.action


def _must_control_risk(
    analysis: AnalysisResult,
    validation: SignalValidationReport | None,
    risk_reward: RiskRewardReport | None,
    timeframe: TimeframeAlignmentReport | None,
) -> bool:
    return (
        analysis.data_quality.score < 50
        or _timeframe_level_in(timeframe, TIMEFRAME_CONTROL_LEVELS)
        or _risk_reward_rating_in(risk_reward, RISK_REWARD_CONTROL_RATINGS)
        or bool(validation and validation.overall_status == "风险优先")
    )


def _weak_timeframe_action(
    alpha: AlphaEvidenceReport,
    validation: SignalValidationReport | None,
    risk_reward: RiskRewardReport | None,
) -> str:
    if (
        risk_reward
        and risk_reward.reward_risk_ratio >= 1.35
        and validation
        and validation.overall_status in {"等待二次确认", "观察为主"}
        and alpha.verdict != "风险证据占优"
    ):
        return "等待确认"
    return "控制风险"


def _timeframe_level_in(timeframe: TimeframeAlignmentReport | None, levels: set[str]) -> bool:
    return bool(timeframe and timeframe.conflict_level in levels)


def _risk_reward_rating_in(risk_reward: RiskRewardReport | None, ratings: set[str]) -> bool:
    return bool(risk_reward and risk_reward.rating in ratings)


def _headline_data_quality_low(context: DiagnosisDecisionContext) -> bool:
    return bool(context.feature and context.feature.data_quality_score < 50)


def _headline_timeframe_control(context: DiagnosisDecisionContext) -> bool:
    return _timeframe_level_in(context.timeframe, TIMEFRAME_CONTROL_LEVELS)


def _headline_timeframe_weak(context: DiagnosisDecisionContext) -> bool:
    return _timeframe_level_in(context.timeframe, TIMEFRAME_WEAK_LEVELS)


def _headline_risk_reward_control(context: DiagnosisDecisionContext) -> bool:
    return _risk_reward_rating_in(context.risk_reward, RISK_REWARD_CONTROL_RATINGS)


def _headline_market_risk_high(context: DiagnosisDecisionContext) -> bool:
    return bool(context.market_regime and context.market_regime.risk_multiplier >= 1.25)


def _headline_validation_defensive(context: DiagnosisDecisionContext) -> bool:
    return bool(context.validation and context.validation.overall_status == "风险优先")


def _headline_risk_signal(context: DiagnosisDecisionContext) -> bool:
    return context.analysis.risk_level == "高风险" or context.alpha.verdict == "风险证据占优"


def _headline_factor_positive(context: DiagnosisDecisionContext) -> bool:
    return bool(context.factor_lab and context.factor_lab.total_score >= 68 and context.factor_lab.calibrated_confidence >= 62)


def _headline_alpha_positive(context: DiagnosisDecisionContext) -> bool:
    return context.alpha.verdict == "积极证据占优"


def _context_must_control_risk(context: DiagnosisDecisionContext) -> bool:
    return _must_control_risk(context.analysis, context.validation, context.risk_reward, context.timeframe)


def _context_timeframe_weak(context: DiagnosisDecisionContext) -> bool:
    return _timeframe_level_in(context.timeframe, TIMEFRAME_WEAK_LEVELS)


def _context_timeframe_wait(context: DiagnosisDecisionContext) -> bool:
    return _timeframe_level_in(context.timeframe, TIMEFRAME_WAIT_LEVELS)


def _context_market_risk_high(context: DiagnosisDecisionContext) -> bool:
    return bool(context.market_regime and context.market_regime.risk_multiplier >= 1.25)


def _context_risk_reward_wait(context: DiagnosisDecisionContext) -> bool:
    return bool(context.risk_reward and context.risk_reward.rating == "等待确认")


def _context_alpha_positive_with_observable_base(context: DiagnosisDecisionContext) -> bool:
    return context.alpha.verdict == "积极证据占优" and context.analysis.action_advice.action in {"回踩关注", "持有观察"}


def _context_needs_cautious_observation(context: DiagnosisDecisionContext) -> bool:
    return context.analysis.action_advice.action in {"等待信号", "持有观察"}


def _fixed_action(action: str) -> Callable[[DiagnosisDecisionContext], str]:
    return lambda _context: action


HEADLINE_RULES: tuple[HeadlineRule, ...] = (
    HeadlineRule("low_data_quality", _headline_data_quality_low, "数据质量不足，先暂停主动买卖判断"),
    HeadlineRule("high_timeframe_conflict", _headline_timeframe_control, "多周期冲突明显，先收缩判断"),
    HeadlineRule("weak_timeframe", _headline_timeframe_weak, "多周期整体偏弱，先守风控线"),
    HeadlineRule("risk_reward_control", _headline_risk_reward_control, "风险收益不占优，先守风控线"),
    HeadlineRule("market_risk_high", _headline_market_risk_high, "环境风险偏高，先缩小判断半径"),
    HeadlineRule("validation_defensive", _headline_validation_defensive, "验证闭环偏防守，先守风控线"),
    HeadlineRule("risk_signal", _headline_risk_signal, "风险信号优先，先守风控线"),
    HeadlineRule("factor_positive", _headline_factor_positive, "因子和证据偏积极，等待价量确认"),
    HeadlineRule("alpha_positive", _headline_alpha_positive, "趋势和证据偏积极，等待价量确认"),
)

ACTION_RULES: tuple[ActionRule, ...] = (
    ActionRule("must_control_risk", _context_must_control_risk, _fixed_action("控制风险")),
    ActionRule(
        "weak_timeframe",
        _context_timeframe_weak,
        lambda context: _weak_timeframe_action(context.alpha, context.validation, context.risk_reward),
    ),
    ActionRule("medium_timeframe_conflict", _context_timeframe_wait, _fixed_action("等待确认")),
    ActionRule("market_risk_high", _context_market_risk_high, _fixed_action("轻仓观察")),
    ActionRule("risk_reward_wait", _context_risk_reward_wait, _fixed_action("等待确认")),
    ActionRule("alpha_positive", _context_alpha_positive_with_observable_base, _fixed_action("积极关注")),
    ActionRule("base_wait_or_hold", _context_needs_cautious_observation, _fixed_action("谨慎观察")),
)
