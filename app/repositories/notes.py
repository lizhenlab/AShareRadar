from __future__ import annotations

from datetime import datetime
import math
import sqlite3

from app.db.user_mappers import STOCK_NOTE_COLUMNS, row_to_stock_note, stock_note_revision_from_row
from app.db.connection import SQLITE_AUDIT_EPOCH_FUNCTION
from app.models.market import (
    Quote,
    StockInfo,
)
from app.models.user_data import (
    StockNoteInput,
    StockNoteItem,
    StockNoteUpdate,
)
from app.repositories.base import SQLiteRepository
from app.repositories.update_fields import FieldCleaner, present_updates, update_sql_parts
from app.utils.audit_time import audit_now_text as now_text
from app.utils.symbols import standard_symbol


def _columns_sql(columns: tuple[str, ...]) -> str:
    return ", ".join(columns)


def _named_placeholders_sql(columns: tuple[str, ...]) -> str:
    return ", ".join(f":{column}" for column in columns)


_STOCK_NOTE_INSERT_COLUMNS = STOCK_NOTE_COLUMNS[1:]
_STOCK_NOTE_SELECT_SQL = _columns_sql(STOCK_NOTE_COLUMNS)

_STOCK_NOTE_INSERT_SQL = f"""
    INSERT INTO stock_note (
        {_columns_sql(_STOCK_NOTE_INSERT_COLUMNS)}
    ) VALUES ({_named_placeholders_sql(_STOCK_NOTE_INSERT_COLUMNS)})
"""

NOTE_TRADE_DATE_FORMATS = (
    ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"),
    ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"),
    ("%Y-%m-%d", "%Y-%m-%d"),
    ("%Y/%m/%d", "%Y-%m-%d"),
)


def _clean_note_content(value: str | None) -> str:
    content = (value or "").strip()[:500]
    if not content:
        raise ValueError("笔记内容不能为空")
    return content


def _clean_note_type(value: str | None) -> str:
    return (value or "").strip()[:20] or "观察"


def _clean_note_price(value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("笔记价格必须是有效数字")
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("笔记价格必须是有效数字") from exc
    if not math.isfinite(price):
        raise ValueError("笔记价格必须是有效数字")
    if price <= 0:
        raise ValueError("笔记价格必须大于0")
    return price


def _clean_note_trade_date(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("笔记交易日期格式不合法")
    raw = value.strip()
    if not raw:
        return None
    for input_format, output_format in NOTE_TRADE_DATE_FORMATS:
        try:
            return datetime.strptime(raw, input_format).strftime(output_format)
        except ValueError:
            continue
    raise ValueError("笔记交易日期格式不合法")


def _clean_note_color(value: str | None) -> str | None:
    return (value.strip()[:20] or None) if value else None


NOTE_UPDATE_CLEANERS: dict[str, FieldCleaner] = {
    "content": _clean_note_content,
    "note_type": _clean_note_type,
    "price": _clean_note_price,
    "trade_date": _clean_note_trade_date,
    "color": _clean_note_color,
    "visible": lambda value: _required_bool_int(value, "标注可见状态不能为空"),
}


class StockNoteRevisionConflictError(ValueError):
    """A mutation was based on a different complete persisted note state."""


class StockNoteRepository(SQLiteRepository):
    def create(self, quote: Quote | StockInfo, payload: StockNoteInput) -> StockNoteItem:
        timestamp = now_text()
        params = _stock_note_insert_values(quote, payload, timestamp)
        with self._lock, self._connect() as conn:
            cursor = conn.execute(_STOCK_NOTE_INSERT_SQL, params)
            row_id = cursor.lastrowid
            if row_id is None:
                raise RuntimeError("个股笔记保存失败：未返回记录 ID")
            row = conn.execute(f"SELECT {_STOCK_NOTE_SELECT_SQL} FROM stock_note WHERE id = ?", (row_id,)).fetchone()
            if row is None:
                raise RuntimeError("个股笔记保存失败")
            item = row_to_stock_note(row)
        return item

    def item(self, row_id: int) -> StockNoteItem | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(f"SELECT {_STOCK_NOTE_SELECT_SQL} FROM stock_note WHERE id = ?", (row_id,)).fetchone()
        return row_to_stock_note(row) if row else None

    def items(self, symbol: str, limit: int = 100, visible_only: bool = False) -> list[StockNoteItem]:
        if limit <= 0:
            return []
        normalized = standard_symbol(symbol)
        clauses = ["symbol = ?"]
        params: list[object] = [normalized]
        if visible_only:
            clauses.append("visible = 1")
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {_STOCK_NOTE_SELECT_SQL} FROM stock_note
                WHERE {" AND ".join(clauses)}
                ORDER BY
                    COALESCE(
                        trade_date,
                        strftime('%Y-%m-%d', {SQLITE_AUDIT_EPOCH_FUNCTION}(created_at), 'unixepoch', '+8 hours')
                    ) DESC,
                    {SQLITE_AUDIT_EPOCH_FUNCTION}(created_at) DESC,
                    id DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [row_to_stock_note(row) for row in rows]

    def delete(self, row_id: int, *, expected_revision: str) -> bool:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if _note_for_revision(conn, row_id, expected_revision) is None:
                return False
            cursor = conn.execute("DELETE FROM stock_note WHERE id = ?", (row_id,))
            return cursor.rowcount > 0

    def update(self, row_id: int, payload: StockNoteUpdate) -> StockNoteItem | None:
        updates = present_updates(payload, NOTE_UPDATE_CLEANERS)
        assignments, params = update_sql_parts(updates)
        assignments.append("updated_at = ?")
        params.append(now_text())
        params.append(row_id)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = _note_for_revision(conn, row_id, payload.expected_revision)
            if current is None:
                return None
            if not updates:
                return row_to_stock_note(current)
            cursor = conn.execute(
                f"UPDATE stock_note SET {', '.join(assignments)} WHERE id = ?",
                params,
            )
            if cursor.rowcount <= 0:
                return None
            row = conn.execute(f"SELECT {_STOCK_NOTE_SELECT_SQL} FROM stock_note WHERE id = ?", (row_id,)).fetchone()
            if row is None:
                raise RuntimeError("个股笔记保存失败")
            item = row_to_stock_note(row)
        return item


def _note_for_revision(conn: sqlite3.Connection, row_id: int, expected_revision: str) -> sqlite3.Row | None:
    row = conn.execute(f"SELECT {_STOCK_NOTE_SELECT_SQL} FROM stock_note WHERE id = ?", (row_id,)).fetchone()
    if row is not None and stock_note_revision_from_row(row) != expected_revision:
        raise StockNoteRevisionConflictError("笔记已更新，请读取最新记录后重试")
    return row


def _stock_note_insert_values(quote: Quote | StockInfo, payload: StockNoteInput, timestamp: str) -> dict[str, object | None]:
    return {
        "symbol": standard_symbol(f"{quote.market}{quote.code}"),
        "code": quote.code,
        "market": quote.market,
        "name": quote.name,
        "note_type": _clean_note_type(payload.note_type),
        "content": _clean_note_content(payload.content),
        "price": _clean_note_price(payload.price),
        "trade_date": _clean_note_trade_date(payload.trade_date),
        "color": _clean_note_color(payload.color),
        "visible": _required_bool_int(payload.visible, "标注可见状态不能为空"),
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _required_bool_int(value: bool | None, message: str) -> int:
    if value is None:
        raise ValueError(message)
    return int(value)
