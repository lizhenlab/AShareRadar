from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable

from app.models.schemas import (
    AlphaEvidencePoint,
    AlphaEvidenceReport,
    AnalysisResult,
    FactorLabReport,
    FeatureSnapshot,
    MarketRegimeReport,
    RiskRewardReport,
    StockInsightBundle,
    TimeframeAlignmentReport,
)
from app.services.research_alpha_points import collect_alpha_points
from app.services.research_factors import _factor_missing_data
from app.services.scoring import clamp_score as _clamp
from app.utils.market_data import finite_float


CONFLICT_TIMEFRAME_LEVELS = {"高冲突", "中冲突", "多周期偏弱"}
BLOCKING_TIMEFRAME_LEVELS = {"高冲突", "多周期偏弱"}
NEGATIVE_RISK_REWARD_RATINGS = {"风险优先", "周期冲突", "性价比不足"}
BLOCKING_RISK_REWARD_RATINGS = {"风险优先", "周期冲突"}
MAX_POSITIVE_POINTS = 6
MAX_NEGATIVE_POINTS = 6
MAX_MISSING_DATA_ITEMS = 10
MAX_DATA_QUALITY_NOTES = 6
TIMEFRAME_CONFLICT_PENALTY = -8
TIMEFRAME_ALIGNMENT_BONUS = 4
RISK_REWARD_CONFIDENCE_PENALTY = -6
MISSING_DATA_PENALTY_PER_ITEM = 2
MAX_MISSING_DATA_PENALTY = 12
MARKET_RISK_SUPPRESSION_MULTIPLIER = 1.25
MARKET_RISK_NEGATIVE_POWER_RATIO = 0.8
POSITIVE_DOMINANCE_MARGIN = 10
POSITIVE_DOMINANCE_MIN_POWER = 18
NEGATIVE_DOMINANCE_MARGIN = 12
MISSING_FACTOR_LAB = "因子实验室报告"
MISSING_MARKET_REGIME = "市场环境报告"
MISSING_TIMEFRAME = "多周期报告"
MISSING_RISK_REWARD = "风险收益报告"
FEATURE_ALPHA_NUMERIC_FIELDS = (
    ("data_quality_score", "特征快照数据质量分"),
    ("leader_score", "特征快照龙头评分"),
)
_NOT_REQUESTED = object()
FACTOR_LAB_BASE_WEIGHTS = (
    ("signal_confidence", 0.3),
    ("data_quality_score", 0.2),
    ("leader_score", 0.1),
    ("overview_score", 0.14),
    ("calibrated_confidence", 0.26),
)
NO_FACTOR_LAB_BASE_WEIGHTS = (
    ("signal_confidence", 0.45),
    ("data_quality_score", 0.25),
    ("leader_score", 0.15),
    ("overview_score", 0.15),
)
INVALID_ALPHA_TEXT_VALUES = {
    "",
    "-",
    "--",
    "n/a",
    "na",
    "none",
    "null",
    "nan",
    "+nan",
    "-nan",
    "inf",
    "+inf",
    "-inf",
    "infinity",
    "+infinity",
    "-infinity",
    "暂无",
    "暂无数据",
    "无",
    "无数据",
}
INVALID_ALPHA_TEXT_TOKEN_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:n/a|none|null|nan|[+-]?(?:inf|infinity))(?![A-Za-z0-9_])"
)


@dataclass(frozen=True)
class AlphaConfidenceAdjustment:
    name: str
    value: int


@dataclass(frozen=True)
class AlphaVerdictContext:
    data_quality_score: int
    positive_power: int
    negative_power: int
    market_risk_multiplier: float | None
    timeframe_conflict: str | None
    risk_reward_rating: str | None


@dataclass(frozen=True)
class AlphaVerdictRule:
    name: str
    verdict: str
    matches: Callable[[AlphaVerdictContext], bool]


@dataclass(frozen=True)
class AlphaEvidenceBuckets:
    positives: list[AlphaEvidencePoint]
    negatives: list[AlphaEvidencePoint]
    missing_data: list[str]


@dataclass(frozen=True)
class AlphaPointCandidate:
    impact: float
    index: int
    point: AlphaEvidencePoint


