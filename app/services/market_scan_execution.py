from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import date, datetime

from app.models.market_scan import (
    MarketScanResultItem,
    MarketScanResultWrite,
    MarketScanRun,
    MarketScanStage,
    is_market_scan_top100_refresh_scope,
)
from app.models.market import Kline, Quote
from app.services.datahub_runtime import run_cache_io
from app.services.market_scan_completion import bulk_quote_coverage_error, quote_batch_error, short_scan_error
from app.services.market_scan_batch_evaluation import evaluate_market_scan_batch
from app.services.market_scan_contracts import (
    MarketScanDataHubProtocol,
    MarketScanKlinePrefetchProtocol,
)
from app.services.market_scan_pressure import MarketScanPressureController, MarketScanPressureSnapshot
from app.services.market_scan_prefetch import MarketScanKlinePrefetchPipeline
from app.services.market_scan_quote_capture import MarketScanFrozenQuoteBatch, capture_market_scan_quote_batches
from app.services.market_scan_quote_provenance import normalized_quote_batch
from app.services.market_scan_recovery import ProviderWaitBudget, wait_for_provider_recovery
from app.services.market_scan_stock_evaluation import MarketScanStockEvaluator
from app.services.market_scan_universe import FULL_MARKET_MARKETS, MarketScanUniverse, build_market_scan_universe
from app.services.market_scan_validation import (
    MarketScanRuntimeGuard,
    minimum_market_counts,
    raise_batch_outcome_error,
    raise_if_scan_cancelled,
    resolve_market_scan_stock_pool,
)
from app.utils.clock import monotonic_now
from app.utils.provider_errors import ProviderChainUnavailable


