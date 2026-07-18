from __future__ import annotations

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
    ThemeContextReport,
    TStrategyAssistantReport,
    TimeframeAlignmentReport,
)
from app.services.research_qa_answer_actions import _default_actions
from app.services.research_qa_answer_conclusions import _default_invalidations
from app.services.research_qa_answer_contracts import (
    _ANSWER_ACTION_LIMIT,
    _ANSWER_EVIDENCE_LIMIT,
    _ANSWER_INVALIDATION_LIMIT,
    _ANSWER_TEXT_ACTION_LIMIT,
    _EMPTY_ACTION_FALLBACK,
    _EMPTY_EVIDENCE_FALLBACK,
    _EMPTY_INVALIDATION_FALLBACK,
    ActionContext,
    ConclusionContext,
    EvidenceContext,
    InvalidationContext,
)
from app.services.research_qa_answer_evidence import _default_evidence
from app.services.research_qa_answer_formatters import _answer_items, _display_text
from app.services.research_qa_answer_strategies import _DEFAULT_ANSWER_STRATEGY, _answer_prefix, _answer_strategy
from app.services.research_qa_utils import bounded_int


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
    reliability_score = bounded_int(confidence, 25, 92, default=25)
    return f"{prefix} 我的建议是：{'；'.join(action_items)} 这次回答可靠度 {reliability_score}/100，需要随行情和数据质量动态更新。"


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
