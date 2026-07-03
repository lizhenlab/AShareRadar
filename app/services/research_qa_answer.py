from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.models.schemas import (
    AnalysisResult,
    EventDigestReport,
    EvidenceChainReport,
    MarketRegimeReport,
    PeerComparisonReport,
    RiskRadarReport,
    RiskRewardReport,
    SignalValidationReport,
    StockDiagnosis,
    StockQuestionAnswer,
    ThemeContextReport,
    TStrategyAssistantReport,
    TimeframeAlignmentReport,
)
from app.services.research_qa_topics import related_questions, stock_question_topic
from app.services.research_qa_utils import bounded_int, clean_items, clean_text, dedupe
from app.utils.market_data import finite_float


@dataclass(frozen=True)
class EvidenceContext:
    analysis: AnalysisResult
    diagnosis: StockDiagnosis
    evidence_chain: EvidenceChainReport
    risk_radar: RiskRadarReport
    event_digest: EventDigestReport
    peer_comparison: PeerComparisonReport
    t_strategy: TStrategyAssistantReport
    market_regime: MarketRegimeReport
    risk_reward: RiskRewardReport
    validation: SignalValidationReport
    timeframe: TimeframeAlignmentReport
    theme_context: ThemeContextReport | None = None


@dataclass(frozen=True)
class ActionContext:
    analysis: AnalysisResult
    diagnosis: StockDiagnosis
    risk_radar: RiskRadarReport
    t_strategy: TStrategyAssistantReport
    market_regime: MarketRegimeReport
    risk_reward: RiskRewardReport
    validation: SignalValidationReport
    theme_context: ThemeContextReport | None = None


@dataclass(frozen=True)
class InvalidationContext:
    analysis: AnalysisResult
    diagnosis: StockDiagnosis
    evidence_chain: EvidenceChainReport
    risk_radar: RiskRadarReport
    t_strategy: TStrategyAssistantReport
    validation: SignalValidationReport
    theme_context: ThemeContextReport | None = None


@dataclass(frozen=True)
class ConclusionContext:
    diagnosis: StockDiagnosis
    risk_radar: RiskRadarReport
    t_strategy: TStrategyAssistantReport
    peer_comparison: PeerComparisonReport
    event_digest: EventDigestReport
    risk_reward: RiskRewardReport
    validation: SignalValidationReport
    theme_context: ThemeContextReport | None = None


@dataclass(frozen=True)
class ConfidenceContext:
    analysis: AnalysisResult
    diagnosis: StockDiagnosis
    market_regime: MarketRegimeReport
    validation: SignalValidationReport
    topic: str
    theme_context: ThemeContextReport | None = None


EvidenceBuilder = Callable[[EvidenceContext], list[str]]
ActionBuilder = Callable[[ActionContext], list[str]]
InvalidationBuilder = Callable[[InvalidationContext], list[str]]
ConclusionBuilder = Callable[[ConclusionContext], str]
AnswerPrefixBuilder = Callable[[str, str], str]
ConfidencePenaltyRule = Callable[[ConfidenceContext], int]


_ANSWER_EVIDENCE_LIMIT = 6
_ANSWER_ACTION_LIMIT = 5
_ANSWER_INVALIDATION_LIMIT = 5
_ANSWER_TEXT_ACTION_LIMIT = 3

_EMPTY_EVIDENCE_FALLBACK = ("关键证据暂不足，先按价格、风险收益和验证信号保守判断。",)
_EMPTY_ACTION_FALLBACK = ("证据不足时先等待确认，不把单一信号当作买卖依据。",)
_EMPTY_INVALIDATION_FALLBACK = ("关键价格或风险条件失效时，结论需要重新评估。",)


@dataclass(frozen=True)
class StockQuestionContext:
    analysis: AnalysisResult
    diagnosis: StockDiagnosis
    evidence_chain: EvidenceChainReport
    risk_radar: RiskRadarReport
    event_digest: EventDigestReport
    peer_comparison: PeerComparisonReport
    t_strategy: TStrategyAssistantReport
    market_regime: MarketRegimeReport
    risk_reward: RiskRewardReport
    validation: SignalValidationReport
    timeframe: TimeframeAlignmentReport
    theme_context: ThemeContextReport | None = None


