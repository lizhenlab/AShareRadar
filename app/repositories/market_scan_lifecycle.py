from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import re
import sqlite3

from app.db.market_scan_action_source import require_market_scan_action_source
from app.db.market_scan_integrity import require_publication_market_scan_snapshot
from app.models.market_scan import (
    MARKET_SCAN_FULL_MARKET_SCOPE,
    MARKET_SCAN_TOP100_REFRESH_SCOPE,
    MarketScanRetryPlan,
    MarketScanMode,
    MarketScanPublicationDiagnostics,
    MarketScanRun,
    MarketScanRunStatus,
    MarketScanStage,
    MarketScanTrigger,
)
from app.repositories.market_scan_lifecycle_support import (
    ACTIVE_SCAN_STATUSES,
    RETRYABLE_SCAN_STATUSES,
    TERMINAL_SCAN_STATUSES,
    _decoded_stage_metrics,
    _finish_stage_metric,
    _required_lastrowid,
    _stage_metric,
    build_retry_plan,
    finish_linked_task_run,
    finish_run_row,
    reconcile_incomplete_run_rows,
    retry_as_of,
    validate_terminal_status,
)
from app.repositories.market_scan_mapping import run_from_row
from app.repositories.market_scan_quote_capture import MarketScanQuoteCaptureLifecycleMixin
from app.repositories.market_scan_results import required_run_row
from app.repositories.market_scan_rule_contracts import (
    register_market_scan_rule_contract,
)
from app.repositories.market_scan_top100_refresh import prepare_top100_refresh_snapshot
from app.utils.audit_time import audit_now_text as now_text
from app.utils.clock import monotonic_now

