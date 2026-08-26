from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
import math
import sqlite3
from threading import Event as ThreadEvent
from typing import Literal, cast
from uuid import uuid4

from app.models.market_scan import (
    MARKET_SCAN_TOP100_REFRESH_LIMIT,
    MarketScanAutomaticState,
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
from app.models.market_scan_delta import MarketScanDeltaResponse
from app.models.market_scan_polling import MarketScanPollingIdentity
from app.models.market_scan_screening import (
    MarketBreadthV1,
    MarketScanScreenEvaluateRequest,
    MarketScanScreenEvaluationV1,
)
from app.repositories.market_scan import RETRYABLE_SCAN_STATUSES
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
    MarketScanPublicationValidationError,
    sensitive_setting_values,
    short_scan_error,
)
from app.services.market_scan_contracts import MarketScanCacheProtocol, MarketScanDataHubProtocol
from app.services.market_scan_execution import MarketScanExecutor
from app.services.market_scan_execution_quote import (
    MARKET_SCAN_EXECUTION_QUOTE_CONTRACT_VERSION,
    MARKET_SCAN_EXECUTION_QUOTE_SCHEMA_VERSION,
)
from app.services.market_scan_export import (
    MarketScanExportFilters,
    MarketScanWorkbookExport,
    build_market_scan_workbook,
)
from app.services.market_scan_lifecycle import MarketScanLifecycle, MarketScanStopSnapshot
from app.services.market_scan_modes import (
    MarketScanTemporalContract,
    OFFICIAL_SCAN_WINDOW_MESSAGE,
    market_scan_temporal_contract,
)
from app.services.market_scan_probability_capture import (
    audit_market_scan_probability_source_archives,
    process_market_scan_probability_capture_outbox,
)
from app.services.market_scan_query_service import MarketScanQueryService
from app.services.market_scan_delta import MarketScanDeltaRepositoryProtocol, MarketScanDeltaService
from app.services.market_scan_research_stores import MarketScanResearchStores
from app.services.market_scan_joint_execution_maintenance import (
    JointExecutionMaintenanceSummary,
    MarketScanJointExecutionMaintenanceService,
)
from app.services.market_scan_automation import MarketScanAutomaticAction
from app.services.market_scan_automation_runner import MarketScanAutomationCoordinator
from app.services.market_scan_scoring import (
    MARKET_SCAN_PRODUCTION_SCORE_SEMANTICS,
    market_scan_score_spec,
    stable_score_spec_hash,
)
from app.services.market_scan_terminal_recovery import MarketScanTerminalRecovery
from app.services.market_scan_universe import FULL_MARKET_SCOPE
from app.services.trading_calendar import DAILY_KLINE_PUBLISH_TIME, is_trading_day
from app.utils.clock import ASHARE_TIMEZONE, market_now
from app.utils.time import datetime_to_text


MARKET_SCAN_TASK_NAME = "full_market_scan"
MARKET_SCAN_TASK_LABEL = "全市场A股扫描"
MARKET_SCAN_INSTANCE_GUARD_BUSY_MESSAGE = "已有其他进程负责全市场扫描，本进程不能修改扫描任务"
HISTORICAL_SCAN_UNAVAILABLE_MESSAGE = "当前数据源只提供当前快照；历史榜单只能读取已持久化快照，不能新建历史扫描"
DAILY_BAR_WINDOW_MESSAGE = OFFICIAL_SCAN_WINDOW_MESSAGE
PROBABILITY_SOURCE_CAPTURE_POLL_SECONDS = 30.0
AUTOMATIC_NO_ACTION_AUDIT_INTERVAL = timedelta(minutes=5)
_JOINT_RESEARCH_SETTING_NAMES = (
    "market_scan_official_execution_registry_path",
    "market_scan_official_execution_registry_digest",
    "market_scan_official_execution_raw_root",
    "market_scan_official_execution_session_directory",
    "market_scan_joint_execution_authorization_path",
    "market_scan_joint_execution_authorization_digest",
    "market_scan_probability_ranking_control_path",
    "market_scan_probability_ranking_control_digest",
)


def _automatic_state_is_terminal_for_date(
    state: MarketScanAutomaticState | None,
    *,
    data_date: str,
) -> bool:
    return state is not None and state.data_date == data_date and state.status in {"success", "degraded", "cancelled"}


def _automatic_audit_is_deferred(checked_at: datetime, *, current: datetime) -> bool:
    return checked_at <= current < checked_at + AUTOMATIC_NO_ACTION_AUDIT_INTERVAL


