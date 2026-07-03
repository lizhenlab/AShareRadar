from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from app.models.schemas import AnalysisResult, FeatureSnapshot, StockConceptItem, ThemeContextReport
from app.services.scoring import clamp_score
from app.utils.market_data import finite_float


@dataclass(frozen=True)
class ThemeStats:
    concepts: list[StockConceptItem]
    industry: str
    industry_change: float | None
    concept_avg: float | None
    strongest: StockConceptItem | None
    relative_to_industry: float | None
    relative_to_concepts: float | None


ThemeTextRule = Callable[[AnalysisResult, FeatureSnapshot, ThemeStats], str | None]

UNKNOWN_INDUSTRY = "行业待确认"
MAX_REPORT_CONCEPTS = 8
MAX_EVIDENCE_ITEMS = 6
MAX_ACTION_ITEMS = 4
MAX_MISSING_DATA_ITEMS = 5
POSITIVE_THEME_LEVELS = frozenset({"主题顺风", "主题配合"})
DEFAULT_OPPORTUNITY = "主题背景暂未给出额外加分，机会仍以个股买卖点和风险收益比为准。"
DEFAULT_RISK = "主题侧暂未发现明显拖累，仍需防范大盘和个股价位失效。"
MISSING_INDUSTRY_STRENGTH = "行业归属或行业涨跌强度"
MISSING_CONCEPT_COMPONENTS = "概念归属成分"


def build_theme_context_report(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    concepts: list[StockConceptItem] | None = None,
) -> ThemeContextReport:
    stats = _theme_stats(analysis, concepts or [])
    symbol = f"{analysis.quote.code}.{analysis.quote.market}"
    score = _theme_score(feature, stats)
    level = _theme_level(score, stats.industry_change, stats.concept_avg)
    style = _theme_style(analysis.quote.change_pct, stats.industry_change, stats.concept_avg)
    evidence = _theme_evidence(analysis, feature, stats)
    relative_strength = _theme_relative_strength(stats.relative_to_industry, stats.relative_to_concepts)
    opportunities = _theme_opportunities(analysis, feature, stats)
    risks = _theme_risks(analysis, feature, stats)
    missing_data = _theme_missing_data(feature, stats)

    return ThemeContextReport(
        symbol=symbol,
        updated_at=analysis.quote.timestamp,
        industry=stats.industry,
        industry_change_pct=stats.industry_change,
        concepts=stats.concepts[:MAX_REPORT_CONCEPTS],
        score=score,
        level=level,
        style=style,
        relative_strength=relative_strength,
        summary=_theme_summary(analysis, level, style, stats),
        evidence=evidence,
        opportunities=_dedupe_limited(opportunities, MAX_ACTION_ITEMS),
        risks=_dedupe_limited(risks, MAX_ACTION_ITEMS),
        missing_data=_dedupe_limited(missing_data, MAX_MISSING_DATA_ITEMS),
    )


def _theme_stats(analysis: AnalysisResult, concepts: list[StockConceptItem]) -> ThemeStats:
    unique_concepts = _unique_concepts(concepts)
    industry = _industry_name(analysis)
    industry_change = _finite_or_none(getattr(analysis.industry_context, "change_pct", None)) if analysis.industry_context else None
    concept_avg = sum(item.change_pct for item in unique_concepts) / len(unique_concepts) if unique_concepts else None
    strongest = max(unique_concepts, key=lambda item: item.change_pct, default=None)
    stock_change = _finite_or_zero(getattr(analysis.quote, "change_pct", None))
    return ThemeStats(
        concepts=unique_concepts,
        industry=industry,
        industry_change=industry_change,
        concept_avg=concept_avg,
        strongest=strongest,
        relative_to_industry=stock_change - industry_change if industry_change is not None else None,
        relative_to_concepts=stock_change - concept_avg if concept_avg is not None else None,
    )


def _unique_concepts(concepts: list[StockConceptItem]) -> list[StockConceptItem]:
    cleaned = [item for item in (_clean_concept(item) for item in concepts) if item is not None]
    ranked = sorted(cleaned, key=lambda item: (_concept_rank(item), -item.change_pct))
    by_name: dict[str, StockConceptItem] = {}
    for item in ranked:
        key = _normalized_key(item.name)
        if key not in by_name:
            by_name[key] = item
    return list(by_name.values())


