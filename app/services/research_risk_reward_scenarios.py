from __future__ import annotations

import math

from app.models.schemas import (
    AnalysisResult,
    FactorLabReport,
    FeatureSnapshot,
    MarketRegimeReport,
    ScenarioPlan,
    SignalValidationReport,
    TimeframeAlignmentReport,
)
from app.services.research_risk_reward_contracts import (
    CONFIRMING_VALIDATION_STATUSES,
    SCENARIO_CYCLE_CONFLICT_POSITIVE_CAP,
    SCENARIO_FACTOR_WEIGHT,
    SCENARIO_MIN_NEUTRAL_PROBABILITY,
    SCENARIO_POSITIVE_BASE,
    SCENARIO_RISK_BASE,
    SCENARIO_RISK_MULTIPLIER_WEIGHT,
    SCENARIO_RISK_PRIORITY_POSITIVE_CAP,
    SCENARIO_WAIT_CONFIRM_POSITIVE_CAP,
    TIMEFRAME_WAIT_LEVELS,
    ScenarioPlanContext,
    ScenarioProbabilities,
)
from app.services.research_risk_reward_values import (
    _analysis_action_text,
    _downside_level_or_zero,
    _positive_or_one,
    _positive_or_zero,
    _score_or_zero,
    _timeframe_conflict_text,
    _upside_level_or_zero,
    _validation_status_text,
)
from app.services.scoring import clamp_score as _clamp
from app.utils.market_data import finite_float


def _scenario_plans(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport,
    market_regime: MarketRegimeReport,
    validation: SignalValidationReport,
    upside_target: float,
    downside_stop: float,
    *,
    rating: str | None = None,
    timeframe: TimeframeAlignmentReport | None = None,
) -> list[ScenarioPlan]:
    context = _scenario_plan_context(
        analysis,
        feature,
        factor_lab,
        market_regime,
        validation,
        upside_target,
        downside_stop,
        rating,
        timeframe,
    )
    return [_positive_scenario_plan(context), _neutral_scenario_plan(context), _defensive_scenario_plan(context)]


def _positive_scenario_plan(context: ScenarioPlanContext) -> ScenarioPlan:
    rule_weight = context.probabilities.positive
    return ScenarioPlan(
        name="积极路径",
        probability=rule_weight,
        rule_weight=rule_weight,
        trigger=_positive_scenario_trigger(context),
        expected_move=_positive_scenario_expected_move(context),
        response="只在确认后提高关注度，避免盘中追高。",
        invalidation=_positive_scenario_invalidation(context),
    )


def _neutral_scenario_plan(context: ScenarioPlanContext) -> ScenarioPlan:
    rule_weight = context.probabilities.neutral
    return ScenarioPlan(
        name="震荡路径",
        probability=rule_weight,
        rule_weight=rule_weight,
        trigger=_neutral_scenario_trigger(context),
        expected_move="以支撑、压力和量能变化为主，不提前给方向结论。",
        response="适合观察或仅底仓做T，新增动作等待确认。",
        invalidation="区间被放量跌破或放量突破。",
    )


def _defensive_scenario_plan(context: ScenarioPlanContext) -> ScenarioPlan:
    rule_weight = context.probabilities.risk
    return ScenarioPlan(
        name="防守路径",
        probability=rule_weight,
        rule_weight=rule_weight,
        trigger=_defensive_scenario_trigger(context),
        expected_move="优先看风险释放，不急于判断反转。",
        response=f"维持「{context.action}」口径，先处理风控线。",
        invalidation="重新站回5日线且量能、量价热度同步修复。",
    )


def _scenario_plan_context(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport,
    market_regime: MarketRegimeReport,
    validation: SignalValidationReport,
    upside_target: float,
    downside_stop: float,
    rating: str | None = None,
    timeframe: TimeframeAlignmentReport | None = None,
) -> ScenarioPlanContext:
    price = _positive_or_zero(getattr(feature, "price", None))
    support = _scenario_support_level(getattr(feature, "support", None), price)
    resistance = _scenario_resistance_level(getattr(feature, "resistance", None), price)
    ma20 = _positive_or_zero(getattr(feature, "ma20", None))
    clean_upside_target = _scenario_resistance_level(upside_target, price)
    clean_downside_stop = _scenario_support_level(downside_stop, price)
    validation_status = _validation_status_text(validation)
    return ScenarioPlanContext(
        price=price,
        probabilities=_scenario_probabilities_for_context(
            factor_lab,
            market_regime,
            price=price,
            support=support,
            resistance=resistance,
            upside_target=clean_upside_target,
            downside_stop=clean_downside_stop,
            rating=rating,
            validation_status=validation_status,
            timeframe_conflict=_timeframe_conflict_text(timeframe),
        ),
        validation_status=validation_status,
        action=_analysis_action_text(analysis),
        support=support,
        resistance=resistance,
        ma20=ma20,
        upside_target=clean_upside_target,
        downside_stop=clean_downside_stop,
    )


