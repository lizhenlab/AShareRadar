from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Iterable
from datetime import date, datetime

from app.models.market_scan import MarketScanResultItem, MarketScanResultWrite, MarketScanRun
from app.models.schemas import Kline, Quote, StockInfo
from app.services.datahub import DataHub
from app.services.datahub_runtime import run_cache_io
from app.services.data_quality_time import latest_expected_daily_kline_date
from app.services.market_scan_completion import quote_batch_error, short_scan_error
from app.services.market_scan_scoring import (
    MarketScanDataMissing,
    MarketScanSkipped,
    completed_market_scan_klines,
    score_market_scan_item,
)
from app.services.market_scan_universe import (
    FULL_MARKET_MARKETS,
    MarketScanUniverse,
    build_market_scan_universe,
)
from app.utils.symbols import standard_symbol


class MarketScanExecutor:
    """Resolve the scan universe and execute quote/K-line scoring batches."""

    def __init__(self, datahub: DataHub, *, sensitive_values: Iterable[object] = ()) -> None:
        self.datahub = datahub
        self.cache = datahub.cache
        self.settings = datahub.settings
        self._sensitive_values = tuple(sensitive_values)

    async def execute(self, run: MarketScanRun, cancel_event: asyncio.Event) -> tuple[str, ...]:
        pending = await self._load_or_seed_pending(run, cancel_event)
        if pending:
            return await self._process_pending(run, pending, cancel_event)
        current = await run_cache_io(self.cache.market_scan_run, run.id)
        if current.total_count == 0 or current.processed_count != current.total_count:
            raise RuntimeError("全市场股票池没有可恢复的待计算股票")
        return ()

    async def _load_or_seed_pending(
        self,
        run: MarketScanRun,
        cancel_event: asyncio.Event,
    ) -> list[MarketScanResultItem]:
        pending = await run_cache_io(self.cache.pending_market_scan_items, run.id)
        if pending:
            if run.retry_of_run_id is None:
                return pending
            universe = await self._validated_stock_pool_universe(run, cancel_event)
            pending_symbols = {item.symbol for item in pending}
            refresh_by_symbol = {seed.symbol: seed for seed in universe.seeds if seed.symbol in pending_symbols}
            missing_symbols = sorted(pending_symbols - set(refresh_by_symbol))
            if missing_symbols:
                examples = "、".join(missing_symbols[:5])
                remainder = f" 等（另有 {len(missing_symbols) - 5} 只）" if len(missing_symbols) > 5 else ""
                raise RuntimeError(f"重试股票池缺少 {len(missing_symbols)} 只待计算股票：{examples}{remainder}")
            await run_cache_io(
                self.cache.refresh_pending_market_scan_metadata,
                run.id,
                list(refresh_by_symbol.values()),
            )
            return await run_cache_io(self.cache.pending_market_scan_items, run.id)
        if run.total_count:
            return pending
        universe = await self._validated_stock_pool_universe(run, cancel_event)
        await run_cache_io(
            self.cache.seed_market_scan_results,
            run.id,
            list(universe.seeds),
            excluded_count=universe.excluded_count,
        )
        return await run_cache_io(self.cache.pending_market_scan_items, run.id)

    async def _validated_stock_pool_universe(
        self,
        run: MarketScanRun,
        cancel_event: asyncio.Event,
    ) -> MarketScanUniverse:
        raise_if_scan_cancelled(cancel_event)
        minimum_counts = minimum_market_counts(self.settings)
        stock_rows, stock_pool_source, resolved = await self._stock_pool_resolution(minimum_counts)
        if stock_pool_source:
            await run_cache_io(
                self.cache.record_market_scan_stock_pool_source,
                run.id,
                stock_pool_source,
            )
        if not resolved:
            raise RuntimeError(f"全市场股票池不可用：{stock_pool_source or 'unknown'}")
        raise_if_scan_cancelled(cancel_event)
        universe = build_market_scan_universe(
            stock_rows,
            data_date=datetime.fromisoformat(run.data_date).date(),
            new_stock_days=self.settings.market_scan_new_stock_days,
        )
        markets = {seed.market for seed in universe.seeds}
        missing_markets = sorted(FULL_MARKET_MARKETS - markets)
        if missing_markets:
            raise RuntimeError(f"全市场股票池缺少市场：{','.join(missing_markets)}")
        if len(universe.seeds) < self.settings.market_scan_min_universe_count:
            raise RuntimeError(f"全市场股票池覆盖不足：有效 {len(universe.seeds)} 只，" f"最低要求 {self.settings.market_scan_min_universe_count} 只")
        market_counts = Counter(seed.market for seed in universe.seeds)
        insufficient = [f"{market} {market_counts[market]}/{minimum}" for market, minimum in minimum_counts.items() if market_counts[market] < minimum]
        if insufficient:
            raise RuntimeError("全市场股票池分市场覆盖不足：" + "，".join(insufficient))
        return universe

    async def _stock_pool_resolution(
        self,
        minimum_counts: dict[str, int],
    ) -> tuple[list[StockInfo], str | None, bool]:
        kwargs = {
            "limit": None,
            "refresh": True,
            "required_markets": FULL_MARKET_MARKETS,
            "minimum_market_counts": minimum_counts,
        }
        resolve = getattr(self.datahub, "stock_pool_resolution", None)
        if not callable(resolve):
            return await self.datahub.stock_pool(**kwargs), None, True
        resolution = await resolve(**kwargs)
        reason = str(getattr(resolution, "reason", "")).strip() or "unknown"
        list_rows = getattr(resolution, "list_rows", None)
        rows = list_rows() if callable(list_rows) else list(getattr(resolution, "rows", ()))
        return rows, reason, bool(getattr(resolution, "resolved", False))

    async def _process_pending(
        self,
        run: MarketScanRun,
        pending: list[MarketScanResultItem],
        cancel_event: asyncio.Event,
    ) -> tuple[str, ...]:
        semaphore = asyncio.Semaphore(self.settings.market_scan_concurrency)
        warnings: list[str] = []
        batch_size = self.settings.market_scan_batch_size
        as_of = datetime.fromisoformat(run.as_of)
        expected_data_date = latest_expected_daily_kline_date(as_of)
        cutoff = expected_data_date
        for index in range(0, len(pending), batch_size):
            raise_if_scan_cancelled(cancel_event)
            batch = pending[index : index + batch_size]
            quote_map, quote_error = await self._quote_batch(batch)
            if quote_error:
                warnings.append(quote_error)
            results = await asyncio.gather(
                *(
                    self._scan_one(
                        item,
                        quote_map.get(item.symbol),
                        quote_error=quote_error,
                        semaphore=semaphore,
                        cancel_event=cancel_event,
                        as_of=as_of,
                        cutoff=cutoff,
                        expected_data_date=expected_data_date,
                    )
                    for item in batch
                )
            )
            raise_if_scan_cancelled(cancel_event)
            await run_cache_io(self.cache.save_market_scan_result_batch, run.id, results)
        return tuple(dict.fromkeys(warnings))

    async def _quote_batch(
        self,
        items: list[MarketScanResultItem],
    ) -> tuple[dict[str, Quote], str | None]:
        symbols = [item.symbol for item in items]
        try:
            available, provider_errors = await asyncio.wait_for(
                self.datahub.partial_quotes_with_errors(symbols, use_cache=True),
                timeout=self.settings.market_scan_quote_batch_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return {}, f"批量行情请求超过 {self.settings.market_scan_quote_batch_timeout_seconds:g} 秒"
        except Exception as exc:
            return {}, short_scan_error(exc, sensitive_values=self._sensitive_values)
        quotes: dict[str, Quote] = {}
        for quote in available:
            try:
                quotes[standard_symbol(f"{quote.code}.{quote.market}")] = quote
            except ValueError:
                continue
        missing_count = len(set(symbols) - quotes.keys())
        error = quote_batch_error(
            missing_count,
            provider_errors,
            sensitive_values=self._sensitive_values,
        )
        return quotes, error

    async def _scan_one(
        self,
        item: MarketScanResultItem,
        quote: Quote | None,
        *,
        quote_error: str | None,
        semaphore: asyncio.Semaphore,
        cancel_event: asyncio.Event,
        as_of: datetime,
        cutoff: date,
        expected_data_date: date,
    ) -> MarketScanResultWrite:
        raise_if_scan_cancelled(cancel_event)
        try:
            async with semaphore:
                rows = await self._fetch_kline(item.symbol, cancel_event)
            if quote is None:
                return self._missing_quote_result(
                    item,
                    rows,
                    cutoff=cutoff,
                    expected_data_date=expected_data_date,
                    quote_error=quote_error,
                )
            return score_market_scan_item(
                item,
                quote,
                rows,
                as_of=as_of,
                completed_cutoff=cutoff,
                expected_data_date=expected_data_date,
                min_history_rows=self.settings.market_scan_min_history_rows,
                min_data_quality_score=self.settings.market_scan_min_data_quality_score,
            )
        except asyncio.CancelledError:
            raise
        except MarketScanSkipped as exc:
            return MarketScanResultWrite(symbol=item.symbol, status="skipped", reason=str(exc))
        except MarketScanDataMissing as exc:
            return MarketScanResultWrite(symbol=item.symbol, status="missing", error=str(exc))
        except Exception as exc:
            return MarketScanResultWrite(
                symbol=item.symbol,
                status="missing",
                error=short_scan_error(exc, sensitive_values=self._sensitive_values),
            )

    async def _fetch_kline(self, symbol: str, cancel_event: asyncio.Event) -> list[Kline]:
        attempts = self.settings.market_scan_retry_attempts
        errors: list[str] = []
        for attempt in range(1, attempts + 1):
            raise_if_scan_cancelled(cancel_event)
            try:
                return await asyncio.wait_for(
                    self.datahub.kline(
                        symbol,
                        limit=self.settings.market_scan_kline_limit,
                        use_cache=True,
                    ),
                    timeout=self.settings.market_scan_symbol_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors.append(short_scan_error(exc, sensitive_values=self._sensitive_values))
                if attempt < attempts:
                    await asyncio.sleep(self.settings.market_scan_retry_backoff_seconds * attempt)
        raise RuntimeError("；".join(dict.fromkeys(errors)) or "日K数据不可用")

    def _missing_quote_result(
        self,
        item: MarketScanResultItem,
        rows: list[Kline],
        *,
        cutoff: date,
        expected_data_date: date,
        quote_error: str | None,
    ) -> MarketScanResultWrite:
        completed = completed_market_scan_klines(rows, cutoff)
        if len(completed) < self.settings.market_scan_min_history_rows:
            return MarketScanResultWrite(
                symbol=item.symbol,
                status="skipped",
                reason=(f"完整前复权日K不足：需要 {self.settings.market_scan_min_history_rows} 根，" f"当前 {len(completed)} 根"),
            )
        if {row.adjustment_mode for row in completed} != {"qfq"}:
            return MarketScanResultWrite(symbol=item.symbol, status="missing", error="日K不是一致的前复权序列")
        latest_date = datetime.fromisoformat(completed[-1].date).date()
        if latest_date < expected_data_date:
            return MarketScanResultWrite(
                symbol=item.symbol,
                status="skipped",
                reason=(f"日K停留在 {latest_date.isoformat()}，早于应有交易日 " f"{expected_data_date.isoformat()}，可能停牌"),
                data_date=latest_date.isoformat(),
                kline_source=completed[-1].source,
                adjustment_mode=completed[-1].adjustment_mode,
            )
        return MarketScanResultWrite(
            symbol=item.symbol,
            status="missing",
            error=quote_error or "报价不可用，无法计算包含换手率和成交额的综合分",
            data_date=latest_date.isoformat(),
            kline_source=completed[-1].source,
            adjustment_mode=completed[-1].adjustment_mode,
        )


def minimum_market_counts(settings: object) -> dict[str, int]:
    return {
        "SH": int(getattr(settings, "market_scan_min_sh_count")),
        "SZ": int(getattr(settings, "market_scan_min_sz_count")),
        "BJ": int(getattr(settings, "market_scan_min_bj_count")),
    }


def raise_if_scan_cancelled(event: asyncio.Event) -> None:
    if event.is_set():
        raise asyncio.CancelledError


__all__ = ["MarketScanExecutor", "minimum_market_counts", "raise_if_scan_cancelled"]