def _clean_concept(item: StockConceptItem) -> StockConceptItem | None:
    name = _clean_text(getattr(item, "name", None))
    change_pct = _finite_or_none(getattr(item, "change_pct", None))
    if not name or change_pct is None:
        return None
    return item.model_copy(update={"name": name, "change_pct": change_pct})


def _concept_rank(item: StockConceptItem) -> int:
    rank = finite_float(getattr(item, "rank", None))
    return int(rank) if rank is not None and rank > 0 else 9999


def _industry_name(analysis: AnalysisResult) -> str:
    profile_industry = _clean_text(getattr(getattr(analysis, "stock_profile", None), "industry", None))
    if profile_industry:
        return profile_industry
    context_name = _clean_text(getattr(getattr(analysis, "industry_context", None), "name", None))
    if context_name:
        return context_name
    return UNKNOWN_INDUSTRY


def _theme_score(feature: FeatureSnapshot, stats: ThemeStats) -> int:
    score = 45 + sum(
        item
        for item in (
            round(stats.industry_change * 6) if stats.industry_change is not None else 0,
            round(stats.concept_avg * 8) if stats.concept_avg is not None else 0,
            round(stats.relative_to_industry * 4) if stats.relative_to_industry is not None else 0,
            round(stats.relative_to_concepts * 4) if stats.relative_to_concepts is not None else 0,
            round((_finite_or_zero(getattr(feature, "trend_score", None)) - 50) * 0.25),
            -8 if _finite_or_zero(getattr(feature, "data_quality_score", None)) < 70 else 0,
        )
    )
    return clamp_score(score)


def _theme_level(score: int, industry_change: float | None, concept_avg: float | None) -> str:
    if industry_change is None and concept_avg is None:
        return "主题待确认"
    if score >= 72:
        return "主题顺风"
    if score >= 58:
        return "主题配合"
    if score <= 38:
        return "主题逆风"
    return "主题中性"


def _theme_style(stock_change: float, industry_change: float | None, concept_avg: float | None) -> str:
    stock_change = _finite_or_zero(stock_change)
    background = _theme_background(industry_change, concept_avg)
    if background is None:
        return "背景不足"
    for rule in THEME_STYLE_RULES:
        label = rule(stock_change, background)
        if label:
            return label
    return "主题震荡"


def _theme_background(industry_change: float | None, concept_avg: float | None) -> float | None:
    values = _available_values(industry_change, concept_avg)
    return max(values) if values else None


def _style_stock_stronger(stock_change: float, background: float) -> str | None:
    return "个股强于主题" if background >= 1 and stock_change >= background else None


def _style_hot_theme_weak_stock(stock_change: float, background: float) -> str | None:
    return "主题热个股弱" if background >= 1 and stock_change < background - 1 else None


def _style_resilient_against_headwind(stock_change: float, background: float) -> str | None:
    return "逆风抗跌" if background <= -1 and stock_change > background + 1 else None


def _style_theme_drag(_: float, background: float) -> str | None:
    return "主题拖累" if background <= -1 else None


THEME_STYLE_RULES = (
    _style_stock_stronger,
    _style_hot_theme_weak_stock,
    _style_resilient_against_headwind,
    _style_theme_drag,
)


def _theme_relative_strength(relative_to_industry: float | None, relative_to_concepts: float | None) -> str:
    values = _available_values(relative_to_industry, relative_to_concepts)
    if not values:
        return "强弱待确认"
    avg_gap = sum(values) / len(values)
    if avg_gap >= 2:
        return "显著强于背景"
    if avg_gap >= 0.8:
        return "强于背景"
    if avg_gap <= -2:
        return "显著弱于背景"
    if avg_gap <= -0.8:
        return "弱于背景"
    return "与背景同步"


def _theme_summary(
    analysis: AnalysisResult,
    level: str,
    style: str,
    stats: ThemeStats,
) -> str:
    parts = [f"{analysis.quote.name}当前属于「{style}」。"]
    if stats.industry_change is not None:
        parts.append(f"行业「{stats.industry}」涨跌幅 {stats.industry_change:.2f}%。")
    if stats.concept_avg is not None:
        concept_text = f"，最强概念为「{stats.strongest.name}」{stats.strongest.change_pct:.2f}%" if stats.strongest else ""
        parts.append(f"概念平均涨跌幅 {stats.concept_avg:.2f}%{concept_text}。")
    if level in POSITIVE_THEME_LEVELS:
        parts.append("结论上可以提高趋势信号的解释权重，但买卖点仍要服从个股价位和风控。")
    elif level == "主题逆风":
        parts.append("结论上需要降低追涨冲动，优先观察个股能否持续强于背景。")
    else:
        parts.append("结论上主题背景暂不提供强支撑，仍以个股趋势、量能和风险收益比为主。")
    return "".join(parts)


