from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.db.system_mappers import row_to_provider_capability_status, row_to_provider_status
from app.models.schemas import ProviderCapabilityStatus, ProviderStatus
from app.repositories.base import SQLiteRepository
from app.repositories.provider_status_aggregation import (
    aggregate_provider_status as _aggregate_provider_status,
    capability_has_activity as _capability_has_activity,
)
from app.utils.time import now_text

MAX_ERROR_LENGTH = 500


@dataclass(frozen=True)
class ProviderRuntimeUpdate:
    name: str
    enabled: bool
    priority: int
    healthy: bool
    latency_ms: float | None
    last_success: str | None
    last_error: str | None
    success_delta: int
    failure_delta: int


PROVIDER_RUNTIME_COLUMNS = (
    "name",
    "enabled",
    "priority",
    "healthy",
    "last_success",
    "last_error",
    "latency_ms",
    "success_count",
    "failure_count",
    "updated_at",
)
PROVIDER_CAPABILITY_RUNTIME_COLUMNS = (
    "name",
    "kind",
    "enabled",
    "priority",
    "healthy",
    "last_success",
    "last_error",
    "latency_ms",
    "success_count",
    "failure_count",
    "updated_at",
)
PROVIDER_STATUS_ORDER_BY = "priority ASC, name ASC"
PROVIDER_CAPABILITY_STATUS_ORDER_BY = "priority ASC, name ASC, kind ASC"


def _runtime_upsert_sql(*, table: str, columns: tuple[str, ...], conflict_target: str, enabled_assignment: str) -> str:
    return (
        f"INSERT INTO {table} ({_column_names(columns)}) VALUES ({_placeholders(columns)}) "
        f"ON CONFLICT({conflict_target}) DO UPDATE SET "
        + _runtime_update_assignments(table, enabled_assignment)
    )


def _column_names(columns: tuple[str, ...]) -> str:
    return ", ".join(columns)


def _placeholders(columns: tuple[str, ...]) -> str:
    return ", ".join("?" for _column in columns)


def _select_columns(columns: tuple[str, ...]) -> str:
    return _column_names(columns)


def _db_bool(value: bool) -> int:
    return 1 if value else 0


def _count_value(value: int | None) -> int:
    return max(int(value or 0), 0)


def _trim_error(error: str) -> str:
    return str(error)[:MAX_ERROR_LENGTH]


def _runtime_update_assignments(table: str, enabled_assignment: str) -> str:
    assignments = [
        enabled_assignment,
        "priority=excluded.priority",
        "healthy=excluded.healthy",
        f"last_success=COALESCE(excluded.last_success, {table}.last_success)",
        "last_error=excluded.last_error",
        "latency_ms=excluded.latency_ms",
        f"success_count={table}.success_count + ?",
        f"failure_count={table}.failure_count + ?",
        "updated_at=excluded.updated_at",
    ]
    return ", ".join(assignments)


def _runtime_update_values(update: ProviderRuntimeUpdate, updated_at: str) -> tuple[object, ...]:
    return (
        update.name,
        _db_bool(update.enabled),
        update.priority,
        _db_bool(update.healthy),
        update.last_success,
        update.last_error,
        update.latency_ms,
        _count_value(update.success_delta),
        _count_value(update.failure_delta),
        updated_at,
    )


def _runtime_update_deltas(update: ProviderRuntimeUpdate) -> tuple[int, int]:
    return _count_value(update.success_delta), _count_value(update.failure_delta)


def _capability_runtime_update_values(update: ProviderRuntimeUpdate, kind: str, updated_at: str) -> tuple[object, ...]:
    values = _runtime_update_values(update, updated_at)
    return (values[0], kind, *values[1:])


def _execute_runtime_upsert(conn, sql: str, values: Iterable[object], update: ProviderRuntimeUpdate) -> None:
    conn.execute(sql, (*values, *_runtime_update_deltas(update)))


def _provider_status_values(item: ProviderStatus) -> tuple[object, ...]:
    return (
        item.name,
        _db_bool(item.enabled),
        item.priority,
        _db_bool(item.healthy),
        item.last_success,
        item.last_error,
        item.latency_ms,
        _count_value(item.success_count),
        _count_value(item.failure_count),
        item.updated_at or now_text(),
    )


def _provider_sync_upsert_sql(enabled_assignment: str) -> str:
    return (
        f"INSERT INTO provider_status ({_column_names(PROVIDER_RUNTIME_COLUMNS)}) "
        f"VALUES ({_placeholders(PROVIDER_RUNTIME_COLUMNS)}) "
        "ON CONFLICT(name) DO UPDATE SET "
        + ", ".join(
            [
                f"enabled={enabled_assignment}",
                "priority=excluded.priority",
                "healthy=excluded.healthy",
                "last_success=COALESCE(excluded.last_success, provider_status.last_success)",
                "last_error=excluded.last_error",
                "latency_ms=excluded.latency_ms",
                "success_count=excluded.success_count",
                "failure_count=excluded.failure_count",
                "updated_at=excluded.updated_at",
            ],
        )
    )


