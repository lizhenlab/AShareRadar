from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
import math
import sqlite3
import threading

from app.models.market_scan import (
    MarketScanResultPage,
    MarketScanResultStatus,
    MarketScanRetryPlan,
    MarketScanRun,
    MarketScanRunPage,
    MarketScanSort,
    MarketScanSortOrder,
    MarketScanStartResponse,
    MarketScanTrigger,
)
from app.repositories.market_scan import ACTIVE_SCAN_STATUSES, RETRYABLE_SCAN_STATUSES
from app.services.advice_review import normalize_review_as_of
from app.services.datahub import DataHub
from app.services.datahub_runtime import run_cache_io
from app.services.data_quality_time import latest_expected_daily_kline_date
from app.services.instance_guard import InstanceGuard
from app.services.market_scan_completion import MarketScanFinalizer, sensitive_setting_values
from app.services.market_scan_execution import MarketScanExecutor
from app.services.market_scan_lifecycle import MarketScanLifecycle, MarketScanStopSnapshot
from app.services.market_scan_scoring import FULL_MARKET_SCORE_RULE_VERSION
from app.services.market_scan_universe import FULL_MARKET_SCOPE
from app.services.trading_calendar import DAILY_KLINE_PUBLISH_TIME, is_trading_day
from app.utils.market_time import ASHARE_TIMEZONE
from app.utils.time import datetime_to_text


MARKET_SCAN_TASK_NAME = "full_market_scan"
MARKET_SCAN_TASK_LABEL = "全市场A股扫描"
MARKET_SCAN_INSTANCE_GUARD_BUSY_MESSAGE = "已有其他进程负责全市场扫描，本进程不能修改扫描任务"
HISTORICAL_SCAN_UNAVAILABLE_MESSAGE = "当前数据源只提供当前快照；历史榜单只能读取已持久化快照，不能新建历史扫描"
TERMINAL_RECOVERY_MESSAGE = "本地扫描任务已退出，终态写入失败后自动中断；可从断点重试"
TERMINAL_RECOVERY_ERROR = "本地后台扫描已退出，但原终态未能持久化"


