from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import sqlite3
from typing import cast

from app.models.market_scan_probability_capture import (
    ProbabilitySourceCaptureState,
    ProbabilitySourceCaptureStatus,
)
from app.db.market_scan_action_source import (
    MarketScanActionSourceError,
    require_market_scan_action_source as require_action_source,
)
from app.repositories.market_scan_context import MarketScanRepositoryContext
from app.repositories.market_scan_lifecycle_support import (
    PROBABILITY_SOURCE_CAPTURE_FULL_MARKET_SCOPE,
    enqueue_probability_source_capture,
)
from app.utils.audit_time import audit_now_text as now_text


class MarketScanProbabilityCaptureOutboxMixin(MarketScanRepositoryContext):
    """Durable leases for post-publication probability source capture."""

    def market_scan_action_source_digest(self, run_id: int) -> str | None:
        """Return the unified action-source receipt, without mutating the run."""
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1:
            raise ValueError("动作来源 run_id 必须是正整数")
        with self._read_snapshot() as conn:
            try:
                return require_action_source(conn, run_id)
            except MarketScanActionSourceError:
                return None

    def probability_source_capture_status(
        self,
        run_id: int,
    ) -> ProbabilitySourceCaptureState | None:
        """Return the durable capture state without claiming or mutating it."""
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1:
            raise ValueError("上涨概率归档 run_id 必须是正整数")
        with self._read_snapshot() as conn:
            return read_probability_source_capture_state(conn, run_id)

    def reconcile_probability_source_capture_outbox(self) -> int:
        """Backfill eligible runs and recover leases after leader startup."""
        stamp = now_text()
        with self._lock, self._connect() as conn:
            candidates = conn.execute(
                """
                SELECT * FROM market_scan_run
                WHERE status IN ('success', 'degraded')
                  AND mode = 'official'
                  AND scope = ?
                ORDER BY id
                """,
                (PROBABILITY_SOURCE_CAPTURE_FULL_MARKET_SCOPE,),
            ).fetchall()
            inserted = sum(
                enqueue_probability_source_capture(conn, row, stamp=stamp)
                for row in candidates
            )
            conn.execute(
                """
                UPDATE market_scan_probability_capture_outbox
                SET status = 'pending', lease_owner = NULL,
                    lease_expires_at = NULL, next_attempt_at = ?, updated_at = ?,
                    last_error = COALESCE(last_error, '应用重启后回收遗留归档租约')
                WHERE status = 'processing'
                """,
                (stamp, stamp),
            )
            _skip_ineligible_capture_rows(conn, stamp=stamp)
        return inserted

    def claim_probability_source_capture(
        self,
        *,
        owner: str,
        lease_expires_at: str,
    ) -> dict[str, object] | None:
        normalized_owner = _normalized_owner(owner)
        stamp = now_text()
        _require_future_lease(lease_expires_at, stamp=stamp)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE market_scan_probability_capture_outbox
                SET status = 'pending', lease_owner = NULL,
                    lease_expires_at = NULL, next_attempt_at = ?, updated_at = ?,
                    last_error = COALESCE(last_error, '回收过期归档租约')
                WHERE status = 'processing'
                  AND lease_expires_at <= ?
                """,
                (stamp, stamp, stamp),
            )
            return _claim_next_capture_row(
                conn,
                owner=normalized_owner,
                lease_expires_at=lease_expires_at,
                stamp=stamp,
            )

    def finish_probability_source_capture(
        self,
        run_id: int,
        *,
        owner: str,
        status: str,
        archive_digest: str | None = None,
        message: str | None = None,
    ) -> None:
        if status not in {"succeeded", "skipped"}:
            raise ValueError(f"无效上涨概率归档终态：{status}")
        normalized_owner = _normalized_owner(owner)
        normalized_digest = _capture_terminal_digest(
            status,
            archive_digest=archive_digest,
            message=message,
        )
        stamp = now_text()
        terminal_error = (
            " ".join(str(message or "").split())[:800] or None
            if status == "skipped"
            else None
        )
        with self._lock, self._connect() as conn:
            if status == "succeeded":
                require_action_source(conn, run_id)
            updated = conn.execute(
                """
                UPDATE market_scan_probability_capture_outbox
                SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                    completed_at = ?, archive_digest = ?, last_error = ?, updated_at = ?
                WHERE run_id = ? AND status = 'processing' AND lease_owner = ?
                """,
                (
                    status,
                    stamp,
                    normalized_digest,
                    terminal_error,
                    stamp,
                    run_id,
                    normalized_owner,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"run {run_id} 上涨概率归档租约已失效")

    def retry_probability_source_capture(
        self,
        run_id: int,
        *,
        owner: str,
        next_attempt_at: str,
        error: str,
    ) -> None:
        stamp = now_text()
        normalized_owner = _normalized_owner(owner)
        _require_retry_time(next_attempt_at)
        normalized_error = " ".join(str(error).split())[:800]
        if not normalized_error:
            raise ValueError("上涨概率归档重试必须记录错误")
        with self._lock, self._connect() as conn:
            updated = conn.execute(
                """
                UPDATE market_scan_probability_capture_outbox
                SET status = 'pending', lease_owner = NULL, lease_expires_at = NULL,
                    next_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE run_id = ? AND status = 'processing' AND lease_owner = ?
                """,
                (next_attempt_at, normalized_error, stamp, run_id, normalized_owner),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"run {run_id} 上涨概率归档租约已失效")

    def audit_probability_source_capture_archives(
        self,
        archives: Mapping[int, str],
    ) -> int:
        """Reset false succeeded claims after restore or external archive loss."""

        normalized = {
            int(run_id): _required_sha256(digest, "archive digest")
            for run_id, digest in archives.items()
        }
        stamp = now_text()
        repaired = 0
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT outbox.run_id, outbox.archive_digest
                FROM market_scan_probability_capture_outbox AS outbox
                JOIN market_scan_run AS run ON run.id = outbox.run_id
                WHERE outbox.status = 'succeeded'
                  AND run.mode = 'official' AND run.scope = ?
                  AND run.status IN ('success', 'degraded')
                ORDER BY run_id
                """,
                (PROBABILITY_SOURCE_CAPTURE_FULL_MARKET_SCOPE,),
            ).fetchall()
            for row in rows:
                run_id = int(row["run_id"])
                try:
                    require_action_source(conn, run_id)
                except MarketScanActionSourceError as exc:
                    _skip_succeeded_capture_row(
                        conn,
                        run_id,
                        stamp=stamp,
                        reason=str(exc),
                    )
                    repaired += 1
                    continue
                expected = _optional_sha256(row["archive_digest"])
                if expected is not None and normalized.get(run_id) == expected:
                    continue
                updated = conn.execute(
                    """
                    UPDATE market_scan_probability_capture_outbox
                    SET status = 'pending', next_attempt_at = ?,
                        completed_at = NULL, archive_digest = NULL,
                        lease_owner = NULL, lease_expires_at = NULL,
                        last_error = '归档证据缺失或摘要不匹配，等待重新归档',
                        updated_at = ?
                    WHERE run_id = ? AND status = 'succeeded'
                    """,
                    (stamp, stamp, run_id),
                )
                repaired += int(updated.rowcount == 1)
        return repaired