MARKET_SCAN_RESULT_RETRY_COPY_SQL = """
    WITH retry_source AS (
        SELECT result.*,
            CASE WHEN result.status = 'success'
                AND run.status IN ('degraded', 'cancelled', 'interrupted')
                AND run.rule_version NOT LIKE 'full-market-scan-v6:%'
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
        is_st, is_new, metadata_source, status, rank, score, raw_score, trend_score,
        leader_score, data_quality_score, price, change_pct, turnover_rate,
        volume_ratio, amount, tags_json, metrics_json, reason, error,
        data_date, quote_timestamp, quote_observed_at, quote_source, kline_source,
        adjustment_mode, quote_fallback_used, kline_fallback_used,
        metadata_degraded, degradation_reasons_json, updated_at
    )
    SELECT
        ?, symbol, code, market, name, industry, list_date,
        is_st, is_new, metadata_source,
        CASE WHEN preserve_success = 1 THEN 'success' ELSE 'pending' END,
        NULL,
        CASE WHEN preserve_success = 1 THEN score END,
        CASE WHEN preserve_success = 1 THEN raw_score END,
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
        CASE WHEN preserve_success = 1 THEN quote_observed_at END,
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


class MarketScanLifecycleMixin(MarketScanQuoteCaptureLifecycleMixin):
    def create_run(
        self,
        *,
        trigger: MarketScanTrigger,
        mode: MarketScanMode = "official",
        rule_version: str,
        as_of: str,
        data_date: str,
        quote_date: str | None = None,
        scope: str,
        rule_contract: Mapping[str, object] | None = None,
    ) -> MarketScanRun:
        stamp = now_text()
        message = {
            "intraday": "等待盘中临时扫描",
            "preopen": "等待盘前复盘扫描",
            "official": "等待盘后正式扫描",
        }[mode]
        with self._lock, self._connect() as conn:
            if rule_contract is not None:
                register_market_scan_rule_contract(
                    conn,
                    rule_version=rule_version,
                    contract=rule_contract,
                    stamp=stamp,
                )
            elif (
                re.fullmatch(r"full-market-scan-v6:[0-9a-f]{64}", rule_version)
                is not None
                and scope in {MARKET_SCAN_FULL_MARKET_SCOPE, MARKET_SCAN_TOP100_REFRESH_SCOPE}
            ):
                raise ValueError("新生产扫描必须封存精确规则合同")
            cursor = conn.execute(
                """
                INSERT INTO market_scan_run (
                    status, trigger, mode, rule_version, as_of, data_date, quote_date, scope,
                    created_at, updated_at, message
                ) VALUES ('queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (trigger, mode, rule_version, as_of, data_date, quote_date or data_date, scope, stamp, stamp, message),
            )
            created_run_id = _required_lastrowid(cursor, operation="创建扫描批次")
            row = required_run_row(conn, created_run_id)
        return run_from_row(row)

    def attach_task_run(self, run_id: int, task_run_id: int) -> None:
        with self._lock, self._connect() as conn:
            row = required_run_row(conn, run_id)
            if row["status"] not in {"queued", "running"} or row["task_run_id"] is not None:
                raise ValueError(f"扫描批次 {run_id} 当前状态不能挂接任务记录")
            updated = conn.execute(
                """
                UPDATE market_scan_run SET task_run_id = ?, updated_at = ?
                WHERE id = ? AND status IN ('queued', 'running') AND task_run_id IS NULL
                """,
                (task_run_id, now_text(), run_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"扫描批次 {run_id} 的任务记录挂接失败")

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

    def update_observability(
        self,
        run_id: int,
        *,
        stage: MarketScanStage,
        stage_items: int = 0,
        work_metrics: dict[MarketScanStage, tuple[int, int]] | None = None,
        message: str | None = None,
    ) -> MarketScanRun:
        stamp = now_text()
        with self._lock, self._connect() as conn:
            row = required_run_row(conn, run_id)
            if row["status"] not in {"queued", "running", "cancelling"}:
                return run_from_row(row)
            metrics = _decoded_stage_metrics(row["stage_metrics_json"])
            current_stage = str(row["current_stage"] or "") or None
            stage_started_at = row["stage_started_at"]
            if current_stage != stage:
                _finish_stage_metric(metrics, current_stage, stage_started_at, stamp)
                metric = _stage_metric(metrics, stage)
                metric["calls"] += 1
                stage_started_at = stamp
            if stage_items > 0:
                _stage_metric(metrics, stage)["items"] += stage_items
            for metric_stage, (duration_ms, item_count) in (work_metrics or {}).items():
                metric = _stage_metric(metrics, metric_stage)
                metric["work_duration_ms"] += max(0, int(duration_ms))
                metric["items"] += max(0, int(item_count))
            conn.execute(
                """
                UPDATE market_scan_run
                SET current_stage = ?, stage_started_at = ?, stage_metrics_json = ?,
                    updated_at = ?, message = COALESCE(?, message)
                WHERE id = ?
                """,
                (
                    stage,
                    stage_started_at,
                    json.dumps(metrics, ensure_ascii=False, separators=(",", ":")),
                    stamp,
                    message[:800] if message else None,
                    run_id,
                ),
            )
            updated = required_run_row(conn, run_id)
        return run_from_row(updated)

    def start_run(self, run_id: int) -> MarketScanRun:
        stamp = now_text()
        with self._lock, self._connect() as conn:
            row = required_run_row(conn, run_id)
            if row["status"] != "queued":
                raise ValueError(f"扫描批次 {run_id} 当前状态不能启动：{row['status']}")
            top100_refresh = row["scope"] == MARKET_SCAN_TOP100_REFRESH_SCOPE
            initial_stage = "bulk_quotes" if top100_refresh else "stock_pool"
            initial_message = (
                "正在获取 TOP100 最新行情并重新评分"
                if top100_refresh
                else "正在加载全市场股票池"
            )
            initial_metrics = json.dumps(
                {initial_stage: {"duration_ms": 0, "work_duration_ms": 0, "calls": 1, "items": 0}},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            conn.execute(
                """
                UPDATE market_scan_run
                SET status = 'running', started_at = COALESCE(started_at, ?),
                    finished_at = NULL, duration_ms = NULL, updated_at = ?,
                    quote_capture_started_at = NULL,
                    quote_capture_finished_at = NULL,
                    quote_capture_duration_ms = NULL,
                    quote_capture_count = 0,
                    current_stage = ?, stage_started_at = ?, stage_metrics_json = ?,
                    message = ?, last_error = NULL,
                    cancel_requested_at = NULL
                WHERE id = ?
                """,
                (stamp, stamp, initial_stage, stamp, initial_metrics, initial_message, run_id),
            )
            updated = required_run_row(conn, run_id)
        self._run_started_monotonic[run_id] = monotonic_now()
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
        with self._read_snapshot() as conn:
            row = required_run_row(conn, run_id)
            if row["status"] == "degraded":
                require_publication_market_scan_snapshot(conn, run_id)
            plan = build_retry_plan(conn, row)
            if row["status"] == "degraded" and plan.preserved_success_count:
                require_market_scan_action_source(conn, run_id)
            return plan

    def prepare_retry(
        self,
        run_id: int,
        expected_plan: MarketScanRetryPlan | None = None,
        *,
        as_of: str | None = None,
        rule_contract: Mapping[str, object] | None = None,
    ) -> MarketScanRun:
        stamp = now_text()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = required_run_row(conn, run_id)
            if rule_contract is not None:
                register_market_scan_rule_contract(
                    conn,
                    rule_version=str(row["rule_version"]),
                    contract=rule_contract,
                    stamp=stamp,
                )
            if row["status"] not in RETRYABLE_SCAN_STATUSES:
                raise ValueError(f"扫描批次 {run_id} 当前状态不能重试：{row['status']}")
            if row["status"] == "degraded":
                require_publication_market_scan_snapshot(conn, run_id)
            plan = build_retry_plan(conn, row)
            if row["status"] == "degraded" and plan.preserved_success_count:
                require_market_scan_action_source(conn, run_id)
            if expected_plan is not None and plan != expected_plan:
                raise ValueError("扫描批次在重试准备期间发生变化，请重新获取状态后再试")
            next_as_of = retry_as_of(row, as_of)
            retry_run_id = _insert_retry_run(
                conn,
                row,
                source_run_id=run_id,
                plan=plan,
                as_of=next_as_of,
                stamp=stamp,
            )
            conn.execute(MARKET_SCAN_RESULT_RETRY_COPY_SQL, (run_id, retry_run_id, stamp))
            updated = required_run_row(conn, retry_run_id)
        return run_from_row(updated)

    def prepare_top100_refresh(
        self,
        source_run_id: int,
        *,
        rule_version: str,
        as_of: str,
        data_date: str,
        quote_date: str,
        limit: int,
        rule_contract: Mapping[str, object] | None = None,
    ) -> MarketScanRun:
        stamp = now_text()
        with self._lock, self._connect() as conn:
            source = required_run_row(conn, source_run_id)
            if rule_contract is not None:
                register_market_scan_rule_contract(
                    conn,
                    rule_version=rule_version,
                    contract=rule_contract,
                    stamp=stamp,
                )
            if source["status"] not in {"success", "degraded"}:
                raise ValueError(f"扫描批次 {source_run_id} 尚未发布，不能快速更新 TOP100")
            require_market_scan_action_source(conn, source_run_id)
            refresh_run_id = prepare_top100_refresh_snapshot(
                conn,
                source,
                source_run_id=source_run_id,
                rule_version=rule_version,
                as_of=as_of,
                data_date=data_date,
                quote_date=quote_date,
                limit=limit,
                stamp=stamp,
            )
            refreshed = required_run_row(conn, refresh_run_id)
        return run_from_row(refreshed)

    def finish_run(
        self,
        run_id: int,
        status: MarketScanRunStatus,
        *,
        message: str,
        error: str | None = None,
        publication_diagnostics: MarketScanPublicationDiagnostics | None = None,
        task_status: str | None = None,
        validate_before_commit: Callable[[], None] | None = None,
    ) -> MarketScanRun:
        if status not in TERMINAL_SCAN_STATUSES:
            raise ValueError(f"不是终态：{status}")
        stamp = now_text()
        with self._lock, self._connect() as conn:
            row = required_run_row(conn, run_id)
            updated = finish_run_row(
                conn,
                row,
                status,
                stamp=stamp,
                message=message,
                error=error,
                publication_diagnostics=publication_diagnostics,
                task_status=task_status,
                started_monotonic=self._run_started_monotonic.get(run_id),
                validate_before_commit=validate_before_commit,
            )
        self._run_started_monotonic.pop(run_id, None)
        return run_from_row(updated)

    def reconcile_incomplete_runs(self) -> int:
        stamp = now_text()
        with self._lock, self._connect() as conn:
            count = reconcile_incomplete_run_rows(conn, stamp=stamp)
            self._run_started_monotonic.clear()
        return count


def _insert_retry_run(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    source_run_id: int,
    plan: MarketScanRetryPlan,
    as_of: str,
    stamp: str,
) -> int:
    message = (
        "等待完整重算"
        if plan.preserved_success_count == 0 and plan.pending_count == plan.result_count
        else "等待断点续跑"
    )
    cursor = conn.execute(
        """
        INSERT INTO market_scan_run (
            retry_of_run_id, status, trigger, mode, rule_version, as_of, data_date, quote_date,
            scope, stock_pool_source, total_count, excluded_count,
            processed_count, success_count, retry_count, created_at,
            updated_at, message
        ) VALUES (?, 'queued', 'retry', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_run_id, row["mode"], row["rule_version"], as_of,
            row["data_date"], row["quote_date"] or row["data_date"],
            row["scope"], row["stock_pool_source"], row["total_count"],
            row["excluded_count"], plan.preserved_success_count,
            plan.preserved_success_count, int(row["retry_count"] or 0) + 1,
            stamp, stamp, message,
        ),
    )
    return _required_lastrowid(cursor, operation="创建重试批次")


__all__ = [
    "ACTIVE_SCAN_STATUSES",
    "MarketScanLifecycleMixin",
    "RETRYABLE_SCAN_STATUSES",
    "TERMINAL_SCAN_STATUSES",
    "build_retry_plan",
    "finish_linked_task_run",
    "validate_terminal_status",
]
