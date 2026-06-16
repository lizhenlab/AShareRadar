from __future__ import annotations

from app.db.mappers import row_to_stock_note
from app.models.schemas import Quote, StockNoteInput, StockNoteItem, StockNoteUpdate
from app.repositories.base import SQLiteRepository
from app.utils.symbols import standard_symbol
from app.utils.time import now_text


class StockNoteRepository(SQLiteRepository):
    def create(self, quote: Quote, payload: StockNoteInput) -> StockNoteItem:
        symbol = standard_symbol(f"{quote.market}{quote.code}")
        timestamp = now_text()
        content = payload.content.strip()[:500]
        note_type = (payload.note_type.strip() or "观察")[:20]
        color = (payload.color.strip()[:20] or None) if payload.color else None
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO stock_note (
                    symbol, code, market, name, note_type, content, price, trade_date, color,
                    visible, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    quote.code,
                    quote.market,
                    quote.name,
                    note_type,
                    content,
                    payload.price,
                    payload.trade_date,
                    color,
                    int(payload.visible),
                    timestamp,
                    timestamp,
                ),
            )
            row_id = int(cursor.lastrowid)
        item = self.item(row_id)
        if item is None:
            raise RuntimeError("个股笔记保存失败")
        return item

    def item(self, row_id: int) -> StockNoteItem | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM stock_note WHERE id = ?", (row_id,)).fetchone()
        return row_to_stock_note(row) if row else None

    def items(self, symbol: str, limit: int = 100, visible_only: bool = False) -> list[StockNoteItem]:
        normalized = standard_symbol(symbol)
        visible_clause = "AND visible = 1" if visible_only else ""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM stock_note
                WHERE symbol = ?
                {visible_clause}
                ORDER BY COALESCE(trade_date, created_at) DESC, id DESC
                LIMIT ?
                """,
                (normalized, limit),
            ).fetchall()
        return [row_to_stock_note(row) for row in rows]

    def delete(self, row_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM stock_note WHERE id = ?", (row_id,))
            return cursor.rowcount > 0

    def update(self, row_id: int, payload: StockNoteUpdate) -> StockNoteItem | None:
        updates = payload.model_dump(exclude_unset=True)
        if not updates:
            return self.item(row_id)

        assignments: list[str] = []
        params: list[object] = []
        if "content" in updates:
            content = (payload.content or "").strip()[:500]
            if not content:
                raise ValueError("笔记内容不能为空")
            assignments.append("content = ?")
            params.append(content)
        if "note_type" in updates:
            note_type = (payload.note_type or "").strip()[:20] or "观察"
            assignments.append("note_type = ?")
            params.append(note_type)
        if "price" in updates:
            assignments.append("price = ?")
            params.append(payload.price)
        if "trade_date" in updates:
            trade_date = (payload.trade_date.strip()[:20] or None) if payload.trade_date else None
            assignments.append("trade_date = ?")
            params.append(trade_date)
        if "color" in updates:
            color = (payload.color.strip()[:20] or None) if payload.color else None
            assignments.append("color = ?")
            params.append(color)
        if "visible" in updates:
            if payload.visible is None:
                raise ValueError("标注可见状态不能为空")
            assignments.append("visible = ?")
            params.append(int(payload.visible))

        if not assignments:
            return self.item(row_id)

        assignments.append("updated_at = ?")
        params.append(now_text())
        params.append(row_id)
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE stock_note SET {', '.join(assignments)} WHERE id = ?",
                params,
            )
            if cursor.rowcount <= 0:
                return None
        return self.item(row_id)