@dataclass(frozen=True)
class TopicAnswerStrategy:
    evidence: EvidenceBuilder
    actions: ActionBuilder
    invalidations: InvalidationBuilder
    conclusion: ConclusionBuilder
    prefix: AnswerPrefixBuilder


def answer_stock_question(
    question: str,
    analysis: AnalysisResult,
    diagnosis: StockDiagnosis,
    evidence_chain: EvidenceChainReport,
    risk_radar: RiskRadarReport,
    event_digest: EventDigestReport,
    peer_comparison: PeerComparisonReport,
    t_strategy: TStrategyAssistantReport,
    market_regime: MarketRegimeReport,
    risk_reward: RiskRewardReport,
    validation: SignalValidationReport,
    timeframe: TimeframeAlignmentReport,
    theme_context: ThemeContextReport | None = None,
) -> StockQuestionAnswer:
    clean_question = " ".join(str(question or "").strip().split())
    topic = stock_question_topic(clean_question)
    context = StockQuestionContext(
        analysis=analysis,
        diagnosis=diagnosis,
        evidence_chain=evidence_chain,
        risk_radar=risk_radar,
        event_digest=event_digest,
        peer_comparison=peer_comparison,
        t_strategy=t_strategy,
        market_regime=market_regime,
        risk_reward=risk_reward,
        validation=validation,
        timeframe=timeframe,
        theme_context=theme_context,
    )
    return _stock_question_answer(clean_question, topic, context)


def _stock_question_answer(question: str, topic: str, context: StockQuestionContext) -> StockQuestionAnswer:
    confidence = _question_context_confidence(topic, context)
    evidence = _question_context_evidence(topic, context)
    actions = _question_context_actions(topic, context)
    invalidations = _question_context_invalidations(topic, context)
    conclusion = _question_context_conclusion(topic, context)
    answer = question_answer_text(topic, context.analysis, context.diagnosis, conclusion, actions, confidence)
    return StockQuestionAnswer(
        symbol=f"{context.analysis.quote.code}.{context.analysis.quote.market}",
        updated_at=context.analysis.quote.timestamp,
        question=question,
        topic=topic,
        conclusion=conclusion,
        answer=answer,
        confidence=confidence,
        evidence=evidence,
        actions=actions,
        invalidations=invalidations,
        related_questions=related_questions(topic),
    )


def _question_context_confidence(topic: str, context: StockQuestionContext) -> int:
    return question_confidence(
        context.analysis,
        context.diagnosis,
        context.market_regime,
        context.validation,
        topic,
        context.theme_context,
    )


def _question_context_evidence(topic: str, context: StockQuestionContext) -> list[str]:
    return question_evidence(
        topic,
        context.analysis,
        context.diagnosis,
        context.evidence_chain,
        context.risk_radar,
        context.event_digest,
        context.peer_comparison,
        context.t_strategy,
        context.market_regime,
        context.risk_reward,
        context.validation,
        context.timeframe,
        context.theme_context,
    )


def _question_context_actions(topic: str, context: StockQuestionContext) -> list[str]:
    return question_actions(
        topic,
        context.analysis,
        context.diagnosis,
        context.risk_radar,
        context.t_strategy,
        context.market_regime,
        context.risk_reward,
        context.validation,
        context.theme_context,
    )


def _question_context_invalidations(topic: str, context: StockQuestionContext) -> list[str]:
    return question_invalidations(
        topic,
        context.analysis,
        context.diagnosis,
        context.evidence_chain,
        context.risk_radar,
        context.t_strategy,
        context.validation,
        context.theme_context,
    )


def _question_context_conclusion(topic: str, context: StockQuestionContext) -> str:
    return question_conclusion(
        topic,
        context.diagnosis,
        context.risk_radar,
        context.t_strategy,
        context.peer_comparison,
        context.event_digest,
        context.risk_reward,
        context.validation,
        context.theme_context,
    )


def question_confidence(
    analysis: AnalysisResult,
    diagnosis: StockDiagnosis,
    market_regime: MarketRegimeReport,
    validation: SignalValidationReport,
    topic: str,
    theme_context: ThemeContextReport | None = None,
) -> int:
    context = ConfidenceContext(
        analysis=analysis,
        diagnosis=diagnosis,
        market_regime=market_regime,
        validation=validation,
        topic=_clean_topic(topic),
        theme_context=theme_context,
    )
    confidence = _base_question_confidence(context) - sum(rule(context) for rule in QUESTION_CONFIDENCE_PENALTIES)
    return bounded_int(confidence, 25, 92)


