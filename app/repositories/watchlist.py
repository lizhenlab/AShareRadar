from __future__ import annotations

from app.db.mappers import row_to_watchlist_item
from app.models.schemas import Quote, WatchlistItem
from app.repositories.base import SQLiteRepository
from app.utils.symbols import standard_symbol
from app.utils.time import now_text


class WatchlistRepository(SQLiteRepository):
    def save_item(
        self,
        quote: Quote,
        note: str | None = None,
        group_name: str | None = None,
        pinned: bool | None = None,
    ) -> WatchlistItem:
        symbol = standard_symbol(f"{quote.market}{quote.code}")
        timestamp = now_text()
        with self._lock, self._connect() as conn:
            existing = conn.execute("SELECT * FROM watchlist WHERE symbol = ?", (symbol,)).fetchone()
            created_at = existing["created_at"] if existing else timestamp
            clean_note = (note.strip()[:80] or None) if note is not None else (existing["note"] if existing else None)
            clean_group = (
                ((group_name or "默认").strip() or "默认")[:20]
                if group_name is not None
                else (existing["group_name"] if existing else "默认")
            )
            clean_pinned = int(pinned) if pinned is not None else (int(existing["pinned"]) if existing else 0)
            conn.execute(
                """
                INSERT INTO watchlist (
                    symbol, code, market, name, note, group_name, pinned, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    code=excluded.code,
                    market=excluded.market,
                    name=excluded.name,
                    note=excluded.note,
                    group_name=excluded.group_name,
                    pinned=excluded.pinned,
                    updated_at=excluded.updated_at
                """,
                (
                    symbol,
                    quote.code,
                    quote.market,
                    quote.name,
                    clean_note,
                    clean_group,
                    clean_pinned,
                    created_at,
                    timestamp,
                ),
            )
        item = self.item(symbol)
        if item is None:
            raise RuntimeError(f"自选股保存失败：{symbol}")
        return item

    def item(self, symbol: str) -> WatchlistItem | None:
        normalized = standard_symbol(symbol)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    w.*,
                    q.price AS latest_price,
                    q.change_pct AS latest_change_pct,
                    q.source AS latest_source,
                    q.quote_timestamp AS latest_at
                FROM watchlist w
                LEFT JOIN quote_snapshot q ON q.symbol = w.symbol
                WHERE w.symbol = ?
                """,
                (normalized,),
            ).fetchone()
        return row_to_watchlist_item(row) if row else None

    def items(self) -> list[WatchlistItem]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    w.*,
                    q.price AS latest_price,
                    q.change_pct AS latest_change_pct,
                    q.source AS latest_source,
                    q.quote_timestamp AS latest_at
                FROM watchlist w
                LEFT JOIN quote_snapshot q ON q.symbol = w.symbol
                ORDER BY w.pinned DESC, w.group_name ASC, w.updated_at DESC
                """
            ).fetchall()
        return [row_to_watchlist_item(row) for row in rows]

    def delete(self, symbol: str) -> bool:
        normalized = standard_symbol(symbol)
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM watchlist WHERE symbol = ?", (normalized,))
            return cursor.rowcount > 0

    def symbols(self) -> list[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT symbol FROM watchlist
                ORDER BY pinned DESC, updated_at DESC
                """
            ).fetchall()
        return [row["symbol"] for row in rows]
