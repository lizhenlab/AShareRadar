from __future__ import annotations

from datetime import datetime
import sqlite3

from app.models.market_scan import MarketScanRun
from app.repositories.market_scan_context import MarketScanRepositoryContext
from app.repositories.market_scan_mapping import run_from_row
from app.repositories.market_scan_results import required_run_row
from app.market_scan_repository_contracts import market_scan_temporal_contract
from app.utils.audit_time import (
    audit_now_text as now_text,
    normalize_audit_time_text,
    parse_audit_time,
)
from app.utils.market_time import market_datetime_epoch, normalize_market_datetime


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
        decision_as_of: str,
        duration_ms: int,
        count: int,
    ) -> MarketScanRun:
        normalized, normalized_decision, normalized_duration, normalized_count = (
            _normalized_quote_capture_seal(
                finished_at=finished_at,
                decision_as_of=decision_as_of,
                duration_ms=duration_ms,
                count=count,
            )
        )
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = required_run_row(conn, run_id)
            started_at = _required_sealable_quote_capture(row, run_id=run_id)
            _validate_quote_capture_time_contract(
                row,
                started_at=started_at,
                finished_at=normalized,
                decision_as_of=normalized_decision,
            )
            _validate_quote_capture_count(row, count=normalized_count)
            _update_sealed_quote_capture(
                conn,
                run_id=run_id,
                started_at=started_at,
                finished_at=normalized,
                decision_as_of=normalized_decision,
                duration_ms=normalized_duration,
                count=normalized_count,
            )
            updated = required_run_row(conn, run_id)
        return run_from_row(updated)


def _normalized_quote_capture_seal(
    *,
    finished_at: str,
    decision_as_of: str,
    duration_ms: int,
    count: int,
) -> tuple[str, str, int, int]:
    normalized = normalize_audit_time_text(finished_at)
    normalized_decision = normalize_market_datetime(decision_as_of)
    if normalized_decision is None:
        raise ValueError("扫描决策时点无法解析")
    normalized_duration = int(duration_ms)
    normalized_count = int(count)
    if normalized_duration < 0 or normalized_count < 0:
        raise ValueError("报价采集时长和数量不能为负数")
    return normalized, normalized_decision, normalized_duration, normalized_count


def _required_sealable_quote_capture(row: sqlite3.Row, *, run_id: int) -> str:
    if row["status"] != "running":
        raise ValueError(f"扫描批次 {run_id} 当前状态不能封存报价采集：{row['status']}")
    started_at = row["quote_capture_started_at"]
    if started_at is None:
        raise ValueError(f"扫描批次 {run_id} 尚未开始报价采集")
    if row["quote_capture_finished_at"] is not None:
        raise ValueError(f"扫描批次 {run_id} 的报价采集信封已封存")
    return str(started_at)


def _validate_quote_capture_count(row: sqlite3.Row, *, count: int) -> None:
    expected_count = int(row["total_count"] or 0)
    strict_v6 = str(row["rule_version"] or "").startswith("full-market-scan-v6:")
    if count > expected_count or strict_v6 and count != expected_count:
        raise ValueError(f"报价采集数量与扫描股票池不一致：{count}/{expected_count}")


def _update_sealed_quote_capture(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    started_at: str,
    finished_at: str,
    decision_as_of: str,
    duration_ms: int,
    count: int,
) -> None:
    updated = conn.execute(
        """
        UPDATE market_scan_run
        SET quote_capture_finished_at = ?, quote_capture_duration_ms = ?,
            as_of = ?, quote_capture_count = ?, updated_at = ?
        WHERE id = ? AND status = 'running'
          AND quote_capture_started_at = ?
          AND quote_capture_finished_at IS NULL
        """,
        (finished_at, duration_ms, decision_as_of, count, now_text(), run_id, started_at),
    )
    if updated.rowcount != 1:
        raise RuntimeError(f"扫描批次 {run_id} 的报价采集封存失败")


def _validate_quote_capture_time_contract(
    row: sqlite3.Row,
    *,
    started_at: str,
    finished_at: str,
    decision_as_of: str,
) -> None:
    if parse_audit_time(finished_at) < parse_audit_time(started_at):
        raise ValueError("报价采集结束时间不能早于开始时间")
    decision_epoch = market_datetime_epoch(decision_as_of)
    original_epoch = market_datetime_epoch(row["as_of"])
    available_epoch = market_datetime_epoch(finished_at)
    if (
        decision_epoch is None
        or original_epoch is None
        or available_epoch is None
        or decision_epoch < original_epoch
        or decision_epoch > available_epoch
    ):
        raise ValueError("扫描决策时点必须位于原截止时点与报价可用时间之间")
    decision = datetime.fromisoformat(decision_as_of)
    original = datetime.fromisoformat(str(row["as_of"]))
    temporal = market_scan_temporal_contract(decision, row["mode"])
    if (
        decision.date() != original.date()
        or temporal.data_date.isoformat() != str(row["data_date"])
        or temporal.quote_date.isoformat() != str(row["quote_date"])
    ):
        raise ValueError("扫描决策时点与模式/交易日合同不一致")


__all__ = ["MarketScanQuoteCaptureLifecycleMixin"]