class MarketScanManager:
    """Public facade that coordinates scan lifecycle, execution and persistence."""

    def __init__(
        self,
        datahub: DataHub,
        *,
        instance_guard: InstanceGuard | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.datahub = datahub
        self.cache = datahub.cache
        self.settings = datahub.settings
        sensitive_values = sensitive_setting_values(self.settings)
        self._executor = MarketScanExecutor(datahub, sensitive_values=sensitive_values)
        self._finalizer = MarketScanFinalizer(self.cache, sensitive_values=sensitive_values)
        self._lifecycle = MarketScanLifecycle(self.cache, instance_guard=instance_guard)
        self._deferred_stop_task: asyncio.Task[None] | None = None
        self._now = now or _market_now
        self._terminal_failure_lock = threading.Lock()
        self._terminal_failure_run_ids: set[int] = set()

    async def start(self) -> int:
        reconciled = await self._lifecycle.start()
        await run_cache_io(self._recover_terminal_persistence_failures)
        return reconciled

    @property
    def is_quiescent(self) -> bool:
        return self._lifecycle.is_quiescent

    async def wait_until_quiescent(self) -> None:
        await self._lifecycle.wait_until_quiescent()

    async def stop(self) -> None:
        await self._run_stop(close=True, task_name="market-scan-manager-stop")

    async def rollback_activation(self) -> None:
        """Undo partial activation while keeping this manager restartable."""

        await self._run_stop(close=False, task_name="market-scan-activation-rollback")

    async def _run_stop(self, *, close: bool, task_name: str) -> None:
        cleanup = asyncio.create_task(self._stop(close=close), name=task_name)
        cleanup.add_done_callback(_consume_stop_exception)
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await asyncio.shield(cleanup)
            raise

    async def _stop(self, *, close: bool) -> None:
        await run_cache_io(self._recover_terminal_persistence_failures)
        snapshot = await self._lifecycle.begin_stop(close=close)
        if snapshot is None:
            return
        pending: set[asyncio.Task[None]] = set()
        if snapshot.tasks:
            _done, pending = await asyncio.wait(
                snapshot.tasks,
                timeout=_market_scan_shutdown_timeout(self.settings),
            )
        if pending:
            deferred = asyncio.create_task(
                self._finish_stop(snapshot),
                name="market-scan-deferred-stop",
            )
            self._deferred_stop_task = deferred
            deferred.add_done_callback(_consume_stop_exception)
            return
        await self._finish_stop(snapshot)

    async def _finish_stop(self, snapshot: MarketScanStopSnapshot) -> None:
        current_task = asyncio.current_task()
        try:
            if snapshot.tasks:
                await asyncio.gather(*snapshot.tasks, return_exceptions=True)
            for run_id in snapshot.run_ids:
                current = await run_cache_io(self.cache.market_scan_run, run_id)
                if current.status in {"queued", "running", "cancelling"}:
                    await self._finish_interrupted(run_id)
        finally:
            try:
                await self._lifecycle.finish_stop()
            finally:
                if self._deferred_stop_task is current_task:
                    self._deferred_stop_task = None

    async def create_scan(
        self,
        *,
        as_of: datetime | None = None,
        trigger: MarketScanTrigger = "manual",
    ) -> MarketScanStartResponse:
        current = self._current_time()
        response = await self._create_scan(
            as_of=as_of,
            trigger=trigger,
            current=current,
            busy_is_noop=False,
        )
        if response is None:
            raise RuntimeError(MARKET_SCAN_INSTANCE_GUARD_BUSY_MESSAGE)
        return response

    async def _create_scan(
        self,
        *,
        as_of: datetime | None,
        trigger: MarketScanTrigger,
        current: datetime,
        busy_is_noop: bool,
    ) -> MarketScanStartResponse | None:
        normalized_as_of = normalize_review_as_of(as_of, now=current)
        if is_trading_day(normalized_as_of.date()) and normalized_as_of.time() < DAILY_KLINE_PUBLISH_TIME:
            raise ValueError("全市场扫描仅使用已完成日线，请在交易日 15:15 后启动")
        data_date = latest_expected_daily_kline_date(normalized_as_of)
        if as_of is not None and data_date != latest_expected_daily_kline_date(current):
            raise ValueError(HISTORICAL_SCAN_UNAVAILABLE_MESSAGE)
        self._validate_settings()
        async with self._lifecycle.lock:
            self._lifecycle.require_open()
            acquired, _reconciled = await self._lifecycle.ensure_instance_guard()
            if not acquired:
                if busy_is_noop:
                    return None
                raise RuntimeError(MARKET_SCAN_INSTANCE_GUARD_BUSY_MESSAGE)
            await run_cache_io(self._recover_terminal_persistence_failures)
            active = await run_cache_io(self.cache.active_market_scan_run)
            if active is not None:
                return MarketScanStartResponse(accepted=False, deduplicated=True, run=active)
            try:
                run = await run_cache_io(
                    self.cache.create_market_scan_run,
                    trigger=trigger,
                    rule_version=market_scan_rule_version(self.settings),
                    as_of=datetime_to_text(normalized_as_of),
                    data_date=data_date.isoformat(),
                    scope=FULL_MARKET_SCOPE,
                )
            except sqlite3.IntegrityError:
                active = await run_cache_io(self.cache.active_market_scan_run)
                if active is None:
                    raise
                return MarketScanStartResponse(accepted=False, deduplicated=True, run=active)
            self._launch(run.id)
            return MarketScanStartResponse(accepted=True, run=run)

    async def retry_scan(self, run_id: int) -> MarketScanStartResponse:
        async with self._lifecycle.lock:
            self._lifecycle.require_open()
            await self._lifecycle.require_instance_guard(MARKET_SCAN_INSTANCE_GUARD_BUSY_MESSAGE)
            await run_cache_io(self._recover_terminal_persistence_failures, run_id)
            candidate = await run_cache_io(self.cache.market_scan_run, run_id)
            retry_plan = await run_cache_io(self.cache.market_scan_retry_plan, run_id)
            self._validate_retry_candidate(candidate, retry_plan)
            active = await run_cache_io(self.cache.active_market_scan_run)
            if active is not None:
                return MarketScanStartResponse(accepted=False, deduplicated=True, run=active)
            try:
                run = await run_cache_io(self.cache.prepare_market_scan_retry, run_id, retry_plan)
            except (sqlite3.IntegrityError, ValueError):
                active = await run_cache_io(self.cache.active_market_scan_run)
                if active is None:
                    raise
                return MarketScanStartResponse(accepted=False, deduplicated=True, run=active)
            self._launch(run.id)
            return MarketScanStartResponse(accepted=True, run=run)

    async def cancel_scan(self, run_id: int) -> MarketScanRun:
        async with self._lifecycle.lock:
            self._lifecycle.require_open()
            await self._lifecycle.require_instance_guard(MARKET_SCAN_INSTANCE_GUARD_BUSY_MESSAGE)
            await run_cache_io(self._recover_terminal_persistence_failures, run_id)
            await run_cache_io(self.cache.request_market_scan_cancel, run_id)
            task = self._lifecycle.cancel_local(run_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        current = await run_cache_io(self.cache.market_scan_run, run_id)
        if current.status == "cancelling":
            await self._finish_cancelled(run_id)
            current = await run_cache_io(self.cache.market_scan_run, run_id)
        return current

    async def scheduled_tick(self, now: datetime | None = None) -> MarketScanStartResponse | None:
        if not self.settings.market_scan_auto_enabled:
            return None
        self._lifecycle.require_open()
        current = self._current_time(now)
        configured_time = (
            self.settings.market_scan_schedule_hour,
            self.settings.market_scan_schedule_minute,
        )
        publish_time = (DAILY_KLINE_PUBLISH_TIME.hour, DAILY_KLINE_PUBLISH_TIME.minute)
        if not is_trading_day(current.date()) or (current.hour, current.minute) < max(
            configured_time,
            publish_time,
        ):
            return None
        async with self._lifecycle.lock:
            acquired, _reconciled = await self._lifecycle.ensure_instance_guard()
            if not acquired:
                return None
            await run_cache_io(self._recover_terminal_persistence_failures)
        data_date = latest_expected_daily_kline_date(current).isoformat()
        latest = await run_cache_io(self.cache.latest_market_scan_run)
        if (
            latest is not None
            and latest.data_date == data_date
            and latest.status
            in {
                "queued",
                "running",
                "cancelling",
                "success",
                "degraded",
                "cancelled",
            }
        ):
            return None
        if latest is not None and latest.data_date == data_date and latest.trigger in {"scheduled", "retry"}:
            return None
        return await self._create_scan(
            as_of=current,
            trigger="scheduled",
            current=current,
            busy_is_noop=True,
        )

    def run(self, run_id: int) -> MarketScanRun:
        self._recover_terminal_persistence_failures(run_id)
        return self.cache.market_scan_run(run_id)

    def latest_run(self) -> MarketScanRun | None:
        self._recover_terminal_persistence_failures()
        return self.cache.latest_market_scan_run()

    def runs(self, *, page: int, page_size: int) -> MarketScanRunPage:
        self._recover_terminal_persistence_failures()
        return self.cache.market_scan_runs(page=page, page_size=page_size)

    def results(
        self,
        run_id: int,
        *,
        page: int,
        page_size: int,
        status: MarketScanResultStatus | None,
        market: str | None,
        industry: str | None,
        is_st: bool | None,
        is_new: bool | None,
        min_data_quality_score: int | None,
        keyword: str | None,
        sort: MarketScanSort,
        order: MarketScanSortOrder,
    ) -> MarketScanResultPage:
        self._recover_terminal_persistence_failures(run_id)
        return self.cache.market_scan_results(
            run_id,
            page=page,
            page_size=page_size,
            status=status,
            market=market,
            industry=industry,
            is_st=is_st,
            is_new=is_new,
            min_data_quality_score=min_data_quality_score,
            keyword=keyword,
            sort=sort,
            order=order,
        )

    def _launch(self, run_id: int) -> None:
        self._lifecycle.launch(run_id, self._execute_run)

    async def _execute_run(self, run_id: int, cancel_event: asyncio.Event) -> None:
        try:
            await run_cache_io(
                self.cache.start_market_scan_task_run,
                run_id,
                MARKET_SCAN_TASK_NAME,
            )
            run = await run_cache_io(self.cache.start_market_scan_run, run_id)
            warnings = await self._executor.execute(run, cancel_event)
            current = await run_cache_io(self.cache.market_scan_run, run_id)
            degraded_count = await run_cache_io(
                self.cache.market_scan_degraded_result_count,
                run_id,
            )
            persisted = await self._finalizer.finish_completed(
                current,
                degraded_count=degraded_count,
                warnings=warnings,
            )
            self._track_terminal_persistence(run_id, persisted)
        except asyncio.CancelledError:
            finish = self._finish_interrupted if self._lifecycle.closed else self._finish_cancelled
            await asyncio.shield(finish(run_id))
            raise
        except Exception as exc:
            await self._finish_failed(run_id, exc)

    async def _finish_cancelled(self, run_id: int) -> None:
        persisted = await self._finalizer.finish_cancelled(run_id)
        self._track_terminal_persistence(run_id, persisted)

    async def _finish_interrupted(self, run_id: int) -> None:
        persisted = await self._finalizer.finish_interrupted(run_id)
        self._track_terminal_persistence(run_id, persisted)

    async def _finish_failed(self, run_id: int, exc: Exception) -> None:
        persisted = await self._finalizer.finish_failed(run_id, exc)
        self._track_terminal_persistence(run_id, persisted)

    def _track_terminal_persistence(self, run_id: int, persisted: bool) -> None:
        with self._terminal_failure_lock:
            if persisted:
                self._terminal_failure_run_ids.discard(run_id)
            else:
                self._terminal_failure_run_ids.add(run_id)

    def _recover_terminal_persistence_failures(self, run_id: int | None = None) -> int:
        if not self._owns_terminal_recovery_lease():
            return 0
        with self._terminal_failure_lock:
            candidates = tuple(
                candidate
                for candidate in self._terminal_failure_run_ids
                if run_id is None or candidate == run_id
            )
        local_active = set(self._lifecycle.active_run_ids)
        recovered = 0
        for candidate in candidates:
            if candidate in local_active:
                continue
            try:
                current = self.cache.market_scan_run(candidate)
                if current.status in ACTIVE_SCAN_STATUSES:
                    current = self.cache.finish_market_scan_run(
                        candidate,
                        "interrupted",
                        message=TERMINAL_RECOVERY_MESSAGE,
                        error=TERMINAL_RECOVERY_ERROR,
                    )
            except Exception:
                continue
            if current.status not in ACTIVE_SCAN_STATUSES:
                self._track_terminal_persistence(candidate, True)
                recovered += 1
        return recovered

    def _owns_terminal_recovery_lease(self) -> bool:
        if self._lifecycle.closed or not bool(getattr(self._lifecycle, "_guard_acquired", False)):
            return False
        guard = getattr(self._lifecycle, "_instance_guard", None)
        acquire = getattr(guard, "acquire", None)
        if not callable(acquire):
            return False
        try:
            return bool(acquire())
        except Exception:
            return False

    def _validate_settings(self) -> None:
        if self.settings.market_scan_min_history_rows > self.settings.market_scan_kline_limit:
            raise ValueError("全市场扫描最少历史行数不能大于K线抓取行数")

    def _validate_retry_candidate(self, run: MarketScanRun, plan: MarketScanRetryPlan) -> None:
        if run.status not in RETRYABLE_SCAN_STATUSES:
            raise ValueError(f"扫描批次 {run.id} 当前状态不能重试：{run.status}")
        effective_rule_version = market_scan_rule_version(self.settings)
        if run.rule_version != effective_rule_version:
            raise ValueError("扫描规则/评分配置已变更，请新建扫描；旧批次将保留为历史快照")
        self._validate_retry_data_date(run, plan)

    def _validate_retry_data_date(self, run: MarketScanRun, plan: MarketScanRetryPlan) -> None:
        if not plan.needs_market_data:
            return
        current_data_date = latest_expected_daily_kline_date(self._current_time()).isoformat()
        if run.data_date != current_data_date:
            raise ValueError(f"批次数据日期 {run.data_date} 已过期，当前完整交易日为 {current_data_date}；" "请新建扫描，旧批次将保留为历史快照")

    def _current_time(self, value: datetime | None = None) -> datetime:
        return normalize_review_as_of(value if value is not None else self._now(), allow_future=True)


def _market_scan_shutdown_timeout(settings: object) -> float:
    try:
        timeout = float(getattr(settings, "scheduler_shutdown_timeout_seconds", 5.0))
    except (TypeError, ValueError):
        return 5.0
    return timeout if math.isfinite(timeout) and timeout > 0 else 5.0


def _consume_stop_exception(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


def market_scan_rule_version(settings: object) -> str:
    return "|".join(
        (
            FULL_MARKET_SCORE_RULE_VERSION,
            f"kline_limit={int(getattr(settings, 'market_scan_kline_limit'))}",
            f"min_history_rows={int(getattr(settings, 'market_scan_min_history_rows'))}",
            f"min_data_quality_score={int(getattr(settings, 'market_scan_min_data_quality_score'))}",
            f"new_stock_days={int(getattr(settings, 'market_scan_new_stock_days'))}",
        )
    )


def _market_now() -> datetime:
    return datetime.now(ASHARE_TIMEZONE)


__all__ = [
    "HISTORICAL_SCAN_UNAVAILABLE_MESSAGE",
    "MARKET_SCAN_INSTANCE_GUARD_BUSY_MESSAGE",
    "MARKET_SCAN_TASK_LABEL",
    "MARKET_SCAN_TASK_NAME",
    "MarketScanManager",
    "market_scan_rule_version",
]