def _manager_research_stores(datahub: MarketScanDataHubProtocol) -> MarketScanResearchStores:
    cache_path = getattr(datahub.cache, "path", None)
    if not all(hasattr(datahub.settings, name) for name in _JOINT_RESEARCH_SETTING_NAMES):
        return MarketScanResearchStores.for_cache_path(cache_path)
    stores = MarketScanResearchStores.for_settings(
        datahub.settings,
        cache_path,
    )
    joint_probability = MarketScanJointExecutionMaintenanceService.from_settings(
        datahub.cache,
        datahub.settings,
        stores.official_execution,
    )
    return MarketScanResearchStores(
        probability=stores.probability,
        probability_source=stores.probability_source,
        future_range=stores.future_range,
        historical_probability=stores.historical_probability,
        official_execution=stores.official_execution,
        joint_probability=joint_probability,
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
        self._research_stores = _manager_research_stores(datahub)
        self._bind_research_stores()
        self._initialize_runtime_services(instance_guard, now)
        self._initialize_probability_runtime_state()

    def _bind_research_stores(self) -> None:
        self._probability_store = self._research_stores.probability
        self._probability_source_research_store = self._research_stores.probability_source
        self._future_range_store = self._research_stores.future_range
        self._historical_probability_store = self._research_stores.historical_probability
        self._official_execution_store = self._research_stores.official_execution
        self._joint_probability_store = self._research_stores.joint_probability
        self._query_service = MarketScanQueryService(self.cache, self._research_stores)
        self._query_service_stores = self._research_stores

    def _initialize_runtime_services(
        self,
        instance_guard: InstanceGuard | None,
        now: Callable[[], datetime] | None,
    ) -> None:
        delta_repositories = getattr(self.cache, "repositories", None)
        delta_repository = cast(
            MarketScanDeltaRepositoryProtocol,
            getattr(delta_repositories, "market_scan_delta", None),
        )
        self._delta_service = MarketScanDeltaService(delta_repository)
        self._now = now or _market_now
        sensitive_values = sensitive_setting_values(self.settings)
        self._sensitive_values = sensitive_values
        self._executor = MarketScanExecutor(
            self.datahub,
            sensitive_values=sensitive_values,
            now=self._current_time,
        )
        self._finalizer = MarketScanFinalizer(self.cache, sensitive_values=sensitive_values)
        self._lifecycle = MarketScanLifecycle(self.cache, instance_guard=instance_guard)
        self._terminal_recovery = MarketScanTerminalRecovery(self.cache, self._lifecycle)
        self._automation = MarketScanAutomationCoordinator(
            self.datahub,
            sensitive_values=sensitive_values,
        )

    def _initialize_probability_runtime_state(self) -> None:
        self._automatic_tick_lock = asyncio.Lock()
        self._settled_automatic_state: MarketScanAutomaticState | None = None
        self._settled_automatic_checked_at: datetime | None = None
        self._settled_automatic_guard_epoch: int | None = None
        self._deferred_stop_task: asyncio.Task[None] | None = None
        self._probability_runtime_warmup_task: asyncio.Task[None] | None = None
        self._probability_capture_task: asyncio.Task[None] | None = None
        self._probability_activation_lock = asyncio.Lock()
        self._probability_preload_lock = asyncio.Lock()
        self._probability_capture_lock = asyncio.Lock()
        self._probability_capture_wakeup = asyncio.Event()
        self._probability_capture_owner = f"market-scan-manager-{uuid4().hex}"
        self._probability_archives_audited = False

    async def start(self) -> int:
        reconciled = await self._lifecycle.start()
        await run_cache_io(self._recover_terminal_persistence_failures)
        self._start_probability_runtime_warmup()
        return reconciled

    async def refresh_probability_research_cache(self) -> int:
        """Verify and atomically publish the compact source/outcome/fit index."""
        # Waiters do not occupy I/O threads. Re-read bindings after admission so
        # an outbox capture arriving during a refresh is not silently dropped.
        async with self._probability_preload_lock:
            return await self._refresh_probability_research_cache()

    async def _refresh_probability_research_cache(self) -> int:
        self._mark_probability_preloads_pending()
        release_leases = self._acquire_probability_preload_leases()
        cancel_event = ThreadEvent()
        try:
            probability_source = self._probability_source_research_store
            bindings_loader = getattr(
                self.cache,
                "probability_source_capture_archive_bindings",
                None,
            )
            archive_bindings = await run_cache_io(bindings_loader) if probability_source is not None and callable(bindings_loader) else None
            source_task = self._start_probability_source_preload(
                archive_bindings, bound=callable(bindings_loader), cancel_event=cancel_event,
            )
            historical = getattr(self, "_historical_probability_store", None)
            historical_task = (
                None
                if historical is None
                else asyncio.create_task(
                    run_cache_io(historical.preload),
                    name="market-scan-probability-history-preload",
                )
            )
            tasks = tuple(task for task in (source_task, historical_task) if task is not None)
            if tasks:
                await _drain_probability_preloads(tasks, cancel_event)
            return 0 if source_task is None else source_task.result()
        finally:
            self._clear_probability_preloads_pending()
            for release in release_leases:
                release()

    def _start_probability_source_preload(
        self,
        archive_bindings: dict[int, str] | None,
        *,
        bound: bool,
        cancel_event: ThreadEvent,
    ) -> asyncio.Task[int] | None:
        source = self._probability_source_research_store
        if source is None:
            return None
        preload = getattr(source, "preload_isolated", None)
        arguments: dict[str, object] = {"archive_bindings": archive_bindings} if bound else {}
        if callable(preload):
            arguments["cancel_event"] = cancel_event
        else:
            preload = source.preload
        return asyncio.create_task(
            run_cache_io(preload, **arguments), name="market-scan-probability-source-preload",
        )

    async def maintain_joint_execution_probability(
        self,
        *,
        now: datetime | None = None,
    ) -> JointExecutionMaintenanceSummary:
        store = self._joint_probability_store
        if store is None:
            raise RuntimeError("联合执行概率维护服务不可用")
        current = now or self._now()
        if current.tzinfo is None or current.utcoffset() is None:
            current = current.replace(tzinfo=ASHARE_TIMEZONE)
        else:
            current = current.astimezone(ASHARE_TIMEZONE)
        return await run_cache_io(store.run, now=current)

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
        cancelled: asyncio.CancelledError | None = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as exc:
                cancelled = exc
        if cancelled is not None:
            raise cancelled
        cleanup.result()

    async def _stop(self, *, close: bool) -> None:
        await run_cache_io(self._recover_terminal_persistence_failures)
        snapshot = await self._lifecycle.begin_stop(close=close)
        await self._stop_probability_runtime_warmup()
        if snapshot is None:
            await self._stop_probability_capture_worker()
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
                try:
                    await self._drain_probability_capture_outbox()
                finally:
                    await self._stop_probability_capture_worker()
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
        normalized_as_of, temporal = _requested_scan_temporal(
            as_of,
            current=current,
            mode=mode,
        )
        self._validate_settings()
        async with self._lifecycle.lock:
            self._lifecycle.require_open()
            acquired, _reconciled = await self._lifecycle.ensure_instance_guard()
            if not acquired:
                if busy_is_noop:
                    return None
                raise RuntimeError(MARKET_SCAN_INSTANCE_GUARD_BUSY_MESSAGE)
            self._start_probability_runtime_warmup()
            await run_cache_io(self._recover_terminal_persistence_failures)
            active = await run_cache_io(self.cache.active_market_scan_run)
            if active is not None:
                return MarketScanStartResponse(accepted=False, deduplicated=True, run=active)
            fresh_current = self._current_time()
            normalized_as_of, temporal = _fresh_scan_temporal(
                as_of,
                normalized_as_of=normalized_as_of,
                temporal=temporal,
                fresh_current=fresh_current,
                mode=mode,
            )
            run_as_of = _required_datetime_text(normalized_as_of)
            rule_contract = market_scan_rule_contract(self.settings, mode=mode)
            try:
                run = await run_cache_io(
                    self.cache.create_market_scan_run,
                    trigger=trigger,
                    mode=mode,
                    rule_version=f"full-market-scan-v6:{stable_score_spec_hash(rule_contract)}",
                    as_of=run_as_of,
                    data_date=temporal.data_date.isoformat(),
                    quote_date=temporal.quote_date.isoformat(),
                    scope=FULL_MARKET_SCOPE,
                    rule_contract=rule_contract,
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
        async with self._lifecycle.lock:
            self._lifecycle.require_open()
            await self._lifecycle.require_instance_guard(MARKET_SCAN_INSTANCE_GUARD_BUSY_MESSAGE)
            self._start_probability_runtime_warmup()
            await run_cache_io(self._recover_terminal_persistence_failures, run_id)
            source = await run_cache_io(self.cache.market_scan_run, run_id)
            current = self._current_time()
            self._validate_top100_refresh_source(source, current=current)
            active = await run_cache_io(self.cache.active_market_scan_run)
            if active is not None:
                return MarketScanStartResponse(accepted=False, deduplicated=True, run=active)
            temporal = market_scan_temporal_contract(current, source.mode)
            run_as_of = _required_datetime_text(current)
            rule_contract = market_scan_rule_contract(self.settings, mode=source.mode)
            try:
                run = await run_cache_io(
                    self.cache.prepare_market_scan_top100_refresh,
                    source.id,
                    rule_version=source.rule_version,
                    as_of=run_as_of,
                    data_date=temporal.data_date.isoformat(),
                    quote_date=temporal.quote_date.isoformat(),
                    limit=MARKET_SCAN_TOP100_REFRESH_LIMIT,
                    rule_contract=rule_contract,
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
            self._start_probability_runtime_warmup()
            await run_cache_io(self._recover_terminal_persistence_failures, run_id)
            candidate = await run_cache_io(self.cache.market_scan_run, run_id)
            if candidate.rule_version != market_scan_rule_version(
                self.settings,
                mode=candidate.mode,
            ):
                raise ValueError("扫描规则/评分配置已变更，请新建扫描；旧批次将保留为历史快照")
            retry_plan = await run_cache_io(self.cache.market_scan_retry_plan, run_id)
            current = self._current_time()
            if retry_plan.needs_market_data:
                market_scan_temporal_contract(current, candidate.mode)
            self._validate_retry_candidate(candidate, retry_plan, current=current)
            active = await run_cache_io(self.cache.active_market_scan_run)
            if active is not None:
                return MarketScanStartResponse(accepted=False, deduplicated=True, run=active)
            try:
                rule_contract = market_scan_rule_contract(
                    self.settings,
                    mode=candidate.mode,
                )
                run = await run_cache_io(
                    self.cache.prepare_market_scan_retry,
                    run_id,
                    retry_plan,
                    as_of=datetime_to_text(current),
                    rule_contract=rule_contract,
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
            self._start_probability_runtime_warmup()
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
        data_date = latest_expected_daily_kline_date(current).isoformat()
        async with self._automatic_tick_lock:
            state = await run_cache_io(self.cache.latest_full_market_scan_automatic_state)
            if self._is_settled_automatic_state(
                state,
                data_date=data_date,
                current=current,
            ):
                return None
            if not await self._claim_automatic_ownership():
                return None
            return await self._run_automatic_tick_decision(
                state,
                current=current,
                data_date=data_date,
            )

    async def _run_automatic_tick_decision(
        self,
        state: MarketScanAutomaticState | None,
        *,
        current: datetime,
        data_date: str,
    ) -> MarketScanStartResponse | None:
        try:
            response = await self._automation.run(
                current=current,
                data_date=data_date,
                start_action=self._start_automatic_action,
                validate_retry=self._validate_automatic_retry,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._remember_terminal_automatic_state(
                state,
                data_date=data_date,
                current=current,
            )
            raise
        if response is not None:
            self._clear_settled_automatic_state()
            return response
        self._remember_terminal_automatic_state(
            state,
            data_date=data_date,
            current=current,
        )
        return None

    def _remember_terminal_automatic_state(
        self,
        state: MarketScanAutomaticState | None,
        *,
        data_date: str,
        current: datetime,
    ) -> None:
        if state is not None and _automatic_state_is_terminal_for_date(
            state,
            data_date=data_date,
        ):
            self._remember_settled_automatic_state(state, current=current)

    def _is_settled_automatic_state(
        self,
        state: MarketScanAutomaticState | None,
        *,
        data_date: str,
        current: datetime,
    ) -> bool:
        return (
            state is not None
            and state == self._settled_automatic_state
            and _automatic_state_is_terminal_for_date(state, data_date=data_date)
            and self._lifecycle.has_instance_guard
            and self._settled_automatic_guard_epoch == self._lifecycle.instance_guard_epoch
            and self._settled_automatic_checked_at is not None
            and _automatic_audit_is_deferred(
                self._settled_automatic_checked_at,
                current=current,
            )
        )

    def _remember_settled_automatic_state(
        self,
        state: MarketScanAutomaticState,
        *,
        current: datetime,
    ) -> None:
        self._settled_automatic_state = state
        self._settled_automatic_checked_at = current
        self._settled_automatic_guard_epoch = self._lifecycle.instance_guard_epoch

    def _clear_settled_automatic_state(self) -> None:
        self._settled_automatic_state = None
        self._settled_automatic_checked_at = None
        self._settled_automatic_guard_epoch = None

    async def _claim_automatic_ownership(self) -> bool:
        async with self._lifecycle.lock:
            acquired, _reconciled = await self._lifecycle.ensure_instance_guard()
            if not acquired:
                return False
            self._start_probability_runtime_warmup()
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
        return self._queries().run(run_id)

    def latest_run(self, *, mode: MarketScanMode | None = None) -> MarketScanRun | None:
        return self._queries().latest_run(mode=mode)

    def polling_identity(self, *, mode: MarketScanMode) -> MarketScanPollingIdentity:
        return self._queries().polling_identity(mode=mode)

    def latest_published_run(self, *, mode: MarketScanMode | None = None) -> MarketScanRun | None:
        return self._queries().latest_published_run(mode=mode)

    def next_automatic_run_at(self) -> datetime | None:
        return self._automation.next_due_at

    def recover_terminal_failures(self, run_id: int | None = None) -> int:
        """Explicitly reconcile terminal writes that previously failed."""
        return self._recover_terminal_persistence_failures(run_id)

    def runs(
        self,
        *,
        page: int,
        page_size: int,
        mode: MarketScanMode | None = None,
        status: MarketScanRunStatus | Literal["published"] | None = None,
        data_date: str | None = None,
    ) -> MarketScanRunPage:
        return self._queries().runs(
            page=page,
            page_size=page_size,
            mode=mode,
            status=status,
            data_date=data_date,
        )

    def run_identities(
        self,
        *,
        page: int,
        page_size: int,
        mode: MarketScanMode | None = None,
        status: MarketScanRunStatus | Literal["published"] | None = None,
        data_date: str | None = None,
    ) -> MarketScanRunPage:
        return self._queries().run_identities(
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
        # fmt: off
        return self._queries().results(
            run_id,
            page=page, page_size=page_size, status=status,
            market=market, industry=industry, is_st=is_st, is_new=is_new,
            min_score=min_score, max_score=max_score,
            min_trend_score=min_trend_score, max_trend_score=max_trend_score,
            min_change_pct=min_change_pct, max_change_pct=max_change_pct,
            min_turnover_rate=min_turnover_rate, max_turnover_rate=max_turnover_rate,
            min_amount=min_amount, max_amount=max_amount,
            min_data_quality_score=min_data_quality_score,
            max_data_quality_score=max_data_quality_score,
            min_confidence=min_confidence, max_risk=max_risk,
            min_tradability=min_tradability, keyword=keyword,
            sort=sort, order=order, probability_horizon=probability_horizon,
            min_upside_probability=min_upside_probability,
        )
        # fmt: on

    def probability_research(self, run_id: int) -> dict[str, object]:
        return self._queries().probability_research(run_id)

    def experimental_probability_results(
        self, run_id: int, *, minimum: float | None = None, market: str | None = None,
        keyword: str = "", sort: Literal["probability", "base_rank"] = "probability", page: int = 1, page_size: int = 50,
        prediction_kind: Literal["net_h5", "close_d1", "close_d2", "close_d5"] = "net_h5",
    ) -> dict[str, object]:
        return self._queries().experimental_probability_results(
            run_id, prediction_kind=prediction_kind, minimum=minimum, market=market, keyword=keyword, sort=sort, page=page, page_size=page_size,
        )

    def breadth(self, run_id: int) -> MarketBreadthV1:
        return self._queries().breadth(run_id)

    def evaluate_screen(
        self,
        run_id: int,
        request: MarketScanScreenEvaluateRequest,
    ) -> MarketScanScreenEvaluationV1:
        return self._queries().evaluate_screen(run_id, request)

    def delta(self, run_id: int) -> MarketScanDeltaResponse:
        return self._delta_service.compare(run_id)

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
        return self._queries().future_range_research(
            run_id,
            page=page,
            page_size=page_size,
            session_offset=session_offset,
            symbol=symbol,
            include_research=include_research,
        )

    def _run_probability_research(self, run_id: int) -> dict[str, object]:
        return self._queries().probability_research(run_id)

    def _run_probability_projection(
        self,
        run_id: int,
        *,
        symbols: tuple[str, ...] | None = None,
    ) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
        return self._queries().probability_projection(run_id, symbols=symbols)

    def _queries(self) -> MarketScanQueryService:
        service = getattr(self, "_query_service", None)
        stores = MarketScanResearchStores(
            probability=getattr(self, "_probability_store", None),
            probability_source=getattr(self, "_probability_source_research_store", None),
            future_range=getattr(self, "_future_range_store", None),
            historical_probability=getattr(self, "_historical_probability_store", None),
            official_execution=getattr(self, "_official_execution_store", None),
            joint_probability=getattr(self, "_joint_probability_store", None),
        )
        if service is not None and getattr(self, "_query_service_stores", None) == stores:
            return service
        cache = cast(MarketScanCacheProtocol, getattr(self, "cache", None))
        service = MarketScanQueryService(cache, stores)
        self._query_service = service
        self._query_service_stores = stores
        return service

    def export_results(
        self,
        run_id: int,
        *,
        filters: MarketScanExportFilters,
    ) -> MarketScanWorkbookExport:
        filters = filters.normalized()
        page, future_range = self._queries().export_projection(
            run_id,
            filters=filters,
        )
        return build_market_scan_workbook(
            page,
            filters,
            exported_at=self._current_time(),
            future_range_research=future_range,
        )

    def _launch(self, run_id: int) -> None:
        self._clear_settled_automatic_state()
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
            self._validate_publication_window(current)
            degraded_count = await run_cache_io(
                self.cache.market_scan_degraded_result_count,
                run_id,
            )
            publication_summary = (
                await run_cache_io(
                    self.cache.market_scan_repo.publication_summary,
                    run_id,
                )
                if not is_market_scan_top100_refresh_scope(current.scope)
                else None
            )
            persisted = await self._finalizer.finish_completed(
                current,
                degraded_count=degraded_count,
                warnings=warnings,
                publication_summary=publication_summary,
                validate_before_commit=lambda: self._validate_publication_window(current),
            )
        except asyncio.CancelledError:
            finish = self._finish_interrupted if self._lifecycle.closed else self._finish_cancelled
            await asyncio.shield(finish(run_id))
            raise
        except Exception as exc:
            await self._finish_failed(run_id, exc)
        else:
            self._track_terminal_persistence(run_id, persisted)
            if persisted:
                self._probability_capture_wakeup.set()
                await self._drain_probability_capture_outbox()

    def _start_probability_capture_worker(self) -> None:
        task = self._probability_capture_task
        if task is not None and not task.done():
            return
        self._probability_capture_wakeup.set()
        task = asyncio.create_task(
            self._probability_capture_worker(),
            name="market-scan-probability-source-capture",
        )
        self._probability_capture_task = task
        task.add_done_callback(_consume_stop_exception)

    def _start_probability_runtime_warmup(self) -> None:
        """Warm verified research state without delaying application readiness."""

        task = getattr(self, "_probability_runtime_warmup_task", None)
        if task is not None and not task.done():
            return
        capture_task = getattr(self, "_probability_capture_task", None)
        if getattr(self, "_probability_archives_audited", False) and capture_task is not None and not capture_task.done():
            return
        self._mark_probability_preloads_pending()
        try:
            task = asyncio.create_task(
                self._warm_probability_runtime(),
                name="market-scan-probability-runtime-warmup",
            )
        except Exception:
            self._clear_probability_preloads_pending()
            raise
        self._probability_runtime_warmup_task = task
        task.add_done_callback(_consume_stop_exception)

    async def _warm_probability_runtime(self) -> None:
        try:
            await self.refresh_probability_research_cache()
            await self._activate_probability_capture_leader()
            if self._joint_probability_store is not None:
                await self.maintain_joint_execution_probability(now=self._current_time())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = "上涨概率运行时预热失败：" f"{short_scan_error(exc, sensitive_values=self._sensitive_values)}"
            try:
                await run_cache_io(
                    self.cache.save_monitor_event,
                    "warning",
                    "research",
                    message[:800],
                )
            except Exception:
                pass

    def _acquire_probability_preload_leases(self) -> tuple[Callable[[], None], ...]:
        releases: list[Callable[[], None]] = []
        for store in (
            getattr(self, "_probability_source_research_store", None),
            getattr(self, "_historical_probability_store", None),
        ):
            acquire = getattr(store, "acquire_preload_lease", None)
            release = getattr(store, "release_preload_lease", None)
            if callable(acquire) and callable(release):
                acquire()
                releases.append(release)
        return tuple(releases)

    def _mark_probability_preloads_pending(self) -> None:
        for store in (
            getattr(self, "_probability_source_research_store", None),
            getattr(self, "_historical_probability_store", None),
        ):
            marker = getattr(store, "mark_preload_pending", None)
            if callable(marker):
                marker()

    def _clear_probability_preloads_pending(self) -> None:
        for store in (
            getattr(self, "_probability_source_research_store", None),
            getattr(self, "_historical_probability_store", None),
        ):
            clear = getattr(store, "clear_preload_pending", None)
            if callable(clear):
                clear()

    async def _stop_probability_runtime_warmup(self) -> None:
        task = getattr(self, "_probability_runtime_warmup_task", None)
        self._probability_runtime_warmup_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if not self._probability_preload_lock.locked():
            self._clear_probability_preloads_pending()

    async def _activate_probability_capture_leader(self) -> None:
        if not self._lifecycle.owns_instance_guard():
            return
        async with self._probability_activation_lock:
            if not self._lifecycle.owns_instance_guard():
                return
            await run_cache_io(self.cache.reconcile_probability_source_capture_outbox)
            if not self._probability_archives_audited:
                source = getattr(self, "_probability_source_research_store", None)
                verified_digests = getattr(source, "verified_archive_digests", None)
                if callable(verified_digests):
                    archives = verified_digests()
                    await run_cache_io(
                        self.cache.audit_probability_source_capture_archives,
                        archives,
                    )
                else:
                    await run_cache_io(
                        audit_market_scan_probability_source_archives,
                        self.cache,
                    )
                self._probability_archives_audited = True
            if self._lifecycle.owns_instance_guard():
                self._start_probability_capture_worker()

    async def _stop_probability_capture_worker(self) -> None:
        task = self._probability_capture_task
        self._probability_capture_task = None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _probability_capture_worker(self) -> None:
        while True:
            self._probability_capture_wakeup.clear()
            try:
                await self._drain_probability_capture_outbox()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                message = "上涨概率PIT归档outbox处理失败：" f"{short_scan_error(exc, sensitive_values=self._sensitive_values)}"
                try:
                    await run_cache_io(
                        self.cache.save_monitor_event,
                        "warning",
                        "research",
                        message[:800],
                    )
                except Exception:
                    pass
            try:
                await asyncio.wait_for(
                    self._probability_capture_wakeup.wait(),
                    timeout=PROBABILITY_SOURCE_CAPTURE_POLL_SECONDS,
                )
            except TimeoutError:
                continue

    async def _drain_probability_capture_outbox(self) -> dict[str, int]:
        if not self._lifecycle.owns_instance_guard():
            return {"captured": 0, "skipped": 0, "failed": 0}
        async with self._probability_capture_lock:
            summary = await process_market_scan_probability_capture_outbox(
                self.cache,
                owner=self._probability_capture_owner,
                sensitive_values=self._sensitive_values,
            )
            if summary["captured"]:
                await self.refresh_probability_research_cache()
            return summary

    async def _finish_cancelled(self, run_id: int) -> None:
        persisted = await self._finalizer.finish_cancelled(run_id)
        self._track_terminal_persistence(run_id, persisted)

    async def _finish_interrupted(self, run_id: int) -> None:
        persisted = await self._finalizer.finish_interrupted(run_id)
        self._track_terminal_persistence(run_id, persisted)

    async def _finish_failed(self, run_id: int, exc: Exception) -> None:
        pressure_warnings = self._executor.pressure_warnings
        failure = RuntimeError(f"{exc}；{pressure_warnings[0]}") if pressure_warnings else exc
        persisted = await self._finalizer.finish_failed(run_id, failure)
        self._track_terminal_persistence(run_id, persisted)

    def _track_terminal_persistence(self, run_id: int, persisted: bool) -> None:
        self._terminal_recovery.track(run_id, persisted)

    def _recover_terminal_persistence_failures(self, run_id: int | None = None) -> int:
        return self._terminal_recovery.recover(run_id)

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
                f"源批次日K/行情日期 {run.data_date}/{run.quote_date} 已过期，" f"当前应为 {expected_data_date}/{expected_quote_date}；请先执行新的全市场扫描"
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

    def _validate_publication_window(self, run: MarketScanRun) -> None:
        try:
            temporal = market_scan_temporal_contract(self._current_time(), run.mode)
        except ValueError as exc:
            raise MarketScanPublicationValidationError(str(exc)) from exc
        if run.data_date != temporal.data_date.isoformat() or run.quote_date != temporal.quote_date.isoformat():
            raise MarketScanPublicationValidationError("扫描完成时日K/行情日期已变化，停止发布；请在当前有效窗口新建扫描")

    def _current_time(self, value: datetime | None = None) -> datetime:
        return normalize_review_as_of(value if value is not None else self._now(), allow_future=True)


async def _drain_probability_preloads(
    tasks: tuple[asyncio.Task[int], ...], cancel_event: ThreadEvent,
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


def _requested_scan_temporal(
    as_of: datetime | None,
    *,
    current: datetime,
    mode: MarketScanMode,
) -> tuple[datetime, MarketScanTemporalContract]:
    normalized = normalize_review_as_of(as_of, now=current)
    temporal = market_scan_temporal_contract(normalized, mode)
    current_temporal = market_scan_temporal_contract(current, mode)
    if as_of is not None and _temporal_dates_differ(temporal, current_temporal):
        raise ValueError(HISTORICAL_SCAN_UNAVAILABLE_MESSAGE)
    return normalized, temporal


def _fresh_scan_temporal(
    requested: datetime | None,
    *,
    normalized_as_of: datetime,
    temporal: MarketScanTemporalContract,
    fresh_current: datetime,
    mode: MarketScanMode,
) -> tuple[datetime, MarketScanTemporalContract]:
    fresh_temporal = market_scan_temporal_contract(fresh_current, mode)
    if requested is None:
        return fresh_current, fresh_temporal
    if _temporal_dates_differ(temporal, fresh_temporal):
        raise ValueError(HISTORICAL_SCAN_UNAVAILABLE_MESSAGE)
    return normalized_as_of, temporal


def _temporal_dates_differ(
    left: MarketScanTemporalContract,
    right: MarketScanTemporalContract,
) -> bool:
    return left.data_date != right.data_date or left.quote_date != right.quote_date


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


def market_scan_rule_contract(
    settings: object,
    *,
    mode: MarketScanMode = "official",
) -> dict[str, object]:
    mode_contract = _market_scan_mode_contract(mode)
    return {
        "schema_version": 6,
        "mode": mode_contract,
        "score_spec": market_scan_score_spec(
            min_data_quality_score=int(getattr(settings, "market_scan_min_data_quality_score")),
        ),
        "production_score_semantics": MARKET_SCAN_PRODUCTION_SCORE_SEMANTICS,
        "history": {
            "adjustment_mode": "qfq",
            "kline_limit": int(getattr(settings, "market_scan_kline_limit")),
            "min_history_rows": int(getattr(settings, "market_scan_min_history_rows")),
        },
        "execution_quote_evidence": _market_scan_execution_quote_contract(),
        "universe": _market_scan_universe_contract(settings),
        "publication": _market_scan_publication_contract(),
    }


def _market_scan_mode_contract(mode: MarketScanMode) -> dict[str, object]:
    if mode == "intraday":
        return {
            "id": mode,
            "quote_date": "current-trading-day",
            "daily_kline_cutoff": "previous-completed-trading-day",
            "quote_kline_consistency": "previous-close",
        }
    elif mode == "preopen":
        return {
            "id": mode,
            "quote_date": "previous-completed-trading-day",
            "daily_kline_cutoff": "same-previous-completed-trading-day",
            "quote_kline_consistency": "same-day-close",
        }
    return {
        "id": mode,
        "quote_date": "completed-daily-bar-date",
        "daily_kline_cutoff": "same-completed-trading-day",
        "quote_kline_consistency": "same-day-close",
    }


def _market_scan_execution_quote_contract() -> dict[str, object]:
    return {
        "schema_version": MARKET_SCAN_EXECUTION_QUOTE_SCHEMA_VERSION,
        "contract_version": MARKET_SCAN_EXECUTION_QUOTE_CONTRACT_VERSION,
        "adjustment_mode": "none",
        "source_authority": "vendor_normalized_quote",
        "corporate_action_status": "unknown",
        "formal_pit_authority": False,
    }


def _market_scan_universe_contract(settings: object) -> dict[str, object]:
    return {
        "classifier": "current-listed-a-share-v2",
        "markets": ["SH", "SZ", "BJ"],
        "authoritative_baseline_min_retain_ratio": 0.98,
        "new_stock_days": int(getattr(settings, "market_scan_new_stock_days")),
    }


def _market_scan_publication_contract() -> dict[str, object]:
    return {
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
    }


def market_scan_rule_version(
    settings: object,
    *,
    mode: MarketScanMode = "official",
) -> str:
    contract = market_scan_rule_contract(settings, mode=mode)
    return f"full-market-scan-v6:{stable_score_spec_hash(contract)}"


def _market_now() -> datetime:
    return market_now()


def _required_datetime_text(value: datetime) -> str:
    serialized = datetime_to_text(value)
    assert serialized is not None
    return serialized


__all__ = [
    "HISTORICAL_SCAN_UNAVAILABLE_MESSAGE",
    "MARKET_SCAN_INSTANCE_GUARD_BUSY_MESSAGE",
    "MARKET_SCAN_TASK_LABEL",
    "MARKET_SCAN_TASK_NAME",
    "MarketScanManager",
    "market_scan_rule_contract",
    "market_scan_rule_version",
]
