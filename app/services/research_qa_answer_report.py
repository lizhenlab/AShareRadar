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
    StockQuestionAnswer,
    ThemeContextReport,
    TStrategyAssistantReport,
    TimeframeAlignmentReport,
)
from app.services.research_qa_answer_confidence import question_confidence
from app.services.research_qa_answer_contracts import StockQuestionContext
from app.services.research_qa_answer_selectors import (
    question_actions,
    question_answer_text,
    question_conclusion,
    question_evidence,
    question_invalidations,
)
from app.services.research_qa_topics import related_questions, stock_question_topic


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