def build_alpha_evidence_report(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport | None = None,
    market_regime: MarketRegimeReport | None = None,
    timeframe: TimeframeAlignmentReport | None = None,
    risk_reward: RiskRewardReport | None = None,
) -> AlphaEvidenceReport:
    buckets = _alpha_evidence_buckets(
        analysis,
        insights,
        feature,
        factor_lab=factor_lab,
        market_regime=market_regime,
        timeframe=timeframe,
        risk_reward=risk_reward,
    )
    confidence = _alpha_confidence(
        analysis,
        insights,
        feature,
        buckets.missing_data,
        factor_lab,
        market_regime,
        timeframe,
        risk_reward,
    )
    verdict = _alpha_verdict(feature, buckets.positives, buckets.negatives, market_regime, timeframe, risk_reward)
    return AlphaEvidenceReport(
        symbol=feature.symbol,
        updated_at=feature.updated_at,
        confidence=confidence,
        verdict=verdict,
        summary=_alpha_summary(buckets.positives, buckets.negatives, buckets.missing_data, confidence),
        positives=buckets.positives,
        negatives=buckets.negatives,
        missing_data=buckets.missing_data,
        data_quality_notes=_alpha_data_quality_notes(analysis),
    )


def _alpha_evidence_buckets(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    *,
    factor_lab: FactorLabReport | None = None,
    market_regime: MarketRegimeReport | None = None,
    timeframe: TimeframeAlignmentReport | None = None,
    risk_reward: RiskRewardReport | None = None,
) -> AlphaEvidenceBuckets:
    points = collect_alpha_points(
        analysis,
        insights,
        factor_lab=factor_lab,
        market_regime=market_regime,
        timeframe=timeframe,
        risk_reward=risk_reward,
    )
    return AlphaEvidenceBuckets(
        positives=_top_alpha_points(points, positive=True, limit=MAX_POSITIVE_POINTS),
        negatives=_top_alpha_points(points, positive=False, limit=MAX_NEGATIVE_POINTS),
        missing_data=_alpha_missing_data(
            insights,
            factor_lab,
            feature=feature,
            market_regime=market_regime,
            timeframe=timeframe,
            risk_reward=risk_reward,
        ),
    )


def _top_alpha_points(
    points: Iterable[AlphaEvidencePoint],
    *,
    positive: bool,
    limit: int,
) -> list[AlphaEvidencePoint]:
    max_items = _alpha_point_limit(limit)
    if max_items <= 0:
        return []

    selected = _alpha_point_candidates(points, positive=positive)
    ordered = sorted(selected, key=_alpha_point_sort_key(positive))
    return _deduped_alpha_points(ordered, limit=max_items)


def _alpha_point_limit(limit: object) -> int:
    parsed = finite_float(limit)
    if parsed is None or parsed <= 0:
        return 0
    return int(parsed)


def _alpha_point_candidates(points: Iterable[AlphaEvidencePoint], *, positive: bool) -> list[AlphaPointCandidate]:
    return [
        candidate
        for index, item in enumerate(_iter_alpha_points(points))
        if (candidate := _alpha_point_candidate(index, item, positive=positive)) is not None
    ]


def _iter_alpha_points(points: Iterable[AlphaEvidencePoint] | None) -> Iterable[AlphaEvidencePoint]:
    if points is None:
        return []
    try:
        iter(points)
    except TypeError:
        return []
    return points


def _alpha_point_candidate(index: int, item: AlphaEvidencePoint, *, positive: bool) -> AlphaPointCandidate | None:
    impact = _finite_impact(item)
    if impact is None or not _alpha_point_matches_direction(impact, positive=positive):
        return None
    if not _alpha_point_is_displayable(item):
        return None
    return AlphaPointCandidate(impact=impact, index=index, point=item)


def _alpha_point_matches_direction(impact: float, *, positive: bool) -> bool:
    return impact > 0 if positive else impact < 0


def _alpha_point_sort_key(positive: bool) -> Callable[[AlphaPointCandidate], tuple[float, int]]:
    if positive:
        return lambda candidate: (-candidate.impact, candidate.index)
    return lambda candidate: (candidate.impact, candidate.index)


def _deduped_alpha_points(
    candidates: Iterable[AlphaPointCandidate],
    *,
    limit: int,
) -> list[AlphaEvidencePoint]:
    result: list[AlphaEvidencePoint] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = _alpha_point_key(candidate.point)
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate.point)
        if len(result) >= limit:
            break
    return result


