from __future__ import annotations

from dataclasses import dataclass

from app.models.schemas import ActionAdvice, DataQuality, PlateItem, Quote, StockInfo
from app.services.analysis_signal_quality import quality_reason


WEAK_RISK_LEVELS = {"中等风险", "高风险"}
STRONG_RISK_LEVELS = {"低风险", "可控观察"}


@dataclass(frozen=True)
class AdviceContext:
    quote: Quote
    score: int
    risk_level: str
    support: float
    resistance: float
    quality: DataQuality | None = None


def beginner_summary(
    quote: Quote,
    score: int,
    label: str,
    risk_level: str,
    support: float,
    resistance: float,
    stock_profile: StockInfo | None,
    industry_context: PlateItem | None,
    action_advice: ActionAdvice,
) -> str:
    industry = ""
    if stock_profile and stock_profile.industry:
        industry = f" 所属行业为「{stock_profile.industry}」。"
    if industry_context:
        industry += f" 行业背景参考「{industry_context.name}」当前涨跌幅 {industry_context.change_pct:.2f}%。"
    return (
        f"{quote.name} 当前属于「{label}」，趋势分 {score}/100，风险级别为「{risk_level}」。"
        f"{industry} 新手可记住两个价位：支撑 {support:.2f}，压力 {resistance:.2f}；"
        f"当前建议「{action_advice.action}」，理由是{action_advice.reason}"
    )


def action_advice(
    quote: Quote,
    score: int,
    risk_level: str,
    support: float,
    resistance: float,
    quality: DataQuality | None = None,
) -> ActionAdvice:
    context = AdviceContext(
        quote=quote,
        score=score,
        risk_level=risk_level,
        support=support,
        resistance=resistance,
        quality=quality,
    )
    return _first_matching_advice(
        context,
        (
            _low_quality_blocks_advice,
            _low_quality_observation_advice,
            _strong_trend_advice,
            _risk_control_advice,
            _hold_observation_advice,
        ),
    ) or _waiting_advice()


def _first_matching_advice(context: AdviceContext, rules) -> ActionAdvice | None:
    for rule in rules:
        advice = rule(context)
        if advice is not None:
            return advice
    return None


def _low_quality_blocks_advice(context: AdviceContext) -> ActionAdvice | None:
    quality = context.quality
    if not quality or quality.score >= 50:
        return None
    return ActionAdvice(
        action="控制风险",
        confidence=max(35, quality.score),
        reason=f"当前数据质量为{quality.level}，存在{quality_reason(quality)}，先暂停新增买点和做T，按控制风险口径等待行情重新确认。",
    )


def _low_quality_observation_advice(context: AdviceContext) -> ActionAdvice | None:
    quality = context.quality
    if not quality or quality.score >= 70:
        return None
    if _weak_trend_or_risk(context):
        return ActionAdvice(
            action="控制风险",
            confidence=min(70, max(55, 100 - context.score)),
            reason=f"趋势偏弱且数据质量只有{quality.level}，先按低置信风控处理；等新行情确认后再考虑买点或做T。",
        )
    return ActionAdvice(
        action="轻仓观察",
        confidence=min(58, max(42, quality.score)),
        reason=f"数据质量只有{quality.level}，建议先观察支撑压力是否被新数据确认。",
    )


def _strong_trend_advice(context: AdviceContext) -> ActionAdvice | None:
    if context.score < 78 or context.risk_level not in STRONG_RISK_LEVELS:
        return None
    return ActionAdvice(
        action="回踩关注",
        confidence=min(90, context.score),
        reason="趋势结构较强，但仍建议等回踩或突破确认后分批处理。",
    )


def _hold_observation_advice(context: AdviceContext) -> ActionAdvice | None:
    if context.score < 58 or context.quote.price <= context.support:
        return None
    return ActionAdvice(
        action="持有观察",
        confidence=context.score,
        reason="趋势没有明显破坏，重点跟踪支撑位和量能变化。",
    )


def _risk_control_advice(context: AdviceContext) -> ActionAdvice | None:
    if not _weak_trend_or_risk(context):
        return None
    return ActionAdvice(
        action="控制风险",
        confidence=max(55, 100 - context.score),
        reason="趋势偏弱或风险信号较多，优先看支撑是否有效，避免盲目加仓。",
    )


def _waiting_advice() -> ActionAdvice:
    return ActionAdvice(
        action="等待信号",
        confidence=55,
        reason="价格位置和趋势强度暂未形成清晰共振。",
    )


def _weak_trend_or_risk(context: AdviceContext) -> bool:
    return context.score <= 42 or context.risk_level in WEAK_RISK_LEVELS


__all__ = ["beginner_summary", "action_advice"]
