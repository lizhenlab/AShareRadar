from __future__ import annotations

import sqlite3
from datetime import datetime

from app.config import get_settings
from app.db.mappers import row_to_advice
from app.models.schemas import AdviceHistoryItem, AnalysisResult
from app.repositories.base import SQLiteRepository
from app.utils.symbols import standard_symbol
from app.utils.time import now_text, parse_text_time


class AdviceHistoryRepository(SQLiteRepository):
    def save_snapshot(self, analysis: AnalysisResult) -> AdviceHistoryItem:
        quote = analysis.quote
        symbol = standard_symbol(f"{quote.market}{quote.code}")
        settings = get_settings()
        created_at = now_text()
        with self._lock, self._connect() as conn:
            latest = conn.execute(
                """
                SELECT * FROM advice_history
                WHERE symbol = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (symbol,),
            ).fetchone()
            if latest and self._is_same_snapshot(latest, analysis, settings.advice_history_dedupe_seconds):
                conn.execute(
                    """
                    UPDATE advice_history
                    SET
                        code = ?,
                        market = ?,
                        name = ?,
                        action = ?,
                        confidence = ?,
                        trend_score = ?,
                        trend_label = ?,
                        risk_level = ?,
                        price = ?,
                        change_pct = ?,
                        support = ?,
                        resistance = ?,
                        data_quality_score = ?,
                        data_quality_level = ?,
                        reason = ?,
                        summary = ?,
                        updated_at = ?,
                        repeat_count = COALESCE(repeat_count, 1) + 1
                    WHERE id = ?
                    """,
                    (
                        quote.code,
                        quote.market,
                        quote.name,
                        analysis.action_advice.action,
                        analysis.action_advice.confidence,
                        analysis.trend_score,
                        analysis.trend_label,
                        analysis.risk_level,
                        quote.price,
                        quote.change_pct,
                        analysis.support,
                        analysis.resistance,
                        analysis.data_quality.score,
                        analysis.data_quality.level,
                        analysis.action_advice.reason,
                        analysis.beginner_summary,
                        created_at,
                        latest["id"],
                    ),
                )
                row = conn.execute("SELECT * FROM advice_history WHERE id = ?", (latest["id"],)).fetchone()
                item = row_to_advice(row) if row else None
                if item is None:
                    raise RuntimeError(f"建议留痕更新失败：{symbol}")
                return item
            cursor = conn.execute(
                """
                INSERT INTO advice_history (
                    symbol, code, market, name, action, confidence, trend_score,
                    trend_label, risk_level, price, change_pct, support, resistance,
                    data_quality_score, data_quality_level, reason, summary, created_at, updated_at, repeat_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    quote.code,
                    quote.market,
                    quote.name,
                    analysis.action_advice.action,
                    analysis.action_advice.confidence,
                    analysis.trend_score,
                    analysis.trend_label,
                    analysis.risk_level,
                    quote.price,
                    quote.change_pct,
                    analysis.support,
                    analysis.resistance,
                    analysis.data_quality.score,
                    analysis.data_quality.level,
                    analysis.action_advice.reason,
                    analysis.beginner_summary,
                    created_at,
                    created_at,
                    1,
                ),
            )
            row_id = int(cursor.lastrowid)
        item = self.by_id(row_id)
        if item is None:
            raise RuntimeError(f"建议留痕保存失败：{symbol}")
        return item

    def by_id(self, row_id: int) -> AdviceHistoryItem | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM advice_history WHERE id = ?", (row_id,)).fetchone()
        return row_to_advice(row) if row else None

    def items(self, symbol: str, limit: int = 30) -> list[AdviceHistoryItem]:
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
        try:
            last_time = parse_text_time(row["updated_at"] or row["created_at"])
        except (TypeError, ValueError):
            return False
        if (datetime.now() - last_time).total_seconds() > dedupe_seconds:
            return False
        return (
            row["action"] == analysis.action_advice.action
            and row["trend_label"] == analysis.trend_label
            and row["risk_level"] == analysis.risk_level
            and abs(int(row["trend_score"]) - int(analysis.trend_score)) <= 2
            and abs(float(row["support"]) - float(analysis.support)) <= 0.01
            and abs(float(row["resistance"]) - float(analysis.resistance)) <= 0.01
        )