def _finite_impact(item: AlphaEvidencePoint) -> float | None:
    return finite_float(getattr(item, "impact", None))


def _alpha_point_key(item: AlphaEvidencePoint) -> tuple[str, str]:
    return (
        _normalized_key(getattr(item, "title", "")),
        _normalized_key(getattr(item, "reason", "")),
    )


def _alpha_point_is_displayable(item: AlphaEvidencePoint) -> bool:
    return bool(_clean_alpha_text(getattr(item, "title", None)) and _clean_alpha_text(getattr(item, "reason", None)))


def _alpha_missing_data(
    insights: StockInsightBundle,
    factor_lab: FactorLabReport | None | object = _NOT_REQUESTED,
    *,
    feature: FeatureSnapshot | None | object = _NOT_REQUESTED,
    market_regime: MarketRegimeReport | None | object = _NOT_REQUESTED,
    timeframe: TimeframeAlignmentReport | None | object = _NOT_REQUESTED,
    risk_reward: RiskRewardReport | None | object = _NOT_REQUESTED,
) -> list[str]:
    items = [
        *_missing_items(getattr(getattr(insights, "valuation", None), "missing_data", [])),
        *_missing_items(getattr(getattr(insights, "financial_health", None), "missing_data", [])),
        *_missing_items(getattr(getattr(insights, "lhb", None), "missing_data", [])),
        *_feature_missing_data(feature),
        *_factor_lab_missing_data(factor_lab),
        *_optional_report_missing_data(market_regime, MISSING_MARKET_REGIME),
        *_optional_report_missing_data(timeframe, MISSING_TIMEFRAME),
        *_optional_report_missing_data(risk_reward, MISSING_RISK_REWARD),
        *_rule_match_missing_data(insights),
    ]
    return _dedupe(items)[:MAX_MISSING_DATA_ITEMS]


def _missing_items(items: Iterable[object] | object) -> list[str]:
    return [text for item in _iter_items(items) if (text := _clean_alpha_text(item))]


def _feature_missing_data(feature: FeatureSnapshot | None | object) -> list[str]:
    if feature is _NOT_REQUESTED:
        return []
    if feature is None:
        return ["特征快照"]
    return [label for field, label in FEATURE_ALPHA_NUMERIC_FIELDS if finite_float(getattr(feature, field, None)) is None]


def _factor_lab_missing_data(factor_lab: FactorLabReport | None | object) -> list[str]:
    if factor_lab is _NOT_REQUESTED:
        return []
    if factor_lab is None:
        return [MISSING_FACTOR_LAB]
    return _factor_missing_data(factor_lab)


def _optional_report_missing_data(report: object, label: str) -> list[str]:
    if report is _NOT_REQUESTED or report is not None:
        return []
    return [label]


def _rule_match_missing_data(insights: StockInsightBundle) -> list[str]:
    matches = getattr(getattr(insights, "rule_matches", None), "matches", [])
    return [item for match in _iter_items(matches) for item in _missing_items(getattr(match, "missing_data", []))]


def _alpha_confidence(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    missing_data: list[str],
    factor_lab: FactorLabReport | None = None,
    market_regime: MarketRegimeReport | None = None,
    timeframe: TimeframeAlignmentReport | None = None,
    risk_reward: RiskRewardReport | None = None,
) -> int:
    raw_confidence = sum(
        adjustment.value
        for adjustment in _alpha_confidence_adjustments(
            analysis,
            insights,
            feature,
            missing_data,
            factor_lab,
            market_regime,
            timeframe,
            risk_reward,
        )
    )
    return _clamp(raw_confidence)


def _alpha_confidence_adjustments(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    missing_data: list[str],
    factor_lab: FactorLabReport | None = None,
    market_regime: MarketRegimeReport | None = None,
    timeframe: TimeframeAlignmentReport | None = None,
    risk_reward: RiskRewardReport | None = None,
) -> list[AlphaConfidenceAdjustment]:
    adjustments = [_base_confidence_adjustment(analysis, insights, feature, factor_lab)]
    if market_regime:
        if adjustment := _confidence_adjustment("market_regime", getattr(market_regime, "confidence_adjustment", None)):
            adjustments.append(adjustment)
    if timeframe_adjustment := _timeframe_confidence_adjustment(timeframe):
        adjustments.append(timeframe_adjustment)
    if risk_reward and getattr(risk_reward, "rating", None) in NEGATIVE_RISK_REWARD_RATINGS:
        adjustments.append(AlphaConfidenceAdjustment("risk_reward_penalty", RISK_REWARD_CONFIDENCE_PENALTY))
    if missing_data:
        adjustments.append(
            AlphaConfidenceAdjustment(
                "missing_data_penalty",
                -min(MAX_MISSING_DATA_PENALTY, len(missing_data) * MISSING_DATA_PENALTY_PER_ITEM),
            )
        )
    return adjustments