def _base_question_confidence(context: ConfidenceContext) -> int:
    return min(
        _confidence_component(context.diagnosis.confidence),
        _confidence_component(context.analysis.signal_snapshot.confidence),
        _confidence_component(context.analysis.data_quality.score),
    )


def _confidence_component(value: object) -> int:
    return bounded_int(value, 25, 92, default=25)


def _market_risk_confidence_penalty(context: ConfidenceContext) -> int:
    risk_multiplier = finite_float(context.market_regime.risk_multiplier)
    return 8 if risk_multiplier is not None and risk_multiplier >= 1.25 else 0


def _validation_confidence_penalty(context: ConfidenceContext) -> int:
    return 8 if _clean_answer_item(context.validation.overall_status) == "风险优先" else 0


def _topic_quality_confidence_penalty(context: ConfidenceContext) -> int:
    topic_needs_context = context.topic in {"事件", "同行龙头", "主题概念"}
    return 5 if topic_needs_context and _confidence_component(context.analysis.data_quality.score) < 82 else 0


def _theme_context_confidence_penalty(context: ConfidenceContext) -> int:
    if context.topic != "主题概念":
        return 0
    if not context.theme_context:
        return 10
    missing_data = _clean_answer_items(context.theme_context.missing_data)
    return min(12, 4 * len(missing_data)) if missing_data else 0


QUESTION_CONFIDENCE_PENALTIES: tuple[ConfidencePenaltyRule, ...] = (
    _market_risk_confidence_penalty,
    _validation_confidence_penalty,
    _topic_quality_confidence_penalty,
    _theme_context_confidence_penalty,
)


def question_evidence(
    topic: str,
    analysis: AnalysisResult,
    diagnosis: StockDiagnosis,
    evidence_chain: EvidenceChainReport,
    risk_radar: RiskRadarReport,
    event_digest: EventDigestReport,
    peer_comparison: PeerComparisonReport,
    t_strategy: TStrategyAssistantReport,
    market_regime: MarketRegimeReport,
    risk_reward: RiskRewardReport,
    validation: SignalValidationReport,
    timeframe: TimeframeAlignmentReport,
    theme_context: ThemeContextReport | None = None,
) -> list[str]:
    context = EvidenceContext(
        analysis=analysis,
        diagnosis=diagnosis,
        evidence_chain=evidence_chain,
        risk_radar=risk_radar,
        event_digest=event_digest,
        peer_comparison=peer_comparison,
        t_strategy=t_strategy,
        market_regime=market_regime,
        risk_reward=risk_reward,
        validation=validation,
        timeframe=timeframe,
        theme_context=theme_context,
    )
    return _evidence_for_topic(topic, context)


def question_actions(
    topic: str,
    analysis: AnalysisResult,
    diagnosis: StockDiagnosis,
    risk_radar: RiskRadarReport,
    t_strategy: TStrategyAssistantReport,
    market_regime: MarketRegimeReport,
    risk_reward: RiskRewardReport,
    validation: SignalValidationReport,
    theme_context: ThemeContextReport | None = None,
) -> list[str]:
    context = ActionContext(
        analysis=analysis,
        diagnosis=diagnosis,
        risk_radar=risk_radar,
        t_strategy=t_strategy,
        market_regime=market_regime,
        risk_reward=risk_reward,
        validation=validation,
        theme_context=theme_context,
    )
    return _actions_for_topic(topic, context)


def question_invalidations(
    topic: str,
    analysis: AnalysisResult,
    diagnosis: StockDiagnosis,
    evidence_chain: EvidenceChainReport,
    risk_radar: RiskRadarReport,
    t_strategy: TStrategyAssistantReport,
    validation: SignalValidationReport,
    theme_context: ThemeContextReport | None = None,
) -> list[str]:
    context = InvalidationContext(
        analysis=analysis,
        diagnosis=diagnosis,
        evidence_chain=evidence_chain,
        risk_radar=risk_radar,
        t_strategy=t_strategy,
        validation=validation,
        theme_context=theme_context,
    )
    return _invalidations_for_topic(topic, context)


