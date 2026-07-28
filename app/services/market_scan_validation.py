from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
import math
from typing import Literal

from app.models.market import Kline, Quote, StockInfo
from app.models.market_scan import MarketScanResultItem, MarketScanResultWrite
from app.services.data_quality_time import latest_expected_daily_kline_date
from app.services.market_scan_contracts import (
    MarketScanDataHubProtocol,
    MarketScanStockPoolResolutionProtocol,
)
from app.services.market_scan_scoring import completed_market_scan_klines
from app.utils.clock import market_now, monotonic_now
from app.utils.provider_errors import ProviderChainUnavailable


MARKET_SCAN_WALL_CLOCK_BUDGET_SECONDS = 30 * 60.0


@dataclass(frozen=True)
class MarketScanRuntimeGuard:
    data_date: date
    wall_clock_budget_seconds: float
    now: Callable[[], datetime]
    monotonic: Callable[[], float]
    started_monotonic: float

    @classmethod
    def create(
        cls,
        data_date: date,
        settings: object,
        *,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> MarketScanRuntimeGuard:
        monotonic_clock = monotonic or monotonic_now
        return cls(
            data_date=data_date,
            wall_clock_budget_seconds=market_scan_wall_clock_budget_seconds(settings),
            now=now or market_now,
            monotonic=monotonic_clock,
            started_monotonic=monotonic_clock(),
        )

    def checkpoint(self) -> None:
        elapsed = self.monotonic() - self.started_monotonic
        if elapsed >= self.wall_clock_budget_seconds:
            raise RuntimeError(
                f"全市场扫描超过 {self.wall_clock_budget_seconds:g} 秒墙钟预算"
            )
        current_data_date = latest_expected_daily_kline_date(self.now())
        if current_data_date != self.data_date:
            raise RuntimeError(
                "扫描运行期间完整交易日已从 "
                f"{self.data_date.isoformat()} 变为 {current_data_date.isoformat()}，停止发布"
            )


def market_scan_wall_clock_budget_seconds(settings: object) -> float:
    value = getattr(
        settings,
        "market_scan_wall_clock_budget_seconds",
        MARKET_SCAN_WALL_CLOCK_BUDGET_SECONDS,
    )
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return MARKET_SCAN_WALL_CLOCK_BUDGET_SECONDS
    if not math.isfinite(parsed) or parsed <= 0:
        return MARKET_SCAN_WALL_CLOCK_BUDGET_SECONDS
    return parsed


def minimum_market_counts(settings: object) -> dict[str, int]:
    return {
        "SH": int(getattr(settings, "market_scan_min_sh_count")),
        "SZ": int(getattr(settings, "market_scan_min_sz_count")),
        "BJ": int(getattr(settings, "market_scan_min_bj_count")),
    }


async def resolve_market_scan_stock_pool(
    datahub: MarketScanDataHubProtocol,
    *,
    required_markets: Iterable[str],
    minimum_counts: dict[str, int],
) -> tuple[list[StockInfo], str | None, bool]:
    if not isinstance(datahub, MarketScanStockPoolResolutionProtocol):
        rows = await datahub.stock_pool(
            limit=None,
            refresh=True,
            required_markets=required_markets,
            minimum_market_counts=minimum_counts,
        )
        return rows, None, True
    resolution = await datahub.stock_pool_resolution(
        limit=None,
        refresh=True,
        required_markets=required_markets,
        minimum_market_counts=minimum_counts,
    )
    reason = str(resolution.reason).strip() or "unknown"
    return resolution.list_rows(), reason, bool(resolution.resolved)


def missing_quote_result(
    item: MarketScanResultItem,
    rows: list[Kline],
    *,
    cutoff: date,
    expected_data_date: date,
    quote_error: str | None,
    min_history_rows: int,
) -> MarketScanResultWrite:
    completed = completed_market_scan_klines(rows, cutoff)
    if len(completed) < min_history_rows:
        return MarketScanResultWrite(
            symbol=item.symbol,
            status="skipped",
            reason=f"完整前复权日K不足：需要 {min_history_rows} 根，当前 {len(completed)} 根",
        )
    if {row.adjustment_mode for row in completed} != {"qfq"}:
        return MarketScanResultWrite(
            symbol=item.symbol,
            status="missing",
            error="日K不是一致的前复权序列",
        )
    latest_date = datetime.fromisoformat(completed[-1].date).date()
    provenance = {
        "data_date": latest_date.isoformat(),
        "kline_source": completed[-1].source,
        "adjustment_mode": completed[-1].adjustment_mode,
    }
    if latest_date < expected_data_date:
        return MarketScanResultWrite(
            symbol=item.symbol,
            status="skipped",
            reason=(
                f"日K停留在 {latest_date.isoformat()}，早于应有交易日 "
                f"{expected_data_date.isoformat()}，可能停牌"
            ),
            **provenance,
        )
    if completed[-1].volume <= 0:
        return MarketScanResultWrite(
            symbol=item.symbol,
            status="skipped",
            reason="当日日K成交量为 0 且报价不可用，可能停牌",
            **provenance,
        )
    return MarketScanResultWrite(
        symbol=item.symbol,
        status="missing",
        error=quote_error or "报价不可用，无法计算包含换手率和成交额的综合分",
        **provenance,
    )


def failed_market_scan_result(
    symbol: str,
    status: Literal["missing", "skipped"],
    quote: Quote | None,
    rows: list[Kline],
    *,
    cutoff: date,
    reason: str | None = None,
    error: str | None = None,
) -> MarketScanResultWrite:
    completed = completed_market_scan_klines(rows, cutoff)
    latest = completed[-1] if completed else None
    return MarketScanResultWrite(
        symbol=symbol,
        status=status,
        reason=reason,
        error=error,
        data_date=latest.date if latest is not None else None,
        quote_timestamp=quote.timestamp if quote is not None else None,
        quote_source=quote.source if quote is not None else None,
        kline_source=latest.source if latest is not None else None,
        adjustment_mode=latest.adjustment_mode if latest is not None else None,
    )


def raise_batch_outcome_error(
    outcomes: list[MarketScanResultWrite | BaseException],
) -> None:
    if any(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes):
        raise asyncio.CancelledError
    unexpected = next(
        (
            outcome
            for outcome in outcomes
            if isinstance(outcome, BaseException)
            and not isinstance(outcome, ProviderChainUnavailable)
        ),
        None,
    )
    if unexpected is not None:
        raise unexpected


__all__ = [
    "MARKET_SCAN_WALL_CLOCK_BUDGET_SECONDS",
    "MarketScanRuntimeGuard",
    "failed_market_scan_result",
    "market_scan_wall_clock_budget_seconds",
    "minimum_market_counts",
    "missing_quote_result",
    "raise_batch_outcome_error",
    "resolve_market_scan_stock_pool",
]
