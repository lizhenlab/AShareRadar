from __future__ import annotations

from app.db.mappers import row_to_provider_capability_status, row_to_provider_status
from app.models.schemas import ProviderCapabilityStatus, ProviderStatus
from app.repositories.base import SQLiteRepository
from app.utils.time import now_text


class ProviderStatusRepository(SQLiteRepository):
    def enabled(self, name: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT enabled FROM provider_status WHERE name = ?", (name,)).fetchone()
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
            last_error=error[:500],
            success_delta=0,
            failure_delta=1,
        )

    def ensure_capability(self, name: str, kind: str, priority: int, enabled: bool = True) -> None:
        timestamp = now_text()
        with self._lock, self._connect() as conn:
            exists = conn.execute(
                "SELECT name FROM provider_capability_status WHERE name = ? AND kind = ?",
                (name, kind),
            ).fetchone()
            if exists:
                conn.execute(
                    """
                    UPDATE provider_capability_status
                    SET enabled = ?, priority = ?
                    WHERE name = ? AND kind = ?
                    """,
                    (int(enabled), priority, name, kind),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO provider_capability_status (
                        name, kind, enabled, priority, healthy, success_count, failure_count, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 0, 0, ?)
                    """,
                    (name, kind, int(enabled), priority, 0, timestamp),
                )
        self._sync_provider_from_capabilities(name, priority)

    def record_capability_success(self, name: str, kind: str, priority: int, latency_ms: float) -> None:
        self._upsert_capability(
            name=name,
            kind=kind,
            enabled=True,
            priority=priority,
            healthy=True,
            latency_ms=latency_ms,
            last_success=now_text(),
            last_error=None,
            success_delta=1,
            failure_delta=0,
        )
        self._sync_provider_from_capabilities(name, priority)

    def record_capability_failure(self, name: str, kind: str, priority: int, error: str) -> None:
        self._upsert_capability(
            name=name,
            kind=kind,
            enabled=True,
            priority=priority,
            healthy=False,
            latency_ms=None,
            last_success=None,
            last_error=error[:500],
            success_delta=0,
            failure_delta=1,
        )
        self._sync_provider_from_capabilities(name, priority)

    def ensure(self, name: str, priority: int, enabled: bool = True) -> None:
        with self._lock, self._connect() as conn:
            exists = conn.execute("SELECT name FROM provider_status WHERE name = ?", (name,)).fetchone()
            if exists:
                conn.execute(
                    """
                    UPDATE provider_status
                    SET enabled = ?, priority = ?
                    WHERE name = ?
                    """,
                    (int(enabled), priority, name),
                )
                return
            conn.execute(
                """
                INSERT INTO provider_status (
                    name, enabled, priority, healthy, success_count, failure_count, updated_at
                ) VALUES (?, ?, ?, ?, 0, 0, ?)
                """,
                (name, int(enabled), priority, int(enabled), now_text()),
            )

    def items(self) -> list[ProviderStatus]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM provider_status ORDER BY priority ASC").fetchall()
            capability_rows = conn.execute("SELECT * FROM provider_capability_status ORDER BY priority ASC, name ASC, kind ASC").fetchall()
        by_name = {row["name"]: row_to_provider_status(row) for row in rows}
        grouped: dict[str, list[ProviderCapabilityStatus]] = {}
        for row in capability_rows:
            item = row_to_provider_capability_status(row)
            grouped.setdefault(item.name, []).append(item)
        for name, statuses in grouped.items():
            by_name[name] = _aggregate_provider_status(name, statuses, by_name.get(name))
        return sorted(by_name.values(), key=lambda item: item.priority)

    def capability_items(self) -> list[ProviderCapabilityStatus]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM provider_capability_status ORDER BY priority ASC, name ASC, kind ASC"
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
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO provider_status (
                    name, enabled, priority, healthy, last_success, last_error,
                    latency_ms, success_count, failure_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    enabled=excluded.enabled,
                    priority=excluded.priority,
                    healthy=excluded.healthy,
                    last_success=COALESCE(excluded.last_success, provider_status.last_success),
                    last_error=excluded.last_error,
                    latency_ms=excluded.latency_ms,
                    success_count=provider_status.success_count + ?,
                    failure_count=provider_status.failure_count + ?,
                    updated_at=excluded.updated_at
                """,
                (
                    name,
                    int(enabled),
                    priority,
                    int(healthy),
                    last_success,
                    last_error,
                    latency_ms,
                    success_delta,
                    failure_delta,
                    now_text(),
                    success_delta,
                    failure_delta,
                ),
            )

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
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO provider_capability_status (
                    name, kind, enabled, priority, healthy, last_success, last_error,
                    latency_ms, success_count, failure_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name, kind) DO UPDATE SET
                    enabled=excluded.enabled,
                    priority=excluded.priority,
                    healthy=excluded.healthy,
                    last_success=COALESCE(excluded.last_success, provider_capability_status.last_success),
                    last_error=excluded.last_error,
                    latency_ms=excluded.latency_ms,
                    success_count=provider_capability_status.success_count + ?,
                    failure_count=provider_capability_status.failure_count + ?,
                    updated_at=excluded.updated_at
                """,
                (
                    name,
                    kind,
                    int(enabled),
                    priority,
                    int(healthy),
                    last_success,
                    last_error,
                    latency_ms,
                    success_delta,
                    failure_delta,
                    now_text(),
                    success_delta,
                    failure_delta,
                ),
            )

    def _sync_provider_from_capabilities(self, name: str, fallback_priority: int) -> None:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM provider_capability_status WHERE name = ?",
                (name,),
            ).fetchall()
            if not rows:
                return
            statuses = [row_to_provider_capability_status(row) for row in rows]
            fallback_row = conn.execute("SELECT * FROM provider_status WHERE name = ?", (name,)).fetchone()
            fallback = row_to_provider_status(fallback_row) if fallback_row else None
            aggregate = _aggregate_provider_status(name, statuses, fallback)
            conn.execute(
                """
                INSERT INTO provider_status (
                    name, enabled, priority, healthy, last_success, last_error,
                    latency_ms, success_count, failure_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    enabled=excluded.enabled,
                    priority=excluded.priority,
                    healthy=excluded.healthy,
                    last_success=COALESCE(excluded.last_success, provider_status.last_success),
                    last_error=excluded.last_error,
                    latency_ms=excluded.latency_ms,
                    success_count=excluded.success_count,
                    failure_count=excluded.failure_count,
                    updated_at=excluded.updated_at
                """,
                (
                    name,
                    int(aggregate.enabled),
                    aggregate.priority or fallback_priority,
                    int(aggregate.healthy),
                    aggregate.last_success,
                    aggregate.last_error,
                    aggregate.latency_ms,
                    aggregate.success_count,
                    aggregate.failure_count,
                    aggregate.updated_at or now_text(),
                ),
            )


def _aggregate_provider_status(
    name: str,
    statuses: list[ProviderCapabilityStatus],
    fallback: ProviderStatus | None,
) -> ProviderStatus:
    if fallback and not any(_capability_has_activity(item) for item in statuses):
        return fallback
    enabled_rows = [item for item in statuses if item.enabled]
    active_rows = enabled_rows or statuses
    priority = min((item.priority for item in active_rows), default=fallback.priority if fallback else 99)
    healthy = any(item.enabled and item.healthy for item in statuses) if statuses else (fallback.healthy if fallback else False)
    last_success = max((item.last_success for item in statuses if item.last_success), default=None)
    error_rows = sorted(
        [item for item in statuses if item.last_error],
        key=lambda item: item.updated_at or "",
        reverse=True,
    )
    last_error = error_rows[0].last_error if error_rows else None
    latency_rows = sorted(
        [item for item in statuses if item.latency_ms is not None],
        key=lambda item: item.updated_at or "",
        reverse=True,
    )
    latency_ms = latency_rows[0].latency_ms if latency_rows else None
    updated_at = max((item.updated_at for item in statuses if item.updated_at), default=fallback.updated_at if fallback else now_text())
    success_count = sum(item.success_count for item in statuses)
    failure_count = sum(item.failure_count for item in statuses)
    return ProviderStatus(
        name=name,
        enabled=any(item.enabled for item in statuses) if statuses else (fallback.enabled if fallback else False),
        priority=priority,
        healthy=healthy,
        last_success=last_success,
        last_error=last_error,
        latency_ms=latency_ms,
        success_count=success_count,
        failure_count=failure_count,
        updated_at=updated_at,
    )


def _capability_has_activity(item: ProviderCapabilityStatus) -> bool:
    return bool(item.last_success or item.last_error or item.success_count or item.failure_count)
