from __future__ import annotations

from app.config import get_settings
from app.repositories.base import SQLiteRepository


class RuntimeMaintenanceRepository(SQLiteRepository):
    def cleanup_runtime_rows(self) -> dict[str, int]:
        settings = get_settings()
        limits = {
            "quote_history": settings.max_quote_history_rows,
            "kline_minute": settings.max_minute_kline_rows,
            "stock_concept": settings.max_stock_concept_rows,
            "task_run": settings.max_task_run_rows,
            "monitor_event": settings.max_monitor_event_rows,
            "advice_history": settings.max_advice_history_rows,
        }
        removed: dict[str, int] = {}
        with self._lock, self._connect() as conn:
            for table, limit in limits.items():
                if limit <= 0:
                    removed[table] = 0
                    continue
                before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                if table == "kline_minute":
                    conn.execute(
                        """
                        DELETE FROM kline_minute
                        WHERE rowid NOT IN (
                            SELECT rowid FROM kline_minute
                            ORDER BY fetched_at DESC, timestamp DESC
                            LIMIT ?
                        )
                        """,
                        (limit,),
                    )
                elif table == "stock_concept":
                    conn.execute(
                        """
                        DELETE FROM stock_concept
                        WHERE rowid NOT IN (
                            SELECT rowid FROM stock_concept
                            ORDER BY updated_at DESC, symbol ASC, rank ASC
                            LIMIT ?
                        )
                        """,
                        (limit,),
                    )
                elif table == "monitor_event":
                    conn.execute(
                        """
                        DELETE FROM monitor_event
                        WHERE id NOT IN (
                            SELECT id FROM monitor_event
                            ORDER BY COALESCE(last_seen_at, created_at) DESC, id DESC
                            LIMIT ?
                        )
                        """,
                        (limit,),
                    )
                else:
                    conn.execute(
                        f"""
                        DELETE FROM {table}
                        WHERE id NOT IN (
                            SELECT id FROM {table}
                            ORDER BY id DESC
                            LIMIT ?
                        )
                        """,
                        (limit,),
                    )
                after = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                removed[table] = before - after
        return removed

    def table_counts(self) -> dict[str, int]:
        tables = [
            "provider_status",
            "quote_snapshot",
            "quote_history",
            "kline_daily",
            "kline_minute",
            "stock_master",
            "plate_rank",
            "stock_concept",
            "task_run",
            "monitor_event",
            "watchlist",
            "advice_history",
            "alert_rule",
            "alert_event",
            "stock_note",
        ]
        with self._lock, self._connect() as conn:
            return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
