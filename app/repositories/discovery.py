from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from pydantic import TypeAdapter

from app.db.connection import SQLITE_MARKET_EPOCH_FUNCTION
from app.db.market_scan_action_source import require_market_scan_action_source
from app.db.market_scan_integrity import (
    MarketScanSnapshotSealError,
    require_publication_market_scan_snapshot,
)
from app.models.market_scan import MARKET_SCAN_FULL_MARKET_SCOPE
from app.models.discovery import (
    DISCOVERY_PRESET_SCHEMA_VERSION,
    DiscoveryCriteria,
    DiscoveryLeaderboardItem,
    DiscoveryPreset,
    DiscoveryPresetCreate,
    DiscoveryPresetPortable,
    DiscoveryPresetUpdate,
    DiscoveryRankChangeItem,
    DiscoveryRankMovement,
    DiscoveryResearchQueueItem,
    DiscoveryResearchQueueRequest,
    DiscoveryRunReference,
    DiscoverySort,
)
from app.repositories.base import SQLiteRepository
from app.repositories.discovery_sql import canonical_json, canonical_model_json, discovery_filter_sql, discovery_order_sql
from app.utils.errors import NotFoundError


class DiscoveryPresetNameExistsError(ValueError):
    pass


class DiscoveryPresetRevisionError(ValueError):
    pass


_SORT_ADAPTER = TypeAdapter(list[DiscoverySort])
_COMPLETED_RUN_STATUSES = frozenset({"success", "degraded"})
_RANK_COMPARISON_CTE = """
WITH current_rows AS (
    SELECT symbol, code, market, name, status, rank
    FROM market_scan_result
    WHERE run_id = ?
),
previous_rows AS (
    SELECT symbol, code, market, name, status, rank
    FROM market_scan_result
    WHERE run_id = ?
),
merged AS (
    SELECT c.symbol, c.code, c.market, c.name,
           p.status AS previous_status, c.status AS current_status,
           p.rank AS previous_rank, c.rank AS current_rank
    FROM current_rows AS c
    LEFT JOIN previous_rows AS p ON p.symbol = c.symbol
    UNION ALL
    SELECT p.symbol, p.code, p.market, p.name,
           p.status AS previous_status, NULL AS current_status,
           p.rank AS previous_rank, NULL AS current_rank
    FROM previous_rows AS p
    LEFT JOIN current_rows AS c ON c.symbol = p.symbol
    WHERE c.symbol IS NULL
)
"""


