"""Database-local integrity seals for immutable published market-scan snapshots."""

from __future__ import annotations

import hmac
import hashlib
import json
import sqlite3
from datetime import datetime
from collections.abc import Callable, Sequence
from typing import Final, Mapping, Protocol

from app.artifacts.io import (
    ArtifactCanonicalJsonError,
    ArtifactIOError,
    canonical_json_text,
    decode_json_bytes,
)
from app.utils.audit_time import audit_now_text, parse_audit_time


MARKET_SCAN_SNAPSHOT_DIGEST_CONTRACT: Final[str] = "market-scan-snapshot-digest-v2"
PUBLISHED_MARKET_SCAN_STATUSES: Final[frozenset[str]] = frozenset(
    {"success", "degraded"}
)
_IMMUTABILITY_TRIGGERS: Final[tuple[str, ...]] = (
    "trg_market_scan_published_run_immutable",
    "trg_market_scan_published_run_no_delete",
    "trg_market_scan_published_result_no_update",
    "trg_market_scan_published_result_no_delete",
    "trg_market_scan_published_result_no_insert",
)

_RUN_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "task_run_id",
    "retry_of_run_id",
    "status",
    "trigger",
    "mode",
    "rule_version",
    "as_of",
    "data_date",
    "quote_date",
    "scope",
    "stock_pool_source",
    "total_count",
    "excluded_count",
    "processed_count",
    "success_count",
    "missing_count",
    "skipped_count",
    "retry_count",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "duration_ms",
    "quote_capture_started_at",
    "quote_capture_finished_at",
    "quote_capture_duration_ms",
    "quote_capture_count",
    "current_stage",
    "stage_started_at",
    "stage_metrics_json",
    "market_progress_json",
    "message",
    "last_error",
    "publication_diagnostics_json",
    "snapshot_seal_origin",
    "snapshot_sealed_at",
    "cancel_requested_at",
)

_RUN_JSON_FIELDS: Final[frozenset[str]] = frozenset(
    {"stage_metrics_json", "market_progress_json", "publication_diagnostics_json"}
)

_RESULT_FIELDS: Final[tuple[str, ...]] = (
    "run_id",
    "symbol",
    "code",
    "market",
    "name",
    "industry",
    "list_date",
    "is_st",
    "is_new",
    "metadata_source",
    "status",
    "rank",
    "score",
    "raw_score",
    "trend_score",
    "leader_score",
    "data_quality_score",
    "price",
    "change_pct",
    "turnover_rate",
    "volume_ratio",
    "amount",
    "tags_json",
    "metrics_json",
    "reason",
    "error",
    "data_date",
    "quote_timestamp",
    "quote_observed_at",
    "quote_source",
    "kline_source",
    "adjustment_mode",
    "quote_fallback_used",
    "kline_fallback_used",
    "metadata_degraded",
    "degradation_reasons_json",
    "updated_at",
)

_RESULT_JSON_FIELDS: Final[frozenset[str]] = frozenset(
    {"tags_json", "metrics_json", "degradation_reasons_json"}
)


class MarketScanSnapshotSealError(ValueError):
    """A published database snapshot is missing, malformed, or has changed."""


class _DigestWriter(Protocol):
    def update(self, value: bytes) -> None: ...


def market_scan_snapshot_digest(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    result_observer: Callable[[Mapping[str, object]], None] | None = None,
) -> str:
    """Rebuild the canonical digest from one run and every persisted result row."""

    run = _query_one(conn, "SELECT * FROM market_scan_run WHERE id = ?", (run_id,))
    if run is None:
        raise MarketScanSnapshotSealError(f"全市场扫描批次不存在：{run_id}")
    if str(run["status"]) not in PUBLISHED_MARKET_SCAN_STATUSES:
        raise MarketScanSnapshotSealError(f"扫描批次 {run_id} 尚未发布，不能生成快照摘要")
    cursor = conn.execute(
        "SELECT * FROM market_scan_result WHERE run_id = ? ORDER BY symbol ASC",
        (run_id,),
    )
    try:
        digest = hashlib.sha256()
        _update_snapshot_digest_prefix(digest)
        _update_snapshot_result_rows(
            digest,
            cursor,
            result_observer=result_observer,
        )
        digest.update(b'],"run":')
        digest.update(
            _canonical_database_row_text(
                _canonical_row(run, _RUN_FIELDS, _RUN_JSON_FIELDS, path="run")
            ).encode("utf-8")
        )
        digest.update(b"}")
        return digest.hexdigest()
    except ArtifactIOError as exc:
        raise MarketScanSnapshotSealError(
            f"扫描批次 {run_id} 包含不可规范化的快照数据"
        ) from exc


