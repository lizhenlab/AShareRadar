from __future__ import annotations

import json
import sqlite3

from app.models.reviews import (
    WatchlistScanHistoryItem,
    WatchlistScanRecord,
    WatchlistScanRequest,
    WatchlistScanResponse,
)
from app.repositories.base import SQLiteRepository
from app.utils.audit_time import audit_now_text


MAX_WATCHLIST_SCAN_HISTORY = 100


class WatchlistScanRepository(SQLiteRepository):
    def save(self, payload: WatchlistScanRequest, result: WatchlistScanResponse) -> WatchlistScanRecord:
        created_at = audit_now_text()
        result_json = json.dumps(result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        conditions_json = json.dumps(result.conditions, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO watchlist_scan_history (
                    universe_kind, as_of, rule_version, conditions_json,
                    universe_count, success_count, matched_count, missing_count,
                    result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.universe,
                    result.as_of,
                    result.rule_version,
                    conditions_json,
                    len(result.universe),
                    len(result.success),
                    sum(item.matched for item in result.success),
                    len(result.missing),
                    result_json,
                    created_at,
                ),
            )
            row_id = int(cursor.lastrowid)
            conn.execute(
                """
                DELETE FROM watchlist_scan_history
                WHERE id NOT IN (
                    SELECT id FROM watchlist_scan_history ORDER BY id DESC LIMIT ?
                )
                """,
                (MAX_WATCHLIST_SCAN_HISTORY,),
            )
            row = conn.execute("SELECT * FROM watchlist_scan_history WHERE id = ?", (row_id,)).fetchone()
        if row is None:
            raise RuntimeError("观察池扫描历史保存失败")
        return _record_from_row(row)

    def items(self, *, limit: int = 20) -> list[WatchlistScanHistoryItem]:
        if limit <= 0:
            return []
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM watchlist_scan_history ORDER BY id DESC LIMIT ?",
                (min(limit, MAX_WATCHLIST_SCAN_HISTORY),),
            ).fetchall()
        return [_history_item_from_row(row) for row in rows]

    def record(self, row_id: int) -> WatchlistScanRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM watchlist_scan_history WHERE id = ?", (row_id,)).fetchone()
        return _record_from_row(row) if row is not None else None


def _history_item_from_row(row: sqlite3.Row) -> WatchlistScanHistoryItem:
    conditions = json.loads(str(row["conditions_json"] or "[]"))
    return WatchlistScanHistoryItem(
        id=int(row["id"]),
        universe_kind=str(row["universe_kind"]),
        as_of=str(row["as_of"]),
        rule_version=str(row["rule_version"]),
        conditions=conditions if isinstance(conditions, list) else [],
        universe_count=int(row["universe_count"]),
        success_count=int(row["success_count"]),
        matched_count=int(row["matched_count"]),
        missing_count=int(row["missing_count"]),
        created_at=str(row["created_at"]),
    )


def _record_from_row(row: sqlite3.Row) -> WatchlistScanRecord:
    payload = json.loads(str(row["result_json"] or "{}"))
    if not isinstance(payload, dict):
        raise ValueError("观察池扫描历史格式异常")
    return WatchlistScanRecord.model_validate(
        {
            **payload,
            "id": int(row["id"]),
            "universe_kind": str(row["universe_kind"]),
            "created_at": str(row["created_at"]),
        }
    )


__all__ = ["MAX_WATCHLIST_SCAN_HISTORY", "WatchlistScanRepository"]