class DiscoveryRepository(SQLiteRepository):
    def __init__(self, path: Path, lock: threading.RLock | None = None) -> None:
        super().__init__(Path(path), lock or threading.RLock())

    def create_preset(self, payload: DiscoveryPresetCreate, *, timestamp: str) -> DiscoveryPreset:
        criteria_json = canonical_model_json(payload.criteria)
        sort_json = canonical_json([item.model_dump(mode="json") for item in payload.sort])
        with self._lock, self._connect() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO discovery_preset (
                        name, schema_version, revision, criteria_json, sort_json,
                        column_view, created_at, updated_at
                    ) VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload.name,
                        DISCOVERY_PRESET_SCHEMA_VERSION,
                        criteria_json,
                        sort_json,
                        payload.column_view,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "discovery_preset.name" in str(exc):
                    raise DiscoveryPresetNameExistsError(f"筛选方案名称已存在：{payload.name}") from exc
                raise
            preset_id = cursor.lastrowid
            if preset_id is None:
                raise RuntimeError("筛选方案保存失败")
            row = _preset_row(conn, int(preset_id))
        return _preset_from_row(row)

    def preset(self, preset_id: int) -> DiscoveryPreset:
        with self._lock, self._connect() as conn:
            row = _preset_row(conn, preset_id)
        return _preset_from_row(row)

    def presets(self, *, page: int, page_size: int) -> tuple[list[DiscoveryPreset], int]:
        offset = (page - 1) * page_size
        with self._lock, self._connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM discovery_preset").fetchone()[0])
            rows = conn.execute(
                """
                SELECT * FROM discovery_preset
                ORDER BY updated_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (page_size, offset),
            ).fetchall()
        return [_preset_from_row(row) for row in rows], total

    def rename_preset(
        self,
        preset_id: int,
        *,
        name: str,
        expected_revision: int,
        timestamp: str,
    ) -> DiscoveryPreset:
        with self._lock, self._connect() as conn:
            try:
                cursor = conn.execute(
                    """
                    UPDATE discovery_preset
                    SET name = ?, revision = revision + 1, updated_at = ?
                    WHERE id = ? AND revision = ?
                    """,
                    (name, timestamp, preset_id, expected_revision),
                )
            except sqlite3.IntegrityError as exc:
                if "discovery_preset.name" in str(exc):
                    raise DiscoveryPresetNameExistsError(f"筛选方案名称已存在：{name}") from exc
                raise
            if cursor.rowcount != 1:
                _raise_missing_or_revision(conn, preset_id, expected_revision)
            row = _preset_row(conn, preset_id)
        return _preset_from_row(row)

    def update_preset(
        self,
        preset_id: int,
        payload: DiscoveryPresetUpdate,
        *,
        timestamp: str,
    ) -> DiscoveryPreset:
        criteria_json = canonical_model_json(payload.criteria)
        sort_json = canonical_json([item.model_dump(mode="json") for item in payload.sort])
        with self._lock, self._connect() as conn:
            try:
                cursor = conn.execute(
                    """
                    UPDATE discovery_preset
                    SET name = ?, schema_version = ?, criteria_json = ?, sort_json = ?,
                        column_view = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE id = ? AND revision = ?
                    """,
                    (
                        payload.name,
                        DISCOVERY_PRESET_SCHEMA_VERSION,
                        criteria_json,
                        sort_json,
                        payload.column_view,
                        timestamp,
                        preset_id,
                        payload.expected_revision,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "discovery_preset.name" in str(exc):
                    raise DiscoveryPresetNameExistsError(f"筛选方案名称已存在：{payload.name}") from exc
                raise
            if cursor.rowcount != 1:
                _raise_missing_or_revision(conn, preset_id, payload.expected_revision)
            row = _preset_row(conn, preset_id)
        return _preset_from_row(row)

    def delete_preset(self, preset_id: int, *, expected_revision: int) -> None:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM discovery_preset WHERE id = ? AND revision = ?",
                (preset_id, expected_revision),
            )
            if cursor.rowcount != 1:
                _raise_missing_or_revision(conn, preset_id, expected_revision)

    def run_reference(self, run_id: int) -> DiscoveryRunReference:
        with self._read_snapshot() as conn:
            row = conn.execute(
                """
                SELECT id, status, mode, rule_version, scope, data_date, as_of,
                       snapshot_digest, snapshot_seal_origin, snapshot_sealed_at
                FROM market_scan_run WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is not None:
                _require_full_market_scope(row, run_id)
            if row is not None and str(row["status"]) in _COMPLETED_RUN_STATUSES:
                require_publication_market_scan_snapshot(conn, run_id)
        if row is None:
            raise NotFoundError(f"全市场扫描批次不存在：{run_id}")
        return DiscoveryRunReference.model_validate(dict(row))

    def previous_completed_run_same_mode_any_rule(
        self,
        current: DiscoveryRunReference,
    ) -> DiscoveryRunReference | None:
        if current.scope != MARKET_SCAN_FULL_MARKET_SCOPE:
            raise MarketScanSnapshotSealError(
                f"发现榜单仅接受完整全市场扫描批次：{current.id}"
            )
        parameters: list[object] = [
            current.id,
            current.scope,
            current.mode,
            current.data_date,
            current.as_of,
        ]
        with self._read_snapshot() as conn:
            row = conn.execute(
                f"""
                SELECT id, status, mode, rule_version, scope, data_date, as_of,
                       snapshot_digest, snapshot_seal_origin, snapshot_sealed_at
                FROM market_scan_run
                WHERE id != ?
                  AND status IN ('success', 'degraded')
                  AND scope = ?
                  AND mode = ?
                  AND data_date < ?
                  AND {SQLITE_MARKET_EPOCH_FUNCTION}(as_of)
                      < {SQLITE_MARKET_EPOCH_FUNCTION}(?)
                ORDER BY data_date DESC,
                         {SQLITE_MARKET_EPOCH_FUNCTION}(as_of) DESC,
                         id DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            if row is not None:
                _require_full_market_scope(row, int(row["id"]))
                require_publication_market_scan_snapshot(conn, int(row["id"]))
        return None if row is None else DiscoveryRunReference.model_validate(dict(row))

    def leaderboard(
        self,
        preset: DiscoveryPreset,
        *,
        run_id: int,
        page: int,
        page_size: int,
    ) -> tuple[list[DiscoveryLeaderboardItem], int]:
        where_sql, parameters = discovery_filter_sql(preset.criteria)
        order_sql = discovery_order_sql(preset.sort)
        offset = (page - 1) * page_size
        base_parameters = [run_id, *parameters]
        with self._read_snapshot() as conn:
            _require_discovery_source(conn, run_id)
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM market_scan_result AS r WHERE r.run_id = ? AND {where_sql}",
                    base_parameters,
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"""
                SELECT r.rank AS source_rank, r.symbol, r.code, r.market, r.name,
                       r.industry, r.is_st, r.is_new,
                       r.data_quality_score AS quality, r.trend_score AS trend,
                       r.change_pct AS change, r.turnover_rate AS turnover,
                       r.amount, r.score, r.raw_score
                FROM market_scan_result AS r
                WHERE r.run_id = ? AND {where_sql}
                ORDER BY {order_sql}
                LIMIT ? OFFSET ?
                """,
                [*base_parameters, page_size, offset],
            ).fetchall()
        return [_leaderboard_item(row, position=offset + index + 1) for index, row in enumerate(rows)], total

    def enqueue_research(
        self,
        preset_id: int,
        request: DiscoveryResearchQueueRequest,
        *,
        timestamp: str,
    ) -> list[DiscoveryResearchQueueItem]:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            preset = _preset_from_row(_preset_row(conn, preset_id))
            if preset.revision != request.expected_preset_revision:
                raise DiscoveryPresetRevisionError(f"筛选方案修订冲突：期望 {request.expected_preset_revision}，当前 {preset.revision}")
            _require_discovery_source(conn, request.run_id)
            require_market_scan_action_source(conn, request.run_id)
            result_rows = _matching_queue_rows(conn, preset.criteria, request.run_id, request.symbols)
            found = {str(row["symbol"]) for row in result_rows}
            rejected = [symbol for symbol in request.symbols if symbol not in found]
            if rejected:
                raise ValueError(f"股票不属于当前榜单：{', '.join(rejected)}")
            rows_by_symbol = {str(row["symbol"]): row for row in result_rows}
            snapshot = canonical_model_json(
                DiscoveryPresetPortable(
                    name=preset.name,
                    criteria=preset.criteria,
                    sort=preset.sort,
                    column_view=preset.column_view,
                )
            )
            items = [_enqueue_symbol(conn, rows_by_symbol[symbol], preset, request.run_id, snapshot, timestamp) for symbol in request.symbols]
        return items

    def rank_change_rows(
        self,
        current_run_id: int,
        previous_run_id: int,
        *,
        page: int,
        page_size: int,
    ) -> tuple[list[DiscoveryRankChangeItem], int]:
        offset = (page - 1) * page_size
        parameters = (current_run_id, previous_run_id)
        with self._read_snapshot() as conn:
            _require_discovery_source(conn, current_run_id)
            _require_discovery_source(conn, previous_run_id)
            total = int(conn.execute(f"{_RANK_COMPARISON_CTE} SELECT COUNT(*) FROM merged", parameters).fetchone()[0])
            rows = conn.execute(
                f"""
                {_RANK_COMPARISON_CTE}
                SELECT symbol, code, market, name, previous_status, current_status,
                       previous_rank, current_rank
                FROM merged
                ORDER BY (current_rank IS NULL) ASC, current_rank ASC,
                         previous_rank ASC, symbol ASC
                LIMIT ? OFFSET ?
                """,
                (*parameters, page_size, offset),
            ).fetchall()
        return [_rank_change_item(row) for row in rows], total


def _preset_row(conn: sqlite3.Connection, preset_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM discovery_preset WHERE id = ?", (preset_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"筛选方案不存在：{preset_id}")
    return row


def _preset_from_row(row: sqlite3.Row) -> DiscoveryPreset:
    return DiscoveryPreset(
        id=int(row["id"]),
        name=str(row["name"]),
        schema_version=int(row["schema_version"]),
        revision=int(row["revision"]),
        criteria=DiscoveryCriteria.model_validate_json(str(row["criteria_json"])),
        sort=_SORT_ADAPTER.validate_json(str(row["sort_json"])),
        column_view=str(row["column_view"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _raise_missing_or_revision(conn: sqlite3.Connection, preset_id: int, expected_revision: int) -> None:
    row = conn.execute("SELECT revision FROM discovery_preset WHERE id = ?", (preset_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"筛选方案不存在：{preset_id}")
    raise DiscoveryPresetRevisionError(f"筛选方案修订冲突：期望 {expected_revision}，当前 {int(row['revision'])}")


def _leaderboard_item(row: sqlite3.Row, *, position: int) -> DiscoveryLeaderboardItem:
    values = dict(row)
    values["position"] = position
    values["is_st"] = bool(values["is_st"])
    values["is_new"] = bool(values["is_new"])
    return DiscoveryLeaderboardItem.model_validate(values)


def _require_discovery_source(conn: sqlite3.Connection, run_id: int) -> None:
    row = conn.execute(
        "SELECT status, scope FROM market_scan_run WHERE id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"全市场扫描批次不存在：{run_id}")
    _require_full_market_scope(row, run_id)
    if str(row["status"]) not in _COMPLETED_RUN_STATUSES:
        raise ValueError(f"全市场扫描批次尚未完成：{run_id}")
    require_publication_market_scan_snapshot(conn, run_id)


def _require_full_market_scope(row: sqlite3.Row, run_id: int) -> None:
    if str(row["scope"]) != MARKET_SCAN_FULL_MARKET_SCOPE:
        raise MarketScanSnapshotSealError(
            f"发现榜单仅接受完整全市场扫描批次：{run_id}"
        )


def _matching_queue_rows(
    conn: sqlite3.Connection,
    criteria: DiscoveryCriteria,
    run_id: int,
    symbols: list[str],
) -> list[sqlite3.Row]:
    where_sql, filter_parameters = discovery_filter_sql(criteria)
    symbol_placeholders = ",".join("?" for _ in symbols)
    return conn.execute(
        f"""
        SELECT r.symbol, r.code, r.market, r.name
        FROM market_scan_result AS r
        WHERE r.run_id = ? AND {where_sql}
          AND r.symbol IN ({symbol_placeholders})
        """,
        [run_id, *filter_parameters, *symbols],
    ).fetchall()


def _enqueue_symbol(
    conn: sqlite3.Connection,
    result: sqlite3.Row,
    preset: DiscoveryPreset,
    run_id: int,
    snapshot: str,
    timestamp: str,
) -> DiscoveryResearchQueueItem:
    symbol = str(result["symbol"])
    _upsert_research_watchlist(conn, result, timestamp=timestamp)
    added = _insert_research_provenance(
        conn,
        symbol=symbol,
        preset=preset,
        run_id=run_id,
        snapshot=snapshot,
        timestamp=timestamp,
    )
    enqueued_at = (
        timestamp
        if added
        else _existing_research_enqueue_time(
            conn,
            symbol=symbol,
            preset=preset,
            run_id=run_id,
        )
    )
    return DiscoveryResearchQueueItem(
        symbol=symbol,
        source_run_id=run_id,
        source_preset_id=preset.id,
        source_preset_revision=preset.revision,
        source_preset_name=preset.name,
        enqueued_at=enqueued_at,
        added=added,
    )


def _upsert_research_watchlist(
    conn: sqlite3.Connection,
    result: sqlite3.Row,
    *,
    timestamp: str,
) -> None:
    conn.execute(
        """
        INSERT INTO watchlist (
            symbol, code, market, name, research_status, priority,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'to_research', 'medium', ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            code = excluded.code,
            market = excluded.market,
            name = excluded.name,
            research_status = CASE
                WHEN watchlist.research_status = 'holding_research'
                    THEN watchlist.research_status
                ELSE 'to_research'
            END,
            updated_at = excluded.updated_at
        """,
        (
            result["symbol"],
            result["code"],
            result["market"],
            result["name"],
            timestamp,
            timestamp,
        ),
    )


def _insert_research_provenance(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    preset: DiscoveryPreset,
    run_id: int,
    snapshot: str,
    timestamp: str,
) -> bool:
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO discovery_research_queue_source (
            symbol, source_run_id, source_preset_id, source_preset_revision,
            source_preset_name, preset_schema_version, preset_snapshot_json,
            enqueued_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            symbol,
            run_id,
            preset.id,
            preset.revision,
            preset.name,
            preset.schema_version,
            snapshot,
            timestamp,
        ),
    )
    return cursor.rowcount == 1


def _existing_research_enqueue_time(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    preset: DiscoveryPreset,
    run_id: int,
) -> str:
    existing = conn.execute(
        """
        SELECT enqueued_at
        FROM discovery_research_queue_source
        WHERE symbol = ? AND source_run_id = ? AND source_preset_id = ?
          AND source_preset_revision = ?
        """,
        (symbol, run_id, preset.id, preset.revision),
    ).fetchone()
    if existing is None:
        raise RuntimeError(f"研究队列来源读取失败：{symbol}")
    return str(existing["enqueued_at"])


def _rank_change_item(row: sqlite3.Row) -> DiscoveryRankChangeItem:
    previous_rank = _optional_int(row["previous_rank"])
    current_rank = _optional_int(row["current_rank"])
    previous_status = _optional_str(row["previous_status"])
    current_status = _optional_str(row["current_status"])
    rank_delta, movement = _rank_change_state(
        previous_rank=previous_rank,
        current_rank=current_rank,
        previous_status=previous_status,
        current_status=current_status,
    )
    return DiscoveryRankChangeItem(
        symbol=str(row["symbol"]),
        code=str(row["code"]),
        market=str(row["market"]),
        name=str(row["name"]),
        previous_rank=previous_rank,
        current_rank=current_rank,
        rank_delta=rank_delta,
        movement=movement,
    )


def _rank_change_state(
    *,
    previous_rank: int | None,
    current_rank: int | None,
    previous_status: str | None,
    current_status: str | None,
) -> tuple[int | None, DiscoveryRankMovement]:
    if previous_status is None and current_status == "success":
        return None, "new"
    if current_status is None and previous_status == "success":
        return None, "exit"
    if previous_status != "success" or current_status != "success":
        return None, "unavailable"
    assert previous_rank is not None and current_rank is not None
    rank_delta = previous_rank - current_rank
    return rank_delta, _rank_delta_movement(rank_delta)


def _rank_delta_movement(rank_delta: int) -> DiscoveryRankMovement:
    if rank_delta > 0:
        return "up"
    if rank_delta < 0:
        return "down"
    return "unchanged"


def _optional_int(value: int | float | str | bytes | bytearray | None) -> int | None:
    return None if value is None else int(value)


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


__all__ = [
    "DiscoveryPresetNameExistsError",
    "DiscoveryPresetRevisionError",
    "DiscoveryRepository",
]
