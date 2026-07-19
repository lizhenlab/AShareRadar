"""Backward-compatible facade for the local data scheduler."""

from app.services.scheduler_contracts import (
    INSTANCE_GUARD_BUSY_MESSAGE as INSTANCE_GUARD_BUSY_MESSAGE,
    KLINE_FAILURE_DETAIL_LIMIT as KLINE_FAILURE_DETAIL_LIMIT,
    PROVIDER_FAILURE_DETAIL_LIMIT as PROVIDER_FAILURE_DETAIL_LIMIT,
    TASK_ERROR_MAX_LENGTH as TASK_ERROR_MAX_LENGTH,
    TASK_STATUS_CANCELLED as TASK_STATUS_CANCELLED,
    TASK_STATUS_DEGRADED as TASK_STATUS_DEGRADED,
    TASK_STATUS_FAILED as TASK_STATUS_FAILED,
    TASK_STATUS_RUNNING as TASK_STATUS_RUNNING,
    TASK_STATUS_SUCCESS as TASK_STATUS_SUCCESS,
    FileSchedulerInstanceGuard as FileSchedulerInstanceGuard,
    HealthEvent as HealthEvent,
    KlineRefreshSummary as KlineRefreshSummary,
    LocalTask as LocalTask,
    NoopSchedulerInstanceGuard as NoopSchedulerInstanceGuard,
    QuoteRefreshSummary as QuoteRefreshSummary,
    SchedulerInstanceGuard as SchedulerInstanceGuard,
    TaskDefinition as TaskDefinition,
    TaskExecutionResult as TaskExecutionResult,
    TaskSpec as TaskSpec,
)
from app.services.scheduler_service import LocalDataScheduler as LocalDataScheduler


__all__ = [
    "FileSchedulerInstanceGuard",
    "HealthEvent",
    "INSTANCE_GUARD_BUSY_MESSAGE",
    "KLINE_FAILURE_DETAIL_LIMIT",
    "KlineRefreshSummary",
    "LocalDataScheduler",
    "LocalTask",
    "NoopSchedulerInstanceGuard",
    "PROVIDER_FAILURE_DETAIL_LIMIT",
    "QuoteRefreshSummary",
    "SchedulerInstanceGuard",
    "TASK_ERROR_MAX_LENGTH",
    "TASK_STATUS_CANCELLED",
    "TASK_STATUS_DEGRADED",
    "TASK_STATUS_FAILED",
    "TASK_STATUS_RUNNING",
    "TASK_STATUS_SUCCESS",
    "TaskDefinition",
    "TaskExecutionResult",
    "TaskSpec",
]
