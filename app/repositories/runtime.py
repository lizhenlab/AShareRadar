from __future__ import annotations

from datetime import datetime

from app.db.mappers import row_to_monitor_event, row_to_task_run
from app.models.schemas import MonitorEvent, TaskRun
from app.repositories.base import SQLiteRepository
from app.utils.symbols import standard_symbol
from app.utils.time import now_text, parse_text_time


class RuntimeEventRepository(SQLiteRepository):
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
            return int(cursor.lastrowid)

    def finish_task_run(self, run_id: int, status: str, message: str | None = None) -> None:
        finished_at = now_text()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT started_at FROM task_run WHERE id = ?", (run_id,)).fetchone()
            duration_ms = None
            if row:
                started_at = parse_text_time(row["started_at"])
                duration_ms = int((datetime.now() - started_at).total_seconds() * 1000)
            conn.execute(
                """
                UPDATE task_run
                SET status = ?, finished_at = ?, duration_ms = ?, message = ?
                WHERE id = ?
                """,
                (status, finished_at, duration_ms, (message or "")[:800], run_id),
            )

    def task_runs(self, limit: int = 20) -> list[TaskRun]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_run
                ORDER BY started_at DESC, id DESC
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
                """
                SELECT id FROM monitor_event
                WHERE level = ? AND category = ? AND symbol IS ? AND message = ?
                ORDER BY COALESCE(last_seen_at, created_at) DESC, id DESC
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
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM monitor_event
                ORDER BY COALESCE(last_seen_at, created_at) DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [row_to_monitor_event(row) for row in rows]
