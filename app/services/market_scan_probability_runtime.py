"""Own probability research warmup, refresh, capture and their real I/O workers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
import logging
from threading import Event as ThreadEvent
from typing import TypeVar
from uuid import uuid4

from app.services.datahub_runtime import run_cache_io
from app.services.advice_review import normalize_review_as_of
from app.services.market_scan_completion import short_scan_error
from app.services.market_scan_contracts import MarketScanCacheProtocol
from app.services.market_scan_joint_execution_maintenance import JointExecutionMaintenanceSummary
from app.services.market_scan_probability_capture import (
    audit_market_scan_probability_source_archives,
    process_market_scan_probability_capture_outbox,
)
from app.services.market_scan_probability_historical_context import (
    HistoricalProbabilityContextError,
    HistoricalProbabilityContextRejectedError,
)
from app.services.market_scan_probability_refresh import ProbabilityResearchRefreshCoordinator
from app.services.market_scan_research_stores import MarketScanResearchStores
from app.utils.clock import ASHARE_TIMEZONE

PROBABILITY_SOURCE_CAPTURE_POLL_SECONDS = 30.0
_ProbabilityWorkerResult = TypeVar("_ProbabilityWorkerResult")


class _HistoricalProbabilityRefreshError(HistoricalProbabilityContextError):
    """An optional historical reference failed and still needs a managed retry."""


class MarketScanProbabilityRuntime:
    """One owner for research background work and fixed research dependencies."""

    def __init__(
        self, cache: MarketScanCacheProtocol, stores: MarketScanResearchStores, *,
        owns_instance_guard: Callable[[], bool], now: Callable[[], datetime],
        sensitive_values: tuple[object, ...] = (),
    ) -> None:
        self.cache = cache
        self._stores = stores
        self._owns_instance_guard = owns_instance_guard
        self._now = now
        self._sensitive_values = sensitive_values
        self._warmup_task: asyncio.Task[None] | None = None
        self._capture_task: asyncio.Task[None] | None = None
        self._activation_lock = asyncio.Lock()
        self._preload_lock = asyncio.Lock()
        self._capture_lock = asyncio.Lock()
        self._capture_wakeup = asyncio.Event()
        self._capture_owner = f"market-scan-manager-{uuid4().hex}"
        self._archives_audited = False
        self._joint_maintenance_active = 0
        self._refresh_stopping = False
        self._refresh_coordinator = ProbabilityResearchRefreshCoordinator(self._resume, self._report_failure)

    async def notify_published(self) -> None:
        self._capture_wakeup.set()
        await self.drain_capture()

    async def refresh(self) -> int:
        """Verify and atomically publish the compact source/outcome/fit index."""
        # Waiters do not occupy I/O threads. Re-read bindings after admission so
        # an outbox capture arriving during a refresh is not silently dropped.
        async with self._preload_lock:
            return await self._refresh()

    async def _refresh(self) -> int:
        self._mark_preloads_pending()
        release_leases = self._acquire_preload_leases()
        cancel_event = ThreadEvent()
        try:
            archive_bindings = (
                await _owned_probability_cache_io(self.cache.probability_source_capture_archive_bindings)
                if self._stores.probability_source is not None else None
            )
            source_task = self._start_source_preload(archive_bindings, cancel_event=cancel_event)
            historical_task = (
                None if self._stores.historical_probability is None else asyncio.create_task(
                    self._preload_history(), name="market-scan-probability-history-preload",
                )
            )
            tasks = tuple(task for task in (source_task, historical_task) if task is not None)
            if tasks:
                await _drain_probability_preloads(tasks, cancel_event)
            return 0 if source_task is None else source_task.result()
        finally:
            self._clear_preloads_pending()
            for release in release_leases:
                release()

    async def _preload_history(self) -> int:
        """An unavailable historical reference cannot disable live capture."""
        historical = self._stores.historical_probability
        if historical is None:
            return 0
        try:
            return await run_cache_io(historical.preload)
        except HistoricalProbabilityContextRejectedError as exc:
            await self._report_failure(exc, historical_reference_only=True)
            return 0
        except HistoricalProbabilityContextError as exc:
            raise _HistoricalProbabilityRefreshError(str(exc)) from exc

    def _start_source_preload(
        self, archive_bindings: dict[int, str] | None, *, cancel_event: ThreadEvent,
    ) -> asyncio.Task[int] | None:
        source = self._stores.probability_source
        if source is None:
            return None
        return asyncio.create_task(
            run_cache_io(source.preload_isolated, archive_bindings=archive_bindings, cancel_event=cancel_event),
            name="market-scan-probability-source-preload",
        )

    async def maintain_joint_execution(
        self,
        *,
        now: datetime | None = None,
    ) -> JointExecutionMaintenanceSummary:
        store = self._stores.joint_probability
        if store is None:
            raise RuntimeError("联合执行概率维护服务不可用")
        current = now or self._now()
        if current.tzinfo is None or current.utcoffset() is None:
            current = current.replace(tzinfo=ASHARE_TIMEZONE)
        else:
            current = current.astimezone(ASHARE_TIMEZONE)
        self._joint_maintenance_active += 1
        task = asyncio.create_task(run_cache_io(store.run, now=current), name="market-scan-joint-probability-maintenance")
        try:
            await _drain_probability_preloads((task,), ThreadEvent())
            return task.result()
        finally:
            self._joint_maintenance_active -= 1

    @property
    def pending(self) -> bool:
        """Cheap admission signal; an idle capture polling loop is not busy."""
        return (
            self._refresh_coordinator.pending
            or (self._warmup_task is not None and not self._warmup_task.done())
            or self._preload_lock.locked()
            or self._capture_lock.locked()
            or self._joint_maintenance_active != 0
        )

    @property
    def status(self) -> dict[str, object]:
        return {"pending": self.pending, **self._refresh_coordinator.status}

    def _start_capture(self) -> None:
        task = self._capture_task
        if task is not None and not task.done():
            return
        self._capture_wakeup.set()
        task = asyncio.create_task(
            self._capture_worker(),
            name="market-scan-probability-source-capture",
        )
        self._capture_task = task
        task.add_done_callback(_consume_stop_exception)

    def start(self) -> None:
        """Warm verified research state without delaying application readiness."""
        self._bind_refresh_requests()
        task = self._warmup_task
        if task is not None and not task.done():
            return
        capture_task = self._capture_task
        if self._archives_audited and capture_task is not None and not capture_task.done():
            return
        self._mark_preloads_pending()
        try:
            task = asyncio.create_task(
                self._warm(),
                name="market-scan-probability-runtime-warmup",
            )
        except Exception:
            self._clear_preloads_pending()
            raise
        self._warmup_task = task
        task.add_done_callback(_consume_stop_exception)

    async def _warm(self) -> None:
        try:
            await self._resume()
            if self._stores.joint_probability is not None:
                await self.maintain_joint_execution(now=normalize_review_as_of(self._now(), allow_future=True))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._report_failure(exc)
            if not self._refresh_stopping:
                self._refresh_coordinator.start()()

    def _bind_refresh_requests(self) -> None:
        self._refresh_stopping = False
        callback = self._refresh_coordinator.start()
        for store in (self._stores.probability_source, self._stores.historical_probability):
            if store is not None:
                store.set_refresh_request(callback)

    async def _resume(self) -> None:
        try:
            await self.refresh()
        except _HistoricalProbabilityRefreshError:
            # Both real workers have drained and the live source succeeded.
            # Activate its outbox, then retain the optional reference's retry.
            await self._activate_capture()
            raise
        await self._activate_capture()

    async def _report_failure(self, exc: Exception, *, historical_reference_only: bool = False) -> None:
        prefix = "历史概率参考不可用，当前来源归档继续：" if historical_reference_only else "上涨概率运行时预热失败："
        message = prefix + short_scan_error(exc, sensitive_values=self._sensitive_values)
        try:
            await _owned_probability_cache_io(self.cache.save_monitor_event, "warning", "research", message[:800])
        except Exception:
            logging.getLogger(__name__).warning("%s；监控事件保存失败", message[:800])

    def _acquire_preload_leases(self) -> tuple[Callable[[], None], ...]:
        releases: list[Callable[[], None]] = []
        for store in (self._stores.probability_source, self._stores.historical_probability):
            if store is not None:
                store.acquire_preload_lease()
                releases.append(store.release_preload_lease)
        return tuple(releases)

    def _mark_preloads_pending(self) -> None:
        for store in (self._stores.probability_source, self._stores.historical_probability):
            if store is not None:
                store.mark_preload_pending()

    def _clear_preloads_pending(self) -> None:
        for store in (self._stores.probability_source, self._stores.historical_probability):
            if store is not None:
                store.clear_preload_pending()

    async def stop_refresh(self) -> None:
        self._refresh_stopping = True
        for store in (self._stores.probability_source, self._stores.historical_probability):
            if store is not None:
                store.set_refresh_request(None)
        try:
            await self._refresh_coordinator.stop()
        finally:
            task = self._warmup_task
            try:
                if task is not None and task is not asyncio.current_task():
                    await _cancel_and_drain_background(task)
            finally:
                if self._warmup_task is task:
                    self._warmup_task = None
                if not self._preload_lock.locked():
                    self._clear_preloads_pending()

    async def _activate_capture(self) -> None:
        if not self._owns_instance_guard():
            return
        async with self._activation_lock:
            if not self._owns_instance_guard():
                return
            await _owned_probability_cache_io(self.cache.reconcile_probability_source_capture_outbox)
            if not self._archives_audited:
                source = self._stores.probability_source
                if source is not None:
                    await _owned_probability_cache_io(
                        self.cache.audit_probability_source_capture_archives, source.verified_archive_digests(),
                    )
                else:
                    await _owned_probability_cache_io(audit_market_scan_probability_source_archives, self.cache)
                self._archives_audited = True
            if self._owns_instance_guard():
                self._start_capture()

    async def stop_capture(self) -> None:
        task = self._capture_task
        if task is None:
            return
        try:
            await _cancel_and_drain_background(task)
        finally:
            if self._capture_task is task:
                self._capture_task = None

    async def _capture_worker(self) -> None:
        while True:
            self._capture_wakeup.clear()
            try:
                await self.drain_capture()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                message = "上涨概率PIT归档outbox处理失败：" f"{short_scan_error(exc, sensitive_values=self._sensitive_values)}"
                try:
                    await _owned_probability_cache_io(
                        self.cache.save_monitor_event,
                        "warning",
                        "research",
                        message[:800],
                    )
                except Exception:
                    pass
            try:
                await asyncio.wait_for(
                    self._capture_wakeup.wait(),
                    timeout=PROBABILITY_SOURCE_CAPTURE_POLL_SECONDS,
                )
            except TimeoutError:
                continue

    async def drain_capture(self) -> dict[str, int]:
        if not self._owns_instance_guard():
            return {"captured": 0, "skipped": 0, "failed": 0}
        async with self._capture_lock:
            summary = await process_market_scan_probability_capture_outbox(
                self.cache,
                owner=self._capture_owner,
                sensitive_values=self._sensitive_values,
            )
            if summary["captured"]:
                await self.refresh()
            return summary


async def _cancel_and_drain_background(task: asyncio.Task[None]) -> None:
    """All concurrent shutdown callers retain the task until its workers settle."""
    if not task.done():
        task.cancel()
    completed = asyncio.gather(task, return_exceptions=True)
    cancelled: asyncio.CancelledError | None = None
    while not completed.done():
        try:
            await asyncio.shield(completed)
        except asyncio.CancelledError as exc:
            cancelled = exc
    if cancelled is not None:
        raise cancelled


async def _owned_probability_cache_io(operation: Callable[..., _ProbabilityWorkerResult], *args: object) -> _ProbabilityWorkerResult:
    """Keep activation and diagnostic writes owned until their thread settles."""
    task = asyncio.create_task(run_cache_io(operation, *args), name="market-scan-probability-owned-cache-io")
    await _drain_probability_preloads((task,), ThreadEvent())
    return task.result()


async def _drain_probability_preloads(
    tasks: tuple[asyncio.Task[_ProbabilityWorkerResult], ...], cancel_event: ThreadEvent,
) -> None:
    # Cancelling to_thread only cancels its awaiter, not the real worker. Shield
    # both workers until they settle; the isolated source worker is cooperatively
    # terminated/reaped, while the finite historical hash read is drained.
    completed = asyncio.gather(*tasks, return_exceptions=True)
    cancelled: asyncio.CancelledError | None = None
    while not completed.done():
        try:
            await asyncio.shield(completed)
        except asyncio.CancelledError as exc:
            cancelled = exc
            cancel_event.set()
    if cancelled is not None:
        raise cancelled
    for result in completed.result():
        if isinstance(result, BaseException):
            raise result


def _consume_stop_exception(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        pass
