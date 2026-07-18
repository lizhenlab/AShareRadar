from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
import threading
from typing import Any

from app.config import Settings
from app.db.user_mappers import row_to_advice, row_to_advice_timeline
from app.models.schemas import AdviceHistoryItem, AdviceTimelineItem, AnalysisResult
from app.repositories.base import SQLiteRepository
from app.repositories.watchlist import increment_watchlist_unread_change_count
from app.services.research_conclusion_change import (
    CONCLUSION_BASIS,
    MODEL_VERSION,
    SNAPSHOT_CONTRACT_VERSION,
    compare_conclusions,
    conclusion_identity,
)
from app.services.stock_rule_contracts import RULE_VERSION
from app.utils.market_data import valid_kline
from app.utils.market_time import normalize_market_datetime
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
    "snapshot_contract_version",
    "conclusion_basis",
    "rule_version",
    "model_version",
    "market_time",
    "data_quality_source",
    "kline_adjustment_mode",
    "kline_anchor_date",
    "kline_anchor_close",
    "kline_data_version",
    "kline_contract_version",
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
    "snapshot_contract_version",
    "conclusion_basis",
    "rule_version",
    "model_version",
    "market_time",
    "data_quality_source",
    "kline_adjustment_mode",
    "kline_anchor_date",
    "kline_anchor_close",
    "kline_data_version",
    "kline_contract_version",
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


@dataclass(frozen=True)
class AdviceSnapshotSaveResult:
    item: AdviceHistoryItem
    inserted: bool


class AdviceHistoryRepository(SQLiteRepository):
    def __init__(self, path: Path, lock: threading.RLock, *, settings: Settings) -> None:
        super().__init__(path, lock)
        self.settings = settings

    def save_snapshot(self, analysis: AnalysisResult) -> AdviceHistoryItem:
        return self.save_snapshot_with_status(analysis).item

    def save_snapshot_with_status(self, analysis: AnalysisResult) -> AdviceSnapshotSaveResult:
        quote = analysis.quote
        symbol = standard_symbol(f"{quote.market}{quote.code}")
        created_at = now_text()
        params = _advice_snapshot_params(symbol, analysis, created_at)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            latest = _latest_advice_snapshot(conn, symbol)
            if latest and self._is_same_snapshot(latest, params, self.settings.advice_history_dedupe_seconds):
                row_id = int(latest["id"])
                _update_advice_snapshot(conn, row_id, params)
                return AdviceSnapshotSaveResult(
                    item=_required_advice_by_id(conn, row_id, symbol, "更新"),
                    inserted=False,
                )
            cursor = conn.execute(_ADVICE_INSERT_SQL, params)
            row_id = int(cursor.lastrowid)
            current = conn.execute("SELECT * FROM advice_history WHERE id = ?", (row_id,)).fetchone()
            if _is_comparable_conclusion_change(current, latest):
                increment_watchlist_unread_change_count(conn, symbol)
            return AdviceSnapshotSaveResult(
                item=_required_advice_by_id(conn, row_id, symbol, "保存"),
                inserted=True,
            )

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

    def timeline_items(self, symbol: str, limit: int) -> list[AdviceTimelineItem]:
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
        return [row_to_advice_timeline(row) for row in rows]

    @staticmethod
    def _is_same_snapshot(row: sqlite3.Row, params: dict[str, Any], dedupe_seconds: int) -> bool:
        if dedupe_seconds <= 0:
            return False
        last_time = _snapshot_time(row)
        return bool(
            last_time
            and _within_dedupe_window(last_time, dedupe_seconds)
            and _same_snapshot_values(row, params)
        )


def _snapshot_time(row: sqlite3.Row) -> datetime | None:
    try:
        return parse_text_time(row["updated_at"] or row["created_at"])
    except (TypeError, ValueError):
        return None


def _within_dedupe_window(last_time: datetime, dedupe_seconds: int) -> bool:
    elapsed = (datetime.now() - last_time).total_seconds()
    return 0 <= elapsed <= dedupe_seconds


def _same_snapshot_values(row: sqlite3.Row, params: dict[str, Any]) -> bool:
    previous_identity = conclusion_identity(row_to_advice_timeline(row))
    current_identity = conclusion_identity(params)
    return previous_identity is not None and previous_identity == current_identity


def _is_comparable_conclusion_change(
    current: sqlite3.Row | None,
    previous: sqlite3.Row | None,
) -> bool:
    if current is None or previous is None:
        return False
    comparison = compare_conclusions(
        row_to_advice_timeline(current),
        row_to_advice_timeline(previous),
    )
    return comparison.comparison_status == "comparable" and comparison.has_changes


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
        "snapshot_contract_version": SNAPSHOT_CONTRACT_VERSION,
        "conclusion_basis": CONCLUSION_BASIS,
        "rule_version": RULE_VERSION,
        "model_version": MODEL_VERSION,
        "market_time": quote.timestamp,
        "data_quality_source": analysis.data_quality.source,
        **_advice_kline_provenance(analysis),
    }


def _advice_kline_provenance(analysis: AnalysisResult) -> dict[str, object | None]:
    market_time = normalize_market_datetime(analysis.quote.timestamp)
    cutoff = _completed_kline_cutoff(market_time)
    candidates = []
    for row in analysis.klines:
        row_date = _strict_date(row.date)
        if row.adjustment_mode == "qfq" and row_date is not None and cutoff is not None and row_date <= cutoff and valid_kline(row):
            candidates.append((row_date, row))
    if not candidates:
        return _unknown_kline_provenance()
    _row_date, anchor = max(candidates, key=lambda item: item[0])
    return {
        "kline_adjustment_mode": anchor.adjustment_mode,
        "kline_anchor_date": anchor.date,
        "kline_anchor_close": float(anchor.close),
        "kline_data_version": str(anchor.data_version or "unknown"),
        "kline_contract_version": str(anchor.contract_version or "unknown"),
    }


def _completed_kline_cutoff(value: str | None) -> date | None:
    if value is None:
        return None
    parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    return parsed.date() if parsed.time() >= time(15, 15) else parsed.date() - timedelta(days=1)


def _strict_date(value: object) -> date | None:
    try:
        parsed = date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None
    return parsed if parsed.isoformat() == str(value).strip() else None


def _unknown_kline_provenance() -> dict[str, object | None]:
    return {
        "kline_adjustment_mode": "unknown",
        "kline_anchor_date": None,
        "kline_anchor_close": None,
        "kline_data_version": "unknown",
        "kline_contract_version": "unknown",
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


__all__ = ["AdviceHistoryRepository", "AdviceSnapshotSaveResult"]
