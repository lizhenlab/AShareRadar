from __future__ import annotations

import asyncio

from app.models.schemas import AnalysisResult, IndividualReview, MinuteAnalysisReport, PeerSampleInfo
from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.datahub import DataHub
from app.services.datahub_cache import _normalize_minute_interval
from app.services.datahub_runtime import run_cache_io, run_cache_io_best_effort
from app.services.market_sampling import PeerQuoteSampleResult, peer_quote_sample as _peer_quote_sample
from app.services.minute_analysis import build_minute_analysis_report, build_unavailable_minute_analysis_report
from app.services.review import build_individual_review
from app.utils.symbols import normalize_symbol
from app.workflows.optional_data import optional_workflow_value, short_error
from app.workflows.stock_lookup import confirmed_stock_profile, match_industry


WORKBENCH_DAILY_KLINE_LIMIT = 240
WORKBENCH_REVIEW_WINDOW_DAYS = 60


async def analyze_individual_stock(datahub: DataHub, symbol: str, persist_history: bool = True) -> AnalysisResult:
    code, market = normalize_symbol(symbol)
    standard = f"{code}.{market.upper()}"
    profile = await confirmed_stock_profile(datahub, standard)
    quote_data, klines, plates_result = await asyncio.gather(
        datahub.quote(symbol),
        datahub.kline(symbol, WORKBENCH_DAILY_KLINE_LIMIT),
        _optional_plate_rank(datahub, standard),
        return_exceptions=True,
    )
    if isinstance(quote_data, asyncio.CancelledError):
        raise quote_data
    if isinstance(quote_data, Exception):
        raise quote_data
    if isinstance(klines, asyncio.CancelledError):
        raise klines
    if isinstance(klines, Exception):
        raise klines
    if isinstance(plates_result, asyncio.CancelledError):
        raise plates_result
    plates = plates_result if isinstance(plates_result, list) else []
    quote_symbol = f"{quote_data.code}.{quote_data.market}"
    data_quality = await _assess_quote_quality_or_fallback(datahub, quote_data, klines, quote_symbol)
    industry = match_industry(profile, plates)
    review = build_individual_review(quote_data, klines, period_days=WORKBENCH_REVIEW_WINDOW_DAYS)
    quote_history = await _safe_quote_history(datahub, quote_symbol)
    peer_sample = await _peer_quote_sample_or_fallback(datahub, profile, quote_symbol)
    result = build_analysis(
        quote_data,
        klines,
        stock_profile=profile,
        industry_context=industry,
        review=review,
        data_quality=data_quality,
        quote_history=quote_history,
        peer_quotes=list(peer_sample.quotes),
        peer_sample=_peer_sample_info(peer_sample),
    )
    if persist_history:
        await _safe_save_advice_snapshot(datahub, result, quote_symbol)
    return result


async def _safe_quote_history(datahub: DataHub, symbol: str) -> list[dict[str, float | str | None]]:
    try:
        return await run_cache_io(datahub.cache.quote_history, symbol, limit=120)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await _log_analysis_fallback(datahub, f"个股历史报价暂不可用：{symbol}；{_short_error(exc)}")
        return []


async def _safe_save_advice_snapshot(datahub: DataHub, result: AnalysisResult, symbol: str) -> None:
    try:
        await run_cache_io(datahub.cache.save_advice_snapshot, result)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await _log_analysis_fallback(datahub, f"分析建议快照暂不可写：{symbol}；{_short_error(exc)}")


async def _optional_plate_rank(datahub: DataHub, symbol: str) -> list:
    failure: Exception | None = None

    def empty_rows(exc: Exception) -> list:
        nonlocal failure
        failure = exc
        return []

    rows = await optional_workflow_value(
        datahub,
        lambda: datahub.plate_rank(limit=20),
        empty_rows,
    )
    if failure is not None:
        await _log_analysis_fallback(datahub, f"个股行业背景暂不可用：{symbol}；{short_error(failure)}")
    return rows


async def _peer_quote_sample_or_fallback(datahub: DataHub, profile, symbol: str) -> PeerQuoteSampleResult:
    failure: Exception | None = None

    def unavailable_sample(exc: Exception) -> PeerQuoteSampleResult:
        nonlocal failure
        failure = exc
        return _unavailable_peer_sample(symbol, exc)

    sample = await optional_workflow_value(
        datahub,
        lambda: _peer_quote_sample(datahub, profile, symbol),
        unavailable_sample,
    )
    if failure is not None:
        await _log_analysis_fallback(datahub, f"同行样本暂不可用：{symbol}；{short_error(failure)}")
    return sample


def _unavailable_peer_sample(symbol: str, exc: Exception) -> PeerQuoteSampleResult:
    return PeerQuoteSampleResult(
        status="unavailable",
        warning="同行样本请求失败，当前仅使用个股历史和行业背景。",
    )


def _peer_sample_info(sample: PeerQuoteSampleResult) -> PeerSampleInfo:
    return PeerSampleInfo(
        status=sample.status,
        requested_count=sample.requested_count,
        missing_count=sample.missing_count,
        warning=sample.warning,
    )


async def _assess_quote_quality_or_fallback(datahub: DataHub, quote, klines, symbol: str):
    failure: Exception | None = None

    def fallback_quality(exc: Exception):
        nonlocal failure
        failure = exc
        return _fallback_data_quality(quote, klines, symbol, exc)

    quality = await optional_workflow_value(
        datahub,
        lambda: datahub.assess_quote_quality(quote, klines),
        fallback_quality,
    )
    if failure is not None:
        await _log_analysis_fallback(datahub, f"数据质量校验暂不可用：{symbol}；{short_error(failure)}")
    return quality


def _fallback_data_quality(quote, klines, symbol: str, exc: Exception):
    message = f"数据质量校验暂不可用：{symbol}；{short_error(exc)}"
    return build_data_quality(
        quote,
        klines,
        consistency_level="未校验",
        consistency_notes=[message],
        consistency_penalty=10,
    )


async def _log_analysis_fallback(datahub: DataHub, message: str) -> None:
    log_event = getattr(datahub.cache, "log_event", None)
    if callable(log_event):
        await run_cache_io_best_effort(log_event, "fallback", message)


def _short_error(exc: Exception) -> str:
    return short_error(exc)


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
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await _safe_log_minute_fallback(datahub, f"分钟分析不可用：{standard} {normalized_interval}；{_short_error(exc)}")
        return build_unavailable_minute_analysis_report(standard, interval=normalized_interval, reason=str(exc))
    return build_minute_analysis_report(standard, rows, interval=normalized_interval)


async def _safe_log_minute_fallback(datahub: DataHub, message: str) -> None:
    await _log_analysis_fallback(datahub, message)


__all__ = ["analyze_individual_stock", "review_individual_stock", "stock_minute_analysis"]