_PROVIDER_COLUMNS_SQL = _select_columns(PROVIDER_RUNTIME_COLUMNS)
_PROVIDER_CAPABILITY_COLUMNS_SQL = _select_columns(PROVIDER_CAPABILITY_RUNTIME_COLUMNS)
_PROVIDER_SELECT_SQL = f"SELECT {_PROVIDER_COLUMNS_SQL} FROM provider_status"
_PROVIDER_CAPABILITY_SELECT_SQL = f"SELECT {_PROVIDER_CAPABILITY_COLUMNS_SQL} FROM provider_capability_status"
_PROVIDER_UPSERT_SQL = _runtime_upsert_sql(
    table="provider_status",
    columns=PROVIDER_RUNTIME_COLUMNS,
    conflict_target="name",
    enabled_assignment="enabled=provider_status.enabled",
)
_PROVIDER_CAPABILITY_UPSERT_SQL = _runtime_upsert_sql(
    table="provider_capability_status",
    columns=PROVIDER_CAPABILITY_RUNTIME_COLUMNS,
    conflict_target="name, kind",
    enabled_assignment="enabled=provider_capability_status.enabled",
)
_PROVIDER_ENSURE_SQL = """
INSERT INTO provider_status (
    name, enabled, priority, healthy, success_count, failure_count, updated_at
) VALUES (?, ?, ?, ?, 0, 0, ?)
ON CONFLICT(name) DO UPDATE SET
    enabled=excluded.enabled,
    priority=excluded.priority,
    updated_at=CASE
        WHEN provider_status.last_success IS NULL
            AND provider_status.last_error IS NULL
            AND COALESCE(provider_status.success_count, 0) = 0
            AND COALESCE(provider_status.failure_count, 0) = 0
        THEN excluded.updated_at
        ELSE provider_status.updated_at
    END
"""
_PROVIDER_CAPABILITY_ENSURE_SQL = """
INSERT INTO provider_capability_status (
    name, kind, enabled, priority, healthy, success_count, failure_count, updated_at
) VALUES (?, ?, ?, ?, ?, 0, 0, ?)
ON CONFLICT(name, kind) DO UPDATE SET
    enabled=excluded.enabled,
    priority=excluded.priority,
    updated_at=CASE
        WHEN provider_capability_status.last_success IS NULL
            AND provider_capability_status.last_error IS NULL
            AND COALESCE(provider_capability_status.success_count, 0) = 0
            AND COALESCE(provider_capability_status.failure_count, 0) = 0
        THEN excluded.updated_at
        ELSE provider_capability_status.updated_at
    END
"""