class MarketScanExecutor:
    """Resolve the scan universe and execute quote/K-line scoring batches."""

    def __init__(
        self,
        datahub: MarketScanDataHubProtocol,
        *,
        sensitive_values: Iterable[object] = (),
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.datahub = datahub
        self.cache = datahub.cache
        self.settings = datahub.settings
        self._sensitive_values = tuple(sensitive_values)
        self._now = now
        self._monotonic = monotonic or monotonic_now
        self._pressure = MarketScanPressureController.from_datahub(self.datahub)
        self._work_duration_ms: Counter[str] = Counter()
        self._kline_prefetch = (
            datahub if isinstance(datahub, MarketScanKlinePrefetchProtocol) else None
        )
        self._stock_evaluator = MarketScanStockEvaluator(
            datahub,
            self._pressure,
            self._work_duration_ms,
            sensitive_values=self._sensitive_values,
            monotonic=self._monotonic,
            kline_prefetch=self._kline_prefetch,
        )

    @property
    def pressure_snapshot(self) -> MarketScanPressureSnapshot:
        return self._pressure.snapshot()

    @property
    def pressure_warnings(self) -> tuple[str, ...]:
        return self._pressure.terminal_warnings([])

    async def execute(self, run: MarketScanRun, cancel_event: asyncio.Event) -> tuple[str, ...]:
        self._pressure.reset()
        self._work_duration_ms.clear()
        runtime_guard = MarketScanRuntimeGuard.create(
            datetime.fromisoformat(run.data_date).date(),
            datetime.fromisoformat(run.quote_date).date(),
            run.mode,
            self.settings,
            now=self._now,
            monotonic=self._monotonic,
        )
        wall_clock_budget = runtime_guard.wall_clock_budget_seconds
        try:
            async with asyncio.timeout(wall_clock_budget):
                if run.retry_of_run_id is None or run.processed_count < run.total_count:
                    runtime_guard.checkpoint()
                pending = await self._load_or_seed_pending(run, cancel_event)
                if pending:
                    runtime_guard.checkpoint()
                    return await self._process_pending(
                        run,
                        pending,
                        cancel_event,
                        runtime_guard,
                    )
                current = await run_cache_io(self.cache.market_scan_run, run.id)
                if current.total_count == 0 or current.processed_count != current.total_count:
                    raise RuntimeError("全市场股票池没有可恢复的待计算股票")
                return ()
        except TimeoutError:
            raise RuntimeError(f"全市场扫描超过 {wall_clock_budget:g} 秒墙钟预算") from None

    async def _load_or_seed_pending(
        self,
        run: MarketScanRun,
        cancel_event: asyncio.Event,
    ) -> list[MarketScanResultItem]:
        pending = await run_cache_io(self.cache.pending_market_scan_items, run.id)
        if pending:
            if is_market_scan_top100_refresh_scope(run.scope):
                return pending
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
        stock_rows, stock_pool_source, resolved = await resolve_market_scan_stock_pool(
            self.datahub,
            required_markets=FULL_MARKET_MARKETS,
            minimum_counts=minimum_counts,
        )
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

    async def _process_pending(
        self,
        run: MarketScanRun,
        pending: list[MarketScanResultItem],
        cancel_event: asyncio.Event,
        runtime_guard: MarketScanRuntimeGuard,
    ) -> tuple[str, ...]:
        warnings: list[str] = []
        batch_size = self.settings.market_scan_batch_size
        provider_wait_budget = ProviderWaitBudget(
            remaining_seconds=self.settings.market_scan_provider_wait_budget_seconds,
        )
        as_of = datetime.fromisoformat(run.as_of)
        expected_data_date = date.fromisoformat(run.data_date)
        expected_quote_date = date.fromisoformat(run.quote_date)
        cutoff = expected_data_date
        batches = [pending[index : index + batch_size] for index in range(0, len(pending), batch_size)]
        quote_capture, quote_warnings = await capture_market_scan_quote_batches(
            run,
            batches,
            cache=self.cache,
            settings=self.settings,
            pressure=self._pressure,
            fetch_quote_batch=self._quote_batch,
            record_stage=self._stage,
            wait_for_recovery=self._wait_for_provider_recovery,
            cancel_event=cancel_event,
            provider_wait_budget=provider_wait_budget,
            runtime_guard=runtime_guard,
            now=self._now,
            monotonic=self._monotonic,
        )
        warnings.extend(quote_warnings)
        kline_batches = [list(batch.items) for batch in quote_capture.batches]
        prefetch = self._prefetch_kline_cache if self._kline_prefetch is not None else None
        async with MarketScanKlinePrefetchPipeline(kline_batches, prefetch) as pipeline:
            for position, frozen_batch in enumerate(quote_capture.batches):
                runtime_guard.checkpoint()
                raise_if_scan_cancelled(cancel_event)
                prefetched_klines = await pipeline.take(position)
                batch_warnings = await self._process_batch(
                    run,
                    frozen_batch,
                    cancel_event=cancel_event,
                    as_of=as_of,
                    cutoff=cutoff,
                    expected_data_date=expected_data_date,
                    expected_quote_date=expected_quote_date,
                    provider_wait_budget=provider_wait_budget,
                    initial_prefetched_klines=prefetched_klines,
                )
                warnings.extend(batch_warnings)
        runtime_guard.checkpoint()
        return self._pressure.terminal_warnings(warnings)

    async def _process_batch(
        self,
        run: MarketScanRun,
        batch: MarketScanFrozenQuoteBatch,
        *,
        cancel_event: asyncio.Event, as_of: datetime, cutoff: date,
        expected_data_date: date, expected_quote_date: date,
        provider_wait_budget: ProviderWaitBudget,
        initial_prefetched_klines: dict[str, list[Kline]] | None,
    ) -> tuple[str, ...]:
        remaining = list(batch.items)
        warnings: list[str] = []
        max_attempts = self.settings.market_scan_batch_retry_attempts
        for attempt in range(1, max_attempts + 1):
            raise_if_scan_cancelled(cancel_event)
            evaluation_as_of = max(as_of, batch.evaluation_as_of)
            semaphore = asyncio.Semaphore(self._pressure.current_concurrency)
            await self._stage(run.id, "klines", items=len(remaining), message=f"正在获取日K并评分（{len(remaining)} 只）")
            prefetched_klines = (
                initial_prefetched_klines
                if attempt == 1
                else await self._prefetch_kline_cache(remaining)
            )
            retry_pairs = await self._scan_and_persist_batch(
                run,
                remaining,
                quote_map=dict(batch.quote_map), quote_error=batch.quote_error,
                quote_observed_at=batch.quote_observed_at,
                semaphore=semaphore, cancel_event=cancel_event,
                as_of=evaluation_as_of, cutoff=cutoff,
                expected_data_date=expected_data_date, expected_quote_date=expected_quote_date,
                prefetched_klines=prefetched_klines,
            )
            if not retry_pairs:
                self._pressure.complete_batch()
                return tuple(dict.fromkeys(warnings))
            retry_errors = tuple(outcome for _item, outcome in retry_pairs)
            decision = self._pressure.observe_kline_failures(retry_errors, len(remaining))
            await self._wait_for_provider_recovery(
                retry_errors, kind="kline", attempt=attempt, max_attempts=max_attempts,
                wait_budget=provider_wait_budget, cancel_event=cancel_event,
                minimum_delay_seconds=decision.minimum_delay_seconds,
            )
            remaining = [item for item, _outcome in retry_pairs]
        raise RuntimeError("全市场扫描批次重试状态异常")

    async def _scan_and_persist_batch(
        self,
        run: MarketScanRun,
        items: list[MarketScanResultItem],
        *,
        quote_map: dict[str, Quote],
        quote_error: str | None,
        quote_observed_at: str,
        semaphore: asyncio.Semaphore,
        cancel_event: asyncio.Event,
        as_of: datetime,
        cutoff: date,
        expected_data_date: date,
        expected_quote_date: date,
        prefetched_klines: dict[str, list[Kline]] | None,
    ) -> list[tuple[MarketScanResultItem, ProviderChainUnavailable]]:
        kline_work_before = self._work_duration_ms["klines"]
        scoring_work_before = self._work_duration_ms["scoring"]
        outcomes = await evaluate_market_scan_batch(
            self._stock_evaluator,
            run,
            items,
            quote_map=quote_map,
            quote_error=quote_error,
            semaphore=semaphore,
            cancel_event=cancel_event,
            as_of=as_of,
            cutoff=cutoff,
            expected_data_date=expected_data_date,
            expected_quote_date=expected_quote_date,
            prefetched_klines=prefetched_klines,
        )
        raise_batch_outcome_error(outcomes)
        writes = [
            replace(outcome, quote_observed_at=quote_observed_at)
            for outcome in outcomes
            if isinstance(outcome, MarketScanResultWrite)
        ]
        await self._stage(
            run.id,
            "persistence",
            items=len(writes),
            work_metrics={
                "klines": (self._work_duration_ms["klines"] - kline_work_before, 0),
                "scoring": (self._work_duration_ms["scoring"] - scoring_work_before, len(writes)),
            },
            message=f"正在持久化批次结果（{len(writes)} 只）",
        )
        if writes:
            raise_if_scan_cancelled(cancel_event)
            await run_cache_io(self.cache.save_market_scan_result_batch, run.id, writes)
        return [(item, outcome) for item, outcome in zip(items, outcomes, strict=True) if isinstance(outcome, ProviderChainUnavailable)]

    async def _wait_for_provider_recovery(
        self,
        errors: tuple[ProviderChainUnavailable, ...],
        *,
        kind: str,
        attempt: int,
        max_attempts: int,
        wait_budget: ProviderWaitBudget,
        cancel_event: asyncio.Event,
        minimum_delay_seconds: float = 0.0,
    ) -> None:
        remaining_before = wait_budget.remaining_seconds
        try:
            await wait_for_provider_recovery(
                self._pressure.recovery_errors(errors, minimum_delay_seconds),
                kind=kind,
                attempt=attempt,
                max_attempts=max_attempts,
                wait_budget=wait_budget,
                cancel_event=cancel_event,
                retry_backoff_seconds=self.settings.market_scan_retry_backoff_seconds,
                chain_state=self._pressure.provider_chain_state,
            )
        finally:
            self._pressure.record_backoff(remaining_before - wait_budget.remaining_seconds)

    async def _quote_batch(
        self,
        items: list[MarketScanResultItem],
        *,
        use_cache: bool = True,
        require_provider_response: bool = False,
    ) -> tuple[dict[str, Quote], str | None]:
        symbols = [item.symbol for item in items]
        try:
            available, provider_errors = await asyncio.wait_for(
                self.datahub.partial_quotes_with_errors(symbols, use_cache=use_cache),
                timeout=self.settings.market_scan_quote_batch_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            message = f"批量行情请求超过 {self.settings.market_scan_quote_batch_timeout_seconds:g} 秒"
            raise self._pressure.unavailable_error(exc, message) from None
        except Exception as exc:
            message = short_scan_error(exc, sensitive_values=self._sensitive_values)
            raise self._pressure.unavailable_error(exc, message) from exc
        quotes, provider_errors, cached_count = normalized_quote_batch(
            available,
            provider_errors,
            require_provider_response=require_provider_response,
        )
        requested_count = len(set(symbols))
        missing_count = len(set(symbols) - quotes.keys())
        chain_state = self._pressure.provider_chain_state("quote")
        chain_status = getattr(chain_state, "status", None)
        if missing_count and chain_status in {
            "temporary_unavailable",
            "permanent_unavailable",
        }:
            raise ProviderChainUnavailable(
                "实时报价数据源当前不可用或仍有调用未结束",
                retry_after_seconds=getattr(chain_state, "retry_after_seconds", None),
            )
        coverage_error = bulk_quote_coverage_error(len(quotes), requested_count)
        if coverage_error:
            if require_provider_response and cached_count:
                coverage_error += f"；其中 {cached_count} 条缓存报价未计入实时快照"
            raise ProviderChainUnavailable(
                coverage_error,
                retry_after_seconds=self.settings.market_scan_retry_backoff_seconds,
            )
        error = quote_batch_error(
            missing_count,
            provider_errors,
            sensitive_values=self._sensitive_values,
        )
        return quotes, error

    async def _stage(
        self,
        run_id: int,
        stage: MarketScanStage,
        *,
        items: int = 0,
        work_metrics: dict[MarketScanStage, tuple[int, int]] | None = None,
        message: str | None = None,
    ) -> None:
        await run_cache_io(
            self.cache.update_market_scan_observability,
            run_id,
            stage=stage,
            stage_items=items,
            work_metrics=work_metrics,
            message=message,
        )

    async def _prefetch_kline_cache(
        self,
        items: list[MarketScanResultItem],
    ) -> dict[str, list[Kline]] | None:
        if self._kline_prefetch is None:
            return None
        return await self._kline_prefetch.prefetch_market_scan_klines(
            [item.symbol for item in items],
            limit=self.settings.market_scan_kline_limit,
        )

    async def _fetch_kline(
        self,
        symbol: str,
        cancel_event: asyncio.Event,
        *,
        prefetched_cache: list[Kline] | None = None,
    ) -> list[Kline]:
        return await self._stock_evaluator.fetch_kline(
            symbol,
            cancel_event,
            prefetched_cache=prefetched_cache,
        )

    def _missing_quote_result(
        self,
        item: MarketScanResultItem,
        rows: list[Kline],
        *,
        cutoff: date,
        expected_data_date: date,
        quote_error: str | None,
    ) -> MarketScanResultWrite:
        return self._stock_evaluator.missing_quote_result(
            item,
            rows,
            cutoff=cutoff,
            expected_data_date=expected_data_date,
            quote_error=quote_error,
        )

__all__ = ["MarketScanExecutor", "minimum_market_counts", "raise_if_scan_cancelled"]