def _scenario_probabilities_for_context(
    factor_lab: FactorLabReport,
    market_regime: MarketRegimeReport,
    *,
    price: float,
    support: float,
    resistance: float,
    upside_target: float,
    downside_stop: float,
    rating: str | None,
    validation_status: str,
    timeframe_conflict: str,
) -> ScenarioProbabilities:
    level_probabilities = _adjust_scenario_probabilities_for_levels(
        _scenario_probabilities(
            getattr(factor_lab, "total_score", None),
            getattr(market_regime, "risk_multiplier", None),
        ),
        price=price,
        support=support,
        resistance=resistance,
        upside_target=upside_target,
        downside_stop=downside_stop,
    )
    return _adjust_scenario_probabilities_for_decision_state(
        level_probabilities,
        rating=rating,
        validation_status=validation_status,
        timeframe_conflict=timeframe_conflict,
    )


def _scenario_support_level(value: object, price: float) -> float:
    if price <= 0:
        return 0
    return _downside_level_or_zero(value, price)


def _scenario_resistance_level(value: object, price: float) -> float:
    if price <= 0:
        return 0
    return _upside_level_or_zero(value, price)


def _positive_scenario_trigger(context: ScenarioPlanContext) -> str:
    if context.price <= 0:
        return (
            f"当前价待确认，暂不设置积极突破触发；"
            f"验证状态需维持在「{context.validation_status}」或更好。"
        )
    if context.resistance <= 0:
        return (
            f"压力位待确认，需先形成可验证突破边界，"
            f"且验证状态维持在「{context.validation_status}」或更好。"
        )
    resistance = _labelled_price_level("压力位", context.resistance)
    return f"放量站稳{resistance}，且验证状态维持在「{context.validation_status}」或更好。"


def _positive_scenario_expected_move(context: ScenarioPlanContext) -> str:
    if context.upside_target > 0:
        return f"先看 {context.upside_target:.2f} 附近，若继续放量再重新评估。"
    return "先等上方目标确认，若继续放量再重新评估。"


def _positive_scenario_invalidation(context: ScenarioPlanContext) -> str:
    if context.price <= 0:
        return "当前价或压力位无法复核，积极路径暂不成立。"
    if context.resistance > 0:
        return f"突破后跌回 {context.resistance:.2f} 下方。"
    return "突破后压力位仍未确认，或放量无法延续。"


def _neutral_scenario_trigger(context: ScenarioPlanContext) -> str:
    if context.price <= 0:
        return "当前价待确认，先按支撑、压力复核后的震荡观察处理。"
    if context.support > 0 and context.resistance > 0:
        return f"价格继续在 {context.support:.2f} 到 {context.resistance:.2f} 区间内波动。"
    if context.support > 0:
        return f"价格继续在 {context.support:.2f} 上方震荡，压力位仍待确认。"
    if context.resistance > 0:
        return f"价格在支撑位待确认、{context.resistance:.2f} 下方震荡。"
    return "支撑和压力位仍待确认，价格延续无明确边界的震荡。"


def _defensive_scenario_trigger(context: ScenarioPlanContext) -> str:
    if context.price <= 0:
        downside_text = "当前价或防守位待确认"
    else:
        downside_text = (
            f"有效跌破 {context.downside_stop:.2f}"
            if context.downside_stop > 0
            else "防守位待确认"
        )
    ma20_text = (
        f"20日线 {context.ma20:.2f} 下方不能修复"
        if context.ma20 > 0
        else "20日线待确认且弱势延续"
    )
    return f"{downside_text}，或{ma20_text}。"


def _labelled_price_level(label: str, value: float) -> str:
    return f"{label} {value:.2f}" if value > 0 else f"{label}待确认"


def _scenario_probabilities(factor_score: object, risk_multiplier: object) -> ScenarioProbabilities:
    score = _score_or_zero(factor_score)
    multiplier = _positive_or_one(risk_multiplier)
    positive = _clamp(
        round(
            SCENARIO_POSITIVE_BASE
            + score * SCENARIO_FACTOR_WEIGHT
            + max(0, 1.1 - multiplier) * SCENARIO_RISK_MULTIPLIER_WEIGHT
        )
    )
    risk = _clamp(
        round(
            SCENARIO_RISK_BASE
            + multiplier * SCENARIO_RISK_MULTIPLIER_WEIGHT
            + max(0, 50 - score) * SCENARIO_FACTOR_WEIGHT
        )
    )
    neutral = max(SCENARIO_MIN_NEUTRAL_PROBABILITY, 100 - positive - risk)
    return _normalize_scenario_probabilities(positive, neutral, risk)


