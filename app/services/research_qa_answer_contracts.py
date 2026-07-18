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
    ThemeContextReport,
    TStrategyAssistantReport,
    TimeframeAlignmentReport,
)


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
