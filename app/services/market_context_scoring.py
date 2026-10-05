"""Bounded, sign-preserving market/industry context for current stock research."""

from __future__ import annotations

from datetime import datetime
import re

from app.models.market import PlateItem, Quote
from app.models.market_context import ContextObservation, MarketContextScore
from app.services.data_quality_components import is_demo_quote_source
from app.services.eastmoney_client import EASTMONEY_BRIDGE_SOURCE_NAME
from app.services.scoring import clamp_score
from app.utils.audit_time import audit_datetime_to_text, parse_audit_time
from app.utils.clock import ASHARE_TIMEZONE
from app.utils.market_data import finite_float


MARKET_CONTEXT_INDEX = "000300.SH"
RELATIVE_STRENGTH_WEIGHT = 0.2
RELATIVE_POINTS_PER_PCT = 5.0
MAX_CONTEXT_EVENT_LAG_SECONDS = 300


def build_market_context_score(
    base_score: int, stock: Quote, *, market: Quote | None, industry: PlateItem | None,
    industry_name: str | None, evaluated_at: str,
    reliability_score: int = 100,
) -> MarketContextScore:
    base = clamp_score(base_score)
    cutoff, stock_time = _full_time(evaluated_at), _full_time(stock.timestamp)
    reasons: list[str] = []
    usable = cutoff is not None and stock_time is not None and stock_time <= cutoff and _usable_quote(stock)
    market_obs = _market_observation(market, stock_time, cutoff) if usable else None
    industry_obs = _industry_observation(industry, industry_name, stock_time, cutoff) if usable else None
    if industry_obs is not None and not _finite_relative_returns(stock, market_obs, industry_obs):
        industry_obs = None
        reasons.append("行业相对涨跌超出可计算的有限数值范围；未作行业及个股相对强弱修正。")
    if market_obs is None:
        reasons.append("沪深300行情缺失，或身份、来源、交易日、事件时间未对齐；未作大盘修正。")
    if industry_obs is None:
        reasons.append("主行业指数缺少可验证的代码、行情时间或唯一归属；未作行业及个股相对强弱修正。")
    return _context_result(base, stock, evaluated_at, market_obs, industry_obs, reasons, clamp_score(reliability_score))


def _context_result(
    base: int, stock: Quote, evaluated_at: str, market: ContextObservation | None,
    industry: ContextObservation | None, reasons: list[str], reliability: int,
) -> MarketContextScore:
    stock_excess = stock.change_pct - industry.change_pct if industry else None
    industry_excess = industry.change_pct - market.change_pct if industry and market else None
    relative = 50 + max(-50, min(50, stock_excess * RELATIVE_POINTS_PER_PCT)) if stock_excess is not None else None
    before = _relative_context_score(base, relative)
    market_gate = _positive_gate(market.change_pct, 0.5) if market else 1.0
    industry_gate = _positive_gate(industry_excess, 0.7) if industry_excess is not None else 1.0
    deviation = before - 50
    contextual = 50 + min(0, deviation) + max(0, deviation) * market_gate * industry_gate
    raw = 50 + (contextual - 50) * reliability / 100
    return MarketContextScore(
        rule_version="current-market-context.v2",
        symbol=f"{stock.code}.{stock.market}",
        base_score=base, reliability_score=reliability, score=clamp_score(raw, round_value=True), raw_score=round(raw, 6),
        evaluated_at=evaluated_at, stock_event_at=stock.timestamp, market=market, industry=industry,
        stock_excess_pct=stock_excess, industry_excess_pct=industry_excess,
        relative_strength_score=relative, relative_weight=RELATIVE_STRENGTH_WEIGHT if industry else 0,
        relative_adjustment=round(before - base, 6),
        before_gates_score=round(before, 6), pre_reliability_score=round(contextual, 6),
        market_multiplier=market_gate, industry_multiplier=industry_gate,
        unavailable_reasons=reasons,
        note="当日相对强弱修正：v2最多调整原方向偏离50的幅度的20%，不混合第二个基础分；不是风险调整 alpha、上涨概率或收益预测，参数尚未经样本外收益验证。",
    )


def _relative_context_score(base: float, relative: float | None) -> float:
    """Scale existing directional amplitude; neutral relative evidence is the identity."""
    strength = (relative - 50) / 50 if relative is not None else 0.0
    adjustment = RELATIVE_STRENGTH_WEIGHT * strength * abs(base - 50)
    return max(0.0, min(100.0, base + adjustment))


def _finite_relative_returns(stock: Quote, market: ContextObservation | None, industry: ContextObservation) -> bool:
    stock_excess = finite_float(stock.change_pct - industry.change_pct)
    industry_excess = finite_float(industry.change_pct - market.change_pct) if market else 0.0
    return stock_excess is not None and industry_excess is not None


def _positive_gate(change_pct: float, lower: float) -> float:
    return max(lower, min(1.0, 1 + change_pct / 10))


def _full_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", value):
        return None
    try:
        return parse_audit_time(value)
    except (ValueError, TypeError, OverflowError):
        return None


def _aligned_event(value: object, stock_time: datetime | None, cutoff: datetime | None) -> datetime | None:
    event = _full_time(value)
    if event is None or stock_time is None or cutoff is None or event > min(stock_time, cutoff):
        return None
    if event.astimezone(ASHARE_TIMEZONE).date() != stock_time.astimezone(ASHARE_TIMEZONE).date():
        return None
    if (stock_time - event).total_seconds() > MAX_CONTEXT_EVENT_LAG_SECONDS:
        return None
    return event


def _usable_quote(quote: Quote) -> bool:
    price, previous, change = (finite_float(value) for value in (quote.price, quote.prev_close, quote.change_pct))
    return bool(
        price is not None and price > 0 and previous is not None and previous > 0 and change is not None
        and abs((price / previous - 1) * 100 - change) <= 0.3
        and quote.source.strip() and not quote.fallback_used and not is_demo_quote_source(quote.source)
    )


def _market_observation(
    market: Quote | None, stock_time: datetime | None, cutoff: datetime | None,
) -> ContextObservation | None:
    if market is None or f"{market.code}.{market.market}" != MARKET_CONTEXT_INDEX or not _usable_quote(market):
        return None
    event = _aligned_event(market.timestamp, stock_time, cutoff)
    if event is None or cutoff is None:
        return None
    return ContextObservation(symbol=MARKET_CONTEXT_INDEX, name="沪深300", change_pct=market.change_pct,
                              event_at=audit_datetime_to_text(event), observed_at=audit_datetime_to_text(cutoff), source=market.source)


def _industry_observation(
    industry: PlateItem | None, expected_name: str | None, stock_time: datetime | None, cutoff: datetime | None,
) -> ContextObservation | None:
    if industry is None or not expected_name or industry.name.strip() != expected_name.strip() or industry.fallback_used:
        return None
    symbol = industry.symbol
    if not symbol or not re.fullmatch(r"BK\d{4}", symbol) or industry.source != EASTMONEY_BRIDGE_SOURCE_NAME:
        return None
    event = _aligned_event(industry.quote_timestamp, stock_time, cutoff)
    observed = _full_time(industry.updated_at)
    change = finite_float(industry.change_pct)
    if event is None or observed is None or cutoff is None or not event <= observed <= cutoff or change is None:
        return None
    return ContextObservation(symbol=symbol, name=industry.name, change_pct=change,
                              event_at=audit_datetime_to_text(event), observed_at=audit_datetime_to_text(observed), source=industry.source)