def question_conclusion(
    topic: str,
    diagnosis: StockDiagnosis,
    risk_radar: RiskRadarReport,
    t_strategy: TStrategyAssistantReport,
    peer_comparison: PeerComparisonReport,
    event_digest: EventDigestReport,
    risk_reward: RiskRewardReport,
    validation: SignalValidationReport,
    theme_context: ThemeContextReport | None = None,
) -> str:
    context = ConclusionContext(
        diagnosis=diagnosis,
        risk_radar=risk_radar,
        t_strategy=t_strategy,
        peer_comparison=peer_comparison,
        event_digest=event_digest,
        risk_reward=risk_reward,
        validation=validation,
        theme_context=theme_context,
    )
    return _conclusion_for_topic(topic, context)


def question_answer_text(
    topic: str,
    analysis: AnalysisResult,
    diagnosis: StockDiagnosis,
    conclusion: str,
    actions: list[str],
    confidence: int,
) -> str:
    name = _display_text(analysis.quote.name, fallback=_display_text(analysis.quote.code, fallback="该股票"))
    prefix = _answer_prefix(topic, name, conclusion)
    action_items = _answer_items(
        actions,
        limit=_ANSWER_TEXT_ACTION_LIMIT,
        fallback=(diagnosis.beginner_summary,),
        empty_fallback=_EMPTY_ACTION_FALLBACK,
    )
    confidence_text = bounded_int(confidence, 25, 92, default=25)
    return f"{prefix} 我的建议是：{'；'.join(action_items)} 这次回答置信度约 {confidence_text}%，需要随行情和数据质量动态更新。"


def _evidence_for_topic(topic: str, context: EvidenceContext) -> list[str]:
    strategy = _answer_strategy(topic)
    fallback = _default_evidence(context) if strategy is not _DEFAULT_ANSWER_STRATEGY else _EMPTY_EVIDENCE_FALLBACK
    return _answer_items(
        strategy.evidence(context),
        limit=_ANSWER_EVIDENCE_LIMIT,
        fallback=fallback,
        empty_fallback=_EMPTY_EVIDENCE_FALLBACK,
    )


def _actions_for_topic(topic: str, context: ActionContext) -> list[str]:
    strategy = _answer_strategy(topic)
    fallback = _default_actions(context) if strategy is not _DEFAULT_ANSWER_STRATEGY else _EMPTY_ACTION_FALLBACK
    return _answer_items(
        strategy.actions(context),
        limit=_ANSWER_ACTION_LIMIT,
        fallback=fallback,
        empty_fallback=_EMPTY_ACTION_FALLBACK,
    )


def _invalidations_for_topic(topic: str, context: InvalidationContext) -> list[str]:
    strategy = _answer_strategy(topic)
    fallback = _default_invalidations(context) if strategy is not _DEFAULT_ANSWER_STRATEGY else _EMPTY_INVALIDATION_FALLBACK
    return _answer_items(
        strategy.invalidations(context),
        limit=_ANSWER_INVALIDATION_LIMIT,
        fallback=fallback,
        empty_fallback=_EMPTY_INVALIDATION_FALLBACK,
    )


def _conclusion_for_topic(topic: str, context: ConclusionContext) -> str:
    return _answer_strategy(topic).conclusion(context)


def _answer_items(
    items: object,
    *,
    limit: int,
    fallback: object,
    empty_fallback: object,
) -> list[str]:
    cleaned = _clean_answer_items(items)
    if cleaned:
        return cleaned[: max(0, limit)]
    fallback_items = _clean_answer_items(fallback)
    empty_fallback_items = _clean_answer_items(empty_fallback)
    return (fallback_items or empty_fallback_items)[: max(0, limit)]


def _clean_answer_items(items: object) -> list[str]:
    return clean_items(items)


def _clean_answer_item(item: object) -> str:
    return clean_text(item)


def _first_clean_items(items: object, limit: int) -> list[str]:
    return _clean_answer_items(items)[: max(0, limit)]


def _display_text(value: object, *, fallback: str = "待确认") -> str:
    return _clean_answer_item(value) or fallback


def _clean_topic(topic: object) -> str:
    return _clean_answer_item(topic)


def _format_number(value: object, *, suffix: str = "", fallback: str = "待确认", precision: int = 2) -> str:
    parsed = finite_float(value)
    if parsed is None:
        return fallback
    return f"{parsed:.{precision}f}{suffix}"


def _format_score(value: object) -> str:
    parsed = finite_float(value)
    if parsed is None:
        return "待确认"
    return str(bounded_int(parsed, 0, 100, round_value=True))


