from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import TracebackType

from app.models.market import Kline
from app.models.market_scan import MarketScanResultItem


KlinePrefetch = Callable[
    [list[MarketScanResultItem]],
    Awaitable[dict[str, list[Kline]] | None],
]


class MarketScanKlinePrefetchPipeline:
    """Keep at most one future batch's preservation-cache read in flight."""

    def __init__(
        self,
        batches: list[list[MarketScanResultItem]],
        prefetch: KlinePrefetch | None,
    ) -> None:
        self._batches = batches
        self._prefetch = prefetch
        self._task: asyncio.Task[dict[str, list[Kline]] | None] | None = None

    async def __aenter__(self) -> MarketScanKlinePrefetchPipeline:
        self._task = self._start(0)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        await _cancel_and_drain(self._task)

    async def take(self, position: int) -> dict[str, list[Kline]] | None:
        current = self._task
        prefetched = await current if current is not None else None
        self._task = self._start(position + 1)
        return prefetched

    def _start(
        self,
        position: int,
    ) -> asyncio.Task[dict[str, list[Kline]] | None] | None:
        if self._prefetch is None or position >= len(self._batches):
            return None
        return asyncio.create_task(
            _run_prefetch(self._prefetch, self._batches[position]),
            name=f"market-scan-kline-prefetch-{position + 1}",
        )


async def _run_prefetch(
    prefetch: KlinePrefetch,
    items: list[MarketScanResultItem],
) -> dict[str, list[Kline]] | None:
    return await prefetch(items)


async def _cancel_and_drain(task: asyncio.Task[object] | None) -> None:
    if task is None:
        return
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


__all__ = ["MarketScanKlinePrefetchPipeline"]