def read_probability_source_capture_state(
    conn: sqlite3.Connection,
    run_id: int,
) -> ProbabilitySourceCaptureState | None:
    """Read one outbox projection inside the caller's SQLite snapshot."""
    row = conn.execute(
        """
        SELECT status, archive_digest, last_error
        FROM market_scan_probability_capture_outbox
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    status = str(row["status"])
    if status not in {"pending", "processing", "succeeded", "skipped"}:
        raise RuntimeError(f"run {run_id} 上涨概率归档状态无效")
    normalized_status = cast(ProbabilitySourceCaptureStatus, status)
    archive_digest = row["archive_digest"]
    if status == "succeeded":
        archive_digest = _required_sha256(archive_digest, "archive digest")
    elif archive_digest is not None:
        raise RuntimeError(f"run {run_id} 非成功上涨概率归档不得携带 archive_digest")
    last_error = row["last_error"]
    if last_error is not None and not isinstance(last_error, str):
        raise RuntimeError(f"run {run_id} 上涨概率归档 last_error 无效")
    return {
        "status": normalized_status,
        "archive_digest": archive_digest,
        "last_error": last_error,
    }


def _claim_next_capture_row(
    conn: sqlite3.Connection,
    *,
    owner: str,
    lease_expires_at: str,
    stamp: str,
) -> dict[str, object] | None:
    while True:
        row = conn.execute(
            """
            SELECT run_id, attempt_count, created_at
            FROM market_scan_probability_capture_outbox
            WHERE status = 'pending' AND next_attempt_at <= ?
            ORDER BY next_attempt_at, run_id
            LIMIT 1
            """,
            (stamp,),
        ).fetchone()
        if row is None:
            return None
        try:
            require_action_source(conn, int(row["run_id"]))
        except MarketScanActionSourceError as exc:
            _skip_capture_row(conn, int(row["run_id"]), stamp=stamp, reason=str(exc))
            continue
        return _claim_capture_row(
            conn,
            row,
            owner=owner,
            lease_expires_at=lease_expires_at,
            stamp=stamp,
        )


def _claim_capture_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    owner: str,
    lease_expires_at: str,
    stamp: str,
) -> dict[str, object] | None:
    claimed = conn.execute(
        """
        UPDATE market_scan_probability_capture_outbox
        SET status = 'processing', attempt_count = attempt_count + 1,
            lease_owner = ?, lease_expires_at = ?,
            last_attempt_at = ?, updated_at = ?
        WHERE run_id = ? AND status = 'pending'
        """,
        (owner, lease_expires_at, stamp, stamp, row["run_id"]),
    )
    if claimed.rowcount != 1:
        return None
    return {
        "run_id": int(row["run_id"]),
        "attempt_count": int(row["attempt_count"] or 0) + 1,
        "captured_at": str(row["created_at"]),
    }


def _skip_ineligible_capture_rows(conn: sqlite3.Connection, *, stamp: str) -> None:
    rows = conn.execute(
        """
        SELECT run_id FROM market_scan_probability_capture_outbox
        WHERE status IN ('pending', 'processing')
        ORDER BY run_id
        """
    ).fetchall()
    for row in rows:
        run_id = int(row["run_id"])
        try:
            require_action_source(conn, run_id)
        except MarketScanActionSourceError as exc:
            _skip_capture_row(conn, run_id, stamp=stamp, reason=str(exc))


def _skip_capture_row(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    stamp: str,
    reason: str,
) -> None:
    message = " ".join(reason.split())[:800] or "动作源门禁未通过"
    conn.execute(
        """
        UPDATE market_scan_probability_capture_outbox
        SET status = 'skipped', lease_owner = NULL, lease_expires_at = NULL,
            completed_at = ?, archive_digest = NULL,
            last_error = ?, updated_at = ?
        WHERE run_id = ? AND status IN ('pending', 'processing')
        """,
        (stamp, message, stamp, run_id),
    )


def _skip_succeeded_capture_row(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    stamp: str,
    reason: str,
) -> None:
    message = " ".join(reason.split())[:800] or "动作源门禁未通过"
    conn.execute(
        """
        UPDATE market_scan_probability_capture_outbox
        SET status = 'skipped', completed_at = ?, archive_digest = NULL,
            lease_owner = NULL, lease_expires_at = NULL,
            last_error = ?, updated_at = ?
        WHERE run_id = ? AND status = 'succeeded'
        """,
        (stamp, message, stamp, run_id),
    )


def _normalized_owner(value: object) -> str:
    normalized = " ".join(str(value).split()).strip()[:120]
    if not normalized:
        raise ValueError("上涨概率归档租约 owner 不能为空")
    return normalized


def _capture_terminal_digest(
    status: str,
    *,
    archive_digest: str | None,
    message: str | None,
) -> str | None:
    if status == "succeeded":
        return _required_sha256(archive_digest, "archive_digest")
    if archive_digest is not None:
        raise ValueError("跳过归档不得携带 archive_digest")
    if not " ".join(str(message or "").split()):
        raise ValueError("跳过归档必须记录原因")
    return None


def _required_sha256(value: object, path: str) -> str:
    digest = str(value or "").strip()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError(f"{path} 必须是 64 位小写 SHA-256")
    return digest


def _optional_sha256(value: object) -> str | None:
    if value is None:
        return None
    try:
        return _required_sha256(value, "archive_digest")
    except ValueError:
        return None


def _require_future_lease(value: str, *, stamp: str) -> None:
    try:
        expires = datetime.fromisoformat(value.replace("Z", "+00:00"))
        claimed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("上涨概率归档 lease_expires_at 必须是 ISO-8601") from exc
    if expires.tzinfo is None or claimed.tzinfo is None or expires <= claimed:
        raise ValueError("上涨概率归档 lease_expires_at 必须是未来的带时区时间")


def _require_retry_time(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("上涨概率归档 next_attempt_at 必须是 ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("上涨概率归档 next_attempt_at 必须包含时区")


__all__ = [
    "MarketScanProbabilityCaptureOutboxMixin",
    "read_probability_source_capture_state",
]
