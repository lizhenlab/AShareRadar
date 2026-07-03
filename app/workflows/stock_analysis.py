from __future__ import annotations

import asyncio

from app.models.schemas import AnalysisResult, IndividualReview, MinuteAnalysisReport
from app.services.analysis import build_analysis
from app.services.datahub import DataHub
from app.services.datahub_cache import _normalize_minute_interval
from app.services.market_sampling import peer_quotes as _peer_quotes
from app.services.minute_analysis import build_minute_analysis_report, build_unavailable_minute_analysis_report
from app.services.review import build_individual_review
from app.utils.symbols import normalize_symbol
from app.workflows.stock_lookup import confirmed_stock_profile, match_industry


async def analyze_individual_stock(datahub: DataHub, symbol: str, persist_history: bool = True) -> AnalysisResult:
    code, market = normalize_symbol(symbol)
    standard = f"{code}.{market.upper()}"
    profile = await confirmed_stock_profile(datahub, standard)
    quote_data, klines, plates_result = await asyncio.gather(
        datahub.quote(symbol),
        datahub.kline(symbol, 120),
        datahub.plate_rank(limit=20),
        return_exceptions=True,
    )
    if isinstance(quote_data, Exception):
        raise quote_data
    if isinstance(klines, Exception):
        raise klines
    plates = _optional_plates(datahub, standard, plates_result)
    data_quality = await datahub.assess_quote_quality(quote_data, klines)
    industry = match_industry(profile, plates)
    review = build_individual_review(quote_data, klines, period_days=60)
    quote_history = datahub.cache.quote_history(f"{quote_data.code}.{quote_data.market}", limit=120)
    peer_quotes = await _peer_quotes(datahub, profile, f"{quote_data.code}.{quote_data.market}")
    result = build_analysis(
        quote_data,
        klines,
        stock_profile=profile,
        industry_context=industry,
        review=review,
        data_quality=data_quality,
        quote_history=quote_history,
        peer_quotes=peer_quotes,
    )
    if persist_history:
        datahub.cache.save_advice_snapshot(result)
    return result


def _optional_plates(datahub: DataHub, symbol: str, result: object) -> list:
    if isinstance(result, Exception):
        datahub.cache.log_event("fallback", f"个股行业背景暂不可用：{symbol}；{_short_error(result)}")
        return []
    return result if isinstance(result, list) else []


def _short_error(exc: Exception) -> str:
    text = str(exc).strip()
    return text[:140] if text else exc.__class__.__name__


async def review_individual_stock(datahub: DataHub, symbol: str, period_days: int) -> IndividualReview:
    code, market = normalize_symbol(symbol)
    await confirmed_stock_profile(datahub, f"{code}.{market.upper()}")
    quote_data, klines = await asyncio.gather(datahub.quote(symbol), datahub.kline(symbol, max(period_days, 120)))
    return build_individual_review(quote_data, klines, period_days=period_days)


async def stock_minute_analysis(
    datahub: DataHub,
    symbol: str,
    interval: str = "5m",
    limit: int = 120,
) -> MinuteAnalysisReport:
    normalized = normalize_symbol(symbol)
    standard = f"{normalized[0]}.{normalized[1].upper()}"
    normalized_interval = _normalize_minute_interval(interval)
    await confirmed_stock_profile(datahub, standard)
    try:
        rows = await datahub.minute_kline(standard, interval=normalized_interval, limit=limit)
    except RuntimeError as exc:
        datahub.cache.log_event("fallback", f"分钟分析不可用：{standard} {normalized_interval}；{exc}")
        return build_unavailable_minute_analysis_report(standard, interval=normalized_interval, reason=str(exc))
    return build_minute_analysis_report(standard, rows, interval=normalized_interval)


__all__ = ["analyze_individual_stock", "review_individual_stock", "stock_minute_analysis"]
