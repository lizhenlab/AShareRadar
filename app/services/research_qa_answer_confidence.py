from __future__ import annotations

from app.models.schemas import AnalysisResult, MarketRegimeReport, SignalValidationReport, StockDiagnosis, ThemeContextReport
from app.services.research_qa_answer_contracts import ConfidenceContext, ConfidencePenaltyRule
from app.services.research_qa_answer_formatters import _clean_answer_item, _clean_answer_items, _clean_topic
from app.services.research_qa_utils import bounded_int
from app.utils.market_data import finite_float


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
