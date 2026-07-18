from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from app.models.reviews import (
    WatchlistScanCondition,
    WatchlistScanItem,
    WatchlistScanMissing,
    WatchlistScanRequest,
    WatchlistScanResponse,
)
from app.models.schemas import Kline
from app.services.advice_review import normalize_review_as_of
from app.services.datahub import DataHub
from app.services.datahub_runtime import run_cache_io
from app.services.provider_errors import sanitize_provider_error
from app.services.research_replay import completed_daily_bar_cutoff
from app.utils.market_data import valid_kline
from app.utils.symbols import standard_symbol_list


WATCHLIST_SCAN_RULE_VERSION = "watchlist-scan-v1"
MAX_SCAN_SYMBOLS = 50
SCAN_CONCURRENCY = 5
SCAN_KLINE_LIMIT = 260


ScanEvaluation = tuple[bool, dict[str, float]]
ScanEvaluator = Callable[[list[Kline]], ScanEvaluation]


@dataclass(frozen=True)
class ScanConditionSpec:
    required_rows: int
    evaluator: ScanEvaluator


class ScanDataMissing(ValueError):
    pass


async def scan_watchlist_conditions(
    datahub: DataHub,
    payload: WatchlistScanRequest,
    *,
    now: datetime | None = None,
) -> WatchlistScanResponse:
    as_of = normalize_review_as_of(payload.as_of, now=now)
    universe = await _scan_universe(datahub, payload)
    conditions = _unique_conditions(payload.conditions)
    semaphore = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def scan_one(symbol: str) -> WatchlistScanItem | WatchlistScanMissing:
        async with semaphore:
            try:
                rows = await datahub.kline(symbol, limit=SCAN_KLINE_LIMIT, use_cache=True)
                return _evaluate_scan_symbol(symbol, rows, conditions, as_of)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = " ".join(sanitize_provider_error(exc).split()).strip()
                return WatchlistScanMissing(symbol=symbol, reason=(detail or "日K数据不可用")[:160])

    results = await asyncio.gather(*(scan_one(symbol) for symbol in universe))
    return WatchlistScanResponse(
        universe=universe,
        success=[item for item in results if isinstance(item, WatchlistScanItem)],
        missing=[item for item in results if isinstance(item, WatchlistScanMissing)],
        as_of=as_of.strftime("%Y-%m-%d %H:%M:%S"),
        rule_version=WATCHLIST_SCAN_RULE_VERSION,
        conditions=conditions,
    )


async def _scan_universe(datahub: DataHub, payload: WatchlistScanRequest) -> list[str]:
    if payload.rule_version != WATCHLIST_SCAN_RULE_VERSION:
        raise ValueError("不支持的扫描规则版本")
    if payload.universe == "watchlist":
        if payload.symbols:
            raise ValueError("扫描当前自选列表时不能同时传 symbols")
        selection = await run_cache_io(datahub.cache.watchlist_symbol_selection)
        symbols = list(selection.active_symbols)
    else:
        if not payload.symbols:
            raise ValueError("显式扫描必须提供 symbols")
        symbols = standard_symbol_list(payload.symbols, max_items=MAX_SCAN_SYMBOLS).symbols
    if len(symbols) > MAX_SCAN_SYMBOLS:
        raise ValueError(f"一次最多扫描 {MAX_SCAN_SYMBOLS} 个股票代码")
    return symbols


def _unique_conditions(conditions: list[WatchlistScanCondition]) -> list[WatchlistScanCondition]:
    return list(dict.fromkeys(conditions))


def _evaluate_scan_symbol(
    symbol: str,
    rows: list[Kline],
    conditions: list[WatchlistScanCondition],
    as_of: datetime,
) -> WatchlistScanItem:
    completed_rows = _completed_scan_rows(rows, completed_daily_bar_cutoff(as_of))
    if not completed_rows:
        raise ScanDataMissing("as_of 时点之前没有完整日K")
    results: dict[str, bool] = {}
    metrics: dict[str, float] = {"close": round(completed_rows[-1].close, 4)}
    for condition in conditions:
        spec = WATCHLIST_SCAN_CONDITIONS[condition]
        if len(completed_rows) < spec.required_rows:
            raise ScanDataMissing(
                f"条件 {condition} 至少需要 {spec.required_rows} 根完整日K，当前只有 {len(completed_rows)} 根"
            )
        matched, condition_metrics = spec.evaluator(completed_rows)
        results[condition] = matched
        metrics.update(condition_metrics)
    return WatchlistScanItem(
        symbol=symbol,
        data_date=completed_rows[-1].date,
        matched=all(results.values()),
        condition_results=results,
        matched_conditions=[condition for condition in conditions if results[condition]],
        metrics=metrics,
    )


def _completed_scan_rows(rows: list[Kline], cutoff: date) -> list[Kline]:
    by_date: dict[date, Kline] = {}
    for row in rows:
        row_date = _strict_date(row.date)
        if row_date is not None and row_date <= cutoff and valid_kline(row):
            by_date[row_date] = row
    return [row for _row_date, row in sorted(by_date.items(), key=lambda item: item[0])]


def _strict_date(value: object) -> date | None:
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == text else None


def _close_above_ma20(rows: list[Kline]) -> ScanEvaluation:
    close = rows[-1].close
    ma20 = sum(row.close for row in rows[-20:]) / 20
    return close > ma20, {"ma20": round(ma20, 4)}


def _close_below_ma20(rows: list[Kline]) -> ScanEvaluation:
    close = rows[-1].close
    ma20 = sum(row.close for row in rows[-20:]) / 20
    return close < ma20, {"ma20": round(ma20, 4)}


def _breakout_20d_high(rows: list[Kline]) -> ScanEvaluation:
    close = rows[-1].close
    previous_high = max(row.high for row in rows[-21:-1])
    return close > previous_high, {"previous_20d_high": round(previous_high, 4)}


def _volume_surge_5d(rows: list[Kline]) -> ScanEvaluation:
    average_volume = sum(row.volume for row in rows[-6:-1]) / 5
    if average_volume <= 0:
        raise ScanDataMissing("最近5日平均成交量无效")
    volume_ratio = rows[-1].volume / average_volume
    return volume_ratio >= 1.5, {"volume_ratio_5d": round(volume_ratio, 4)}


WATCHLIST_SCAN_CONDITIONS: dict[WatchlistScanCondition, ScanConditionSpec] = {
    "close_above_ma20": ScanConditionSpec(20, _close_above_ma20),
    "close_below_ma20": ScanConditionSpec(20, _close_below_ma20),
    "breakout_20d_high": ScanConditionSpec(21, _breakout_20d_high),
    "volume_surge_5d": ScanConditionSpec(6, _volume_surge_5d),
}


__all__ = [
    "MAX_SCAN_SYMBOLS",
    "SCAN_CONCURRENCY",
    "WATCHLIST_SCAN_CONDITIONS",
    "WATCHLIST_SCAN_RULE_VERSION",
    "scan_watchlist_conditions",
]
