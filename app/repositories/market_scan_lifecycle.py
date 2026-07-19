from __future__ import annotations

import sqlite3

from app.models.market_scan import (
    MarketScanRetryPlan,
    MarketScanRun,
    MarketScanRunStatus,
    MarketScanTrigger,
)
from app.repositories.market_scan_context import MarketScanRepositoryContext
from app.repositories.market_scan_mapping import run_from_row
from app.repositories.market_scan_results import (
    assign_result_ranks,
    count_degraded_results,
    required_run_row,
    sync_run_counts,
)
from app.utils.time import now_text, parse_text_time


ACTIVE_SCAN_STATUSES = ("queued", "running", "cancelling")
TERMINAL_SCAN_STATUSES = ("success", "degraded", "failed", "cancelled", "interrupted")
RETRYABLE_SCAN_STATUSES = ("degraded", "failed", "cancelled", "interrupted")

MARKET_SCAN_RESULT_RETRY_COPY_SQL = """
    WITH retry_source AS (
        SELECT result.*,
            CASE WHEN result.status = 'success'
                AND result.quote_fallback_used = 0
                AND result.kline_fallback_used = 0
                AND result.metadata_degraded = 0
                AND COALESCE(run.stock_pool_source, '') <> 'stale-fallback'
            THEN 1 ELSE 0 END AS preserve_success
        FROM market_scan_result AS result
        JOIN market_scan_run AS run ON run.id = result.run_id
        WHERE result.run_id = ?
    )
    INSERT INTO market_scan_result (
        run_id, symbol, code, market, name, industry, list_date,
        is_st, is_new, metadata_source, status, rank, score, trend_score,
        leader_score, data_quality_score, price, change_pct, turnover_rate,
        volume_ratio, amount, tags_json, metrics_json, reason, error,
        data_date, quote_timestamp, quote_source, kline_source,
        adjustment_mode, quote_fallback_used, kline_fallback_used,
        metadata_degraded, degradation_reasons_json, updated_at
    )
    SELECT
        ?, symbol, code, market, name, industry, list_date,
        is_st, is_new, metadata_source,
        CASE WHEN preserve_success = 1 THEN 'success' ELSE 'pending' END,
        NULL,
        CASE WHEN preserve_success = 1 THEN score END,
        CASE WHEN preserve_success = 1 THEN trend_score END,
        CASE WHEN preserve_success = 1 THEN leader_score END,
        CASE WHEN preserve_success = 1 THEN data_quality_score END,
        CASE WHEN preserve_success = 1 THEN price END,
        CASE WHEN preserve_success = 1 THEN change_pct END,
        CASE WHEN preserve_success = 1 THEN turnover_rate END,
        CASE WHEN preserve_success = 1 THEN volume_ratio END,
        CASE WHEN preserve_success = 1 THEN amount END,
        CASE WHEN preserve_success = 1 THEN tags_json ELSE '[]' END,
        CASE WHEN preserve_success = 1 THEN metrics_json ELSE '{}' END,
        CASE WHEN preserve_success = 1 THEN reason END,
        NULL,
        CASE WHEN preserve_success = 1 THEN data_date END,
        CASE WHEN preserve_success = 1 THEN quote_timestamp END,
        CASE WHEN preserve_success = 1 THEN quote_source END,
        CASE WHEN preserve_success = 1 THEN kline_source END,
        CASE WHEN preserve_success = 1 THEN adjustment_mode END,
        CASE WHEN preserve_success = 1 THEN quote_fallback_used ELSE 0 END,
        CASE WHEN preserve_success = 1 THEN kline_fallback_used ELSE 0 END,
        CASE WHEN preserve_success = 1 THEN metadata_degraded ELSE 0 END,
        CASE WHEN preserve_success = 1 THEN degradation_reasons_json ELSE '[]' END,
        ?
    FROM retry_source
"""