def _theme_evidence(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    stats: ThemeStats,
) -> list[str]:
    return _collect_rule_messages(THEME_EVIDENCE_RULES, analysis, feature, stats, limit=MAX_EVIDENCE_ITEMS)


def _theme_base_evidence(analysis: AnalysisResult, feature: FeatureSnapshot, _: ThemeStats) -> str:
    return (
        f"个股涨跌幅 {_finite_or_zero(getattr(analysis.quote, 'change_pct', None)):.2f}%，"
        f"趋势评分 {clamp_score(getattr(feature, 'trend_score', None))}，"
        f"龙头评分 {clamp_score(getattr(feature, 'leader_score', None))}。"
    )


def _theme_industry_evidence(analysis: AnalysisResult, _: FeatureSnapshot, stats: ThemeStats) -> str | None:
    if stats.industry == UNKNOWN_INDUSTRY or stats.industry_change is None:
        return None
    industry_name = _clean_text(getattr(getattr(analysis, "industry_context", None), "name", None)) or stats.industry
    leading_stock = _clean_text(getattr(getattr(analysis, "industry_context", None), "leading_stock", None)) or "待确认"
    return f"行业「{industry_name}」涨跌幅 {stats.industry_change:.2f}%，领涨股为{leading_stock}。"


def _theme_concepts_evidence(_: AnalysisResult, __: FeatureSnapshot, stats: ThemeStats) -> str | None:
    if not stats.concepts:
        return None
    top = sorted(stats.concepts, key=lambda item: item.change_pct, reverse=True)[:3]
    return "相关概念：" + "、".join(f"{item.name}{item.change_pct:.2f}%" for item in top) + "。"


def _theme_concept_average_evidence(_: AnalysisResult, __: FeatureSnapshot, stats: ThemeStats) -> str | None:
    if stats.concept_avg is None:
        return None
    return f"概念平均涨跌幅 {stats.concept_avg:.2f}%，用于判断主题是否配合个股走势。"


def _theme_relative_industry_evidence(_: AnalysisResult, __: FeatureSnapshot, stats: ThemeStats) -> str | None:
    if stats.relative_to_industry is None:
        return None
    return f"个股相对行业强弱差 {stats.relative_to_industry:.2f} 个百分点。"


def _theme_relative_concepts_evidence(_: AnalysisResult, __: FeatureSnapshot, stats: ThemeStats) -> str | None:
    if stats.relative_to_concepts is None:
        return None
    return f"个股相对概念均值强弱差 {stats.relative_to_concepts:.2f} 个百分点。"


THEME_EVIDENCE_RULES: tuple[ThemeTextRule, ...] = (
    _theme_base_evidence,
    _theme_industry_evidence,
    _theme_concepts_evidence,
    _theme_concept_average_evidence,
    _theme_relative_industry_evidence,
    _theme_relative_concepts_evidence,
)


def _theme_opportunities(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    stats: ThemeStats,
) -> list[str]:
    result = _collect_rule_messages(THEME_OPPORTUNITY_RULES, analysis, feature, stats)
    return result or [DEFAULT_OPPORTUNITY]


def _opportunity_stock_beats_industry(_: AnalysisResult, __: FeatureSnapshot, stats: ThemeStats) -> str | None:
    return "个股强于行业背景，说明资金认可度可能高于同板块平均。" if stats.relative_to_industry is not None and stats.relative_to_industry >= 1 else None


def _opportunity_stock_beats_concepts(_: AnalysisResult, __: FeatureSnapshot, stats: ThemeStats) -> str | None:
    return "个股强于相关概念均值，可作为龙头候选的辅助证据。" if stats.relative_to_concepts is not None and stats.relative_to_concepts >= 1 else None