def _base_evidence(context: EvidenceContext) -> list[str]:
    return [
        context.diagnosis.headline,
        f"当前价 {_format_number(context.analysis.quote.price)}，支撑 {_format_number(context.analysis.support)}，压力 {_format_number(context.analysis.resistance)}。",
        f"{_display_text(context.market_regime.market_label)}，风险倍率 {_format_number(context.market_regime.risk_multiplier)}。",
    ]


def _t_strategy_evidence(context: EvidenceContext) -> list[str]:
    t_plan_evidence = [item.reason for item in context.analysis.t_plan[:2]]
    return dedupe([context.t_strategy.summary, *context.t_strategy.execution_steps[:2], *_base_evidence(context), *t_plan_evidence])


def _risk_evidence(context: EvidenceContext) -> list[str]:
    return dedupe([
        context.risk_radar.summary,
        *context.risk_radar.top_risks,
        *context.diagnosis.hard_risks[:3],
        context.risk_reward.summary,
        context.timeframe.summary,
    ])


def _risk_reward_evidence(context: EvidenceContext) -> list[str]:
    scenario_text = [
        f"{_display_text(item.name)}：{_display_text(item.trigger)}；{_display_text(item.expected_move)}；应对：{_display_text(item.response)}"
        for item in context.risk_reward.scenarios[:3]
    ]
    return dedupe([
        context.risk_reward.summary,
        f"上方目标 {_format_number(context.risk_reward.upside_target)}（{_format_number(context.risk_reward.upside_pct, suffix='%')}），下方防守 {_format_number(context.risk_reward.downside_stop)}（{_format_number(context.risk_reward.downside_pct, suffix='%')}），收益风险比 {_format_number(context.risk_reward.reward_risk_ratio)}。",
        context.validation.summary,
        context.timeframe.summary,
        *scenario_text,
        *context.risk_reward.notes[:2],
    ])


def _buy_evidence(context: EvidenceContext) -> list[str]:
    buy_evidence = [item.reason for item in context.analysis.buy_points[:3]]
    confirmations = [item.confirmation_condition for item in context.validation.items[:3]]
    return dedupe([*_base_evidence(context), context.risk_reward.summary, *context.evidence_chain.support[:3], *buy_evidence, *confirmations])


def _sell_evidence(context: EvidenceContext) -> list[str]:
    sell_evidence = [item.reason for item in context.analysis.sell_points[:3]]
    return dedupe([*_base_evidence(context), context.risk_reward.summary, *context.evidence_chain.opposition[:3], *sell_evidence, *context.risk_radar.top_risks[:2]])


def _peer_evidence(context: EvidenceContext) -> list[str]:
    return dedupe([
        context.peer_comparison.summary,
        context.peer_comparison.valuation_position,
        context.peer_comparison.strength_position,
        *context.peer_comparison.metrics[:3],
        *context.peer_comparison.risks[:2],
    ])


def _theme_evidence(context: EvidenceContext) -> list[str]:
    if not context.theme_context:
        return dedupe([*_base_evidence(context), "主题概念报告暂不可用，先按行业、个股趋势和数据质量保守解释。"])
    theme = context.theme_context
    concepts = [f"{_display_text(item.name)}{_format_number(item.change_pct, suffix='%')}" for item in theme.concepts[:4]]
    industry_change = f" {_format_number(theme.industry_change_pct, suffix='%')}" if theme.industry_change_pct is not None else " 待确认"
    return dedupe([
        theme.summary,
        f"主题评分 {_format_score(theme.score)}，状态 {_display_text(theme.level)}，风格 {_display_text(theme.style)}，相对强弱 {_display_text(theme.relative_strength)}。",
        f"行业 {_display_text(theme.industry)}{industry_change}。",
        "相关概念：" + "、".join(concepts) + "。" if concepts else "相关概念待确认。",
        *theme.evidence[:4],
        *theme.risks[:2],
    ])


def _event_evidence(context: EvidenceContext) -> list[str]:
    return dedupe([
        context.event_digest.summary,
        *context.event_digest.negative_events[:3],
        *context.event_digest.positive_events[:3],
        *context.event_digest.watch_events[:3],
        *context.event_digest.missing_data[:2],
    ])


def _short_term_evidence(context: EvidenceContext) -> list[str]:
    return dedupe([
        *_base_evidence(context),
        context.timeframe.summary,
        context.validation.summary,
        *context.diagnosis.confirmation_signals[:3],
        *context.diagnosis.watch_focus[:2],
    ])


