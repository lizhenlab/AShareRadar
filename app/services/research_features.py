from __future__ import annotations

from dataclasses import dataclass

from app.models.schemas import AnalysisResult, FeatureSnapshot, LeadershipReport, StockConceptItem, StockInsightBundle
from app.services.indicators import average_true_range, daily_return_volatility, recent_volume_ratio
from app.services.leader_scoring import (
    FEATURE_LEADER_PROFILE,
    FEATURE_TAG_RULES,
    LeaderScoreInput,
    leader_score,
    leader_tags,
)
from app.services.scoring import clamp_score, score_level
from app.utils.market_data import finite_float

HOT_CONCEPT_LIMIT = 2
LEADERSHIP_TAG_LIMIT = 8
LEADERSHIP_DATA_QUALITY_DOWNGRADE_THRESHOLD = 70
MISSING_SCORE_DEFAULT = 0
MISSING_TEXT = "待确认"
LEADERSHIP_SUMMARY_RULES = (
    (70, "具备龙头候选特征"),
    (55, "属于强势观察个股"),
)
LEADERSHIP_DEFAULT_SUMMARY = "暂不具备龙头特征"
LEADERSHIP_MISSING_RULES = (
    ("龙虎榜席位", lambda analysis, insights, concepts: not insights.lhb.available),
    ("逐笔大单资金流", lambda analysis, insights, concepts: not insights.fund_flow.available),
    ("公司画像", lambda analysis, insights, concepts: analysis.stock_profile is None),
    ("行业强度排名", lambda analysis, insights, concepts: not analysis.industry_context),
    ("概念归属", lambda analysis, insights, concepts: not concepts),
)


@dataclass(frozen=True)
class FeatureMetrics:
    volume_ratio: float
    atr14: float
    atr_pct: float
    volatility_pct: float
    valuation_score: int
    financial_score: int
    fund_flow_score: int
    leader_score: int
    tags: list[str]
    notes: list[str]


def build_feature_snapshot(analysis: AnalysisResult, insights: StockInsightBundle) -> FeatureSnapshot:
    quote = analysis.quote
    metrics = _feature_metrics(analysis, insights)
    industry = analysis.industry_context
    price = _positive_or_zero(quote.price)
    change_pct = finite_float(quote.change_pct) or 0
    trend_score = _score_or_zero(analysis.trend_score)
    signal_confidence = _score_or_zero(analysis.signal_snapshot.confidence)
    data_quality_score = _score_or_zero(analysis.data_quality.score)
    return FeatureSnapshot(
        symbol=f"{quote.code}.{quote.market}",
        updated_at=quote.timestamp,
        price=price,
        change_pct=change_pct,
        trend_score=trend_score,
        trend_label=_safe_trend_label(analysis.trend_score, analysis.trend_label),
        signal_confidence=signal_confidence,
        data_quality_score=data_quality_score,
        data_quality_level=_safe_quality_level(analysis.data_quality.score, analysis.data_quality.level),
        leader_score=metrics.leader_score,
        leader_level=score_level(metrics.leader_score),
        support=_non_negative_or_zero(analysis.support),
        resistance=_non_negative_or_zero(analysis.resistance),
        ma5=_non_negative_or_zero(analysis.ma5),
        ma10=_non_negative_or_zero(analysis.ma10),
        ma20=_non_negative_or_zero(analysis.ma20),
        volume_ratio=metrics.volume_ratio,
        atr14=round(metrics.atr14, 2),
        atr_pct=round(metrics.atr_pct, 2),
        volatility_pct=round(metrics.volatility_pct, 2),
        turnover_rate=_optional_non_negative(quote.turnover_rate),
        amount=_optional_non_negative(quote.amount),
        valuation_score=metrics.valuation_score,
        financial_score=metrics.financial_score,
        fund_flow_score=metrics.fund_flow_score,
        order_pressure=_non_empty_text(insights.order_pressure.pressure_level) or "--",
        industry_name=_non_empty_text(industry.name) if industry else None,
        industry_change_pct=finite_float(industry.change_pct) if industry else None,
        tags=metrics.tags,
        notes=metrics.notes,
    )


def _feature_metrics(analysis: AnalysisResult, insights: StockInsightBundle) -> FeatureMetrics:
    quote = analysis.quote
    volume_ratio = _non_negative_or_zero(recent_volume_ratio(analysis.klines))
    atr14 = _non_negative_or_zero(average_true_range(analysis.klines, 14))
    price = _positive_or_zero(quote.price)
    atr_pct = _non_negative_or_zero(atr14 / price * 100 if price > 0 else 0)
    volatility_pct = _non_negative_or_zero(daily_return_volatility(analysis.klines, 20))
    leader_inputs = _feature_leader_inputs(analysis, insights, volume_ratio)
    leader_score_value = _leader_score(leader_inputs)
    return FeatureMetrics(
        volume_ratio=volume_ratio,
        atr14=atr14,
        atr_pct=atr_pct,
        volatility_pct=volatility_pct,
        valuation_score=_score_or_zero(insights.valuation.score),
        financial_score=_score_or_zero(insights.financial_health.score),
        fund_flow_score=_score_or_zero(insights.fund_flow.overall_score),
        leader_score=leader_score_value,
        tags=_feature_tags(leader_inputs, leader_score_value),
        notes=_feature_notes(analysis, insights),
    )


