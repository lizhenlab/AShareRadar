from __future__ import annotations

import sqlite3

from app.models.schemas import MonitorEvent, ProviderCapabilityStatus, ProviderStatus, TaskRun
from app.services.provider_errors import sanitize_provider_error


def _sanitized_provider_error(value: object | None) -> str | None:
    return None if value is None else sanitize_provider_error(value)


def row_to_provider_status(row: sqlite3.Row) -> ProviderStatus:
    return ProviderStatus(
        name=row["name"],
        enabled=bool(row["enabled"]),
        priority=row["priority"],
        healthy=bool(row["healthy"]),
        last_success=row["last_success"],
        last_error=_sanitized_provider_error(row["last_error"]),
        latency_ms=row["latency_ms"],
        success_count=row["success_count"],
        failure_count=row["failure_count"],
        updated_at=row["updated_at"],
    )


def row_to_provider_capability_status(row: sqlite3.Row) -> ProviderCapabilityStatus:
    return ProviderCapabilityStatus(
        name=row["name"],
        kind=row["kind"],
        enabled=bool(row["enabled"]),
        priority=row["priority"],
        healthy=bool(row["healthy"]),
        last_success=row["last_success"],
        last_error=_sanitized_provider_error(row["last_error"]),
        latency_ms=row["latency_ms"],
        success_count=row["success_count"],
        failure_count=row["failure_count"],
        updated_at=row["updated_at"],
    )


def row_to_task_run(row: sqlite3.Row) -> TaskRun:
    return TaskRun(
        id=row["id"],
        task_name=row["task_name"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        duration_ms=row["duration_ms"],
        message=row["message"],
    )


def row_to_monitor_event(row: sqlite3.Row) -> MonitorEvent:
    return MonitorEvent(
        id=row["id"],
        level=row["level"],
        category=row["category"],
        symbol=row["symbol"],
        message=row["message"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        repeat_count=row["repeat_count"] or 1,
    )


__all__ = [
    "row_to_provider_status",
    "row_to_provider_capability_status",
    "row_to_task_run",
    "row_to_monitor_event",
]