def _default_evidence(context: EvidenceContext) -> list[str]:
    return dedupe([
        *_base_evidence(context),
        context.evidence_chain.summary,
        context.risk_reward.summary,
        context.validation.summary,
        context.timeframe.summary,
        *context.evidence_chain.support[:2],
        *context.evidence_chain.opposition[:2],
    ])


def _t_strategy_actions(context: ActionContext) -> list[str]:
    return dedupe([
        f"只在已有可卖底仓前提下执行，低吸参考 {_display_text(context.t_strategy.low_zone)}，高抛参考 {_display_text(context.t_strategy.high_zone)}。",
        *context.t_strategy.execution_steps,
        "若无法严格执行高抛低吸纪律，则宁可不做。",
    ])


def _risk_actions(context: ActionContext) -> list[str]:
    ranked_actions = [item.action for item in sorted(context.risk_radar.items, key=lambda row: _sort_score(row.score), reverse=True)]
    return _first_clean_items(ranked_actions, 4)


def _sort_score(value: object) -> int:
    return bounded_int(value, 0, 100, default=0)


def _risk_reward_actions(context: ActionContext) -> list[str]:
    rating = _display_text(context.risk_reward.rating)
    validation_status = _display_text(context.validation.overall_status)
    actions = [
        f"只有风险收益评级达到「性价比较好」或「性价比一般」，且验证状态不是「风险优先」时，才考虑观察级动作；当前为「{rating} / {validation_status}」。",
        f"若价格贴近上方目标 {_format_number(context.risk_reward.upside_target)} 或收益风险比低于 1.2，不新增追高。",
        f"若跌近下方防守 {_format_number(context.risk_reward.downside_stop)}，先执行防守而不是补仓摊低。",
    ]
    return dedupe([*actions, *[item.response for item in context.risk_reward.scenarios[:2]]])


def _buy_actions(context: ActionContext) -> list[str]:
    actions = [
        item.action_hint
        for item in context.validation.items
        if item.category == "买点" and item.status in {"接近确认", "等待确认", "低置信观察", "环境压制", "周期冲突降级"}
    ][:3]
    return dedupe([
        f"只有站稳支撑 {_format_number(context.analysis.support)} 且不过度贴近压力 {_format_number(context.analysis.resistance)} 时，才考虑观察级动作。",
        *actions,
        "风险收益比没有修复前，不把反弹直接当买点。",
    ])


def _sell_actions(context: ActionContext) -> list[str]:
    return dedupe([
        f"接近压力 {_format_number(context.analysis.resistance)} 且量价乏力时优先保护利润。",
        f"跌破支撑 {_format_number(context.analysis.support)} 后不要用主观预期替代纪律。",
        *[item.action_hint for item in context.validation.items if item.status in {"风险触发", "周期冲突降级"}][:3],
    ])


def _peer_actions(context: ActionContext) -> list[str]:
    return dedupe(["先确认强弱分位是否持续靠前，再看成交额是否同步放大。", "若同行更强而本股滞涨，不主动上调龙头判断。"])


def _theme_actions(context: ActionContext) -> list[str]:
    if not context.theme_context:
        return ["主题概念数据未确认前，不把题材当作买入理由。", "先看个股是否守住关键价位和量能确认。"]
    return dedupe([
        *context.theme_context.opportunities[:3],
        *context.theme_context.risks[:2],
        "主题只作为解释背景，具体动作仍以支撑、压力、量能和失效条件为准。",
    ])


def _event_actions(context: ActionContext) -> list[str]:
    return dedupe(["把事件作为结论修正项，不单独作为买卖依据。", "事件偏风险时，先等价格和量能验证风险是否消化。"])


def _short_term_actions(context: ActionContext) -> list[str]:
    return dedupe([
        f"明线看支撑 {_format_number(context.analysis.support)} 和压力 {_format_number(context.analysis.resistance)}。",
        *context.diagnosis.confirmation_signals[:3],
        f"环境风险倍率 {_format_number(context.market_regime.risk_multiplier)}，风险收益评级 {_display_text(context.risk_reward.rating)}。",
    ])


def _default_actions(context: ActionContext) -> list[str]:
    return dedupe([context.diagnosis.action, *context.diagnosis.watch_focus, context.validation.summary])


