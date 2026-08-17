from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any, Protocol

from app.models.advice_change import (
    CONCLUSION_BASIS,
    MODEL_VERSION,
    SNAPSHOT_CONTRACT_VERSION,
    conclusion_identity,
)
from app.models.analysis import AnalysisResult
from app.models.market import DAILY_KLINE_CONTRACT_VERSION, Kline
from app.models.reviews import ResearchQueueRefreshItem, ResearchQueueRefreshSummary
from app.models.rule_versions import RULE_VERSION
from app.services.datahub import DataHub
from app.services.datahub_runtime import run_cache_io
from app.services.data_quality_time import quote_event_time_error
from app.services.research_factor_execution_contract import factor_calibration_evidence_issue
from app.services.trading_calendar import (
    DAILY_KLINE_PUBLISH_TIME,
    MARKET_CLOSE_TIME,
    is_trading_day,
)
from app.utils.audit_time import audit_datetime_to_text
from app.utils.clock import market_now_naive
from app.utils.market_data import valid_kline
from app.utils.market_time import market_local_naive, normalize_market_datetime
from app.utils.provider_errors import sanitize_provider_error
from app.utils.symbols import standard_symbol
from app.workflows.stock_analysis import analyze_individual_stock


ACTIVE_RESEARCH_REFRESH_LIMIT = 20
MAX_ACTIVE_RESEARCH_REFRESH_LIMIT = 100
MIN_ACTIVE_RESEARCH_QUALITY_SCORE = 50
MIN_ACTIVE_RESEARCH_KLINE_COUNT = 60
INVALID_RESEARCH_CONTRACT_VERSIONS = frozenset({"", "unknown", "legacy"})
ACTIVE_RESEARCH_CURSOR_ATTR = "_active_research_queue_cursor"


class ActiveResearchAnalyzer(Protocol):
    async def __call__(
        self,
        datahub: DataHub,
        symbol: str,
        *,
        persist_history: bool,
    ) -> AnalysisResult: ...


TradingDayCheck = Callable[[date], bool]


async def refresh_active_research_queue(
    datahub: DataHub, *,
    now: datetime | None = None,
    limit: int = ACTIVE_RESEARCH_REFRESH_LIMIT,
    analyzer: ActiveResearchAnalyzer = analyze_individual_stock,
    trading_day_check: TradingDayCheck = is_trading_day,
) -> ResearchQueueRefreshSummary:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("主动研究刷新上限必须是正整数")
    bounded_limit = min(limit, MAX_ACTIVE_RESEARCH_REFRESH_LIMIT)
    current = market_local_naive(now) if now is not None else market_now_naive()
    started_at = audit_datetime_to_text(current)
    if not _active_research_refresh_window(current, trading_day_check=trading_day_check):
        return ResearchQueueRefreshSummary(
            started_at=started_at,
            deferred=True,
            reason_code="not_after_close",
        )

    data_date = current.date().isoformat()
    selection = await run_cache_io(datahub.cache.watchlist_symbol_selection)
    excluded_symbols = set(selection.excluded_symbols)
    active_symbols = [symbol for symbol in selection.active_symbols if symbol not in excluded_symbols]
    latest_by_symbol = await run_cache_io(
        datahub.cache.latest_advice_timeline_by_symbols,
        active_symbols,
    )
    candidate_symbols = _active_research_candidate_order(
        datahub.cache,
        active_symbols,
        latest_by_symbol,
        data_date=data_date,
    )
    selected_symbols = candidate_symbols[:bounded_limit]
    _advance_active_research_cursor(
        datahub.cache,
        active_count=len(active_symbols),
        attempted_count=len(selected_symbols),
    )
    items = await _refresh_active_research_symbols(
        datahub,
        selected_symbols,
        data_date=data_date,
        now=current,
        analyzer=analyzer,
    )
    return ResearchQueueRefreshSummary(
        started_at=started_at,
        data_date=data_date,
        active_count=len(active_symbols),
        selected_count=len(selected_symbols),
        saved_count=sum(item.status == "saved" for item in items),
        unchanged_count=sum(item.status == "unchanged" for item in items),
        skipped_count=sum(item.status == "skipped" for item in items),
        failed_count=sum(item.status == "failed" for item in items),
        items=items,
    )


