from __future__ import annotations

from collections.abc import Callable
import json
import sqlite3

from app.models.market_scan import (
    MarketScanPublicationDiagnostics,
    MarketScanRetryPlan,
    MarketScanRunStatus,
)
from app.repositories.market_scan_results import (
    assign_result_ranks,
    count_degraded_results,
    required_run_row,
    sync_run_counts,
)
from app.utils.clock import monotonic_now
from app.utils.time import datetime_to_text, parse_text_time


ACTIVE_SCAN_STATUSES = ("queued", "running", "cancelling")
TERMINAL_SCAN_STATUSES = ("success", "degraded", "failed", "cancelled", "interrupted")
RETRYABLE_SCAN_STATUSES = ("degraded", "failed", "cancelled", "interrupted")
PROBABILITY_SOURCE_CAPTURE_FULL_MARKET_SCOPE = "沪市 + 深市 + 北交所当前上市A股"


def finish_run_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    status: MarketScanRunStatus,
    *,
    stamp: str,
    message: str,
    error: str | None,
    publication_diagnostics: MarketScanPublicationDiagnostics | None,
    task_status: str | None,
    started_monotonic: float | None,
    validate_before_commit: Callable[[], None] | None,
) -> sqlite3.Row:
    if row["status"] in TERMINAL_SCAN_STATUSES:
        _finish_existing_terminal(conn, row, stamp=stamp, message=message, task_status=task_status)
        enqueue_probability_source_capture(conn, row, stamp=stamp)
        return row
    return _finish_active_run(
        conn,
        row,
        status,
        stamp=stamp,
        message=message,
        error=error,
        publication_diagnostics=publication_diagnostics,
        task_status=task_status,
        started_monotonic=started_monotonic,
        validate_before_commit=validate_before_commit,
    )


def _finish_existing_terminal(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    stamp: str,
    message: str,
    task_status: str | None,
) -> None:
    finish_linked_task_run(
        conn,
        row,
        scan_status=str(row["status"]),
        task_status=task_status,
        stamp=str(row["finished_at"] or stamp),
        message=str(row["message"] or message),
        duration_ms=row["duration_ms"],
    )


def _finish_active_run(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    status: MarketScanRunStatus,
    *,
    stamp: str,
    message: str,
    error: str | None,
    publication_diagnostics: MarketScanPublicationDiagnostics | None,
    task_status: str | None,
    started_monotonic: float | None,
    validate_before_commit: Callable[[], None] | None,
) -> sqlite3.Row:
    run_id = int(row["id"])
    sync_run_counts(conn, run_id, stamp=stamp)
    synced = required_run_row(conn, run_id)
    validate_terminal_status(conn, synced, status)
    _prepare_published_results(
        conn,
        run_id,
        status=status,
    )
    duration_ms = _duration_ms(row["started_at"], stamp, started_monotonic=started_monotonic)
    stage_metrics = _decoded_stage_metrics(synced["stage_metrics_json"])
    _finish_stage_metric(stage_metrics, str(synced["current_stage"] or "") or None, synced["stage_started_at"], stamp)
    values = _terminal_update_values(
        status=status,
        stamp=stamp,
        duration_ms=duration_ms,
        stage_metrics=stage_metrics,
        message=message,
        error=error,
        publication_diagnostics=publication_diagnostics,
        run_id=run_id,
    )
    conn.execute(
        """
        UPDATE market_scan_run
        SET status = ?, updated_at = ?, finished_at = ?, duration_ms = ?,
            current_stage = NULL, stage_started_at = NULL, stage_metrics_json = ?,
            message = ?, last_error = ?, publication_diagnostics_json = ?
        WHERE id = ?
        """,
        values,
    )
    updated = required_run_row(conn, run_id)
    finish_linked_task_run(
        conn,
        updated,
        scan_status=status,
        task_status=task_status,
        stamp=stamp,
        message=message,
        duration_ms=duration_ms,
    )
    _validate_published_commit(status, validate_before_commit)
    enqueue_probability_source_capture(conn, updated, stamp=stamp)
    return updated