def _t_strategy_invalidations(context: InvalidationContext) -> list[str]:
    return dedupe([*context.t_strategy.stop_conditions, f"跌破支撑 {_format_number(context.analysis.support)} 或冲高回落放量。"])


def _buy_or_short_term_invalidations(context: InvalidationContext) -> list[str]:
    return dedupe([*_first_clean_items(context.evidence_chain.invalidations, 3), *_first_clean_items(context.diagnosis.hard_risks, 2), f"价格有效跌破 {_format_number(context.analysis.support)}。"])


def _sell_invalidations(context: InvalidationContext) -> list[str]:
    return dedupe([
        f"放量站稳压力 {_format_number(context.analysis.resistance)} 后，卖点需要重新评估。",
        *[item.confirmation_condition for item in context.validation.items[:2]],
    ])


def _risk_invalidations(context: InvalidationContext) -> list[str]:
    invalidations = [
        f"{_display_text(item.name)}：{_display_text(item.action)}"
        for item in context.risk_radar.items
        if _sort_score(item.score) >= 42
    ]
    return _first_clean_items(invalidations, 4)


def _risk_reward_invalidations(context: InvalidationContext) -> list[str]:
    return dedupe([
        "收益风险比跌破 1.2，或风险收益评级降为「性价比不足 / 风险优先」。",
        f"有效跌破支撑 {_format_number(context.analysis.support)}，说明下方风险开始兑现。",
        *[item.invalidation_condition for item in context.validation.items[:3]],
    ])


def _theme_invalidations(context: InvalidationContext) -> list[str]:
    theme_invalidations = [
        "概念热度转弱但个股仍无法放量走强，题材支撑需要降权。",
        "概念上涨只来自少数龙头，本股相对强弱转为落后时，不上调主题判断。",
        f"跌破关键支撑 {_format_number(context.analysis.support)} 后，题材解释不能替代价格纪律。",
    ]
    if context.theme_context and context.theme_context.missing_data:
        theme_invalidations.append("主题归属、行业涨跌或数据质量仍有缺口时，不能把题材作为核心依据。")
    return dedupe([*theme_invalidations, *context.diagnosis.hard_risks[:2]])


def _default_invalidations(context: InvalidationContext) -> list[str]:
    return dedupe([*_first_clean_items(context.evidence_chain.invalidations, 4), *_first_clean_items(context.diagnosis.hard_risks, 2)])


def _t_strategy_conclusion(context: ConclusionContext) -> str:
    return f"{_display_text(context.t_strategy.suitability)}：{_display_text(context.t_strategy.style)}"


def _risk_conclusion(context: ConclusionContext) -> str:
    primary_risks = ", ".join(item.split("：")[0] for item in _first_clean_items(context.risk_radar.top_risks, 2))
    return f"{_display_text(context.risk_radar.overall_level)}，优先看 {primary_risks or '关键风险'}"


def _risk_reward_conclusion(context: ConclusionContext) -> str:
    return f"{_display_text(context.risk_reward.rating)}，收益风险比 {_format_number(context.risk_reward.reward_risk_ratio)}，验证状态「{_display_text(context.validation.overall_status)}」"


def _buy_conclusion(context: ConclusionContext) -> str:
    return f"{_display_text(context.diagnosis.action)}，买点必须服从「{_display_text(context.validation.overall_status)}」和「{_display_text(context.risk_reward.rating)}」"


def _sell_conclusion(context: ConclusionContext) -> str:
    return f"以压力位和失效条件为先，当前总建议「{_display_text(context.diagnosis.action)}」"


def _peer_conclusion(context: ConclusionContext) -> str:
    return f"{_display_text(context.peer_comparison.strength_position)}，{_display_text(context.peer_comparison.valuation_position)}"


def _theme_conclusion(context: ConclusionContext) -> str:
    if not context.theme_context:
        return "主题概念待确认，暂不提高结论权重"
    theme = context.theme_context
    return f"{_display_text(theme.level)}，{_display_text(theme.style)}，{_display_text(theme.relative_strength)}，主题评分 {_format_score(theme.score)}"


def _event_conclusion(context: ConclusionContext) -> str:
    return _display_text(context.event_digest.impact_label)


def _short_term_conclusion(context: ConclusionContext) -> str:
    return f"短线先看确认，不抢结论；当前总建议「{_display_text(context.diagnosis.action)}」"


