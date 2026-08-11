from __future__ import annotations

from app.models.market_scan import MarketScanRun
from app.repositories.market_scan_context import MarketScanRepositoryContext
from app.repositories.market_scan_mapping import run_from_row
from app.repositories.market_scan_results import required_run_row
from app.utils.audit_time import (
    audit_now_text as now_text,
    normalize_audit_time_text,
    parse_audit_time,
)


class MarketScanQuoteCaptureLifecycleMixin(MarketScanRepositoryContext):
    def begin_quote_capture(self, run_id: int, started_at: str) -> MarketScanRun:
        normalized = normalize_audit_time_text(started_at)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = required_run_row(conn, run_id)
            if row["status"] != "running":
                raise ValueError(f"扫描批次 {run_id} 当前状态不能开始报价采集：{row['status']}")
            if row["quote_capture_started_at"] is not None:
                raise ValueError(f"扫描批次 {run_id} 已存在报价采集信封")
            updated_count = conn.execute(
                """
                UPDATE market_scan_run
                SET quote_capture_started_at = ?, quote_capture_finished_at = NULL,
                    quote_capture_duration_ms = NULL, quote_capture_count = 0,
                    updated_at = ?
                WHERE id = ? AND status = 'running'
                  AND quote_capture_started_at IS NULL
                """,
                (normalized, now_text(), run_id),
            )
            if updated_count.rowcount != 1:
                raise RuntimeError(f"扫描批次 {run_id} 的报价采集起表失败")
            updated = required_run_row(conn, run_id)
        return run_from_row(updated)

    def seal_quote_capture(
        self,
        run_id: int,
        *,
        finished_at: str,
        duration_ms: int,
        count: int,
    ) -> MarketScanRun:
        normalized = normalize_audit_time_text(finished_at)
        normalized_duration = int(duration_ms)
        normalized_count = int(count)
        if normalized_duration < 0 or normalized_count < 0:
            raise ValueError("报价采集时长和数量不能为负数")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = required_run_row(conn, run_id)
            if row["status"] != "running":
                raise ValueError(f"扫描批次 {run_id} 当前状态不能封存报价采集：{row['status']}")
            started_at = row["quote_capture_started_at"]
            if started_at is None:
                raise ValueError(f"扫描批次 {run_id} 尚未开始报价采集")
            if row["quote_capture_finished_at"] is not None:
                raise ValueError(f"扫描批次 {run_id} 的报价采集信封已封存")
            if parse_audit_time(normalized) < parse_audit_time(str(started_at)):
                raise ValueError("报价采集结束时间不能早于开始时间")
            expected_count = int(row["total_count"] or 0)
            strict_v6 = str(row["rule_version"] or "").startswith("full-market-scan-v6:")
            if normalized_count > expected_count or (strict_v6 and normalized_count != expected_count):
                raise ValueError(
                    f"报价采集数量与扫描股票池不一致：{normalized_count}/{expected_count}"
                )
            updated_count = conn.execute(
                """
                UPDATE market_scan_run
                SET quote_capture_finished_at = ?, quote_capture_duration_ms = ?,
                    quote_capture_count = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                  AND quote_capture_started_at = ?
                  AND quote_capture_finished_at IS NULL
                """,
                (normalized, normalized_duration, normalized_count, now_text(), run_id, started_at),
            )
            if updated_count.rowcount != 1:
                raise RuntimeError(f"扫描批次 {run_id} 的报价采集封存失败")
            updated = required_run_row(conn, run_id)
        return run_from_row(updated)


__all__ = ["MarketScanQuoteCaptureLifecycleMixin"]
