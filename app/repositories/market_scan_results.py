from __future__ import annotations

import json
import math
import sqlite3
from typing import Iterable

from app.models.market_scan import MarketScanResultWrite, MarketScanRun, MarketScanSeed
from app.repositories.market_scan_context import MarketScanRepositoryContext
from app.repositories.market_scan_mapping import run_from_row
from app.utils.errors import NotFoundError
from app.utils.time import now_text


MARKET_SCAN_RESULT_SEED_SQL = """
    INSERT INTO market_scan_result (
        run_id, symbol, code, market, name, industry, list_date,
        is_st, is_new, metadata_source, status, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
    ON CONFLICT(run_id, symbol) DO UPDATE SET
        code = excluded.code,
        market = excluded.market,
        name = excluded.name,
        industry = excluded.industry,
        list_date = excluded.list_date,
        is_st = excluded.is_st,
        is_new = excluded.is_new,
        metadata_source = excluded.metadata_source,
        updated_at = excluded.updated_at
"""


class MarketScanResultWriterMixin(MarketScanRepositoryContext):
    def seed_results(
        self,
        run_id: int,
        seeds: Iterable[MarketScanSeed],
        *,
        excluded_count: int,
    ) -> int:
        rows = tuple(seeds)
        stamp = now_text()
        payload = tuple(
            (
                run_id,
                seed.symbol,
                seed.code,
                seed.market,
                seed.name,
                seed.industry,
                seed.list_date,
                int(seed.is_st),
                int(seed.is_new),
                seed.metadata_source,
                stamp,
            )
            for seed in rows
        )
        with self._lock, self._connect() as conn:
            run = required_run_row(conn, run_id)
            if run["status"] != "running":
                raise ValueError("只有运行中的扫描批次可以写入股票池")
            if payload:
                conn.executemany(MARKET_SCAN_RESULT_SEED_SQL, payload)
            total_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM market_scan_result WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0]
            )
            conn.execute(
                """
                UPDATE market_scan_run
                SET total_count = ?, excluded_count = ?, updated_at = ?,
                    message = ?
                WHERE id = ?
                """,
                (total_count, excluded_count, stamp, f"已加载 {total_count} 只股票，开始分批计算", run_id),
            )
        return total_count

    def refresh_pending_metadata(
        self,
        run_id: int,
        seeds: Iterable[MarketScanSeed],
    ) -> int:
        rows = tuple(seeds)
        symbols = [seed.symbol for seed in rows]
        if len(symbols) != len(set(symbols)):
            raise ValueError("股票池元数据包含重复股票")
        stamp = now_text()
        with self._lock, self._connect() as conn:
            run = required_run_row(conn, run_id)
            if run["status"] != "running":
                raise ValueError("只有运行中的扫描批次可以刷新股票元数据")
            cursor = conn.executemany(
                """
                UPDATE market_scan_result
                SET name = ?, industry = ?, list_date = ?, is_st = ?, is_new = ?,
                    metadata_source = ?, updated_at = ?
                WHERE run_id = ? AND symbol = ? AND status = 'pending'
                """,
                (
                    (
                        seed.name,
                        seed.industry,
                        seed.list_date,
                        int(seed.is_st),
                        int(seed.is_new),
                        seed.metadata_source,
                        stamp,
                        run_id,
                        seed.symbol,
                    )
                    for seed in rows
                ),
            )
        return max(0, int(cursor.rowcount))

    def save_result_batch(
        self,
        run_id: int,
        results: Iterable[MarketScanResultWrite],
    ) -> MarketScanRun:
        batch = tuple(results)
        if not batch:
            with self._lock, self._connect() as conn:
                return run_from_row(required_run_row(conn, run_id))
        symbols = [result.symbol for result in batch]
        if len(symbols) != len(set(symbols)):
            raise ValueError("扫描结果批次包含重复股票")
        for result in batch:
            validate_result_write(result)
        stamp = now_text()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = required_run_row(conn, run_id)
            if run["status"] != "running":
                raise ValueError(f"扫描批次 {run_id} 当前状态不能写入结果：{run['status']}")
            placeholders = ", ".join("?" for _symbol in symbols)
            pending = {
                str(row[0])
                for row in conn.execute(
                    f"""
                    SELECT symbol FROM market_scan_result
                    WHERE run_id = ? AND status = 'pending'
                      AND symbol IN ({placeholders})
                    """,
                    (run_id, *symbols),
                ).fetchall()
            }
            missing = sorted(set(symbols) - pending)
            if missing:
                raise ValueError("扫描结果不属于待处理股票：" + "、".join(missing[:10]))
            conn.executemany(
                """
                UPDATE market_scan_result
                SET status = ?, rank = NULL, score = ?, trend_score = ?, leader_score = ?,
                    data_quality_score = ?, price = ?, change_pct = ?, turnover_rate = ?,
                    volume_ratio = ?, amount = ?, tags_json = ?, metrics_json = ?,
                    reason = ?, error = ?, data_date = ?, quote_timestamp = ?,
                    quote_source = ?, kline_source = ?, adjustment_mode = ?,
                    quote_fallback_used = ?, kline_fallback_used = ?,
                    metadata_degraded = ?, degradation_reasons_json = ?, updated_at = ?
                WHERE run_id = ? AND symbol = ? AND status = 'pending'
                """,
                tuple(_result_update_params(run_id, result, stamp) for result in batch),
            )
            sync_run_counts(conn, run_id, stamp=stamp)
            updated = required_run_row(conn, run_id)
        return run_from_row(updated)