def _default_conclusion(context: ConclusionContext) -> str:
    return f"当前总建议「{_display_text(context.diagnosis.action)}」，风险收益评级「{_display_text(context.risk_reward.rating)}」"


def _answer_prefix(topic: str, name: str, conclusion: str) -> str:
    return _answer_strategy(topic).prefix(_display_text(name, fallback="该股票"), _display_text(conclusion))


def _default_answer_prefix(name: str, conclusion: str) -> str:
    return f"{_display_text(name, fallback='该股票')}这只个股的回答是：{_display_text(conclusion)}。"


_DEFAULT_ANSWER_STRATEGY = TopicAnswerStrategy(
    evidence=_default_evidence,
    actions=_default_actions,
    invalidations=_default_invalidations,
    conclusion=_default_conclusion,
    prefix=_default_answer_prefix,
)


TOPIC_ANSWER_STRATEGIES: dict[str, TopicAnswerStrategy] = {
    "做T": TopicAnswerStrategy(
        evidence=_t_strategy_evidence,
        actions=_t_strategy_actions,
        invalidations=_t_strategy_invalidations,
        conclusion=_t_strategy_conclusion,
        prefix=lambda name, conclusion: f"{name}的做T判断是：{conclusion}。做T只服务于已有底仓降成本。",
    ),
    "风险": TopicAnswerStrategy(
        evidence=_risk_evidence,
        actions=_risk_actions,
        invalidations=_risk_invalidations,
        conclusion=_risk_conclusion,
        prefix=lambda name, conclusion: f"{name}当前风险判断是：{conclusion}。",
    ),
    "风险收益": TopicAnswerStrategy(
        evidence=_risk_reward_evidence,
        actions=_risk_reward_actions,
        invalidations=_risk_reward_invalidations,
        conclusion=_risk_reward_conclusion,
        prefix=lambda name, conclusion: f"{name}当前风险收益判断是：{conclusion}。",
    ),
    "买点": TopicAnswerStrategy(
        evidence=_buy_evidence,
        actions=_buy_actions,
        invalidations=_buy_or_short_term_invalidations,
        conclusion=_buy_conclusion,
        prefix=lambda name, conclusion: f"{name}当前不能只按“想买”处理，系统结论是：{conclusion}。",
    ),
    "卖点": TopicAnswerStrategy(
        evidence=_sell_evidence,
        actions=_sell_actions,
        invalidations=_sell_invalidations,
        conclusion=_sell_conclusion,
        prefix=lambda name, conclusion: f"{name}的卖点更适合按压力和失效条件处理，结论是：{conclusion}。",
    ),
    "同行龙头": TopicAnswerStrategy(
        evidence=_peer_evidence,
        actions=_peer_actions,
        invalidations=_default_invalidations,
        conclusion=_peer_conclusion,
        prefix=lambda name, conclusion: f"{name}的同行强弱判断是：{conclusion}。",
    ),
    "主题概念": TopicAnswerStrategy(
        evidence=_theme_evidence,
        actions=_theme_actions,
        invalidations=_theme_invalidations,
        conclusion=_theme_conclusion,
        prefix=lambda name, conclusion: f"{name}的题材背景判断是：{conclusion}。题材只解释背景，不能单独替代价格和风险收益。",
    ),
    "事件": TopicAnswerStrategy(
        evidence=_event_evidence,
        actions=_event_actions,
        invalidations=_default_invalidations,
        conclusion=_event_conclusion,
        prefix=lambda name, conclusion: f"{name}的事件影响判断是：{conclusion}。",
    ),
    "短线观察": TopicAnswerStrategy(
        evidence=_short_term_evidence,
        actions=_short_term_actions,
        invalidations=_buy_or_short_term_invalidations,
        conclusion=_short_term_conclusion,
        prefix=lambda name, conclusion: f"{name}的短线观察结论是：{conclusion}。",
    ),
    "综合判断": _DEFAULT_ANSWER_STRATEGY,
}


def _answer_strategy(topic: str) -> TopicAnswerStrategy:
    return TOPIC_ANSWER_STRATEGIES.get(_clean_topic(topic), _DEFAULT_ANSWER_STRATEGY)


__all__ = [
    "answer_stock_question",
    "question_actions",
    "question_answer_text",
    "question_conclusion",
    "question_confidence",
    "question_evidence",
    "question_invalidations",
]
