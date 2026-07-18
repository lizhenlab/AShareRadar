from __future__ import annotations

from app.models.schemas import AnalysisResult, RuleMatch
from app.services.stock_rule_contracts import (
    LEVEL_CAUTIOUS,
    LEVEL_POSITIVE,
    LEVEL_RISK,
    LEVEL_WATCH,
    QUALITY_GATE_CLOSE_CONFIDENCE_FLOOR,
    QUALITY_GATE_CLOSE_PENALTY,
    QUALITY_GATE_MATCHED_CONFIDENCE_FLOOR,
    QUALITY_GATE_MATCHED_PENALTY,
    QUALITY_GATE_MISSED_CONFIDENCE_FLOOR,
    QUALITY_GATE_MISSED_PENALTY,
    QUALITY_GATE_PASS_SCORE,
    QUALITY_GATE_RISK_MULTIPLIER,
    QUALITY_GATE_WEAK_SCORE,
    STATUS_CLOSE,
    STATUS_MATCHED,
    STATUS_MISSED,
    QualityGateContext,
    QualityGateDecision,
)


def _apply_quality_gate(match: RuleMatch, analysis: AnalysisResult) -> RuleMatch:
    context = QualityGateContext(match=match, score=analysis.data_quality.score, level=analysis.data_quality.level)
    if _quality_gate_not_needed(context):
        return match
    decision = _quality_gate_decision(context)
    return _copy_with_quality_gate(match, decision, _quality_gate_reason(context))


def _quality_gate_not_needed(context: QualityGateContext) -> bool:
    return context.score >= QUALITY_GATE_PASS_SCORE


def _quality_gate_decision(context: QualityGateContext) -> QualityGateDecision:
    if context.match.level == LEVEL_RISK:
        return _risk_quality_gate_decision(context)
    if context.match.status == STATUS_MATCHED:
        return QualityGateDecision(
            status=STATUS_CLOSE,
            level=_quality_gate_level(context),
            confidence=_reduced_confidence(
                context.match.confidence,
                QUALITY_GATE_MATCHED_PENALTY,
                QUALITY_GATE_MATCHED_CONFIDENCE_FLOOR,
            ),
        )
    if context.match.status == STATUS_CLOSE:
        status = STATUS_MISSED if context.score < QUALITY_GATE_WEAK_SCORE else STATUS_CLOSE
        return QualityGateDecision(
            status=status,
            level=_quality_gate_level(context),
            confidence=_reduced_confidence(
                context.match.confidence,
                QUALITY_GATE_CLOSE_PENALTY,
                QUALITY_GATE_CLOSE_CONFIDENCE_FLOOR,
            ),
        )
    return QualityGateDecision(
        status=context.match.status,
        level=_quality_gate_level(context),
        confidence=_reduced_confidence(
            context.match.confidence,
            QUALITY_GATE_MISSED_PENALTY,
            QUALITY_GATE_MISSED_CONFIDENCE_FLOOR,
        ),
    )


def _risk_quality_gate_decision(context: QualityGateContext) -> QualityGateDecision:
    return QualityGateDecision(
        status=context.match.status,
        level=context.match.level,
        confidence=max(
            QUALITY_GATE_MISSED_CONFIDENCE_FLOOR,
            round(context.match.confidence * QUALITY_GATE_RISK_MULTIPLIER),
        ),
    )


def _quality_gate_level(context: QualityGateContext) -> str:
    if context.match.level == LEVEL_CAUTIOUS:
        return LEVEL_CAUTIOUS
    return LEVEL_CAUTIOUS if context.match.level == LEVEL_POSITIVE and context.score < QUALITY_GATE_WEAK_SCORE else LEVEL_WATCH


def _reduced_confidence(confidence: int, penalty: int, floor: int) -> int:
    return max(floor, confidence - penalty)


def _quality_gate_reason(context: QualityGateContext) -> str:
    return f"数据质量{context.level}，该规则结论已降权。"


def _copy_with_quality_gate(match: RuleMatch, decision: QualityGateDecision, reason: str) -> RuleMatch:
    return match.model_copy(
        update={
            "status": decision.status,
            "level": decision.level,
            "confidence": decision.confidence,
            "evidence": [*match.evidence, reason],
        }
    )
