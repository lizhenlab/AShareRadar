from __future__ import annotations

import json
import sqlite3
from typing import Iterable

from app.models.market_scan import (
    MarketScanResultWrite,
    MarketScanRun,
    MarketScanSeed,
)
from app.repositories.market_scan_context import MarketScanRepositoryContext
from app.repositories.market_scan_mapping import (
    encode_result_payload,
    rank_order_sql,
    run_from_row,
)
from app.repositories.market_scan_result_validation import (
    validate_production_result_write,
    validate_result_write,
)
from app.utils.audit_time import audit_now_text as now_text
from app.utils.errors import NotFoundError


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
                    market_progress_json = ?,
                    message = ?
                WHERE id = ?
                """,
                (
                    total_count,
                    excluded_count,
                    stamp,
                    _market_progress_json(conn, run_id),
                    f"已加载 {total_count} 只股票，开始分批计算",
                    run_id,
                ),
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
            for result in batch:
                validate_production_result_write(result, run, conn)
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
                SET status = ?, rank = NULL, score = ?, raw_score = ?, trend_score = ?, leader_score = ?,
                    data_quality_score = ?, price = ?, change_pct = ?, turnover_rate = ?,
                    volume_ratio = ?, amount = ?, tags_json = ?, metrics_json = ?,
                    reason = ?, error = ?, data_date = ?, quote_timestamp = ?, quote_observed_at = ?,
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
            skipped_count = ?, market_progress_json = ?, updated_at = ?, message = ?
        WHERE id = ?
        """,
        (
            processed,
            success,
            missing,
            skipped,
            _market_progress_json(conn, run_id),
            stamp,
            f"已处理 {processed} 只股票",
            run_id,
        ),
    )


def _market_progress_json(conn: sqlite3.Connection, run_id: int) -> str:
    rows = conn.execute(
        """
        SELECT market,
               COUNT(*) AS total_count,
               SUM(CASE WHEN status != 'pending' THEN 1 ELSE 0 END) AS processed_count,
               SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count,
               SUM(CASE WHEN status = 'missing' THEN 1 ELSE 0 END) AS missing_count,
               SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped_count
        FROM market_scan_result
        WHERE run_id = ?
        GROUP BY market
        """,
        (run_id,),
    ).fetchall()
    by_market = {str(row["market"]): row for row in rows}
    payload = [_market_progress_item(market, by_market.get(market)) for market in ("SH", "SZ", "BJ")]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _market_progress_item(market: str, row: sqlite3.Row | None) -> dict[str, object]:
    if row is None:
        return {
            "market": market,
            "total_count": 0,
            "processed_count": 0,
            "success_count": 0,
            "missing_count": 0,
            "skipped_count": 0,
            "coverage_pct": 0.0,
        }
    total = int(row["total_count"] or 0)
    success = int(row["success_count"] or 0)
    skipped = int(row["skipped_count"] or 0)
    eligible = max(0, total - skipped)
    coverage = success / eligible * 100 if eligible else 0.0
    return {
        "market": market,
        "total_count": total,
        "processed_count": int(row["processed_count"] or 0),
        "success_count": success,
        "missing_count": int(row["missing_count"] or 0),
        "skipped_count": skipped,
        "coverage_pct": min(100.0, max(0.0, coverage)),
    }


def assign_result_ranks(conn: sqlite3.Connection, run_id: int) -> None:
    conn.execute("UPDATE market_scan_result SET rank = NULL WHERE run_id = ?", (run_id,))
    order_sql = rank_order_sql()
    conn.execute(
        f"""
        WITH ranked AS (
            SELECT symbol,
                   ROW_NUMBER() OVER (
                       ORDER BY {order_sql}
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


def _result_update_params(
    run_id: int,
    result: MarketScanResultWrite,
    stamp: str,
) -> tuple[object, ...]:
    return (
        result.status,
        result.score,
        result.raw_score if result.raw_score is not None else result.score,
        result.trend_score,
        result.leader_score,
        result.data_quality_score,
        result.price,
        result.change_pct,
        result.turnover_rate,
        result.volume_ratio,
        result.amount,
        json.dumps(list(result.tags), ensure_ascii=False, separators=(",", ":")),
        encode_result_payload(result.metrics, result.score_details),
        (result.reason or "")[:800] or None,
        (result.error or "")[:800] or None,
        result.data_date,
        result.quote_timestamp,
        result.quote_observed_at,
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