def _opportunity_active_concept(_: AnalysisResult, __: FeatureSnapshot, stats: ThemeStats) -> str | None:
    if stats.strongest and stats.strongest.change_pct >= 1.5:
        return f"「{stats.strongest.name}」概念表现活跃，若个股同步放量，短线弹性更容易被市场理解。"
    return None


def _opportunity_positive_trend(analysis: AnalysisResult, feature: FeatureSnapshot, _: ThemeStats) -> str | None:
    return (
        "趋势和主题背景同时偏正时，买点更适合等待回踩承接而不是追高。"
        if clamp_score(getattr(feature, "trend_score", None)) >= 65
        and _finite_or_zero(getattr(analysis.quote, "change_pct", None)) > 0
        else None
    )


THEME_OPPORTUNITY_RULES: tuple[ThemeTextRule, ...] = (
    _opportunity_stock_beats_industry,
    _opportunity_stock_beats_concepts,
    _opportunity_active_concept,
    _opportunity_positive_trend,
)


def _theme_risks(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    stats: ThemeStats,
) -> list[str]:
    risks = _collect_rule_messages(THEME_RISK_RULES, analysis, feature, stats)
    return risks or [DEFAULT_RISK]


def _risk_hot_concept_weak_stock(_: AnalysisResult, __: FeatureSnapshot, stats: ThemeStats) -> str | None:
    if stats.concept_avg is not None and stats.concept_avg >= 1 and stats.relative_to_concepts is not None and stats.relative_to_concepts <= -1:
        return "概念热但个股弱，容易出现跟风不足或冲高回落。"
    return None


def _risk_industry_headwind(analysis: AnalysisResult, _: FeatureSnapshot, stats: ThemeStats) -> str | None:
    if stats.industry_change is not None and stats.industry_change <= -1 and _finite_or_zero(getattr(analysis.quote, "change_pct", None)) < 0:
        return "行业逆风且个股同步走弱，短线需要降低信号权重。"
    return None


def _risk_low_leadership(_: AnalysisResult, feature: FeatureSnapshot, stats: ThemeStats) -> str | None:
    return "概念归属存在，但龙头强度不足，暂不宜把题材当作核心买入理由。" if clamp_score(getattr(feature, "leader_score", None)) < 50 and stats.concepts else None


def _risk_missing_concepts(_: AnalysisResult, __: FeatureSnapshot, stats: ThemeStats) -> str | None:
    return "概念成分暂未确认，主题判断只能按行业和个股走势保守解释。" if not stats.concepts else None


def _risk_low_quality(_: AnalysisResult, feature: FeatureSnapshot, __: ThemeStats) -> str | None:
    return "数据质量不足，行业概念结论需要降权。" if _finite_or_zero(getattr(feature, "data_quality_score", None)) < 70 else None


THEME_RISK_RULES: tuple[ThemeTextRule, ...] = (
    _risk_hot_concept_weak_stock,
    _risk_industry_headwind,
    _risk_low_leadership,
    _risk_missing_concepts,
    _risk_low_quality,
)


def _theme_missing_data(feature: FeatureSnapshot, stats: ThemeStats) -> list[str]:
    missing_data = []
    if stats.industry == UNKNOWN_INDUSTRY or stats.industry_change is None:
        missing_data.append(MISSING_INDUSTRY_STRENGTH)
    if not stats.concepts:
        missing_data.append(MISSING_CONCEPT_COMPONENTS)
    if _finite_or_zero(getattr(feature, "data_quality_score", None)) < 80:
        missing_data.append(f"数据质量{_clean_text(getattr(feature, 'data_quality_level', None)) or '待确认'}")
    return missing_data


def _available_values(*values: float | None) -> list[float]:
    return [value for value in (_finite_or_none(value) for value in values) if value is not None]


def _collect_rule_messages(
    rules: Iterable[ThemeTextRule],
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    stats: ThemeStats,
    *,
    limit: int | None = None,
) -> list[str]:
    messages = [message for rule in rules if (message := rule(analysis, feature, stats))]
    return messages[:limit] if limit is not None else messages


def _dedupe_limited(items: Iterable[str], limit: int) -> list[str]:
    return _dedupe(items)[:limit]


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _finite_or_none(value: object) -> float | None:
    return finite_float(value)


def _finite_or_zero(value: object) -> float:
    parsed = finite_float(value)
    return parsed if parsed is not None else 0.0


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _normalized_key(value: object) -> str:
    return _clean_text(value).casefold()