def _update_snapshot_digest_prefix(digest: _DigestWriter) -> None:
    encoded_contract = canonical_json_text(MARKET_SCAN_SNAPSHOT_DIGEST_CONTRACT)
    digest.update(b'{"contract_version":')
    digest.update(encoded_contract.encode("utf-8"))
    digest.update(b',"results":[')


def _update_snapshot_result_rows(
    digest: _DigestWriter,
    cursor: sqlite3.Cursor,
    *,
    result_observer: Callable[[Mapping[str, object]], None] | None = None,
) -> None:
    first = True
    for raw_row in cursor:
        row = _row_mapping(cursor, raw_row)
        if not first:
            digest.update(b",")
        first = False
        canonical = _canonical_row(
            row,
            _RESULT_FIELDS,
            _RESULT_JSON_FIELDS,
            path=f"result[{row['symbol']}]",
        )
        digest.update(_canonical_database_row_text(canonical).encode("utf-8"))
        if result_observer is not None:
            result_observer(canonical)


def _canonical_database_row_text(value: Mapping[str, object]) -> str:
    """Encode a row whose nested JSON was already decoded by the strict parser."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ArtifactCanonicalJsonError from exc


def seal_market_scan_snapshot(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    origin: str = "publication",
    sealed_at: str | None = None,
) -> str:
    """Set a publication seal exactly once, or verify an existing seal."""

    row = _query_one(
        conn,
        """
        SELECT status, snapshot_digest, snapshot_seal_origin, snapshot_sealed_at
        FROM market_scan_run WHERE id = ?
        """,
        (run_id,),
    )
    if row is None:
        raise MarketScanSnapshotSealError(f"全市场扫描批次不存在：{run_id}")
    if str(row["status"]) not in PUBLISHED_MARKET_SCAN_STATUSES:
        raise MarketScanSnapshotSealError(f"扫描批次 {run_id} 尚未发布，不能封印快照")
    expected = _optional_digest(row["snapshot_digest"])
    normalized_origin = _seal_origin(origin)
    if expected is not None:
        return _verify_existing_seal(
            conn,
            run_id,
            row,
            expected=expected,
            origin=normalized_origin,
        )
    stamp = _required_seal_stamp(sealed_at)
    if normalized_origin == "publication":
        _require_publication_time_order(conn, run_id, stamp)
    _persist_seal_provenance(conn, run_id, normalized_origin, stamp)
    return _persist_seal_digest(conn, run_id)


def _verify_existing_seal(
    conn: sqlite3.Connection,
    run_id: int,
    row: Mapping[str, object],
    *,
    expected: str,
    origin: str,
) -> str:
    if row["snapshot_seal_origin"] != origin or not str(
        row["snapshot_sealed_at"] or ""
    ).strip():
        raise MarketScanSnapshotSealError(f"扫描批次 {run_id} 的快照封印来源不一致")
    if origin == "publication":
        _require_publication_time_order(
            conn,
            run_id,
            str(row["snapshot_sealed_at"]),
        )
    actual = market_scan_snapshot_digest(conn, run_id)
    if not hmac.compare_digest(expected, actual):
        raise MarketScanSnapshotSealError(f"扫描批次 {run_id} 的已发布快照摘要不一致")
    return actual


def _required_seal_stamp(value: str | None) -> str:
    stamp = str(value or audit_now_text()).strip()
    if not stamp:
        raise MarketScanSnapshotSealError("快照封印时间不能为空")
    return stamp


def _persist_seal_provenance(
    conn: sqlite3.Connection,
    run_id: int,
    origin: str,
    stamp: str,
) -> None:
    provenance = conn.execute(
        """
        UPDATE market_scan_run
        SET snapshot_seal_origin = ?, snapshot_sealed_at = ?
        WHERE id = ? AND status IN ('success', 'degraded')
          AND snapshot_digest IS NULL AND snapshot_seal_origin IS NULL
          AND snapshot_sealed_at IS NULL
        """,
        (origin, stamp, run_id),
    )
    if provenance.rowcount != 1:
        raise MarketScanSnapshotSealError(f"扫描批次 {run_id} 的快照来源并发封印失败")


def _persist_seal_digest(conn: sqlite3.Connection, run_id: int) -> str:
    actual = market_scan_snapshot_digest(conn, run_id)
    updated = conn.execute(
        """
        UPDATE market_scan_run
        SET snapshot_digest = ?
        WHERE id = ? AND status IN ('success', 'degraded') AND snapshot_digest IS NULL
        """,
        (actual, run_id),
    )
    if updated.rowcount != 1:
        raise MarketScanSnapshotSealError(f"扫描批次 {run_id} 的快照摘要并发封印失败")
    return actual


def verify_market_scan_snapshot(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    result_observer: Callable[[Mapping[str, object]], None] | None = None,
) -> str:
    """Fail closed unless a published snapshot still matches its original seal."""

    row = _query_one(
        conn,
        """
        SELECT status, snapshot_digest, snapshot_seal_origin, snapshot_sealed_at
        FROM market_scan_run WHERE id = ?
        """,
        (run_id,),
    )
    if row is None:
        raise MarketScanSnapshotSealError(f"全市场扫描批次不存在：{run_id}")
    if str(row["status"]) not in PUBLISHED_MARKET_SCAN_STATUSES:
        raise MarketScanSnapshotSealError(f"扫描批次 {run_id} 尚未发布，不能验证快照")
    expected = _optional_digest(row["snapshot_digest"])
    if expected is None:
        raise MarketScanSnapshotSealError(f"扫描批次 {run_id} 缺少已发布快照摘要")
    _seal_origin(row["snapshot_seal_origin"])
    if not str(row["snapshot_sealed_at"] or "").strip():
        raise MarketScanSnapshotSealError(f"扫描批次 {run_id} 缺少快照封印时间")
    if str(row["snapshot_seal_origin"]) == "publication":
        _require_publication_time_order(
            conn,
            run_id,
            str(row["snapshot_sealed_at"]),
        )
    actual = market_scan_snapshot_digest(
        conn,
        run_id,
        result_observer=result_observer,
    )
    if not hmac.compare_digest(expected, actual):
        raise MarketScanSnapshotSealError(f"扫描批次 {run_id} 的已发布快照摘要不一致")
    return actual


def _require_publication_time_order(
    conn: sqlite3.Connection,
    run_id: int,
    sealed_at: str,
) -> None:
    row = conn.execute(
        "SELECT finished_at, updated_at FROM market_scan_run WHERE id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise MarketScanSnapshotSealError(f"全市场扫描批次不存在：{run_id}")
    finished = _audit_time(row[0], "finished_at", run_id)
    updated = _audit_time(row[1], "updated_at", run_id)
    sealed = _audit_time(sealed_at, "snapshot_sealed_at", run_id)
    if finished > updated:
        raise MarketScanSnapshotSealError(
            f"扫描批次 {run_id} 的 updated_at 早于 finished_at"
        )
    if updated > sealed:
        raise MarketScanSnapshotSealError(
            f"扫描批次 {run_id} 的 snapshot_sealed_at 早于 updated_at"
        )
    _require_result_times_not_after(conn, run_id, updated)


def _require_result_times_not_after(
    conn: sqlite3.Connection,
    run_id: int,
    run_updated_at: datetime,
) -> None:
    rows = conn.execute(
        "SELECT symbol, updated_at FROM market_scan_result WHERE run_id = ?",
        (run_id,),
    )
    for row in rows:
        if _audit_time(row[1], f"result[{row[0]}].updated_at", run_id) > run_updated_at:
            raise MarketScanSnapshotSealError(
                f"扫描批次 {run_id} 的结果更新时间晚于批次更新时间"
            )


def _audit_time(value: object, field: str, run_id: int) -> datetime:
    try:
        return parse_audit_time(str(value or ""))
    except (TypeError, ValueError) as exc:
        raise MarketScanSnapshotSealError(
            f"扫描批次 {run_id} 的 {field} 不是有效审计时间"
        ) from exc


def require_publication_market_scan_snapshot(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    result_observer: Callable[[Mapping[str, object]], None] | None = None,
) -> str:
    """Require an original publication-time seal, never a migration backfill."""

    digest = verify_market_scan_snapshot(
        conn,
        run_id,
        result_observer=result_observer,
    )
    row = conn.execute(
        "SELECT snapshot_seal_origin FROM market_scan_run WHERE id = ?",
        (run_id,),
    ).fetchone()
    if row is None or str(row[0]) != "publication":
        raise MarketScanSnapshotSealError(
            f"扫描批次 {run_id} 仅有 legacy backfill 封印，不能证明原发布快照"
        )
    return digest


def backfill_market_scan_snapshot_digests(conn: sqlite3.Connection) -> int:
    """Seal legacy published rows after all compatibility migrations have finished."""

    drop_market_scan_immutability_triggers(conn)
    rows = conn.execute(
        """
        SELECT id, snapshot_digest, snapshot_seal_origin, snapshot_sealed_at
        FROM market_scan_run
        WHERE status IN ('success', 'degraded')
        ORDER BY id ASC
        """
    ).fetchall()
    stamp = audit_now_text()
    for row in rows:
        conn.execute(
            """
            UPDATE market_scan_run
            SET snapshot_digest = NULL,
                snapshot_seal_origin = NULL,
                snapshot_sealed_at = NULL
            WHERE id = ?
            """,
            (int(row[0]),),
        )
        seal_market_scan_snapshot(
            conn,
            int(row[0]),
            origin="legacy_backfill",
            sealed_at=stamp,
        )
    sealed_rows = conn.execute(
        """
        SELECT id FROM market_scan_run
        WHERE status IN ('success', 'degraded')
        ORDER BY id ASC
        """
    ).fetchall()
    for sealed in sealed_rows:
        verify_market_scan_snapshot(conn, int(sealed[0]))
    create_market_scan_immutability_triggers(conn)
    return len(rows)


def drop_market_scan_immutability_triggers(conn: sqlite3.Connection) -> None:
    """Remove write guards before an atomic compatibility table rebuild."""

    for name in _IMMUTABILITY_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")


def market_scan_seal_columns_ready(conn: sqlite3.Connection) -> bool:
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(market_scan_run)").fetchall()
    }
    return {
        "snapshot_digest",
        "snapshot_seal_origin",
        "snapshot_sealed_at",
    }.issubset(columns)


def create_market_scan_immutability_triggers(conn: sqlite3.Connection) -> None:
    """Protect a published snapshot after its digest has been persisted."""

    statements = (
        """
        CREATE TRIGGER IF NOT EXISTS trg_market_scan_published_run_immutable
        BEFORE UPDATE ON market_scan_run WHEN OLD.snapshot_digest IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'published market_scan_run is immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_market_scan_published_run_no_delete
        BEFORE DELETE ON market_scan_run WHEN OLD.snapshot_digest IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'published market_scan_run is immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_market_scan_published_result_no_update
        BEFORE UPDATE ON market_scan_result
        WHEN EXISTS (SELECT 1 FROM market_scan_run AS run
                     WHERE run.id = OLD.run_id AND run.snapshot_digest IS NOT NULL)
        BEGIN
            SELECT RAISE(ABORT, 'published market_scan_result is immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_market_scan_published_result_no_delete
        BEFORE DELETE ON market_scan_result
        WHEN EXISTS (SELECT 1 FROM market_scan_run AS run
                     WHERE run.id = OLD.run_id AND run.snapshot_digest IS NOT NULL)
        BEGIN
            SELECT RAISE(ABORT, 'published market_scan_result is immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_market_scan_published_result_no_insert
        BEFORE INSERT ON market_scan_result
        WHEN EXISTS (SELECT 1 FROM market_scan_run AS run
                     WHERE run.id = NEW.run_id AND run.snapshot_digest IS NOT NULL)
        BEGIN
            SELECT RAISE(ABORT, 'published market_scan_result is immutable');
        END
        """,
    )
    for statement in statements:
        conn.execute(statement)


def market_scan_immutability_triggers_present(
    conn: sqlite3.Connection,
) -> frozenset[str]:
    """Return the installed published-snapshot write guards."""

    placeholders = ", ".join("?" for _ in _IMMUTABILITY_TRIGGERS)
    rows = conn.execute(
        f"""
        SELECT name FROM sqlite_master
        WHERE type = 'trigger' AND name IN ({placeholders})
        """,
        _IMMUTABILITY_TRIGGERS,
    ).fetchall()
    return frozenset(str(row[0]) for row in rows)


def delete_verified_market_scan_snapshots(
    conn: sqlite3.Connection,
    run_ids: Sequence[int],
) -> int:
    """Delete exact, unreferenced sealed snapshots inside the caller transaction."""

    if not conn.in_transaction:
        raise MarketScanSnapshotSealError("已发布快照清理必须在显式写事务内执行")
    normalized = tuple(sorted(set(int(run_id) for run_id in run_ids)))
    if not normalized:
        return 0
    if any(run_id <= 0 for run_id in normalized):
        raise MarketScanSnapshotSealError("待清理的扫描批次 ID 无效")
    create_market_scan_immutability_triggers(conn)
    _require_market_scan_immutability_triggers(conn)
    for run_id in normalized:
        verify_market_scan_snapshot(conn, run_id)
    deleted = 0
    try:
        drop_market_scan_immutability_triggers(conn)
        for batch in _id_batches(normalized):
            placeholders = ", ".join("?" for _ in batch)
            conn.execute(
                f"DELETE FROM market_scan_result WHERE run_id IN ({placeholders})",
                batch,
            )
            cursor = conn.execute(
                f"DELETE FROM market_scan_run WHERE id IN ({placeholders})",
                batch,
            )
            deleted += max(0, int(cursor.rowcount))
    finally:
        create_market_scan_immutability_triggers(conn)
    _require_market_scan_immutability_triggers(conn)
    if deleted != len(normalized):
        raise MarketScanSnapshotSealError("已验证扫描快照未被完整删除")
    return deleted


def _require_market_scan_immutability_triggers(conn: sqlite3.Connection) -> None:
    installed = market_scan_immutability_triggers_present(conn)
    expected = frozenset(_IMMUTABILITY_TRIGGERS)
    if installed != expected:
        raise MarketScanSnapshotSealError("全市场扫描不可变触发器不完整")


def _id_batches(run_ids: tuple[int, ...]) -> tuple[tuple[int, ...], ...]:
    return tuple(run_ids[start : start + 900] for start in range(0, len(run_ids), 900))


def _canonical_row(
    row: Mapping[str, object],
    fields: tuple[str, ...],
    json_fields: frozenset[str],
    *,
    path: str,
) -> dict[str, object]:
    output: dict[str, object] = {}
    for field in fields:
        value = row[field]
        output[field] = (
            _strict_json(value, f"{path}.{field}") if field in json_fields else value
        )
    return output


def _query_one(
    conn: sqlite3.Connection,
    sql: str,
    parameters: tuple[object, ...],
) -> dict[str, object] | None:
    cursor = conn.execute(sql, parameters)
    row = cursor.fetchone()
    if row is None:
        return None
    return _row_mapping(cursor, row)


def _row_mapping(cursor: sqlite3.Cursor, row: object) -> dict[str, object]:
    names = tuple(str(column[0]) for column in cursor.description or ())
    values = tuple(row) if isinstance(row, sqlite3.Row | tuple) else ()
    if len(names) != len(values):
        raise MarketScanSnapshotSealError("无法读取全市场扫描快照行")
    return dict(zip(names, values, strict=True))


def _strict_json(value: object, path: str) -> object:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MarketScanSnapshotSealError(f"{path} 必须是 JSON 文本")
    try:
        return decode_json_bytes(value.encode("utf-8"))
    except ArtifactIOError as exc:
        raise MarketScanSnapshotSealError(f"{path} 不是严格有限 JSON") from exc


def _optional_digest(value: object) -> str | None:
    if value is None:
        return None
    digest = str(value)
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise MarketScanSnapshotSealError("已发布快照摘要格式无效")
    return digest


def _seal_origin(value: object) -> str:
    origin = str(value or "").strip()
    if origin not in {"publication", "legacy_backfill"}:
        raise MarketScanSnapshotSealError("快照封印来源无效")
    return origin


__all__ = [
    "MARKET_SCAN_SNAPSHOT_DIGEST_CONTRACT",
    "MarketScanSnapshotSealError",
    "backfill_market_scan_snapshot_digests",
    "create_market_scan_immutability_triggers",
    "drop_market_scan_immutability_triggers",
    "market_scan_seal_columns_ready",
    "market_scan_snapshot_digest",
    "require_publication_market_scan_snapshot",
    "seal_market_scan_snapshot",
    "verify_market_scan_snapshot",
]