def _positive_or_zero(value: float | None) -> float:
    parsed = finite_float(value)
    return parsed if parsed is not None and parsed > 0 else 0


def _optional_non_negative(value: float | None) -> float | None:
    parsed = finite_float(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _non_negative_or_zero(value: float | None) -> float:
    parsed = _optional_non_negative(value)
    return parsed if parsed is not None else 0


def _score_or_zero(value: object) -> int:
    return clamp_score(value, default=MISSING_SCORE_DEFAULT)


def _optional_score(value: object) -> int | None:
    return clamp_score(value) if finite_float(value) is not None else None


def _non_empty_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _safe_trend_label(score: object, label: object) -> str:
    return (_non_empty_text(label) or "数据不足") if finite_float(score) is not None else "数据不足"


def _safe_quality_level(score: object, level: object) -> str:
    return (_non_empty_text(level) or MISSING_TEXT) if finite_float(score) is not None else MISSING_TEXT


def _feature_notes(analysis: AnalysisResult, insights: StockInsightBundle) -> list[str]:
    signal_confidence = _score_or_zero(analysis.signal_snapshot.confidence)
    data_quality_score = _score_or_zero(analysis.data_quality.score)
    data_quality_level = _safe_quality_level(analysis.data_quality.score, analysis.data_quality.level)
    trend_label = _safe_trend_label(analysis.trend_score, analysis.trend_label)
    notes = [
        f"信号可信度 {signal_confidence}%，数据质量 {data_quality_level} {data_quality_score} 分。",
        f"趋势 {trend_label}，资金面 {insights.fund_flow.level}，估值 {insights.valuation.level}。",
    ]
    if insights.lhb.missing_data:
        notes.append("龙虎榜席位、公告和逐笔资金仍是后续精确化重点。")
    return notes


def build_leadership_report(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    concepts: list[StockConceptItem] | None = None,
    *,
    concept_error: str | None = None,
) -> LeadershipReport:
    concepts = concepts or []
    feature = _safe_report_feature(feature)
    return LeadershipReport(
        symbol=feature.symbol,
        updated_at=feature.updated_at,
        score=feature.leader_score,
        level=score_level(feature.leader_score),
        summary=_leadership_summary(feature),
        tags=feature.tags[:LEADERSHIP_TAG_LIMIT],
        evidence=_leadership_evidence(feature, concepts),
        missing_data=_leadership_missing_data(analysis, insights, concepts, concept_error),
    )


def _leadership_evidence(feature: FeatureSnapshot, concepts: list[StockConceptItem]) -> list[str]:
    optional_evidence = [
        _industry_evidence(feature),
        _concept_background_evidence(concepts),
    ]
    return [
        f"趋势评分 {feature.trend_score}，涨跌幅 {feature.change_pct:.2f}%。",
        _liquidity_evidence(feature),
        f"资金评分 {feature.fund_flow_score}，盘口状态：{feature.order_pressure}。",
        *(item for item in optional_evidence if item),
    ]


def _liquidity_evidence(feature: FeatureSnapshot) -> str:
    if feature.amount:
        return f"成交额 {feature.amount / 100000000:.1f} 亿，量能比 {feature.volume_ratio:.2f}。"
    return f"量能比 {feature.volume_ratio:.2f}。"


def _industry_evidence(feature: FeatureSnapshot) -> str | None:
    if not feature.industry_name or feature.industry_change_pct is None:
        return None
    return f"行业 {feature.industry_name} 涨跌幅 {feature.industry_change_pct:.2f}%。"


def _concept_background_evidence(concepts: list[StockConceptItem]) -> str | None:
    hot_concepts = _hot_concepts(concepts)
    if not hot_concepts:
        return None
    return "概念背景：" + "、".join(f"{name}{change_pct:.2f}%" for name, change_pct in hot_concepts) + "。"


def _hot_concepts(concepts: list[StockConceptItem], limit: int = HOT_CONCEPT_LIMIT) -> list[tuple[str, float]]:
    if limit <= 0:
        return []
    scored = []
    for index, item in enumerate(concepts):
        name = _non_empty_text(getattr(item, "name", None))
        change_pct = finite_float(getattr(item, "change_pct", None))
        if not name or change_pct is None:
            continue
        rank = _non_negative_or_zero(getattr(item, "rank", index + 1))
        scored.append((name, change_pct, rank, index))
    return [
        (name, change_pct)
        for name, change_pct, _rank, _index in sorted(scored, key=lambda item: (-item[1], item[2], item[3]))[:limit]
    ]


def _leadership_missing_data(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    concepts: list[StockConceptItem],
    concept_error: str | None,
) -> list[str]:
    missing = [
        label
        for label, is_missing in LEADERSHIP_MISSING_RULES
        if is_missing(analysis, insights, concepts)
    ]
    reason = _non_empty_text(concept_error)
    if reason and "概念归属" in missing:
        missing[missing.index("概念归属")] = f"概念归属：{reason}"
    return missing


def _leadership_summary(feature: FeatureSnapshot) -> str:
    summary = _leadership_base_summary(feature.leader_score)
    if feature.data_quality_score < LEADERSHIP_DATA_QUALITY_DOWNGRADE_THRESHOLD:
        return f"数据质量{feature.data_quality_level}，{summary}需要降权。"
    return summary


def _leadership_base_summary(score: int) -> str:
    for minimum_score, summary in LEADERSHIP_SUMMARY_RULES:
        if score >= minimum_score:
            return summary
    return LEADERSHIP_DEFAULT_SUMMARY


def _leader_score(inputs: LeaderScoreInput) -> int:
    return leader_score(inputs, FEATURE_LEADER_PROFILE)


def _feature_tags(inputs: LeaderScoreInput, leader_score_value: int) -> list[str]:
    return leader_tags(inputs, leader_score_value, FEATURE_TAG_RULES, "常规观察")


def _feature_leader_inputs(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    volume_ratio: float,
) -> LeaderScoreInput:
    return LeaderScoreInput(
        trend_score=_score_or_zero(analysis.trend_score),
        change_pct=finite_float(analysis.quote.change_pct) or 0,
        volume_ratio=_non_negative_or_zero(volume_ratio),
        amount=_non_negative_or_zero(analysis.quote.amount),
        turnover_rate=_optional_non_negative(analysis.quote.turnover_rate),
        fund_flow_score=_optional_score(insights.fund_flow.overall_score),
        industry_change_pct=finite_float(analysis.industry_context.change_pct) if analysis.industry_context else None,
        abnormal_level=insights.abnormal_events.level,
        data_quality_score=_score_or_zero(analysis.data_quality.score),
    )


def _safe_report_feature(feature: FeatureSnapshot) -> FeatureSnapshot:
    leader_score_value = _score_or_zero(feature.leader_score)
    data_quality_score = _score_or_zero(feature.data_quality_score)
    return feature.model_copy(
        update={
            "price": _positive_or_zero(feature.price),
            "change_pct": finite_float(feature.change_pct) or 0,
            "trend_score": _score_or_zero(feature.trend_score),
            "trend_label": _safe_trend_label(feature.trend_score, feature.trend_label),
            "signal_confidence": _score_or_zero(feature.signal_confidence),
            "data_quality_score": data_quality_score,
            "data_quality_level": _safe_quality_level(feature.data_quality_score, feature.data_quality_level),
            "leader_score": leader_score_value,
            "leader_level": score_level(leader_score_value),
            "support": _non_negative_or_zero(feature.support),
            "resistance": _non_negative_or_zero(feature.resistance),
            "ma5": _non_negative_or_zero(feature.ma5),
            "ma10": _non_negative_or_zero(feature.ma10),
            "ma20": _non_negative_or_zero(feature.ma20),
            "volume_ratio": _non_negative_or_zero(feature.volume_ratio),
            "atr14": _non_negative_or_zero(feature.atr14),
            "atr_pct": _non_negative_or_zero(feature.atr_pct),
            "volatility_pct": _non_negative_or_zero(feature.volatility_pct),
            "turnover_rate": _optional_non_negative(feature.turnover_rate),
            "amount": _optional_non_negative(feature.amount),
            "valuation_score": _score_or_zero(feature.valuation_score),
            "financial_score": _score_or_zero(feature.financial_score),
            "fund_flow_score": _score_or_zero(feature.fund_flow_score),
            "order_pressure": _non_empty_text(feature.order_pressure) or "--",
            "industry_name": _non_empty_text(feature.industry_name),
            "industry_change_pct": finite_float(feature.industry_change_pct),
            "tags": _clean_tags(feature.tags),
        }
    )


def _clean_tags(tags: list[str] | object) -> list[str]:
    if not tags:
        return []
    try:
        values = list(tags)
    except TypeError:
        return []
    return [text for item in values if (text := _non_empty_text(item))]
