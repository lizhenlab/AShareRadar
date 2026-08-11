from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path
import threading

from app.config import Settings
from app.models.schemas import Kline, Quote, StockInfo
from app.services.cache import SQLiteCache
from app.services.datahub_metadata import StockPoolResolution
from app.services.market_scan_manager import MarketScanManager, market_scan_rule_version
from tests.factories import make_kline, make_quote, make_stock_info


SCAN_AS_OF = datetime(2026, 7, 17, 16, 30)
SCAN_DATA_DATE = date(2026, 7, 17)


class _MarketScanHub:
    def __init__(
        self,
        tmp_path: Path,
        *,
        block_klines: asyncio.Event | None = None,
    ) -> None:
        self.settings = Settings(
            cache_path=tmp_path / "market-scan.sqlite3",
            scheduler_enabled=False,
            market_scan_min_universe_count=1,
            market_scan_min_sh_count=1,
            market_scan_min_sz_count=1,
            market_scan_min_bj_count=1,
            market_scan_batch_size=2,
            market_scan_concurrency=2,
            market_scan_symbol_timeout_seconds=2,
            market_scan_retry_attempts=1,
            market_scan_provider_wait_budget_seconds=0,
        )
        self.cache = SQLiteCache(settings=self.settings)
        self.block_klines = block_klines
        self.stock_pool_calls = 0
        self.kline_calls: dict[str, int] = {}
        self.active_klines = 0
        self.max_active_klines = 0
        self.rows = [
            make_stock_info("600001", "SH").model_copy(update={"name": "*ST测试", "list_date": "20260601"}),
            make_stock_info("000001", "SZ").model_copy(update={"name": "停牌样本"}),
            make_stock_info("920066", "BJ").model_copy(update={"name": "北交样本"}),
            make_stock_info("600099", "SH").model_copy(update={"name": "退市样本"}),
        ]
        self.quotes_by_symbol = {
            "600001.SH": _quote_for("600001", "SH", "*ST测试", change_pct=3.2),
            "000001.SZ": _quote_for("000001", "SZ", "停牌样本", change_pct=0.0),
        }
        self.klines_by_symbol = {
            "600001.SH": _daily_rows(
                SCAN_DATA_DATE,
                80,
                last_close=self.quotes_by_symbol["600001.SH"].price,
            ),
            "000001.SZ": _daily_rows(date(2026, 7, 10), 80),
            "920066.BJ": _daily_rows(SCAN_DATA_DATE, 80),
        }

    async def stock_pool(
        self,
        keyword: str | None = None,
        limit: int | None = 5000,
        refresh: bool = False,
        required_markets=None,
        minimum_market_counts=None,
    ) -> list[StockInfo]:
        del keyword, limit, refresh, required_markets, minimum_market_counts
        self.stock_pool_calls += 1
        return self.rows

    async def quotes(self, symbols, use_cache: bool = True) -> list[Quote]:
        del use_cache
        return [self.quotes_by_symbol[symbol] for symbol in symbols if symbol in self.quotes_by_symbol]

    async def partial_quotes(self, symbols, use_cache: bool = True) -> list[Quote]:
        return await self.quotes(symbols, use_cache=use_cache)

    async def partial_quotes_with_errors(
        self,
        symbols,
        use_cache: bool = True,
    ) -> tuple[list[Quote], tuple[str, ...]]:
        quotes = await self.partial_quotes(symbols, use_cache=use_cache)
        returned = {f"{quote.code}.{quote.market}" for quote in quotes}
        errors = ("测试行情源部分缺失",) if set(symbols) - returned else ()
        return quotes, errors

    async def kline(
        self,
        symbol: str,
        limit: int = 120,
        use_cache: bool = True,
        *,
        allow_stale: bool = False,
        require_provider_response: bool = False,
    ) -> list[Kline]:
        del use_cache, allow_stale, require_provider_response
        self.kline_calls[symbol] = self.kline_calls.get(symbol, 0) + 1
        self.active_klines += 1
        self.max_active_klines = max(self.max_active_klines, self.active_klines)
        try:
            if self.block_klines is not None:
                await self.block_klines.wait()
            await asyncio.sleep(0)
            return self.klines_by_symbol[symbol][-limit:]
        finally:
            self.active_klines -= 1


