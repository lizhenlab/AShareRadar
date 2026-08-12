from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from functools import partial

from app.models.market_scan import (
    MarketScanRetryPlan,
    MarketScanRun,
    MarketScanStartResponse,
)
from app.services.datahub_runtime import run_cache_io
from app.services.market_scan_automation import (
    MarketScanAutomaticAction,
    automatic_retry_decision,
    configured_auto_retry_policy,
)
from app.services.market_scan_completion import short_scan_error
from app.services.market_scan_contracts import MarketScanDataHubProtocol
from app.services.market_scan_preflight import (
    positive_preflight_timeout,
    run_market_scan_preflight,
)
from app.services.market_scan_preflight_state import (
    MarketScanPreflightAttemptDecision,
    finish_market_scan_preflight_task,
    format_preflight_report,
    market_scan_preflight_task_name,
    persisted_preflight_attempt_decision,
    save_market_scan_preflight_event,
    start_market_scan_preflight_task,
)


StartAutomaticAction = Callable[
    [MarketScanAutomaticAction, datetime],
    Awaitable[MarketScanStartResponse],
]
ValidateAutomaticRetry = Callable[[MarketScanRun, MarketScanRetryPlan, datetime], None]


@dataclass(frozen=True)
class _RetryCacheLookup:
    resolved: bool
    action: MarketScanAutomaticAction | None = None


