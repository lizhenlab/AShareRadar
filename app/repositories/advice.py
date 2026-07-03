from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from app.config import get_settings
from app.db.user_mappers import row_to_advice
from app.models.schemas import AdviceHistoryItem, AnalysisResult
from app.repositories.base import SQLiteRepository
from app.utils.market_data import finite_float
from app.utils.symbols import standard_symbol
from app.utils.time import now_text, parse_text_time


_ADVICE_INSERT_COLUMNS = (
    "symbol",
    "code",
    "market",
    "name",
    "action",
    "confidence",
    "trend_score",
    "trend_label",
    "risk_level",
    "price",
    "change_pct",
    "support",
    "resistance",
    "data_quality_score",
    "data_quality_level",
    "reason",
    "summary",
    "created_at",
    "updated_at",
    "repeat_count",
)

_ADVICE_UPDATE_COLUMNS = (
    "code",
    "market",
    "name",
    "action",
    "confidence",
    "trend_score",
    "trend_label",
    "risk_level",
    "price",
    "change_pct",
    "support",
    "resistance",
    "data_quality_score",
    "data_quality_level",
    "reason",
    "summary",
    "updated_at",
)

_ADVICE_INSERT_SQL = f"""
    INSERT INTO advice_history ({", ".join(_ADVICE_INSERT_COLUMNS)})
    VALUES ({", ".join(f":{column}" for column in _ADVICE_INSERT_COLUMNS)})
"""

_ADVICE_UPDATE_SQL = f"""
    UPDATE advice_history
    SET
        {", ".join(f"{column} = :{column}" for column in _ADVICE_UPDATE_COLUMNS)},
        repeat_count = COALESCE(repeat_count, 1) + 1
    WHERE id = :row_id
"""


class AdviceHistoryRepository(SQLiteRepository):
    def save_snapshot(self, analysis: AnalysisResult) -> AdviceHistoryItem:
        quote = analysis.quote
        symbol = standard_symbol(f"{quote.market}{quote.code}")
        settings = get_settings()
        created_at = now_text()
        params = _advice_snapshot_params(symbol, analysis, created_at)
        with self._lock, self._connect() as conn:
            latest = _latest_advice_snapshot(conn, symbol)
            if latest and self._is_same_snapshot(latest, analysis, settings.advice_history_dedupe_seconds):
                row_id = int(latest["id"])
                _update_advice_snapshot(conn, row_id, params)
                return _required_advice_by_id(conn, row_id, symbol, "更新")
            cursor = conn.execute(_ADVICE_INSERT_SQL, params)
            row_id = int(cursor.lastrowid)
            return _required_advice_by_id(conn, row_id, symbol, "保存")

    def by_id(self, row_id: int) -> AdviceHistoryItem | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM advice_history WHERE id = ?", (row_id,)).fetchone()
        return row_to_advice(row) if row else None

    def items(self, symbol: str, limit: int = 30) -> list[AdviceHistoryItem]:
        if limit <= 0:
            return []
        normalized = standard_symbol(symbol)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM advice_history
                WHERE symbol = ?
                ORDER BY COALESCE(updated_at, created_at) DESC, id DESC
                LIMIT ?
                """,
                (normalized, limit),
            ).fetchall()
        return [row_to_advice(row) for row in rows]

    @staticmethod
    def _is_same_snapshot(row: sqlite3.Row, analysis: AnalysisResult, dedupe_seconds: int) -> bool:
        if dedupe_seconds <= 0:
            return False
        last_time = _snapshot_time(row)
        return bool(last_time and _within_dedupe_window(last_time, dedupe_seconds) and _same_snapshot_values(row, analysis))


def _snapshot_time(row: sqlite3.Row) -> datetime | None:
    try:
        return parse_text_time(row["updated_at"] or row["created_at"])
    except (TypeError, ValueError):
        return None


def _within_dedupe_window(last_time: datetime, dedupe_seconds: int) -> bool:
    return (datetime.now() - last_time).total_seconds() <= dedupe_seconds


def _same_snapshot_values(row: sqlite3.Row, analysis: AnalysisResult) -> bool:
    return _same_snapshot_labels(row, analysis) and _same_snapshot_numbers(row, analysis)


def _same_snapshot_labels(row: sqlite3.Row, analysis: AnalysisResult) -> bool:
    return (
        row["action"] == analysis.action_advice.action
        and row["trend_label"] == analysis.trend_label
        and row["risk_level"] == analysis.risk_level
    )


def _same_snapshot_numbers(row: sqlite3.Row, analysis: AnalysisResult) -> bool:
    trend_score = finite_float(row["trend_score"])
    support = finite_float(row["support"])
    resistance = finite_float(row["resistance"])
    analysis_trend_score = finite_float(analysis.trend_score)
    analysis_support = finite_float(analysis.support)
    analysis_resistance = finite_float(analysis.resistance)
    if None in (trend_score, support, resistance, analysis_trend_score, analysis_support, analysis_resistance):
        return False
    return (
        abs(trend_score - analysis_trend_score) <= 2
        and abs(support - analysis_support) <= 0.01
        and abs(resistance - analysis_resistance) <= 0.01
    )


def _advice_snapshot_params(symbol: str, analysis: AnalysisResult, created_at: str) -> dict[str, Any]:
    quote = analysis.quote
    return {
        "symbol": symbol,
        "code": quote.code,
        "market": quote.market,
        "name": quote.name,
        "action": analysis.action_advice.action,
        "confidence": analysis.action_advice.confidence,
        "trend_score": analysis.trend_score,
        "trend_label": analysis.trend_label,
        "risk_level": analysis.risk_level,
        "price": quote.price,
        "change_pct": quote.change_pct,
        "support": analysis.support,
        "resistance": analysis.resistance,
        "data_quality_score": analysis.data_quality.score,
        "data_quality_level": analysis.data_quality.level,
        "reason": analysis.action_advice.reason,
        "summary": analysis.beginner_summary,
        "created_at": created_at,
        "updated_at": created_at,
        "repeat_count": 1,
    }


def _latest_advice_snapshot(conn: sqlite3.Connection, symbol: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM advice_history
        WHERE symbol = ?
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (symbol,),
    ).fetchone()


def _update_advice_snapshot(conn: sqlite3.Connection, row_id: int, params: dict[str, Any]) -> None:
    conn.execute(_ADVICE_UPDATE_SQL, {**params, "row_id": row_id})


def _required_advice_by_id(conn: sqlite3.Connection, row_id: int, symbol: str, action: str) -> AdviceHistoryItem:
    row = conn.execute("SELECT * FROM advice_history WHERE id = ?", (row_id,)).fetchone()
    item = row_to_advice(row) if row else None
    if item is None:
        raise RuntimeError(f"建议留痕{action}失败：{symbol}")
    return item
