from __future__ import annotations

import asyncio
from collections.abc import Callable
from threading import Lock
from typing import TypeVar

from app.api.errors import MarketScanHeavyReadBusy, run_sync_api_async


T = TypeVar("T")


class MarketScanHeavyReadAdmission:
    """Process-local, non-authorizing admission for snapshot-verifying reads."""

    def __init__(self) -> None:
        self._slot = Lock()
        self._worker_guard = Lock()
        self._workers: set[asyncio.Task[object]] = set()
        self._closing = False

    @property
    def active_count(self) -> int:
        return int(self._slot.locked())

    @property
    def worker_count(self) -> int:
        with self._worker_guard:
            return len(self._workers)

    def _start(self, call: Callable[[], T]) -> asyncio.Task[T] | None:
        with self._worker_guard:
            if self._closing or not self._slot.acquire(blocking=False):
                return None
            lease = _MarketScanHeavyReadLease(self)
            try:
                worker = asyncio.create_task(_run_and_release(lease, call))
            except Exception:
                lease.release()
                raise
            self._workers.add(worker)
            worker.add_done_callback(self._finish_worker)
            return worker

    async def aclose(self) -> None:
        """Stop admitting reads and wait for every owned verifier worker."""

        with self._worker_guard:
            self._closing = True
        drain = asyncio.create_task(self._drain())
        try:
            await asyncio.shield(drain)
        except asyncio.CancelledError:
            await asyncio.shield(drain)
            raise

    async def _drain(self) -> None:
        while True:
            with self._worker_guard:
                workers = tuple(self._workers)
            if not workers:
                return
            await asyncio.gather(*(asyncio.shield(worker) for worker in workers), return_exceptions=True)

    def _release(self) -> None:
        self._slot.release()

    def _finish_worker(self, worker: asyncio.Task[object]) -> None:
        try:
            if not worker.cancelled():
                worker.exception()
        finally:
            with self._worker_guard:
                self._workers.discard(worker)


class _MarketScanHeavyReadLease:
    def __init__(self, owner: MarketScanHeavyReadAdmission) -> None:
        self._owner: MarketScanHeavyReadAdmission | None = owner

    def release(self) -> None:
        owner, self._owner = self._owner, None
        if owner is not None:
            owner._release()


async def run_admitted_market_scan_read(
    admission: MarketScanHeavyReadAdmission,
    call: Callable[[], T],
) -> T:
    """Run one fresh heavy read without queueing or reusing authorization state."""

    worker = admission._start(call)
    if worker is None:
        raise MarketScanHeavyReadBusy()
    return await asyncio.shield(worker)


async def _run_and_release(lease: _MarketScanHeavyReadLease, call: Callable[[], T]) -> T:
    try:
        return await run_sync_api_async(call)
    finally:
        lease.release()
