from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

from app.models.system import TaskRun
from app.services.datahub_runtime import run_cache_io
from app.services.market_scan_automation import (
    MarketScanAutomaticAction,
    effective_auto_retry_limit,
)
from app.services.market_scan_preflight import (
    MarketScanPreflightCheck,
    MarketScanPreflightReport,
)
from app.services.task_run_lifecycle import TaskRunCache, start_task_run_cancel_safe
from app.utils.time import parse_text_time


MARKET_SCAN_PREFLIGHT_TASK_PREFIX = "full_market_scan_preflight"
MARKET_SCAN_PREFLIGHT_MONITOR_CATEGORY = "market_scan_preflight"
PREFLIGHT_TASK_LOOKBACK_MIN_ROWS = 512
PREFLIGHT_TASK_LOOKBACK_ROWS_PER_RETRY = 128


@dataclass(frozen=True)
class MarketScanPreflightAttemptDecision:
    allowed: bool
    attempt_number: int
    due_at: datetime | None = None
    exhausted: bool = False
    reason: str = ""


def format_preflight_report(report: MarketScanPreflightReport) -> str:
    status = "通过" if report.ok else "失败"
    details = "；".join(_format_preflight_check(check) for check in report.checks)
    return f"全市场扫描预检{status}：{details}"[:650]


def _format_preflight_check(check: MarketScanPreflightCheck) -> str:
    status = "通过" if check.ok else "失败"
    detail = " ".join(check.detail.split())[:96]
    return f"{check.capability}={status}({detail})"


def market_scan_preflight_task_name(action: MarketScanAutomaticAction) -> str:
    source = str(action.source_run_id or 0)
    return f"{MARKET_SCAN_PREFLIGHT_TASK_PREFIX}|{action.data_date}|{action.kind}|{source}"


async def persisted_preflight_attempt_decision(
    cache: object,
    settings: object,
    *,
    task_name: str,
    current: datetime,
    delays_seconds: Sequence[float],
    max_retry_attempts: int,
) -> MarketScanPreflightAttemptDecision:
    """Recover retry cadence without scanning unrelated scheduler history."""
    named_lookup = getattr(cache, "task_runs_for_name", None)
    if callable(named_lookup):
        task_runs = await run_cache_io(
            named_lookup,
            task_name,
            max(1, max_retry_attempts + 1),
        )
        return preflight_attempt_decision(
            task_runs,
            task_name=task_name,
            current=current,
            delays_seconds=delays_seconds,
            max_retry_attempts=max_retry_attempts,
        )
    retention_limit = max(1, int(getattr(settings, "max_task_run_rows", 2000)))
    recovery_window = max(
        PREFLIGHT_TASK_LOOKBACK_MIN_ROWS,
        (max_retry_attempts + 1) * PREFLIGHT_TASK_LOOKBACK_ROWS_PER_RETRY,
    )
    task_runs = await run_cache_io(
        getattr(cache, "recent_task_runs"),
        min(retention_limit, recovery_window),
    )
    return preflight_attempt_decision(
        task_runs,
        task_name=task_name,
        current=current,
        delays_seconds=delays_seconds,
        max_retry_attempts=max_retry_attempts,
    )


async def start_market_scan_preflight_task(cache: object, task_name: str) -> int:
    return await start_task_run_cancel_safe(
        cast(TaskRunCache, cache),
        task_name,
        "全市场扫描预检已取消",
    )


async def finish_market_scan_preflight_task(
    cache: object,
    task_run_id: int,
    status: str,
    message: str,
) -> None:
    await run_cache_io(getattr(cache, "finish_task_run"), task_run_id, status, message)


async def save_market_scan_preflight_event(
    cache: object,
    level: str,
    message: str,
) -> None:
    await run_cache_io(
        getattr(cache, "save_monitor_event"),
        level,
        MARKET_SCAN_PREFLIGHT_MONITOR_CATEGORY,
        message,
    )


def preflight_attempt_decision(
    task_runs: Sequence[TaskRun],
    *,
    task_name: str,
    current: datetime,
    delays_seconds: Sequence[float],
    max_retry_attempts: int,
) -> MarketScanPreflightAttemptDecision:
    matching = sorted(
        (run for run in task_runs if run.task_name == task_name),
        key=lambda run: run.id,
        reverse=True,
    )
    if not matching:
        return MarketScanPreflightAttemptDecision(True, 1, due_at=current)
    latest = matching[0]
    if latest.status in {"running", "success"}:
        return MarketScanPreflightAttemptDecision(False, len(matching), reason=f"latest-{latest.status}")
    if latest.status not in {"failed", "cancelled"}:
        return MarketScanPreflightAttemptDecision(False, len(matching), exhausted=True, reason="unexpected-task-status")
    failed = [run for run in matching if run.status in {"failed", "cancelled"}]
    retry_limit = effective_auto_retry_limit(delays_seconds, max_retry_attempts)
    if len(failed) > retry_limit:
        return MarketScanPreflightAttemptDecision(False, len(failed), exhausted=True, reason="attempts-exhausted")
    due_at = _task_retry_due_at(failed[0], delays_seconds[len(failed) - 1])
    if due_at is None:
        return MarketScanPreflightAttemptDecision(False, len(failed), exhausted=True, reason="invalid-task-time")
    return MarketScanPreflightAttemptDecision(
        current >= due_at,
        len(failed) + 1,
        due_at=due_at,
        reason="due" if current >= due_at else "waiting",
    )


def _task_retry_due_at(task_run: TaskRun, delay_seconds: float) -> datetime | None:
    timestamp = task_run.finished_at or task_run.started_at
    try:
        return parse_text_time(timestamp) + timedelta(seconds=float(delay_seconds))
    except (TypeError, ValueError):
        return None


__all__ = [
    "MARKET_SCAN_PREFLIGHT_MONITOR_CATEGORY",
    "MARKET_SCAN_PREFLIGHT_TASK_PREFIX",
    "MarketScanPreflightAttemptDecision",
    "finish_market_scan_preflight_task",
    "format_preflight_report",
    "market_scan_preflight_task_name",
    "persisted_preflight_attempt_decision",
    "preflight_attempt_decision",
    "save_market_scan_preflight_event",
    "start_market_scan_preflight_task",
]
