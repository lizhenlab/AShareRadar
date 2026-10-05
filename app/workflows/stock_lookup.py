from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import re

from app.models.market import (
    PlateItem,
    Quote,
    StockInfo,
)
from app.services.datahub import DataHub
from app.services.datahub_runtime import run_cache_io_best_effort
from app.services.eastmoney_client import EASTMONEY_BRIDGE_SOURCE_NAME
from app.utils.audit_time import audit_now_text as now_text, parse_audit_time
from app.utils.clock import ASHARE_TIMEZONE
from app.utils.errors import NotFoundError
from app.utils.market_data import finite_float
from app.utils.symbols import normalize_symbol
from app.workflows.optional_data import optional_timeout_seconds, short_error


INDUSTRY_QUOTE_ALIGNMENT_TOLERANCE_SECONDS = 300


async def confirmed_stock_profile(datahub: DataHub, symbol: str) -> StockInfo | None:
    try:
        profile = await _stock_profile_or_timeout(datahub, symbol)
    except (RuntimeError, TimeoutError) as exc:
        fallback = await _quote_confirmed_profile(datahub, symbol)
        if fallback is not None:
            await _log_quote_confirmation(datahub, fallback.symbol, _profile_failure_reason(exc))
            return fallback
        raise RuntimeError(f"股票池暂不可用，无法确认股票代码：{symbol}；{_profile_error_text(exc)}") from exc
    if profile is None:
        fallback = await _quote_confirmed_profile(datahub, symbol)
        if fallback is not None:
            await _log_quote_confirmation(datahub, fallback.symbol, "股票池未命中")
            return fallback
        raise NotFoundError(f"股票代码不存在，且实时行情也无法确认：{symbol}")
    return profile


async def _stock_profile_or_timeout(datahub: DataHub, symbol: str) -> StockInfo | None:
    return await asyncio.wait_for(datahub.stock_profile(symbol), timeout=optional_timeout_seconds(datahub))


async def _quote_confirmed_profile(datahub: DataHub, symbol: str) -> StockInfo | None:
    try:
        quote = await datahub.quote(symbol, use_cache=False)
    except RuntimeError:
        return None
    profile = _profile_from_quote(symbol, quote)
    if profile is not None:
        await _cache_quote_confirmed_profile(datahub, profile)
    return profile


async def _cache_quote_confirmed_profile(datahub: DataHub, profile: StockInfo) -> None:
    save_stock_pool = getattr(getattr(datahub, "cache", None), "save_stock_pool", None)
    if not callable(save_stock_pool):
        return
    try:
        await run_cache_io_best_effort(save_stock_pool, [profile])
    except asyncio.CancelledError:
        raise


def _profile_failure_reason(exc: Exception) -> str:
    return "股票池查询超时" if isinstance(exc, TimeoutError) else "股票池暂不可用"


def _profile_error_text(exc: Exception) -> str:
    return "查询超时" if isinstance(exc, TimeoutError) else short_error(exc)


def _profile_from_quote(symbol: str, quote: Quote) -> StockInfo | None:
    if quote.from_cache or quote.fallback_used:
        return None
    code, market = normalize_symbol(symbol)
    if quote.code != code or quote.market.upper() != market.upper():
        return None
    name = str(quote.name or "").strip()
    if not name:
        return None
    standard = f"{code}.{market.upper()}"
    return StockInfo(
        symbol=standard,
        code=code,
        market=market.upper(),
        name=name,
        industry=None,
        list_date=None,
        source=f"{str(quote.source or '行情').strip()}确认",
        updated_at=now_text(),
    )


async def _log_quote_confirmation(datahub: DataHub, symbol: str, reason: str) -> None:
    log_event = getattr(getattr(datahub, "cache", None), "log_event", None)
    if callable(log_event):
        await run_cache_io_best_effort(log_event, "fallback", f"{reason}，使用行情确认股票代码：{symbol}")


def match_industry(
    profile: StockInfo | None,
    plates: list[PlateItem],
    *,
    quote: Quote,
    evaluated_at: str,
) -> PlateItem | None:
    if not profile or not profile.industry or not profile.industry.strip():
        return None
    matches = [item for item in plates if item.name == profile.industry]
    if len(matches) != 1:
        return None
    item = matches[0]
    return item if _current_industry_observation(item, quote, evaluated_at) else None


def _current_industry_observation(item: PlateItem, quote: Quote, evaluated_at: str) -> bool:
    observed = _industry_timestamp(item.quote_timestamp)
    fetched = _industry_timestamp(item.updated_at)
    quote_time = _industry_timestamp(quote.timestamp)
    decision_time = _industry_timestamp(evaluated_at)
    if observed is None or fetched is None or quote_time is None or decision_time is None:
        return False
    return (
        not item.fallback_used
        and item.source == EASTMONEY_BRIDGE_SOURCE_NAME
        and bool(re.fullmatch(r"BK[0-9]{4}", item.symbol or ""))
        and item.rank > 0
        and finite_float(item.change_pct) is not None
        and observed.astimezone(ASHARE_TIMEZONE).date() == quote_time.astimezone(ASHARE_TIMEZONE).date()
        and observed <= min(quote_time, decision_time)
        and quote_time - observed <= timedelta(seconds=INDUSTRY_QUOTE_ALIGNMENT_TOLERANCE_SECONDS)
        and observed <= fetched <= decision_time
    )


def _industry_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not re.match(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}", value):
        return None
    try:
        return parse_audit_time(value)
    except (TypeError, ValueError, OverflowError):
        return None


__all__ = ["confirmed_stock_profile", "match_industry"]