def enqueue_probability_source_capture(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    stamp: str,
) -> bool:
    """Transactionally enqueue only a published official full-market source."""
    if (
        str(row["status"]) not in {"success", "degraded"}
        or str(row["mode"]) != "official"
        or str(row["scope"]) != PROBABILITY_SOURCE_CAPTURE_FULL_MARKET_SCOPE
        or int(row["success_count"] or 0) <= 0
    ):
        return False
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO market_scan_probability_capture_outbox (
            run_id, status, attempt_count, next_attempt_at,
            created_at, updated_at
        ) VALUES (?, 'pending', 0, ?, ?, ?)
        """,
        (row["id"], stamp, stamp, stamp),
    )
    return cursor.rowcount == 1


def _terminal_update_values(
    *, status: MarketScanRunStatus, stamp: str, duration_ms: int | None,
    stage_metrics: dict[str, dict[str, int]], message: str, error: str | None,
    publication_diagnostics: MarketScanPublicationDiagnostics | None, run_id: int,
) -> tuple[object, ...]:
    diagnostics_json = (
        json.dumps(
            publication_diagnostics.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if publication_diagnostics is not None
        else None
    )
    return (
        status, stamp, stamp, duration_ms,
        json.dumps(stage_metrics, ensure_ascii=False, separators=(",", ":")),
        message[:800], (error or "")[:800] or None, diagnostics_json, run_id,
    )


def _prepare_published_results(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: MarketScanRunStatus,
) -> None:
    if status not in {"success", "degraded"}:
        return
    assign_result_ranks(conn, run_id)


def _validate_published_commit(
    status: MarketScanRunStatus,
    validate_before_commit: Callable[[], None] | None,
) -> None:
    if status not in {"success", "degraded"}:
        return
    if validate_before_commit is not None:
        validate_before_commit()


def build_retry_plan(conn: sqlite3.Connection, run: sqlite3.Row) -> MarketScanRetryPlan:
    force_recompute = force_recompute_retry(run)
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
        rule_version=str(run["rule_version"]),
    )


def force_recompute_retry(run: sqlite3.Row) -> bool:
    return (
        str(run["rule_version"] or "").startswith("full-market-scan-v6:")
        or str(run["status"]) == "failed"
        or str(run["stock_pool_source"] or "") == "stale-fallback"
    )


def retry_as_of(run: sqlite3.Row, triggered_as_of: str | None) -> str:
    if not force_recompute_retry(run):
        return str(run["as_of"])
    if not str(triggered_as_of or "").strip():
        raise ValueError("完整重算必须记录本次重试触发时间")
    normalized = datetime_to_text(parse_text_time(str(triggered_as_of)))
    if normalized is None:  # pragma: no cover - parse_text_time already returns a datetime
        raise ValueError("完整重算触发时间无效")
    return normalized


def _decoded_stage_metrics(value: object) -> dict[str, dict[str, int]]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    metrics: dict[str, dict[str, int]] = {}
    for stage, raw in parsed.items():
        if not isinstance(raw, dict):
            continue
        metrics[str(stage)] = {
            key: max(0, int(raw.get(key, 0) or 0))
            for key in ("duration_ms", "work_duration_ms", "calls", "items")
        }
    return metrics


def _stage_metric(metrics: dict[str, dict[str, int]], stage: str) -> dict[str, int]:
    return metrics.setdefault(
        stage,
        {"duration_ms": 0, "work_duration_ms": 0, "calls": 0, "items": 0},
    )


def _finish_stage_metric(
    metrics: dict[str, dict[str, int]],
    stage: str | None,
    started_at: object,
    finished_at: str,
) -> None:
    if not stage or not started_at:
        return
    try:
        elapsed_ms = round(max(0.0, (parse_text_time(finished_at) - parse_text_time(str(started_at))).total_seconds()) * 1000)
    except (TypeError, ValueError):
        return
    _stage_metric(metrics, stage)["duration_ms"] += elapsed_ms


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
    duration_ms: int | None = None,
) -> None:
    task_run_id = run["task_run_id"]
    if task_run_id is None:
        return
    resolved_status = task_status or _task_status_for_scan(scan_status)
    conn.execute(
        """
        UPDATE task_run
        SET status = ?, finished_at = ?,
            duration_ms = COALESCE(?, CASE
                WHEN julianday(started_at) IS NULL THEN NULL
                ELSE MAX(0, CAST((julianday(?) - julianday(started_at)) * 86400000 AS INTEGER))
            END),
            message = ?
        WHERE id = ?
        """,
        (resolved_status, stamp, duration_ms, stamp, message[:800], task_run_id),
    )


def _task_status_for_scan(scan_status: str) -> str:
    return "cancelled" if scan_status in {"cancelled", "interrupted"} else scan_status


def _required_lastrowid(cursor: sqlite3.Cursor, *, operation: str) -> int:
    if cursor.lastrowid is None:
        raise RuntimeError(f"{operation}未返回记录 ID")
    return cursor.lastrowid


def _duration_ms(
    started_at: str | None,
    finished_at: str,
    *,
    started_monotonic: float | None = None,
) -> int | None:
    if started_monotonic is not None:
        return max(0, round((monotonic_now() - started_monotonic) * 1000))
    if not started_at:
        return None
    try:
        return max(0, round((parse_text_time(finished_at) - parse_text_time(started_at)).total_seconds() * 1000))
    except ValueError:
        return None


__all__ = [
    "ACTIVE_SCAN_STATUSES",
    "RETRYABLE_SCAN_STATUSES",
    "TERMINAL_SCAN_STATUSES",
    "build_retry_plan",
    "finish_linked_task_run",
    "finish_run_row",
    "validate_terminal_status",
]
