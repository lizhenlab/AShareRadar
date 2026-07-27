from __future__ import annotations

from pathlib import Path
import threading

from app.db.system_mappers import row_to_monitor_event, row_to_task_run
from app.db.connection import SQLITE_AUDIT_EPOCH_FUNCTION
from app.models.system import (
    MonitorEvent,
    TaskRun,
)
from app.repositories.base import SQLiteRepository
from app.utils.audit_time import audit_now_text as now_text
from app.utils.clock import market_now_naive, monotonic_now
from app.utils.symbols import standard_symbol
from app.utils.time import parse_text_time


class RuntimeEventRepository(SQLiteRepository):
    def __init__(self, path: Path, lock: threading.RLock) -> None:
        super().__init__(path, lock)
        self._task_started_monotonic: dict[int, float] = {}

    def log_event(self, category: str, message: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO cache_event (category, message, created_at) VALUES (?, ?, ?)",
                (category, message, now_text()),
            )

    def start_task_run(self, task_name: str) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO task_run (task_name, status, started_at)
                VALUES (?, ?, ?)
                """,
                (task_name, "running", now_text()),
            )
            run_id = int(cursor.lastrowid)
            self._task_started_monotonic[run_id] = monotonic_now()
            return run_id

    def finish_task_run(self, run_id: int, status: str, message: str | None = None) -> None:
        finished_at = now_text()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT started_at FROM task_run WHERE id = ?", (run_id,)).fetchone()
            duration_ms = _task_duration_ms(
                row["started_at"] if row else None,
                started_monotonic=self._task_started_monotonic.get(run_id),
            )
            conn.execute(
                """
                UPDATE task_run
                SET status = ?, finished_at = ?, duration_ms = ?, message = ?
                WHERE id = ? AND (status = 'running' OR ? = 'cancelled')
                """,
                (status, finished_at, duration_ms, (message or "")[:800], run_id, status),
            )
            self._task_started_monotonic.pop(run_id, None)

    def reconcile_orphaned_task_runs(self, message: str = "应用重启时终止遗留运行记录") -> int:
        finished_at = now_text()
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE task_run
                SET status = 'cancelled',
                    finished_at = ?,
                    duration_ms = CASE
                        WHEN julianday(started_at) IS NULL THEN NULL
                        ELSE MAX(0, CAST((julianday(?) - julianday(started_at)) * 86400000 AS INTEGER))
                    END,
                    message = ?
                WHERE status = 'running'
                """,
                (finished_at, finished_at, message[:800]),
            )
            return max(0, int(cursor.rowcount))

    def task_runs(self, limit: int = 20) -> list[TaskRun]:
        if limit <= 0:
            return []
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM task_run
                ORDER BY {SQLITE_AUDIT_EPOCH_FUNCTION}(started_at) DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [row_to_task_run(row) for row in rows]

    def save_monitor_event(self, level: str, category: str, message: str, symbol: str | None = None) -> None:
        timestamp = now_text()
        normalized_symbol = standard_symbol(symbol) if symbol else None
        trimmed_message = message[:800]
        with self._lock, self._connect() as conn:
            recent = conn.execute(
                f"""
                SELECT id FROM monitor_event
                WHERE level = ? AND category = ? AND symbol IS ? AND message = ?
                ORDER BY {SQLITE_AUDIT_EPOCH_FUNCTION}(COALESCE(last_seen_at, created_at)) DESC, id DESC
                LIMIT 1
                """,
                (level, category, normalized_symbol, trimmed_message),
            ).fetchone()
            if recent:
                conn.execute(
                    """
                    UPDATE monitor_event
                    SET last_seen_at = ?, repeat_count = COALESCE(repeat_count, 1) + 1
                    WHERE id = ?
                    """,
                    (timestamp, recent["id"]),
                )
                return
            conn.execute(
                """
                INSERT INTO monitor_event (level, category, symbol, message, created_at, last_seen_at, repeat_count)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (level, category, normalized_symbol, trimmed_message, timestamp, timestamp),
            )

    def monitor_events(self, limit: int = 30) -> list[MonitorEvent]:
        if limit <= 0:
            return []
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM monitor_event
                ORDER BY {SQLITE_AUDIT_EPOCH_FUNCTION}(COALESCE(last_seen_at, created_at)) DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [row_to_monitor_event(row) for row in rows]


def _task_duration_ms(started_at_text: object, *, started_monotonic: float | None = None) -> int | None:
    if started_monotonic is not None:
        return max(0, round((monotonic_now() - started_monotonic) * 1000))
    if not isinstance(started_at_text, str):
        return None
    try:
        started_at = parse_text_time(started_at_text)
    except ValueError:
        return None
    return max(0, int((market_now_naive() - started_at).total_seconds() * 1000))