def _adjust_scenario_probabilities_for_levels(
    probabilities: ScenarioProbabilities,
    *,
    price: float,
    support: float,
    resistance: float,
    upside_target: float,
    downside_stop: float,
) -> ScenarioProbabilities:
    positive = probabilities.positive
    neutral = probabilities.neutral
    risk = probabilities.risk
    if price <= 0:
        moved = positive
        positive = 0
        neutral += moved // 2
        risk += moved - moved // 2
        return _normalize_scenario_probabilities(positive, neutral, risk)
    if resistance <= 0:
        positive, neutral = _shift_probability(positive, neutral, 8)
    if upside_target <= 0:
        positive, neutral = _shift_probability(positive, neutral, 10)
    if support <= 0:
        positive, risk = _shift_probability(positive, risk, 6)
    if downside_stop <= 0:
        positive, risk = _shift_probability(positive, risk, 10)
    return _normalize_scenario_probabilities(positive, neutral, risk)


def _adjust_scenario_probabilities_for_decision_state(
    probabilities: ScenarioProbabilities,
    *,
    rating: str | None,
    validation_status: str,
    timeframe_conflict: str,
) -> ScenarioProbabilities:
    if rating == "风险优先" or validation_status == "风险优先":
        return _cap_positive_probability(probabilities, SCENARIO_RISK_PRIORITY_POSITIVE_CAP, neutral_share=0.25)
    if rating == "周期冲突" or timeframe_conflict == "高冲突":
        return _cap_positive_probability(probabilities, SCENARIO_CYCLE_CONFLICT_POSITIVE_CAP, neutral_share=0.65)
    if rating == "等待确认" or timeframe_conflict in TIMEFRAME_WAIT_LEVELS:
        return _cap_positive_probability(probabilities, SCENARIO_WAIT_CONFIRM_POSITIVE_CAP, neutral_share=1.0)
    if validation_status not in CONFIRMING_VALIDATION_STATUSES:
        return _cap_positive_probability(probabilities, SCENARIO_WAIT_CONFIRM_POSITIVE_CAP, neutral_share=1.0)
    return probabilities


def _cap_positive_probability(
    probabilities: ScenarioProbabilities,
    cap: int,
    *,
    neutral_share: float,
) -> ScenarioProbabilities:
    positive = probabilities.positive
    neutral = probabilities.neutral
    risk = probabilities.risk
    excess = max(0, positive - max(0, cap))
    if excess <= 0:
        return probabilities
    positive -= excess
    neutral_move = round(excess * max(0, min(1, neutral_share)))
    neutral += neutral_move
    risk += excess - neutral_move
    return _normalize_scenario_probabilities(positive, neutral, risk)


def _shift_probability(source: int, destination: int, amount: int) -> tuple[int, int]:
    moved = min(max(0, source), max(0, amount))
    return source - moved, destination + moved


def _normalize_scenario_probabilities(positive: object, neutral: object, risk: object) -> ScenarioProbabilities:
    positive = _probability_or_zero(positive)
    neutral = _probability_or_zero(neutral)
    risk = _probability_or_zero(risk)
    total = positive + neutral + risk
    if total <= 0:
        return ScenarioProbabilities(positive=0, neutral=100, risk=0)
    normalized_positive, normalized_neutral, normalized_risk = _integer_probability_split(
        (positive, neutral, risk),
        100,
    )
    if normalized_neutral < SCENARIO_MIN_NEUTRAL_PROBABILITY:
        return _reserve_neutral_probability(normalized_positive, normalized_risk)
    return ScenarioProbabilities(
        positive=normalized_positive,
        neutral=normalized_neutral,
        risk=normalized_risk,
    )


def _reserve_neutral_probability(positive: int, risk: int) -> ScenarioProbabilities:
    positive = _probability_or_zero(positive)
    risk = _probability_or_zero(risk)
    directional_total = positive + risk
    if directional_total <= 0:
        return ScenarioProbabilities(positive=0, neutral=100, risk=0)
    directional_budget = 100 - SCENARIO_MIN_NEUTRAL_PROBABILITY
    normalized_positive, normalized_risk = _integer_probability_split(
        (positive, risk),
        directional_budget,
    )
    return ScenarioProbabilities(
        positive=normalized_positive,
        neutral=SCENARIO_MIN_NEUTRAL_PROBABILITY,
        risk=normalized_risk,
    )


def _integer_probability_split(values: tuple[int, ...], budget: int) -> tuple[int, ...]:
    total = sum(values)
    if budget <= 0 or total <= 0:
        return tuple(0 for _ in values)
    exact = [value / total * budget for value in values]
    base = [math.floor(value) for value in exact]
    remainder = budget - sum(base)
    order = sorted(range(len(values)), key=lambda index: exact[index] - base[index], reverse=True)
    for index in order[:remainder]:
        base[index] += 1
    return tuple(base)


def _probability_or_zero(value: object) -> int:
    parsed = finite_float(value)
    if parsed is None or parsed <= 0:
        return 0
    return round(parsed)
