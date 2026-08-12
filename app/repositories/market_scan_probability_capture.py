from __future__ import annotations

from app.repositories.market_scan_context import MarketScanRepositoryContext
from app.repositories.market_scan_lifecycle_support import (
    PROBABILITY_SOURCE_CAPTURE_FULL_MARKET_SCOPE,
    enqueue_probability_source_capture,
)
from app.utils.audit_time import audit_now_text as now_text


class MarketScanProbabilityCaptureOutboxMixin(MarketScanRepositoryContext):
    """Durable leases for post-publication probability source capture."""

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
        return inserted

    def claim_probability_source_capture(
        self,
        *,
        owner: str,
        lease_expires_at: str,
    ) -> dict[str, object] | None:
        normalized_owner = " ".join(str(owner).split()).strip()[:120]
        if not normalized_owner:
            raise ValueError("上涨概率归档租约 owner 不能为空")
        stamp = now_text()
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
            claimed = conn.execute(
                """
                UPDATE market_scan_probability_capture_outbox
                SET status = 'processing', attempt_count = attempt_count + 1,
                    lease_owner = ?, lease_expires_at = ?,
                    last_attempt_at = ?, updated_at = ?
                WHERE run_id = ? AND status = 'pending'
                """,
                (normalized_owner, lease_expires_at, stamp, stamp, row["run_id"]),
            )
            if claimed.rowcount != 1:
                return None
            return {
                "run_id": int(row["run_id"]),
                "attempt_count": int(row["attempt_count"] or 0) + 1,
                "captured_at": str(row["created_at"]),
            }

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
        stamp = now_text()
        terminal_error = (
            " ".join(str(message or "").split())[:800] or None
            if status == "skipped"
            else None
        )
        with self._lock, self._connect() as conn:
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
                    str(archive_digest or "").strip()[:128] or None,
                    terminal_error,
                    stamp,
                    run_id,
                    owner,
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
        with self._lock, self._connect() as conn:
            updated = conn.execute(
                """
                UPDATE market_scan_probability_capture_outbox
                SET status = 'pending', lease_owner = NULL, lease_expires_at = NULL,
                    next_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE run_id = ? AND status = 'processing' AND lease_owner = ?
                """,
                (next_attempt_at, " ".join(str(error).split())[:800], stamp, run_id, owner),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"run {run_id} 上涨概率归档租约已失效")


__all__ = ["MarketScanProbabilityCaptureOutboxMixin"]