def _base_confidence_adjustment(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport | None = None,
) -> AlphaConfidenceAdjustment:
    if factor_lab:
        return AlphaConfidenceAdjustment(
            "base_with_factor_lab",
            _weighted_confidence(
                _base_confidence_values(analysis, insights, feature, factor_lab),
                FACTOR_LAB_BASE_WEIGHTS,
            ),
        )
    return AlphaConfidenceAdjustment(
        "base_without_factor_lab",
        _weighted_confidence(
            _base_confidence_values(analysis, insights, feature, factor_lab),
            NO_FACTOR_LAB_BASE_WEIGHTS,
        ),
    )


def _base_confidence_values(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport | None = None,
) -> dict[str, object]:
    return {
        "signal_confidence": getattr(getattr(analysis, "signal_snapshot", None), "confidence", None),
        "data_quality_score": getattr(getattr(analysis, "data_quality", None), "score", None),
        "leader_score": getattr(feature, "leader_score", None),
        "overview_score": getattr(getattr(insights, "overview", None), "total_score", None),
        "calibrated_confidence": getattr(factor_lab, "calibrated_confidence", None) if factor_lab else None,
    }


def _weighted_confidence(values: dict[str, object], weights: tuple[tuple[str, float], ...]) -> int:
    return round(sum(_bounded_score_value(values.get(name)) * weight for name, weight in weights))


def _bounded_score_value(value: object) -> float:
    parsed = finite_float(value)
    if parsed is None:
        return 0
    return max(0.0, min(100.0, parsed))


def _confidence_adjustment(name: str, value: object) -> AlphaConfidenceAdjustment | None:
    parsed = finite_float(value)
    if parsed is None:
        return None
    return AlphaConfidenceAdjustment(name, round(parsed))


def _timeframe_confidence_adjustment(timeframe: TimeframeAlignmentReport | None) -> AlphaConfidenceAdjustment | None:
    if not timeframe:
        return None
    conflict_level = getattr(timeframe, "conflict_level", None)
    if conflict_level in CONFLICT_TIMEFRAME_LEVELS:
        return AlphaConfidenceAdjustment("timeframe_conflict_penalty", TIMEFRAME_CONFLICT_PENALTY)
    if conflict_level == "多周期顺向":
        return AlphaConfidenceAdjustment("timeframe_alignment_bonus", TIMEFRAME_ALIGNMENT_BONUS)
    return None


def _alpha_verdict(
    feature: FeatureSnapshot,
    positives: list[AlphaEvidencePoint],
    negatives: list[AlphaEvidencePoint],
    market_regime: MarketRegimeReport | None = None,
    timeframe: TimeframeAlignmentReport | None = None,
    risk_reward: RiskRewardReport | None = None,
) -> str:
    context = _alpha_verdict_context(feature, positives, negatives, market_regime, timeframe, risk_reward)
    for rule in ALPHA_VERDICT_RULES:
        if rule.matches(context):
            return rule.verdict
    return "等待确认"


def _alpha_verdict_context(
    feature: FeatureSnapshot,
    positives: list[AlphaEvidencePoint],
    negatives: list[AlphaEvidencePoint],
    market_regime: MarketRegimeReport | None = None,
    timeframe: TimeframeAlignmentReport | None = None,
    risk_reward: RiskRewardReport | None = None,
) -> AlphaVerdictContext:
    return AlphaVerdictContext(
        data_quality_score=round(_bounded_score_value(getattr(feature, "data_quality_score", None))),
        positive_power=round(_alpha_positive_power(positives)),
        negative_power=round(_alpha_negative_power(negatives)),
        market_risk_multiplier=_positive_multiplier_or_none(getattr(market_regime, "risk_multiplier", None)) if market_regime else None,
        timeframe_conflict=getattr(timeframe, "conflict_level", None) if timeframe else None,
        risk_reward_rating=getattr(risk_reward, "rating", None) if risk_reward else None,
    )