def required_run_row(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM market_scan_run WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"全市场扫描批次不存在：{run_id}")
    return row


def sync_run_counts(conn: sqlite3.Connection, run_id: int, *, stamp: str) -> None:
    counts = conn.execute(
        """
        SELECT
            SUM(CASE WHEN status != 'pending' THEN 1 ELSE 0 END) AS processed_count,
            SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count,
            SUM(CASE WHEN status = 'missing' THEN 1 ELSE 0 END) AS missing_count,
            SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped_count
        FROM market_scan_result
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    processed = int(counts["processed_count"] or 0)
    success = int(counts["success_count"] or 0)
    missing = int(counts["missing_count"] or 0)
    skipped = int(counts["skipped_count"] or 0)
    conn.execute(
        """
        UPDATE market_scan_run
        SET processed_count = ?, success_count = ?, missing_count = ?,
            skipped_count = ?, updated_at = ?, message = ?
        WHERE id = ?
        """,
        (processed, success, missing, skipped, stamp, f"已处理 {processed} 只股票", run_id),
    )


def assign_result_ranks(conn: sqlite3.Connection, run_id: int) -> None:
    conn.execute("UPDATE market_scan_result SET rank = NULL WHERE run_id = ?", (run_id,))
    conn.execute(
        """
        WITH ranked AS (
            SELECT symbol,
                   ROW_NUMBER() OVER (
                       ORDER BY score DESC, trend_score DESC, change_pct DESC,
                                amount DESC, symbol ASC
                   ) AS calculated_rank
            FROM market_scan_result
            WHERE run_id = ? AND status = 'success' AND score IS NOT NULL
        )
        UPDATE market_scan_result
        SET rank = (
            SELECT calculated_rank FROM ranked
            WHERE ranked.symbol = market_scan_result.symbol
        )
        WHERE run_id = ? AND status = 'success'
        """,
        (run_id, run_id),
    )


def count_degraded_results(conn: sqlite3.Connection, run_id: int) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM market_scan_result
            WHERE run_id = ? AND status = 'success'
              AND (quote_fallback_used = 1 OR kline_fallback_used = 1 OR metadata_degraded = 1)
            """,
            (run_id,),
        ).fetchone()[0]
    )


def validate_result_write(result: MarketScanResultWrite) -> None:
    if result.status == "pending":
        raise ValueError("待处理状态不是有效的扫描计算结果")
    score_values = (
        result.score,
        result.trend_score,
        result.leader_score,
        result.data_quality_score,
    )
    _require_finite_values(
        result,
        (
            *score_values,
            result.price,
            result.change_pct,
            result.turnover_rate,
            result.volume_ratio,
            result.amount,
            *result.metrics.values(),
        ),
    )
    _require_valid_scores(result, score_values)
    _require_valid_status_fields(result, score_values)
    _require_valid_degradation_fields(result)


