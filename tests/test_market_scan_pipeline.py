from __future__ import annotations

import asyncio
from pathlib import Path

from app.models.schemas import Kline
from tests.market_scan_test_support import (
    SCAN_AS_OF,
    _MarketScanHub,
    _configure_clean_full_market,
    _scanner,
    _wait_for_terminal,
)


class _PrefetchingHub(_MarketScanHub):
    def __init__(
        self,
        path: Path,
        *,
        block_klines: asyncio.Event | None = None,
    ) -> None:
        super().__init__(path, block_klines=block_klines)
        self.prefetch_batches: list[tuple[str, ...]] = []

    async def prefetch_market_scan_klines(
        self,
        symbols: list[str],
        *,
        limit: int,
    ) -> dict[str, list[Kline]]:
        self.prefetch_batches.append(tuple(symbols))
        return {
            symbol: self.klines_by_symbol.get(symbol, [])[-limit:]
            for symbol in symbols
        }

    async def market_scan_kline_from_prefetch(
        self,
        symbol: str,
        prefetched_cache: list[Kline],
        *,
        limit: int,
        allow_stale: bool,
        require_provider_response: bool,
    ) -> list[Kline]:
        assert prefetched_cache == self.klines_by_symbol.get(symbol, [])[-limit:]
        return await super().kline(
            symbol,
            limit=limit,
            allow_stale=allow_stale,
            require_provider_response=require_provider_response,
        )


def test_next_batch_cache_prefetch_overlaps_current_batch_kline_work(
    tmp_path: Path,
) -> None:
    class OverlapHub(_PrefetchingHub):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.first_kline_started = asyncio.Event()
            self.next_prefetch_finished = asyncio.Event()
            self.first_batch_finished = asyncio.Event()
            self.first_batch_finish_count = 0
            self.events: list[str] = []

        async def prefetch_market_scan_klines(
            self,
            symbols: list[str],
            *,
            limit: int,
        ) -> dict[str, list[Kline]]:
            self.events.append("prefetch")
            if len(self.prefetch_batches) == 1:
                await asyncio.wait_for(self.first_kline_started.wait(), timeout=1)
                self.next_prefetch_finished.set()
            return await super().prefetch_market_scan_klines(symbols, limit=limit)

        async def market_scan_kline_from_prefetch(
            self,
            symbol: str,
            prefetched_cache: list[Kline],
            *,
            limit: int,
            allow_stale: bool,
            require_provider_response: bool,
        ) -> list[Kline]:
            self.events.append("kline")
            first_batch = set(self.prefetch_batches[0])
            if symbol in first_batch:
                self.first_kline_started.set()
                await asyncio.wait_for(self.next_prefetch_finished.wait(), timeout=1)
            rows = await super().market_scan_kline_from_prefetch(
                symbol,
                prefetched_cache,
                limit=limit,
                allow_stale=allow_stale,
                require_provider_response=require_provider_response,
            )
            if symbol in first_batch:
                self.first_batch_finish_count += 1
                if self.first_batch_finish_count == len(first_batch):
                    self.first_batch_finished.set()
            return rows

        async def partial_quotes_with_errors(self, symbols, use_cache: bool = True):
            self.events.append("quote")
            return await super().partial_quotes_with_errors(symbols, use_cache=use_cache)

    async def scenario() -> tuple[OverlapHub, str, list[str]]:
        hub = OverlapHub(tmp_path)
        _configure_clean_full_market(hub)
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        final = await _wait_for_terminal(scanner, started.run.id)
        await scanner.stop()
        leaked = [
            task.get_name()
            for task in asyncio.all_tasks()
            if task.get_name().startswith("market-scan-kline-prefetch-")
            and not task.done()
        ]
        return hub, final.status, leaked

    hub, status, leaked = asyncio.run(scenario())

    assert status == "success"
    assert [len(batch) for batch in hub.prefetch_batches] == [2, 1]
    assert hub.next_prefetch_finished.is_set()
    assert hub.events.count("quote") == 2
    assert max(index for index, event in enumerate(hub.events) if event == "quote") < min(
        index for index, event in enumerate(hub.events) if event in {"prefetch", "kline"}
    )
    assert leaked == []


def test_scan_cancellation_cancels_and_drains_speculative_prefetch(
    tmp_path: Path,
) -> None:
    class CancellationHub(_PrefetchingHub):
        def __init__(self, path: Path, *, block_klines: asyncio.Event) -> None:
            super().__init__(path, block_klines=block_klines)
            self.next_prefetch_started = asyncio.Event()
            self.next_prefetch_cancelled = asyncio.Event()

        async def prefetch_market_scan_klines(
            self,
            symbols: list[str],
            *,
            limit: int,
        ) -> dict[str, list[Kline]]:
            if len(self.prefetch_batches) == 1:
                self.next_prefetch_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.next_prefetch_cancelled.set()
                    raise
            return await super().prefetch_market_scan_klines(symbols, limit=limit)

    async def scenario() -> tuple[CancellationHub, str, list[str]]:
        hub = CancellationHub(tmp_path, block_klines=asyncio.Event())
        _configure_clean_full_market(hub)
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        await asyncio.wait_for(hub.next_prefetch_started.wait(), timeout=1)
        final = await scanner.cancel_scan(started.run.id)
        await scanner.stop()
        leaked = [
            task.get_name()
            for task in asyncio.all_tasks()
            if task.get_name().startswith("market-scan-kline-prefetch-")
            and not task.done()
        ]
        return hub, final.status, leaked

    hub, status, leaked = asyncio.run(scenario())

    assert status == "cancelled"
    assert hub.next_prefetch_cancelled.is_set()
    assert len(hub.prefetch_batches) == 1
    assert leaked == []
