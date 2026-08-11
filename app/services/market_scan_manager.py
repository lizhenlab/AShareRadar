from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
import math
from pathlib import Path
import sqlite3
import threading
from typing import Literal

from app.models.market_scan import (
    MARKET_SCAN_TOP100_REFRESH_LIMIT,
    MarketScanFilterValues,
    MarketScanMode,
    MarketScanResultPage,
    MarketScanResultStatus,
    MarketScanRetryPlan,
    MarketScanRun,
    MarketScanRunPage,
    MarketScanRunStatus,
    MarketScanSortOrderValues,
    MarketScanSortValues,
    MarketScanStartResponse,
    MarketScanTrigger,
    is_market_scan_top100_refresh_scope,
)
from app.repositories.market_scan import ACTIVE_SCAN_STATUSES, RETRYABLE_SCAN_STATUSES
from app.services.advice_review import normalize_review_as_of
from app.services.datahub_runtime import run_cache_io
from app.services.data_quality_time import latest_expected_daily_kline_date
from app.services.instance_guard import InstanceGuard
from app.services.market_scan_completion import (
    MARKET_SCAN_MAX_SNAPSHOT_SPAN_SECONDS,
    MARKET_SCAN_PUBLISH_MIN_COVERAGE,
    MARKET_SCAN_PUBLISH_MIN_ELIGIBLE_RATIO,
    MARKET_SCAN_SCORE_DISTRIBUTION_POLICY,
    MarketScanFinalizer,
    sensitive_setting_values,
)
from app.services.market_scan_contracts import MarketScanDataHubProtocol
from app.services.market_scan_execution import MarketScanExecutor
from app.services.market_scan_export import (
    PUBLISHED_MARKET_SCAN_STATUSES,
    MarketScanExportFilters,
    MarketScanWorkbookExport,
    build_market_scan_workbook,
)
from app.services.market_scan_future_range_store import (
    FutureRangeResearchUnavailable,
    MarketScanFutureRangeStore,
    not_generated_future_range_research,
)
from app.services.market_scan_lifecycle import MarketScanLifecycle, MarketScanStopSnapshot
from app.services.market_scan_modes import (
    OFFICIAL_SCAN_WINDOW_MESSAGE,
    market_scan_temporal_contract,
)
from app.services.market_scan_probability_research import PROBABILITY_PRIMARY_TARGET
from app.services.market_scan_probability_store import (
    MarketScanProbabilityStore,
    ProbabilityFilterUnavailable,
    not_generated_probability_research,
)
from app.services.market_scan_automation import MarketScanAutomaticAction
from app.services.market_scan_automation_runner import MarketScanAutomationCoordinator
from app.services.market_scan_scoring import market_scan_score_spec, stable_score_spec_hash
from app.services.market_scan_universe import FULL_MARKET_SCOPE
from app.services.trading_calendar import DAILY_KLINE_PUBLISH_TIME, is_trading_day
from app.utils.clock import market_now
from app.utils.time import datetime_to_text


MARKET_SCAN_TASK_NAME = "full_market_scan"
MARKET_SCAN_TASK_LABEL = "全市场A股扫描"
MARKET_SCAN_INSTANCE_GUARD_BUSY_MESSAGE = "已有其他进程负责全市场扫描，本进程不能修改扫描任务"
HISTORICAL_SCAN_UNAVAILABLE_MESSAGE = "当前数据源只提供当前快照；历史榜单只能读取已持久化快照，不能新建历史扫描"
DAILY_BAR_WINDOW_MESSAGE = OFFICIAL_SCAN_WINDOW_MESSAGE
TERMINAL_RECOVERY_MESSAGE = "本地扫描任务已退出，终态写入失败后自动中断；可从断点重试"
TERMINAL_RECOVERY_ERROR = "本地后台扫描已退出，但原终态未能持久化"
_MARKET_SCAN_RESULT_QUERY_FIELDS = (
    "page", "page_size", "status", "market", "industry", "is_st", "is_new",
    "min_score", "max_score", "min_trend_score", "max_trend_score",
    "min_change_pct", "max_change_pct", "min_turnover_rate", "max_turnover_rate",
    "min_amount", "max_amount", "min_data_quality_score", "max_data_quality_score",
    "min_confidence", "max_risk", "min_tradability", "keyword", "symbols", "sort", "order",
)