def _require_finite_values(
    result: MarketScanResultWrite,
    values: tuple[int | float | None, ...],
) -> None:
    if any(value is not None and not math.isfinite(float(value)) for value in values):
        raise ValueError(f"扫描结果包含非有限数值：{result.symbol}")


def _require_valid_scores(result: MarketScanResultWrite, values: tuple[int | None, ...]) -> None:
    if any(value is not None and not 0 <= int(value) <= 100 for value in values):
        raise ValueError(f"扫描评分超出 0-100：{result.symbol}")


def _require_valid_status_fields(
    result: MarketScanResultWrite,
    scores: tuple[int | None, ...],
) -> None:
    if result.status == "success":
        _require_success_fields(result, scores)
        return
    if any(value is not None for value in scores):
        raise ValueError(f"非成功扫描结果不得携带评分：{result.symbol}")
    if result.status == "missing" and not str(result.error or "").strip():
        raise ValueError(f"缺失扫描结果必须记录错误原因：{result.symbol}")
    if result.status == "skipped" and not str(result.reason or "").strip():
        raise ValueError(f"跳过扫描结果必须记录跳过原因：{result.symbol}")


def _require_success_fields(
    result: MarketScanResultWrite,
    scores: tuple[int | None, ...],
) -> None:
    if any(value is None for value in scores) or not result.data_date:
        raise ValueError(f"成功扫描结果缺少评分或数据日期：{result.symbol}")
    if result.price is None or result.price <= 0:
        raise ValueError(f"成功扫描结果缺少有效价格：{result.symbol}")
    provenance = (result.quote_timestamp, result.quote_source, result.kline_source, result.reason)
    if not all(str(value or "").strip() for value in provenance):
        raise ValueError(f"成功扫描结果缺少数据来源或评分依据：{result.symbol}")
    if result.adjustment_mode != "qfq":
        raise ValueError(f"成功扫描结果不是前复权数据：{result.symbol}")
    if not result.metrics:
        raise ValueError(f"成功扫描结果缺少指标快照：{result.symbol}")


def _require_valid_degradation_fields(result: MarketScanResultWrite) -> None:
    reasons = tuple(reason.strip() for reason in result.degradation_reasons)
    if any(not reason for reason in reasons) or len(reasons) != len(set(reasons)):
        raise ValueError(f"扫描结果降级原因无效或重复：{result.symbol}")
    expected = {
        reason
        for enabled, reason in (
            (result.quote_fallback_used, "quote_fallback"),
            (result.kline_fallback_used, "kline_fallback"),
            (result.metadata_degraded, "metadata_incomplete"),
        )
        if enabled
    }
    if set(reasons) != expected:
        raise ValueError(f"扫描结果降级标记与原因不一致：{result.symbol}")


def _result_update_params(
    run_id: int,
    result: MarketScanResultWrite,
    stamp: str,
) -> tuple[object, ...]:
    return (
        result.status,
        result.score,
        result.trend_score,
        result.leader_score,
        result.data_quality_score,
        result.price,
        result.change_pct,
        result.turnover_rate,
        result.volume_ratio,
        result.amount,
        json.dumps(list(result.tags), ensure_ascii=False, separators=(",", ":")),
        json.dumps(result.metrics, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        (result.reason or "")[:800] or None,
        (result.error or "")[:800] or None,
        result.data_date,
        result.quote_timestamp,
        result.quote_source,
        result.kline_source,
        result.adjustment_mode,
        int(result.quote_fallback_used),
        int(result.kline_fallback_used),
        int(result.metadata_degraded),
        json.dumps(list(result.degradation_reasons), ensure_ascii=True, separators=(",", ":")),
        stamp,
        run_id,
        result.symbol,
    )


__all__ = [
    "MarketScanResultWriterMixin",
    "assign_result_ranks",
    "count_degraded_results",
    "required_run_row",
    "sync_run_counts",
    "validate_result_write",
]