def _low_data_quality(context: AlphaVerdictContext) -> bool:
    return context.data_quality_score < 50


def _blocking_timeframe_conflict(context: AlphaVerdictContext) -> bool:
    return context.timeframe_conflict in BLOCKING_TIMEFRAME_LEVELS


def _blocking_risk_reward(context: AlphaVerdictContext) -> bool:
    return context.risk_reward_rating in BLOCKING_RISK_REWARD_RATINGS


def _market_risk_suppresses(context: AlphaVerdictContext) -> bool:
    return bool(
        context.market_risk_multiplier is not None
        and context.market_risk_multiplier >= MARKET_RISK_SUPPRESSION_MULTIPLIER
        and context.negative_power >= context.positive_power * MARKET_RISK_NEGATIVE_POWER_RATIO
    )


def _alpha_positive_power(points: Iterable[AlphaEvidencePoint]) -> float:
    return sum(impact for item in points if (impact := _finite_impact(item)) is not None and impact > 0)


def _alpha_negative_power(points: Iterable[AlphaEvidencePoint]) -> float:
    return sum(abs(impact) for item in points if (impact := _finite_impact(item)) is not None and impact < 0)


def _positive_multiplier_or_none(value: object) -> float | None:
    parsed = finite_float(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _positive_evidence_dominates(context: AlphaVerdictContext) -> bool:
    return (
        context.positive_power >= context.negative_power + POSITIVE_DOMINANCE_MARGIN
        and context.positive_power >= POSITIVE_DOMINANCE_MIN_POWER
    )


def _negative_evidence_dominates(context: AlphaVerdictContext) -> bool:
    return context.negative_power > context.positive_power + NEGATIVE_DOMINANCE_MARGIN


ALPHA_VERDICT_RULES = (
    AlphaVerdictRule("low_data_quality", "暂停主动判断", _low_data_quality),
    AlphaVerdictRule("blocking_timeframe", "周期冲突", _blocking_timeframe_conflict),
    AlphaVerdictRule("blocking_risk_reward", "环境风险压制", _blocking_risk_reward),
    AlphaVerdictRule("market_risk_suppression", "环境风险压制", _market_risk_suppresses),
    AlphaVerdictRule("positive_evidence", "积极证据占优", _positive_evidence_dominates),
    AlphaVerdictRule("negative_evidence", "风险证据占优", _negative_evidence_dominates),
)


def _alpha_summary(
    positives: list[AlphaEvidencePoint],
    negatives: list[AlphaEvidencePoint],
    missing_data: list[str],
    confidence: int,
) -> str:
    top_positive = _first_alpha_title(positives, "暂无核心加分项")
    top_negative = _first_alpha_title(negatives, "暂无核心风险项")
    clean_missing_data = _dedupe(_missing_items(missing_data))
    missing_text = f"，但缺少{clean_missing_data[0]}等数据" if clean_missing_data else ""
    return f"核心加分来自「{top_positive}」，核心风险来自「{top_negative}」，Alpha证据充分度 {_clamp(confidence)}/100{missing_text}。"


def _first_alpha_title(points: list[AlphaEvidencePoint], fallback: str) -> str:
    return next((title for item in points if (title := _clean_alpha_text(getattr(item, "title", None)))), fallback)


def _alpha_data_quality_notes(analysis: AnalysisResult) -> list[str]:
    return _dedupe(_missing_items(getattr(getattr(analysis, "data_quality", None), "notes", [])))[:MAX_DATA_QUALITY_NOTES]


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        item = _clean_alpha_text(item)
        key = item.casefold()
        if not item or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _normalized_text(value: object) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _normalized_key(value: object) -> str:
    return _clean_alpha_text(value).casefold()


def _clean_alpha_text(value: object) -> str:
    text = _normalized_text(value)
    folded = text.casefold()
    if folded in INVALID_ALPHA_TEXT_VALUES:
        return ""
    return "" if INVALID_ALPHA_TEXT_TOKEN_RE.search(text) else text


def _iter_items(items: Iterable[object] | object) -> list[object]:
    if items is None:
        return []
    if isinstance(items, str):
        return [items]
    try:
        return list(items)
    except TypeError:
        return [items]