class ProviderStatusRepository(SQLiteRepository):
    def enabled(self, name: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT enabled FROM provider_status WHERE name = ?", (name,)).fetchone()
        return bool(row["enabled"]) if row else True

    def capability_enabled(self, name: str, kind: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT enabled FROM provider_capability_status WHERE name = ? AND kind = ?",
                (name, kind),
            ).fetchone()
        return bool(row["enabled"]) if row else True

    def record_success(self, name: str, priority: int, latency_ms: float) -> None:
        self._upsert(
            name=name,
            enabled=self.enabled(name),
            priority=priority,
            healthy=True,
            latency_ms=latency_ms,
            last_success=now_text(),
            last_error=None,
            success_delta=1,
            failure_delta=0,
        )

    def record_failure(self, name: str, priority: int, error: str) -> None:
        self._upsert(
            name=name,
            enabled=self.enabled(name),
            priority=priority,
            healthy=False,
            latency_ms=None,
            last_success=None,
            last_error=_trim_error(error),
            success_delta=0,
            failure_delta=1,
        )

    def ensure_capability(self, name: str, kind: str, priority: int, enabled: bool = True) -> None:
        timestamp = now_text()
        with self._lock, self._connect() as conn:
            conn.execute(
                _PROVIDER_CAPABILITY_ENSURE_SQL,
                (name, kind, _db_bool(enabled), priority, 0, timestamp),
            )
        self._sync_provider_from_capabilities(name, priority, preserve_enabled=False)

    def record_capability_success(self, name: str, kind: str, priority: int, latency_ms: float) -> None:
        self._upsert_capability(
            name=name,
            kind=kind,
            enabled=self.capability_enabled(name, kind),
            priority=priority,
            healthy=True,
            latency_ms=latency_ms,
            last_success=now_text(),
            last_error=None,
            success_delta=1,
            failure_delta=0,
        )
        self._sync_provider_from_capabilities(name, priority, preserve_enabled=True)

    def record_capability_failure(self, name: str, kind: str, priority: int, error: str) -> None:
        self._upsert_capability(
            name=name,
            kind=kind,
            enabled=self.capability_enabled(name, kind),
            priority=priority,
            healthy=False,
            latency_ms=None,
            last_success=None,
            last_error=_trim_error(error),
            success_delta=0,
            failure_delta=1,
        )
        self._sync_provider_from_capabilities(name, priority, preserve_enabled=True)

    def ensure(self, name: str, priority: int, enabled: bool = True) -> None:
        timestamp = now_text()
        with self._lock, self._connect() as conn:
            conn.execute(
                _PROVIDER_ENSURE_SQL,
                (name, _db_bool(enabled), priority, _db_bool(enabled), timestamp),
            )

    def items(self) -> list[ProviderStatus]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(f"{_PROVIDER_SELECT_SQL} ORDER BY {PROVIDER_STATUS_ORDER_BY}").fetchall()
            capability_rows = conn.execute(
                f"{_PROVIDER_CAPABILITY_SELECT_SQL} ORDER BY {PROVIDER_CAPABILITY_STATUS_ORDER_BY}",
            ).fetchall()
        by_name = {row["name"]: row_to_provider_status(row) for row in rows}
        grouped: dict[str, list[ProviderCapabilityStatus]] = {}
        for row in capability_rows:
            item = row_to_provider_capability_status(row)
            grouped.setdefault(item.name, []).append(item)
        for name, statuses in grouped.items():
            by_name[name] = _aggregate_provider_status(name, statuses, by_name.get(name))
        return sorted(by_name.values(), key=lambda item: (item.priority, item.name))

    def capability_items(self) -> list[ProviderCapabilityStatus]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"{_PROVIDER_CAPABILITY_SELECT_SQL} ORDER BY {PROVIDER_CAPABILITY_STATUS_ORDER_BY}",
            ).fetchall()
        return [row_to_provider_capability_status(row) for row in rows]

    def _upsert(
        self,
        *,
        name: str,
        enabled: bool,
        priority: int,
        healthy: bool,
        latency_ms: float | None,
        last_success: str | None,
        last_error: str | None,
        success_delta: int,
        failure_delta: int,
    ) -> None:
        update = ProviderRuntimeUpdate(
            name=name,
            enabled=enabled,
            priority=priority,
            healthy=healthy,
            latency_ms=latency_ms,
            last_success=last_success,
            last_error=last_error,
            success_delta=success_delta,
            failure_delta=failure_delta,
        )
        updated_at = now_text()
        with self._lock, self._connect() as conn:
            _execute_runtime_upsert(conn, _PROVIDER_UPSERT_SQL, _runtime_update_values(update, updated_at), update)

    def _upsert_capability(
        self,
        *,
        name: str,
        kind: str,
        enabled: bool,
        priority: int,
        healthy: bool,
        latency_ms: float | None,
        last_success: str | None,
        last_error: str | None,
        success_delta: int,
        failure_delta: int,
    ) -> None:
        update = ProviderRuntimeUpdate(
            name=name,
            enabled=enabled,
            priority=priority,
            healthy=healthy,
            latency_ms=latency_ms,
            last_success=last_success,
            last_error=last_error,
            success_delta=success_delta,
            failure_delta=failure_delta,
        )
        updated_at = now_text()
        with self._lock, self._connect() as conn:
            _execute_runtime_upsert(
                conn,
                _PROVIDER_CAPABILITY_UPSERT_SQL,
                _capability_runtime_update_values(update, kind, updated_at),
                update,
            )

    def _sync_provider_from_capabilities(self, name: str, fallback_priority: int, *, preserve_enabled: bool) -> None:
        enabled_assignment = "provider_status.enabled" if preserve_enabled else "excluded.enabled"
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"{_PROVIDER_CAPABILITY_SELECT_SQL} WHERE name = ? ORDER BY {PROVIDER_CAPABILITY_STATUS_ORDER_BY}",
                (name,),
            ).fetchall()
            if not rows:
                return
            statuses = [row_to_provider_capability_status(row) for row in rows]
            fallback_row = conn.execute(f"{_PROVIDER_SELECT_SQL} WHERE name = ?", (name,)).fetchone()
            fallback = row_to_provider_status(fallback_row) if fallback_row else None
            aggregate = _aggregate_provider_status(name, statuses, fallback)
            conn.execute(
                _provider_sync_upsert_sql(enabled_assignment),
                _provider_status_values(
                    aggregate.model_copy(
                        update={"priority": aggregate.priority if aggregate.priority is not None else fallback_priority},
                    ),
                ),
            )

__all__ = ["ProviderStatusRepository", "_aggregate_provider_status", "_capability_has_activity"]