async def _refresh_active_research_symbols(
    datahub: DataHub,
    symbols: list[str],
    *,
    data_date: str,
    now: datetime,
    analyzer: ActiveResearchAnalyzer,
) -> list[ResearchQueueRefreshItem]:
    items: list[ResearchQueueRefreshItem] = []
    for symbol in symbols:
        items.append(
            await _refresh_active_research_symbol(
                datahub,
                symbol,
                data_date=data_date,
                now=now,
                analyzer=analyzer,
            )
        )
        await asyncio.sleep(0)
    return items


def _active_research_refresh_window(
    now: datetime,
    *,
    trading_day_check: TradingDayCheck = is_trading_day,
) -> bool:
    return trading_day_check(now.date()) and now.time() >= DAILY_KLINE_PUBLISH_TIME


async def _refresh_active_research_symbol(
    datahub: DataHub,
    symbol: str,
    *,
    data_date: str,
    now: datetime,
    analyzer: ActiveResearchAnalyzer = analyze_individual_stock,
) -> ResearchQueueRefreshItem:
    try:
        normalized = standard_symbol(symbol)
        analysis = await analyzer(datahub, normalized, persist_history=False)
        rejection = _active_research_snapshot_rejection(
            analysis,
            normalized,
            data_date,
            now=now,
        )
        if rejection is not None:
            return ResearchQueueRefreshItem(
                symbol=normalized,
                status="skipped",
                reason_code=rejection,
                data_date=data_date,
            )
        latest = await run_cache_io(datahub.cache.advice_timeline, normalized, limit=1)
        if latest and _advice_snapshot_is_current(latest[0], analysis, data_date):
            return ResearchQueueRefreshItem(
                symbol=normalized,
                status="unchanged",
                reason_code="already_current",
                advice_id=latest[0].id,
                data_date=data_date,
            )
        snapshot_market_time = f"{data_date} {DAILY_KLINE_PUBLISH_TIME.strftime('%H:%M:%S')}"
        saved = await run_cache_io(
            datahub.cache.save_advice_snapshot,
            analysis,
            snapshot_market_time=snapshot_market_time,
        )
        return ResearchQueueRefreshItem(
            symbol=normalized,
            status="saved",
            advice_id=saved.id,
            data_date=data_date,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return ResearchQueueRefreshItem(
            symbol=str(symbol),
            status="failed",
            reason_code="analysis_failed",
            data_date=data_date,
            message=_short_research_refresh_error(
                exc,
                sensitive_values=_background_sensitive_values(datahub),
            ),
        )


def _active_research_snapshot_rejection(
    analysis: AnalysisResult,
    expected_symbol: str,
    data_date: str,
    *,
    now: datetime,
) -> str | None:
    if analysis.research_mode != "official":
        return "non_official_research_mode"
    if _analysis_data_date(analysis) != data_date:
        return "stale_data_date"
    if _active_research_quote_is_invalid(analysis, now):
        return "low_data_quality"
    if _active_research_quality_is_invalid(analysis, data_date):
        return "low_data_quality"
    anchor = _latest_research_kline(analysis.klines, data_date)
    if anchor is None:
        return "stale_data_date"
    formal_rows = analysis.klines[-MIN_ACTIVE_RESEARCH_KLINE_COUNT:]
    if _active_research_formal_rows_are_invalid(formal_rows):
        return "invalid_rule_contract"
    if _active_research_contract_is_invalid(analysis, anchor, expected_symbol):
        return "invalid_rule_contract"
    return None


def _active_research_quote_is_invalid(
    analysis: AnalysisResult,
    now: datetime,
) -> bool:
    if quote_event_time_error(analysis.quote.timestamp, now=now) is not None:
        return True
    quote_time = normalize_market_datetime(analysis.quote.timestamp)
    return quote_time is None or datetime.fromisoformat(quote_time).time() > MARKET_CLOSE_TIME


def _active_research_quality_is_invalid(
    analysis: AnalysisResult,
    data_date: str,
) -> bool:
    quality = analysis.data_quality
    kline_quality = quality.kline_quality
    return (
        quality.score < MIN_ACTIVE_RESEARCH_QUALITY_SCORE
        or quality.kline_count < MIN_ACTIVE_RESEARCH_KLINE_COUNT
        or len(analysis.klines) < MIN_ACTIVE_RESEARCH_KLINE_COUNT
        or not analysis.support_available
        or not analysis.resistance_available
        or not analysis.ma20_available
        or kline_quality is None
        or kline_quality.last_date != data_date
        or kline_quality.days_behind_expected != 0
        or kline_quality.fallback_used
        or analysis.quote.fallback_used
    )


def _active_research_formal_rows_are_invalid(rows: list[Kline]) -> bool:
    return (
        len(rows) != MIN_ACTIVE_RESEARCH_KLINE_COUNT
        or factor_calibration_evidence_issue(rows) is not None
        or len({row.data_version for row in rows}) != 1
        or any(
            row.contract_version != DAILY_KLINE_CONTRACT_VERSION or row.fallback_used
            for row in rows
        )
    )


def _active_research_contract_is_invalid(
    analysis: AnalysisResult,
    anchor: Kline,
    expected_symbol: str,
) -> bool:
    observed_symbol = standard_symbol(f"{analysis.quote.code}.{analysis.quote.market}")
    return (
        observed_symbol != expected_symbol
        or anchor.adjustment_mode != "qfq"
        or anchor.data_version in INVALID_RESEARCH_CONTRACT_VERSIONS
        or anchor.contract_version != DAILY_KLINE_CONTRACT_VERSION
        or anchor.fallback_used
        or RULE_VERSION in INVALID_RESEARCH_CONTRACT_VERSIONS
        or SNAPSHOT_CONTRACT_VERSION in INVALID_RESEARCH_CONTRACT_VERSIONS
        or not str(analysis.action_advice.action or "").strip()
    )


def _analysis_data_date(analysis: AnalysisResult) -> str | None:
    normalized = normalize_market_datetime(analysis.quote.timestamp)
    return normalized[:10] if normalized is not None else None


def _latest_research_kline(rows: list[Kline], data_date: str) -> Kline | None:
    candidates = [row for row in rows if row.date == data_date and valid_kline(row)]
    if len(candidates) != 1:
        return None
    return candidates[0]


def _active_research_candidate_order(
    cache: object,
    active_symbols: list[str],
    latest_by_symbol: Mapping[str, object],
    *,
    data_date: str,
) -> list[str]:
    if not active_symbols:
        return []
    cursor = _active_research_cursor(cache, len(active_symbols))
    rotated = active_symbols[cursor:] + active_symbols[:cursor]
    rotated_rank = {symbol: index for index, symbol in enumerate(rotated)}

    def candidate_key(symbol: str) -> tuple[int, str, int]:
        snapshot = latest_by_symbol.get(symbol)
        is_current = snapshot is not None and _advice_snapshot_contract_is_current(
            snapshot,
            data_date,
        )
        snapshot_time = (
            normalize_market_datetime(getattr(snapshot, "market_time", None))
            if snapshot is not None
            else None
        )
        return (int(is_current), snapshot_time or "", rotated_rank[symbol])

    return sorted(active_symbols, key=candidate_key)


def _active_research_cursor(cache: object, active_count: int) -> int:
    try:
        cursor = int(getattr(cache, ACTIVE_RESEARCH_CURSOR_ATTR, 0))
    except (TypeError, ValueError):
        cursor = 0
    return cursor % active_count


def _advance_active_research_cursor(
    cache: object,
    *,
    active_count: int,
    attempted_count: int,
) -> None:
    if active_count <= 0 or attempted_count <= 0:
        return
    cursor = (_active_research_cursor(cache, active_count) + attempted_count) % active_count
    setattr(cache, ACTIVE_RESEARCH_CURSOR_ATTR, cursor)


def _advice_snapshot_is_current(
    snapshot: Any,
    analysis: AnalysisResult,
    data_date: str,
) -> bool:
    anchor = _latest_research_kline(analysis.klines, data_date)
    if anchor is None:
        return False
    snapshot_identity = conclusion_identity(snapshot)
    analysis_identity = conclusion_identity(_analysis_conclusion_values(analysis))
    return bool(
        _advice_snapshot_contract_is_current(snapshot, data_date)
        and snapshot_identity is not None
        and snapshot_identity == analysis_identity
        and snapshot.kline_adjustment_mode == anchor.adjustment_mode
        and snapshot.kline_data_version == anchor.data_version
        and snapshot.kline_contract_version == anchor.contract_version
        and _same_research_price(snapshot.kline_anchor_close, anchor.close)
    )


def _advice_snapshot_contract_is_current(snapshot: Any, data_date: str) -> bool:
    market_time = normalize_market_datetime(snapshot.market_time)
    return bool(
        market_time is not None
        and market_time[:10] == data_date
        and snapshot.kline_anchor_date == data_date
        and snapshot.kline_adjustment_mode == "qfq"
        and snapshot.kline_data_version not in INVALID_RESEARCH_CONTRACT_VERSIONS
        and snapshot.rule_version == RULE_VERSION
        and snapshot.snapshot_contract_version == SNAPSHOT_CONTRACT_VERSION
        and snapshot.kline_contract_version == DAILY_KLINE_CONTRACT_VERSION
    )


def _analysis_conclusion_values(analysis: AnalysisResult) -> dict[str, object]:
    return {
        "action": analysis.action_advice.action,
        "confidence": analysis.action_advice.confidence,
        "trend_score": analysis.trend_score,
        "trend_label": analysis.trend_label,
        "risk_level": analysis.risk_level,
        "support": analysis.support,
        "resistance": analysis.resistance,
        "data_quality_score": analysis.data_quality.score,
        "data_quality_level": analysis.data_quality.level,
        "data_quality_source": analysis.data_quality.source,
        "snapshot_contract_version": SNAPSHOT_CONTRACT_VERSION,
        "conclusion_basis": CONCLUSION_BASIS,
        "rule_version": RULE_VERSION,
        "model_version": MODEL_VERSION,
    }


def _same_research_price(value: object, expected: float) -> bool:
    if isinstance(value, bool) or not isinstance(value, str | bytes | bytearray | int | float):
        return False
    try:
        return float(value) == float(expected)
    except (TypeError, ValueError):
        return False


def _short_research_refresh_error(
    exc: Exception,
    *,
    sensitive_values: tuple[object, ...] = (),
) -> str:
    message = " ".join(
        sanitize_provider_error(exc, sensitive_values=sensitive_values).split()
    ).strip()
    return (message or exc.__class__.__name__)[:160]


def _background_sensitive_values(datahub: object) -> tuple[object, ...]:
    cache = getattr(datahub, "cache", None)
    settings = getattr(datahub, "settings", None) or getattr(cache, "settings", None)
    if settings is None:
        return ()
    values = (
        getattr(settings, "tushare_token", None),
        getattr(settings, "llm_api_key", None),
        getattr(settings, "llm_base_url", None),
    )
    return tuple(value for value in values if value not in (None, ""))


# Public compatibility contracts used by the legacy ``individual`` façade.
# The implementation names remain private so the extraction does not change
# existing monkeypatch or traceback identities.
active_research_candidate_order = _active_research_candidate_order
active_research_cursor = _active_research_cursor
active_research_refresh_window = _active_research_refresh_window
active_research_snapshot_rejection = _active_research_snapshot_rejection
advice_snapshot_contract_is_current = _advice_snapshot_contract_is_current
advice_snapshot_is_current = _advice_snapshot_is_current
advance_active_research_cursor = _advance_active_research_cursor
analysis_conclusion_values = _analysis_conclusion_values
analysis_data_date = _analysis_data_date
background_sensitive_values = _background_sensitive_values
latest_research_kline = _latest_research_kline
refresh_active_research_symbol = _refresh_active_research_symbol
same_research_price = _same_research_price
short_research_refresh_error = _short_research_refresh_error


__all__ = [
    "ACTIVE_RESEARCH_CURSOR_ATTR",
    "ACTIVE_RESEARCH_REFRESH_LIMIT",
    "INVALID_RESEARCH_CONTRACT_VERSIONS",
    "MAX_ACTIVE_RESEARCH_REFRESH_LIMIT",
    "MIN_ACTIVE_RESEARCH_QUALITY_SCORE",
    "active_research_candidate_order",
    "active_research_cursor",
    "active_research_refresh_window",
    "active_research_snapshot_rejection",
    "advice_snapshot_contract_is_current",
    "advice_snapshot_is_current",
    "advance_active_research_cursor",
    "analysis_conclusion_values",
    "analysis_data_date",
    "background_sensitive_values",
    "latest_research_kline",
    "refresh_active_research_queue",
    "refresh_active_research_symbol",
    "same_research_price",
    "short_research_refresh_error",
]