class _ResolutionMarketScanHub(_MarketScanHub):
    def __init__(self, tmp_path: Path, *, stock_pool_reason: str) -> None:
        super().__init__(tmp_path)
        self.stock_pool_reason = stock_pool_reason

    async def stock_pool_resolution(
        self,
        keyword: str | None = None,
        limit: int | None = 5000,
        refresh: bool = False,
        required_markets=None,
        minimum_market_counts=None,
    ) -> StockPoolResolution:
        del keyword, limit, refresh, required_markets, minimum_market_counts
        self.stock_pool_calls += 1
        return StockPoolResolution.hit(self.rows, self.stock_pool_reason)


class _BlockingTaskRunCache(SQLiteCache):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings=settings)
        self.start_entered = threading.Event()
        self.allow_start = threading.Event()

    def start_market_scan_task_run(self, run_id: int, task_name: str) -> int:
        task_run_id = super().start_market_scan_task_run(run_id, task_name)
        self.start_entered.set()
        self.allow_start.wait(timeout=5)
        return task_run_id


async def _wait_for_terminal(scanner: MarketScanManager, run_id: int):
    return await _wait_for_status(
        scanner,
        run_id,
        {"success", "degraded", "failed", "cancelled", "interrupted"},
    )


def _scanner(
    hub: _MarketScanHub,
    *,
    now: datetime = SCAN_AS_OF,
) -> MarketScanManager:
    return MarketScanManager(hub, now=lambda: now)  # type: ignore[arg-type]


def _rule_version(hub: _MarketScanHub) -> str:
    return market_scan_rule_version(hub.settings)


def _configure_clean_full_market(hub: _MarketScanHub) -> None:
    hub.rows = [
        make_stock_info("600001", "SH").model_copy(update={"name": "沪市样本", "list_date": "20000101"}),
        make_stock_info("000001", "SZ").model_copy(update={"name": "深市样本", "list_date": "19910403"}),
        make_stock_info("920066", "BJ").model_copy(update={"name": "北交样本", "list_date": "20200101"}),
    ]
    hub.quotes_by_symbol = {
        "600001.SH": _quote_for("600001", "SH", "沪市样本", change_pct=1.0),
        "000001.SZ": _quote_for("000001", "SZ", "深市样本", change_pct=1.1),
        "920066.BJ": _quote_for("920066", "BJ", "北交样本", change_pct=1.2),
    }
    hub.klines_by_symbol = {
        symbol: _daily_rows(SCAN_DATA_DATE, 80, last_close=quote.price)
        for symbol, quote in hub.quotes_by_symbol.items()
    }


async def _wait_for_status(scanner: MarketScanManager, run_id: int, statuses: set[str]):
    for _attempt in range(200):
        run = scanner.run(run_id)
        if run.status in statuses:
            return run
        await asyncio.sleep(0.01)
    raise AssertionError(f"scan {run_id} did not reach {statuses}")


def _quote_for(code: str, market: str, name: str, *, change_pct: float) -> Quote:
    price = 10.0 * (1 + change_pct / 100)
    return make_quote(
        price=price,
        prev_close=10.0,
        high=max(10.5, price),
        low=9.9,
        change_pct=change_pct,
        turnover_rate=4.2,
        timestamp="2026-07-17 15:00:00",
    ).model_copy(
        update={
            "code": code,
            "market": market,
            "name": name,
            "open": 10.0,
            "amount": 800_000_000,
            "change": price - 10.0,
        }
    )


def _daily_rows(latest: date, count: int, *, last_close: float = 10.3) -> list[Kline]:
    days: list[date] = []
    cursor = latest
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()
    first_close = last_close - (count - 1) * 0.03
    return [
        make_kline(
            date=day.isoformat(),
            close=first_close + index * 0.03,
            volume=1_000_000 + index * 10_000,
            source="测试前复权日K",
            as_of=latest.isoformat(),
            data_version=f"test|qfq|{latest.isoformat()}",
        )
        for index, day in enumerate(days)
    ]