class MarketScanLifecycleMixin(MarketScanRepositoryContext):
    def create_run(
        self,
        *,
        trigger: MarketScanTrigger,
        rule_version: str,
        as_of: str,
        data_date: str,
        scope: str,
    ) -> MarketScanRun:
        stamp = now_text()
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO market_scan_run (
                    status, trigger, rule_version, as_of, data_date, scope,
                    created_at, updated_at, message
                ) VALUES ('queued', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (trigger, rule_version, as_of, data_date, scope, stamp, stamp, "等待全市场扫描"),
            )
            created_run_id = _required_lastrowid(cursor, operation="创建扫描批次")
            row = required_run_row(conn, created_run_id)
        return run_from_row(row)

    def attach_task_run(self, run_id: int, task_run_id: int) -> None:
        with self._lock, self._connect() as conn:
            required_run_row(conn, run_id)
            conn.execute(
                "UPDATE market_scan_run SET task_run_id = ?, updated_at = ? WHERE id = ?",
                (task_run_id, now_text(), run_id),
            )

    def create_and_attach_task_run(self, run_id: int, task_name: str) -> int:
        normalized_task_name = " ".join(str(task_name).split()).strip()[:120]
        if not normalized_task_name:
            raise ValueError("任务名称不能为空")
        stamp = now_text()
        with self._lock, self._connect() as conn:
            row = required_run_row(conn, run_id)
            if row["status"] != "queued":
                raise ValueError(f"扫描批次 {run_id} 当前状态不能创建任务记录：{row['status']}")
            if row["task_run_id"] is not None:
                raise ValueError(f"扫描批次 {run_id} 已挂接任务记录")
            cursor = conn.execute(
                "INSERT INTO task_run (task_name, status, started_at) VALUES (?, 'running', ?)",
                (normalized_task_name, stamp),
            )
            task_run_id = _required_lastrowid(cursor, operation="创建全市场扫描任务记录")
            attached = conn.execute(
                """
                UPDATE market_scan_run
                SET task_run_id = ?, updated_at = ?
                WHERE id = ? AND status = 'queued' AND task_run_id IS NULL
                """,
                (task_run_id, stamp, run_id),
            )
            if attached.rowcount != 1:
                raise RuntimeError(f"扫描批次 {run_id} 的任务记录挂接失败")
        return task_run_id

    def record_stock_pool_source(self, run_id: int, source: str) -> MarketScanRun:
        normalized = " ".join(str(source).split()).strip()[:80]
        if not normalized:
            raise ValueError("股票池来源不能为空")
        with self._lock, self._connect() as conn:
            row = required_run_row(conn, run_id)
            if row["status"] not in {"queued", "running"}:
                raise ValueError(f"扫描批次 {run_id} 当前状态不能记录股票池来源：{row['status']}")
            conn.execute(
                "UPDATE market_scan_run SET stock_pool_source = ?, updated_at = ? WHERE id = ?",
                (normalized, now_text(), run_id),
            )
            updated = required_run_row(conn, run_id)
        return run_from_row(updated)

    def start_run(self, run_id: int) -> MarketScanRun:
        stamp = now_text()
        with self._lock, self._connect() as conn:
            row = required_run_row(conn, run_id)
            if row["status"] != "queued":
                raise ValueError(f"扫描批次 {run_id} 当前状态不能启动：{row['status']}")
            conn.execute(
                """
                UPDATE market_scan_run
                SET status = 'running', started_at = COALESCE(started_at, ?),
                    finished_at = NULL, duration_ms = NULL, updated_at = ?,
                    message = '正在加载全市场股票池', last_error = NULL,
                    cancel_requested_at = NULL
                WHERE id = ?
                """,
                (stamp, stamp, run_id),
            )
            updated = required_run_row(conn, run_id)
        return run_from_row(updated)

    def request_cancel(self, run_id: int) -> MarketScanRun:
        stamp = now_text()
        with self._lock, self._connect() as conn:
            row = required_run_row(conn, run_id)
            if row["status"] not in ACTIVE_SCAN_STATUSES:
                raise ValueError(f"扫描批次 {run_id} 已结束，不能取消")
            conn.execute(
                """
                UPDATE market_scan_run
                SET status = 'cancelling', cancel_requested_at = ?, updated_at = ?,
                    message = '正在取消扫描'
                WHERE id = ?
                """,
                (stamp, stamp, run_id),
            )
            updated = required_run_row(conn, run_id)
        return run_from_row(updated)

    def retry_plan(self, run_id: int) -> MarketScanRetryPlan:
        with self._lock, self._connect() as conn:
            row = required_run_row(conn, run_id)
            return build_retry_plan(conn, row)

    def prepare_retry(
        self,
        run_id: int,
        expected_plan: MarketScanRetryPlan | None = None,
    ) -> MarketScanRun:
        stamp = now_text()
        with self._lock, self._connect() as conn:
            row = required_run_row(conn, run_id)
            if row["status"] not in RETRYABLE_SCAN_STATUSES:
                raise ValueError(f"扫描批次 {run_id} 当前状态不能重试：{row['status']}")
            plan = build_retry_plan(conn, row)
            if expected_plan is not None and plan != expected_plan:
                raise ValueError("扫描批次在重试准备期间发生变化，请重新获取状态后再试")
            cursor = conn.execute(
                """
                INSERT INTO market_scan_run (
                    retry_of_run_id, status, trigger, rule_version, as_of, data_date,
                    scope, stock_pool_source, total_count, excluded_count,
                    processed_count, success_count, retry_count, created_at,
                    updated_at, message
                ) VALUES (?, 'queued', 'retry', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    row["rule_version"],
                    row["as_of"],
                    row["data_date"],
                    row["scope"],
                    row["stock_pool_source"],
                    row["total_count"],
                    row["excluded_count"],
                    plan.preserved_success_count,
                    plan.preserved_success_count,
                    int(row["retry_count"] or 0) + 1,
                    stamp,
                    stamp,
                    "等待断点续跑",
                ),
            )
            retry_run_id = _required_lastrowid(cursor, operation="创建重试批次")
            conn.execute(MARKET_SCAN_RESULT_RETRY_COPY_SQL, (run_id, retry_run_id, stamp))
            updated = required_run_row(conn, retry_run_id)
        return run_from_row(updated)

    def finish_run(
        self,
        run_id: int,
        status: MarketScanRunStatus,
        *,
        message: str,
        error: str | None = None,
        task_status: str | None = None,
    ) -> MarketScanRun:
        if status not in TERMINAL_SCAN_STATUSES:
            raise ValueError(f"不是终态：{status}")
        stamp = now_text()
        with self._lock, self._connect() as conn:
            row = required_run_row(conn, run_id)
            if row["status"] in TERMINAL_SCAN_STATUSES:
                finish_linked_task_run(
                    conn,
                    row,
                    scan_status=str(row["status"]),
                    task_status=task_status,
                    stamp=str(row["finished_at"] or stamp),
                    message=str(row["message"] or message),
                )
                return run_from_row(row)
            sync_run_counts(conn, run_id, stamp=stamp)
            synced = required_run_row(conn, run_id)
            validate_terminal_status(conn, synced, status)
            if status in {"success", "degraded"}:
                assign_result_ranks(conn, run_id)
            duration_ms = _duration_ms(row["started_at"], stamp)
            conn.execute(
                """
                UPDATE market_scan_run
                SET status = ?, updated_at = ?, finished_at = ?, duration_ms = ?,
                    message = ?, last_error = ?
                WHERE id = ?
                """,
                (status, stamp, stamp, duration_ms, message[:800], (error or "")[:800] or None, run_id),
            )
            updated = required_run_row(conn, run_id)
            finish_linked_task_run(
                conn,
                updated,
                scan_status=status,
                task_status=task_status,
                stamp=stamp,
                message=message,
            )
        return run_from_row(updated)

    def reconcile_incomplete_runs(self) -> int:
        stamp = now_text()
        placeholders = ", ".join("?" for _status in ACTIVE_SCAN_STATUSES)
        with self._lock, self._connect() as conn:
            terminal_rows = conn.execute(
                """
                SELECT * FROM market_scan_run
                WHERE status IN ('success', 'degraded', 'failed', 'cancelled', 'interrupted')
                  AND task_run_id IS NOT NULL
                """
            ).fetchall()
            for row in terminal_rows:
                finish_linked_task_run(
                    conn,
                    row,
                    scan_status=str(row["status"]),
                    task_status=None,
                    stamp=str(row["finished_at"] or stamp),
                    message=str(row["message"] or "全市场扫描已结束"),
                )
            rows = conn.execute(
                f"SELECT * FROM market_scan_run WHERE status IN ({placeholders})",
                ACTIVE_SCAN_STATUSES,
            ).fetchall()
            for row in rows:
                conn.execute(
                    """
                    UPDATE market_scan_run
                    SET status = 'interrupted', updated_at = ?, finished_at = ?, duration_ms = ?,
                        message = '应用重启中断扫描，可从断点重试',
                        last_error = '应用重启时终止遗留扫描任务'
                    WHERE id = ?
                    """,
                    (stamp, stamp, _duration_ms(row["started_at"], stamp), row["id"]),
                )
                interrupted = required_run_row(conn, int(row["id"]))
                finish_linked_task_run(
                    conn,
                    interrupted,
                    scan_status="interrupted",
                    task_status="cancelled",
                    stamp=stamp,
                    message="应用重启时终止遗留全市场扫描记录",
                )
        return len(rows)


def build_retry_plan(conn: sqlite3.Connection, run: sqlite3.Row) -> MarketScanRetryPlan:
    force_recompute = str(run["stock_pool_source"] or "") == "stale-fallback"
    counts = conn.execute(
        """
        SELECT
            COUNT(*) AS result_count,
            SUM(CASE
                WHEN status = 'success'
                 AND quote_fallback_used = 0
                 AND kline_fallback_used = 0
                 AND metadata_degraded = 0
                THEN 1 ELSE 0
            END) AS clean_success_count
        FROM market_scan_result
        WHERE run_id = ?
        """,
        (run["id"],),
    ).fetchone()
    result_count = int(counts["result_count"] or 0)
    preserved = 0 if force_recompute else int(counts["clean_success_count"] or 0)
    pending = result_count - preserved
    return MarketScanRetryPlan(
        source_run_id=int(run["id"]),
        result_count=result_count,
        preserved_success_count=preserved,
        pending_count=pending,
        needs_market_data=result_count == 0 or pending > 0,
    )


def validate_terminal_status(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    status: MarketScanRunStatus,
) -> None:
    if status not in {"success", "degraded"}:
        return
    total = int(row["total_count"] or 0)
    processed = int(row["processed_count"] or 0)
    success = int(row["success_count"] or 0)
    degraded = count_degraded_results(conn, int(row["id"]))
    fallback_pool = row["stock_pool_source"] == "stale-fallback"
    _validate_complete_coverage(total, processed)
    if status == "success":
        _validate_clean_success(total, success, degraded, fallback_pool)
        return
    _validate_degraded_success(total, success, degraded, fallback_pool)


def _validate_complete_coverage(total: int, processed: int) -> None:
    if total <= 0 or processed != total:
        raise ValueError("成功或降级批次必须完成全部股票记录")


def _validate_clean_success(total: int, success: int, degraded: int, fallback_pool: bool) -> None:
    if success != total:
        raise ValueError("成功批次不得包含缺失或跳过记录")
    if degraded or fallback_pool:
        raise ValueError("含兜底或元数据不完整结果的批次必须标记为降级")


def _validate_degraded_success(total: int, success: int, degraded: int, fallback_pool: bool) -> None:
    has_partial_coverage = 0 < success < total
    has_degraded_success = success == total and (degraded > 0 or fallback_pool)
    if not has_partial_coverage and not has_degraded_success:
        raise ValueError("降级批次必须包含缺失、跳过或明确的降级结果")


def finish_linked_task_run(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    *,
    scan_status: str,
    task_status: str | None,
    stamp: str,
    message: str,
) -> None:
    task_run_id = run["task_run_id"]
    if task_run_id is None:
        return
    resolved_status = task_status or _task_status_for_scan(scan_status)
    conn.execute(
        """
        UPDATE task_run
        SET status = ?, finished_at = ?,
            duration_ms = CASE
                WHEN julianday(started_at) IS NULL THEN NULL
                ELSE MAX(0, CAST((julianday(?) - julianday(started_at)) * 86400000 AS INTEGER))
            END,
            message = ?
        WHERE id = ?
        """,
        (resolved_status, stamp, stamp, message[:800], task_run_id),
    )


def _task_status_for_scan(scan_status: str) -> str:
    return "cancelled" if scan_status in {"cancelled", "interrupted"} else scan_status


def _required_lastrowid(cursor: sqlite3.Cursor, *, operation: str) -> int:
    if cursor.lastrowid is None:
        raise RuntimeError(f"{operation}未返回记录 ID")
    return cursor.lastrowid


def _duration_ms(started_at: str | None, finished_at: str) -> int | None:
    if not started_at:
        return None
    try:
        return max(0, round((parse_text_time(finished_at) - parse_text_time(started_at)).total_seconds() * 1000))
    except ValueError:
        return None


__all__ = [
    "ACTIVE_SCAN_STATUSES",
    "MarketScanLifecycleMixin",
    "RETRYABLE_SCAN_STATUSES",
    "TERMINAL_SCAN_STATUSES",
    "build_retry_plan",
    "finish_linked_task_run",
    "validate_terminal_status",
]
