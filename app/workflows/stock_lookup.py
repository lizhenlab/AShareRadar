from __future__ import annotations

import asyncio

from app.models.schemas import PlateItem, Quote, StockInfo
from app.services.datahub import DataHub
from app.services.datahub_runtime import run_cache_io_best_effort
from app.utils.errors import NotFoundError
from app.utils.symbols import normalize_symbol
from app.utils.time import now_text
from app.workflows.optional_data import optional_timeout_seconds, short_error


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


def match_industry(profile: StockInfo | None, plates: list[PlateItem]) -> PlateItem | None:
    if not profile or not profile.industry:
        return None
    for item in plates:
        if item.name == profile.industry:
            return item
    return None


__all__ = ["confirmed_stock_profile", "match_industry"]