class MarketScanAutomationCoordinator:
    """Resolve one automatic action and run its bounded preflight gate."""

    def __init__(
        self,
        datahub: MarketScanDataHubProtocol,
        *,
        sensitive_values: Iterable[object] = (),
    ) -> None:
        self.datahub = datahub
        self.cache = datahub.cache
        self.settings = datahub.settings
        self._sensitive_values = tuple(sensitive_values)
        self._lock = asyncio.Lock()
        self._retry_not_before: dict[int, datetime] = {}
        self._retry_ready: set[int] = set()
        self._retry_excluded: set[int] = set()
        self._preflight_not_before: dict[str, datetime] = {}
        self._preflight_consumed: set[str] = set()
        self._next_due_at: datetime | None = None

    @property
    def next_due_at(self) -> datetime | None:
        return self._next_due_at

    async def run(
        self,
        *,
        current: datetime,
        data_date: str,
        start_action: StartAutomaticAction,
        validate_retry: ValidateAutomaticRetry,
    ) -> MarketScanStartResponse | None:
        async with self._lock:
            action = await self._automatic_action(
                current,
                data_date=data_date,
                validate_retry=validate_retry,
            )
            if action is None:
                return None
            return await self._run_action(
                action,
                current=current,
                start_action=start_action,
            )

    async def _automatic_action(
        self,
        current: datetime,
        *,
        data_date: str,
        validate_retry: ValidateAutomaticRetry,
    ) -> MarketScanAutomaticAction | None:
        active = await run_cache_io(self.cache.active_market_scan_run)
        if active is not None:
            return None
        # The automatic workflow is an after-close official publication loop.
        # Pre-open review and intraday scans have independent cohorts and must
        # never suppress or become retry parents for the scheduled official run.
        latest = await run_cache_io(
            partial(self.cache.latest_full_market_scan_run, mode="official")
        )
        if latest is None or latest.data_date != data_date:
            return MarketScanAutomaticAction("scheduled", data_date)
        if latest.status in {"success", "degraded", "cancelled"}:
            return None
        if latest.trigger not in {"scheduled", "retry"}:
            return MarketScanAutomaticAction("scheduled", data_date)
        if latest.status not in {"failed", "interrupted"}:
            return None
        return await self._automatic_retry_action(
            latest,
            current=current,
            validate_retry=validate_retry,
        )

    async def _automatic_retry_action(
        self,
        run: MarketScanRun,
        *,
        current: datetime,
        validate_retry: ValidateAutomaticRetry,
    ) -> MarketScanAutomaticAction | None:
        cached = self._cached_retry_action(run, current=current)
        if cached.resolved:
            return cached.action
        retry_plan = await run_cache_io(self.cache.market_scan_retry_plan, run.id)
        summary = await run_cache_io(self.cache.market_scan_repo.publication_summary, run.id)
        delays, max_attempts = configured_auto_retry_policy(self.settings)
        decision = automatic_retry_decision(
            run,
            retry_plan,
            summary,
            current=current,
            delays_seconds=delays,
            max_retry_attempts=max_attempts,
        )
        if not decision.eligible:
            self._retry_excluded.add(run.id)
            self._next_due_at = None
            return None
        if not decision.is_due(current):
            if decision.due_at is not None:
                self._retry_not_before[run.id] = decision.due_at
                self._next_due_at = decision.due_at
            return None
        self._next_due_at = None
        try:
            validate_retry(run, retry_plan, current)
        except ValueError:
            self._retry_excluded.add(run.id)
            return None
        self._retry_ready.add(run.id)
        return MarketScanAutomaticAction("retry", run.data_date, source_run_id=run.id)

    def _cached_retry_action(
        self,
        run: MarketScanRun,
        *,
        current: datetime,
    ) -> _RetryCacheLookup:
        if run.id in self._retry_excluded:
            return _RetryCacheLookup(True)
        if run.id in self._retry_ready:
            return _RetryCacheLookup(
                True,
                MarketScanAutomaticAction("retry", run.data_date, source_run_id=run.id),
            )
        not_before = self._retry_not_before.get(run.id)
        if not_before is not None and current < not_before:
            return _RetryCacheLookup(True)
        self._retry_not_before.pop(run.id, None)
        return _RetryCacheLookup(False)

    async def _run_action(
        self,
        action: MarketScanAutomaticAction,
        *,
        current: datetime,
        start_action: StartAutomaticAction,
    ) -> MarketScanStartResponse | None:
        if not bool(getattr(self.settings, "market_scan_preflight_enabled", True)):
            return await start_action(action, current)
        delays, max_attempts = configured_auto_retry_policy(self.settings)
        task_name = market_scan_preflight_task_name(action)
        if self._preflight_is_deferred(task_name, current=current):
            return None
        attempt = await persisted_preflight_attempt_decision(
            self.cache,
            self.settings,
            task_name=task_name,
            current=current,
            delays_seconds=delays,
            max_retry_attempts=max_attempts,
        )
        if not attempt.allowed:
            self._remember_preflight_decision(task_name, attempt, current=current)
            return None
        return await self._execute_preflight_attempt(
            action,
            current=current,
            task_name=task_name,
            attempt_number=attempt.attempt_number,
            start_action=start_action,
        )

    def _preflight_is_deferred(self, task_name: str, *, current: datetime) -> bool:
        if task_name in self._preflight_consumed:
            return True
        not_before = self._preflight_not_before.get(task_name)
        if not_before is not None and current < not_before:
            return True
        self._preflight_not_before.pop(task_name, None)
        return False

    def _remember_preflight_decision(
        self,
        task_name: str,
        attempt: MarketScanPreflightAttemptDecision,
        *,
        current: datetime,
    ) -> None:
        if attempt.due_at is not None and current < attempt.due_at:
            self._preflight_not_before[task_name] = attempt.due_at
            self._next_due_at = attempt.due_at
        if attempt.exhausted or attempt.reason == "latest-success":
            self._preflight_consumed.add(task_name)
            self._next_due_at = None

    async def _execute_preflight_attempt(
        self,
        action: MarketScanAutomaticAction,
        *,
        current: datetime,
        task_name: str,
        attempt_number: int,
        start_action: StartAutomaticAction,
    ) -> MarketScanStartResponse | None:
        task_run_id = await start_market_scan_preflight_task(self.cache, task_name)
        try:
            report = await run_market_scan_preflight(
                self.datahub,
                current=current,
                timeout_seconds=positive_preflight_timeout(
                    getattr(self.settings, "market_scan_preflight_timeout_seconds", 30.0)
                ),
                sensitive_values=self._sensitive_values,
            )
            message = format_preflight_report(report)
            if not report.ok:
                await self._record_outcome(task_run_id, "failed", "warning", message)
                return None
            response = await start_action(action, current)
            outcome = f"{message}；第 {attempt_number} 次预检后已启动批次 {response.run.id}"
            await self._record_outcome(task_run_id, "success", "info", outcome)
            self._preflight_consumed.add(task_name)
            self._next_due_at = None
            return response
        except asyncio.CancelledError:
            await asyncio.shield(
                self._record_outcome(
                    task_run_id,
                    "cancelled",
                    "warning",
                    "全市场扫描预检已取消，未创建正式扫描",
                )
            )
            raise
        except Exception as exc:
            error = short_scan_error(exc, sensitive_values=self._sensitive_values)
            await self._record_outcome(
                task_run_id,
                "failed",
                "warning",
                f"全市场扫描预检或启动失败：{error}",
            )
            return None

    async def _record_outcome(
        self,
        task_run_id: int,
        status: str,
        level: str,
        message: str,
    ) -> None:
        await finish_market_scan_preflight_task(
            self.cache,
            task_run_id,
            status,
            message[:800],
        )
        await save_market_scan_preflight_event(self.cache, level, message[:800])

__all__ = ["MarketScanAutomationCoordinator"]