class MarketScanManager:
    """Public facade that coordinates scan lifecycle, execution and persistence."""

    def __init__(
        self,
        datahub: MarketScanDataHubProtocol,
        *,
        instance_guard: InstanceGuard | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.datahub = datahub
        self.cache = datahub.cache
        self.settings = datahub.settings
        cache_path = getattr(self.cache, "path", None)
        self._probability_store = (
            MarketScanProbabilityStore(Path(cache_path).parent / "market-scan-probability")
            if isinstance(cache_path, str | Path)
            else None
        )
        self._future_range_store = (
            MarketScanFutureRangeStore(
                Path(cache_path).parent / "research" / "market_scan_future_range"
            )
            if isinstance(cache_path, str | Path)
            else None
        )
        self._now = now or _market_now
        sensitive_values = sensitive_setting_values(self.settings)
        self._sensitive_values = sensitive_values
        self._executor = MarketScanExecutor(
            datahub,
            sensitive_values=sensitive_values,
            now=self._current_time,
        )
        self._finalizer = MarketScanFinalizer(self.cache, sensitive_values=sensitive_values)
        self._lifecycle = MarketScanLifecycle(self.cache, instance_guard=instance_guard)
        self._automation = MarketScanAutomationCoordinator(
            datahub,
            sensitive_values=sensitive_values,
        )
        self._deferred_stop_task: asyncio.Task[None] | None = None
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
        mode: MarketScanMode = "official",
    ) -> MarketScanStartResponse:
        current = self._current_time()
        response = await self._create_scan(
            as_of=as_of,
            trigger=trigger,
            mode=mode,
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
        mode: MarketScanMode,
        current: datetime,
        busy_is_noop: bool,
    ) -> MarketScanStartResponse | None:
        normalized_as_of = normalize_review_as_of(as_of, now=current)
        temporal = market_scan_temporal_contract(normalized_as_of, mode)
        if as_of is not None:
            current_temporal = market_scan_temporal_contract(current, mode)
            if (
                temporal.data_date != current_temporal.data_date
                or temporal.quote_date != current_temporal.quote_date
            ):
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
                    mode=mode,
                    rule_version=market_scan_rule_version(self.settings, mode=mode),
                    as_of=datetime_to_text(normalized_as_of),
                    data_date=temporal.data_date.isoformat(),
                    quote_date=temporal.quote_date.isoformat(),
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
        return await self._retry_scan(run_id, current=self._current_time())

    async def refresh_top100_scores(self, run_id: int) -> MarketScanStartResponse:
        current = self._current_time()
        async with self._lifecycle.lock:
            self._lifecycle.require_open()
            await self._lifecycle.require_instance_guard(MARKET_SCAN_INSTANCE_GUARD_BUSY_MESSAGE)
            await run_cache_io(self._recover_terminal_persistence_failures, run_id)
            source = await run_cache_io(self.cache.market_scan_run, run_id)
            self._validate_top100_refresh_source(source, current=current)
            active = await run_cache_io(self.cache.active_market_scan_run)
            if active is not None:
                return MarketScanStartResponse(accepted=False, deduplicated=True, run=active)
            temporal = market_scan_temporal_contract(current, source.mode)
            try:
                run = await run_cache_io(
                    self.cache.prepare_market_scan_top100_refresh,
                    source.id,
                    rule_version=source.rule_version,
                    as_of=datetime_to_text(current),
                    data_date=temporal.data_date.isoformat(),
                    quote_date=temporal.quote_date.isoformat(),
                    limit=MARKET_SCAN_TOP100_REFRESH_LIMIT,
                )
            except sqlite3.IntegrityError:
                active = await run_cache_io(self.cache.active_market_scan_run)
                if active is None:
                    raise
                return MarketScanStartResponse(accepted=False, deduplicated=True, run=active)
            self._launch(run.id)
            return MarketScanStartResponse(accepted=True, run=run)

    async def _retry_scan(
        self,
        run_id: int,
        *,
        current: datetime,
    ) -> MarketScanStartResponse:
        async with self._lifecycle.lock:
            self._lifecycle.require_open()
            await self._lifecycle.require_instance_guard(MARKET_SCAN_INSTANCE_GUARD_BUSY_MESSAGE)
            await run_cache_io(self._recover_terminal_persistence_failures, run_id)
            candidate = await run_cache_io(self.cache.market_scan_run, run_id)
            retry_plan = await run_cache_io(self.cache.market_scan_retry_plan, run_id)
            if retry_plan.needs_market_data:
                market_scan_temporal_contract(current, candidate.mode)
            self._validate_retry_candidate(candidate, retry_plan, current=current)
            active = await run_cache_io(self.cache.active_market_scan_run)
            if active is not None:
                return MarketScanStartResponse(accepted=False, deduplicated=True, run=active)
            try:
                run = await run_cache_io(
                    self.cache.prepare_market_scan_retry,
                    run_id,
                    retry_plan,
                    as_of=datetime_to_text(current),
                )
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
        if not await self._claim_automatic_ownership():
            return None
        data_date = latest_expected_daily_kline_date(current).isoformat()
        return await self._automation.run(
            current=current,
            data_date=data_date,
            start_action=self._start_automatic_action,
            validate_retry=self._validate_automatic_retry,
        )

    async def _claim_automatic_ownership(self) -> bool:
        async with self._lifecycle.lock:
            acquired, _reconciled = await self._lifecycle.ensure_instance_guard()
            if not acquired:
                return False
            await run_cache_io(self._recover_terminal_persistence_failures)
            return True

    def _validate_automatic_retry(
        self,
        run: MarketScanRun,
        retry_plan: MarketScanRetryPlan,
        current: datetime,
    ) -> None:
        self._validate_retry_candidate(run, retry_plan, current=current)

    async def _start_automatic_action(
        self,
        action: MarketScanAutomaticAction,
        current: datetime,
    ) -> MarketScanStartResponse:
        if action.kind == "retry" and action.source_run_id is not None:
            return await self._retry_scan(action.source_run_id, current=current)
        response = await self._create_scan(
            as_of=current,
            trigger="scheduled",
            mode="official",
            current=current,
            busy_is_noop=True,
        )
        if response is None:
            raise RuntimeError(MARKET_SCAN_INSTANCE_GUARD_BUSY_MESSAGE)
        return response

    def run(self, run_id: int) -> MarketScanRun:
        self._recover_terminal_persistence_failures(run_id)
        return self.cache.market_scan_run(run_id)

    def latest_run(self, *, mode: MarketScanMode | None = None) -> MarketScanRun | None:
        self._recover_terminal_persistence_failures()
        return self.cache.latest_market_scan_run(mode=mode)

    def latest_published_run(self, *, mode: MarketScanMode | None = None) -> MarketScanRun | None:
        self._recover_terminal_persistence_failures()
        return self.cache.latest_published_market_scan_run(mode=mode)

    def next_automatic_run_at(self) -> datetime | None:
        return self._automation.next_due_at

    def runs(
        self,
        *,
        page: int,
        page_size: int,
        mode: MarketScanMode | None = None,
        status: MarketScanRunStatus | Literal["published"] | None = None,
        data_date: str | None = None,
    ) -> MarketScanRunPage:
        self._recover_terminal_persistence_failures()
        return self.cache.market_scan_runs(
            page=page,
            page_size=page_size,
            mode=mode,
            status=status,
            data_date=data_date,
        )

    def results(
        self,
        run_id: int,
        *,
        page: int,
        page_size: int,
        status: MarketScanResultStatus | None,
        market: MarketScanFilterValues,
        industry: MarketScanFilterValues,
        is_st: bool | None,
        is_new: bool | None,
        min_score: int | None = None,
        max_score: int | None = None,
        min_trend_score: int | None = None,
        max_trend_score: int | None = None,
        min_change_pct: float | None = None,
        max_change_pct: float | None = None,
        min_turnover_rate: float | None = None,
        max_turnover_rate: float | None = None,
        min_amount: float | None = None,
        max_amount: float | None = None,
        min_data_quality_score: int | None,
        max_data_quality_score: int | None = None,
        min_confidence: float | None = None,
        max_risk: float | None = None,
        min_tradability: float | None = None,
        keyword: str | None,
        sort: MarketScanSortValues,
        order: MarketScanSortOrderValues,
        probability_horizon: Literal[1, 5, 20] = 5,
        min_upside_probability: float | None = None,
    ) -> MarketScanResultPage:
        self._recover_terminal_persistence_failures(run_id)
        research = self._run_probability_research(run_id)
        filter_probabilities = (
            self._run_probability_projection(run_id)[1]
            if min_upside_probability is not None
            else {}
        )
        symbols = _probability_filter_symbols(
            research,
            filter_probabilities,
            horizon=probability_horizon,
            minimum=min_upside_probability,
        )
        query = _market_scan_result_query(locals())
        page_result = self.cache.market_scan_results(run_id, **query)  # type: ignore[arg-type]
        page_symbols = tuple(item.symbol for item in page_result.items)
        _summary, probabilities = self._run_probability_projection(run_id, symbols=page_symbols)
        return _attach_probability_projection(page_result, research, probabilities)

    def probability_research(self, run_id: int) -> dict[str, object]:
        self.run(run_id)
        return self._run_probability_research(run_id)

    def future_range_research(
        self,
        run_id: int,
        *,
        page: int = 1,
        page_size: int = 100,
        session_offset: Literal[1, 2, 3] | None = None,
        symbol: str | None = None,
        include_research: bool = True,
    ) -> dict[str, object]:
        # This endpoint is deliberately stricter than the lifecycle-oriented
        # ``run`` facade: it must never perform terminal-state recovery writes.
        run = self.cache.market_scan_run(run_id)
        _require_future_range_eligible_run(run)
        if self._future_range_store is None:
            return not_generated_future_range_research(run_id)
        return self._future_range_store.research_projection(
            run_id,
            page=page,
            page_size=page_size,
            session_offset=session_offset,
            symbol=symbol,
            include_research=include_research,
        )

    def _run_probability_research(self, run_id: int) -> dict[str, object]:
        if self._probability_store is None:
            return not_generated_probability_research(run_id)
        return self._probability_store.research_projection(run_id)

    def _run_probability_projection(
        self,
        run_id: int,
        *,
        symbols: tuple[str, ...] | None = None,
    ) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
        if self._probability_store is None:
            return not_generated_probability_research(run_id), {}
        return self._probability_store.run_projection(run_id, symbols=symbols)

    def export_results(
        self,
        run_id: int,
        *,
        filters: MarketScanExportFilters,
    ) -> MarketScanWorkbookExport:
        filters = filters.normalized()
        run = self.run(run_id)
        if run.status not in PUBLISHED_MARKET_SCAN_STATUSES:
            raise ValueError("只有已发布的全市场榜单可以导出 Excel")
        page = self.results(
            run_id,
            page=1,
            page_size=max(1, run.total_count),
            status=filters.status,
            market=filters.market,
            industry=filters.industry,
            is_st=filters.is_st,
            is_new=filters.is_new,
            min_score=filters.min_score,
            max_score=filters.max_score,
            min_trend_score=filters.min_trend_score,
            max_trend_score=filters.max_trend_score,
            min_change_pct=filters.min_change_pct,
            max_change_pct=filters.max_change_pct,
            min_turnover_rate=filters.min_turnover_rate,
            max_turnover_rate=filters.max_turnover_rate,
            min_amount=filters.min_amount,
            max_amount=filters.max_amount,
            min_data_quality_score=filters.min_data_quality_score,
            max_data_quality_score=filters.max_data_quality_score,
            min_confidence=filters.min_confidence,
            max_risk=filters.max_risk,
            min_tradability=filters.min_tradability,
            keyword=filters.keyword,
            sort=filters.sort,
            order=filters.order,
            probability_horizon=filters.probability_horizon,
            min_upside_probability=filters.min_upside_probability,
        )
        future_range_store = getattr(self, "_future_range_store", None)
        future_range = (
            future_range_store.export_projection(run_id)
            if (
                run.mode == "official"
                and run.scope == FULL_MARKET_SCOPE
                and future_range_store is not None
            )
            else not_generated_future_range_research(run_id)
        )
        return build_market_scan_workbook(
            page,
            filters,
            exported_at=self._current_time(),
            future_range_research=future_range,
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
            await run_cache_io(
                self.cache.update_market_scan_observability,
                run_id,
                stage="publication",
                message="正在执行发布门槛验证",
            )
            current = await run_cache_io(self.cache.market_scan_run, run_id)
            degraded_count = await run_cache_io(
                self.cache.market_scan_degraded_result_count,
                run_id,
            )
            publication_summary = await run_cache_io(
                self.cache.market_scan_repo.publication_summary,
                run_id,
            ) if not is_market_scan_top100_refresh_scope(current.scope) else None
            persisted = await self._finalizer.finish_completed(
                current,
                degraded_count=degraded_count,
                warnings=warnings,
                publication_summary=publication_summary,
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
        pressure_warnings = self._executor.pressure_warnings
        failure = (
            RuntimeError(f"{exc}；{pressure_warnings[0]}")
            if pressure_warnings
            else exc
        )
        persisted = await self._finalizer.finish_failed(run_id, failure)
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

    def _validate_retry_candidate(
        self,
        run: MarketScanRun,
        plan: MarketScanRetryPlan,
        *,
        current: datetime,
    ) -> None:
        if run.status not in RETRYABLE_SCAN_STATUSES:
            raise ValueError(f"扫描批次 {run.id} 当前状态不能重试：{run.status}")
        if plan.rule_version != run.rule_version:
            raise ValueError("扫描批次规则指纹在重试准备期间发生变化，请重新获取状态后再试")
        effective_rule_version = market_scan_rule_version(self.settings, mode=run.mode)
        if run.rule_version != effective_rule_version:
            raise ValueError("扫描规则/评分配置已变更，请新建扫描；旧批次将保留为历史快照")
        self._validate_retry_data_date(run, plan, current=current)

    def _validate_top100_refresh_source(
        self,
        run: MarketScanRun,
        *,
        current: datetime,
    ) -> None:
        if run.status not in {"success", "degraded"}:
            raise ValueError(f"扫描批次 {run.id} 尚未发布，不能快速更新 TOP100")
        if run.success_count <= 0:
            raise ValueError(f"扫描批次 {run.id} 没有有效排名，不能快速更新 TOP100")
        effective_rule_version = market_scan_rule_version(self.settings, mode=run.mode)
        if run.rule_version != effective_rule_version:
            raise ValueError("评分规则已经变更，请先执行新的全市场扫描，再快速更新 TOP100")
        temporal = market_scan_temporal_contract(current, run.mode)
        expected_data_date = temporal.data_date.isoformat()
        expected_quote_date = temporal.quote_date.isoformat()
        if run.data_date != expected_data_date or run.quote_date != expected_quote_date:
            raise ValueError(
                f"源批次日K/行情日期 {run.data_date}/{run.quote_date} 已过期，"
                f"当前应为 {expected_data_date}/{expected_quote_date}；请先执行新的全市场扫描"
            )

    def _validate_retry_data_date(
        self,
        run: MarketScanRun,
        plan: MarketScanRetryPlan,
        *,
        current: datetime,
    ) -> None:
        if not plan.needs_market_data:
            return
        temporal = market_scan_temporal_contract(current, run.mode)
        current_data_date = temporal.data_date.isoformat()
        current_quote_date = temporal.quote_date.isoformat()
        if run.data_date != current_data_date or run.quote_date != current_quote_date:
            raise ValueError(
                f"批次日K/行情日期 {run.data_date}/{run.quote_date} 已过期，"
                f"当前应为 {current_data_date}/{current_quote_date}；"
                "请新建扫描，旧批次将保留为历史快照"
            )

    def _current_time(self, value: datetime | None = None) -> datetime:
        return normalize_review_as_of(value if value is not None else self._now(), allow_future=True)


def _probability_filter_symbols(
    research: dict[str, object],
    probabilities: dict[str, dict[str, object]],
    *,
    horizon: Literal[1, 5, 20],
    minimum: float | None,
) -> tuple[str, ...] | None:
    if minimum is None:
        return None
    if not math.isfinite(minimum) or not 0 <= minimum <= 1:
        raise ValueError("最低上涨概率必须在 0 到 1 之间")
    summary = _probability_summary(research, horizon)
    if summary.get("status") != "calibrated_shadow":
        raise ProbabilityFilterUnavailable("当前批次与周期尚无已校准 Shadow 概率，不能使用概率筛选")
    return tuple(
        symbol
        for symbol, horizons in probabilities.items()
        if _meets_probability_minimum(horizons, horizon, minimum)
    )


def _require_future_range_eligible_run(run: MarketScanRun) -> None:
    if run.mode != "official":
        raise FutureRangeResearchUnavailable("未来区间研究仅支持盘后正式批次")
    if run.scope != FULL_MARKET_SCOPE:
        raise FutureRangeResearchUnavailable("未来区间研究仅支持盘后正式全市场批次")
    if run.status not in PUBLISHED_MARKET_SCAN_STATUSES:
        raise FutureRangeResearchUnavailable("未来区间研究仅支持已发布批次")


def _market_scan_result_query(values: dict[str, object]) -> dict[str, object]:
    return {name: values[name] for name in _MARKET_SCAN_RESULT_QUERY_FIELDS}


def _probability_summary(research: dict[str, object], horizon: int) -> dict[str, object]:
    horizons = research.get("horizons")
    targets = horizons.get(str(horizon)) if isinstance(horizons, dict) else None
    summary = targets.get(PROBABILITY_PRIMARY_TARGET) if isinstance(targets, dict) else None
    return summary if isinstance(summary, dict) else {}


def _meets_probability_minimum(
    horizons: dict[str, object],
    horizon: int,
    minimum: float,
) -> bool:
    targets = horizons.get(str(horizon))
    record = targets.get(PROBABILITY_PRIMARY_TARGET) if isinstance(targets, dict) else None
    probability = record.get("probability") if isinstance(record, dict) else None
    return (
        isinstance(record, dict)
        and record.get("status") == "calibrated_shadow"
        and isinstance(probability, int | float)
        and not isinstance(probability, bool)
        and math.isfinite(float(probability))
        and float(probability) >= minimum
    )


def _attach_probability_projection(
    page: MarketScanResultPage,
    research: dict[str, object],
    probabilities: dict[str, dict[str, object]],
) -> MarketScanResultPage:
    items = [
        item.model_copy(update={"upside_probabilities": probabilities.get(item.symbol, {})})
        for item in page.items
    ]
    return page.model_copy(update={"items": items, "probability_research": research})


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


def market_scan_rule_version(
    settings: object,
    *,
    mode: MarketScanMode = "official",
) -> str:
    contract = {
        "schema_version": 6,
        "mode": {
            "id": mode,
            "quote_date": "current-trading-day" if mode == "intraday" else "completed-daily-bar-date",
            "daily_kline_cutoff": "previous-completed-trading-day" if mode == "intraday" else "same-completed-trading-day",
            "quote_kline_consistency": "previous-close" if mode == "intraday" else "same-day-close",
        },
        "score_spec": market_scan_score_spec(
            min_data_quality_score=int(getattr(settings, "market_scan_min_data_quality_score")),
        ),
        "history": {
            "adjustment_mode": "qfq",
            "kline_limit": int(getattr(settings, "market_scan_kline_limit")),
            "min_history_rows": int(getattr(settings, "market_scan_min_history_rows")),
        },
        "universe": {
            "classifier": "current-listed-a-share-v2",
            "markets": ["SH", "SZ", "BJ"],
            "authoritative_baseline_min_retain_ratio": 0.98,
            "new_stock_days": int(getattr(settings, "market_scan_new_stock_days")),
        },
        "publication": {
            "coverage_denominator_statuses": ["success", "missing"],
            "excluded_denominator_statuses": ["skipped"],
            "minimum_coverage": MARKET_SCAN_PUBLISH_MIN_COVERAGE,
            "minimum_eligible_ratio": MARKET_SCAN_PUBLISH_MIN_ELIGIBLE_RATIO,
            "quote_capture_scope": "all-quote-chunks-before-klines",
            "quote_capture_envelope": "required",
            "quote_observed_at": "per-provider-response-batch",
            "event_time_span_scope": "per-market",
            "global_event_time_span": "diagnostic-only",
            "max_snapshot_span_seconds": MARKET_SCAN_MAX_SNAPSHOT_SPAN_SECONDS,
            "score_distribution": MARKET_SCAN_SCORE_DISTRIBUTION_POLICY.spec(),
        },
    }
    return f"full-market-scan-v6:{stable_score_spec_hash(contract)}"


def _market_now() -> datetime:
    return market_now()


__all__ = [
    "HISTORICAL_SCAN_UNAVAILABLE_MESSAGE",
    "MARKET_SCAN_INSTANCE_GUARD_BUSY_MESSAGE",
    "MARKET_SCAN_TASK_LABEL",
    "MARKET_SCAN_TASK_NAME",
    "MarketScanManager",
    "market_scan_rule_version",
]
