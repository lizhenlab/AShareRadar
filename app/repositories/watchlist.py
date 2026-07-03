from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from app.config import get_settings
from app.db.user_mappers import row_to_watchlist_item
from app.models.schemas import Quote, WatchlistItem
from app.repositories.base import SQLiteRepository
from app.utils.market_data import finite_float
from app.utils.symbols import standard_symbol
from app.utils.time import now_text, seconds_ago_text


@dataclass(frozen=True)
class WatchlistSaveValues:
    symbol: str
    code: str
    market: str
    name: str
    note: str | None
    group_name: str
    pinned: int
    created_at: str
    updated_at: str


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
            existing = _watchlist_row(conn, symbol)
            values = _watchlist_save_values(quote, symbol, existing, timestamp, note, group_name, pinned)
            _upsert_watchlist_row(conn, values)
        item = self.item(symbol)
        if item is None:
            raise RuntimeError(f"自选股保存失败：{symbol}")
        return item

    def item(self, symbol: str) -> WatchlistItem | None:
        normalized = standard_symbol(symbol)
        quote_join_sql, quote_params = _quote_snapshot_join()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT
                    w.*,
                    q.price AS latest_price,
                    q.change_pct AS latest_change_pct,
                    q.source AS latest_source,
                    q.quote_timestamp AS latest_at
                FROM watchlist w
                {quote_join_sql}
                WHERE w.symbol = ?
                """,
                (*quote_params, normalized),
            ).fetchone()
        return _watchlist_item_from_row(row) if row else None

    def items(self) -> list[WatchlistItem]:
        quote_join_sql, quote_params = _quote_snapshot_join()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    w.*,
                    q.price AS latest_price,
                    q.change_pct AS latest_change_pct,
                    q.source AS latest_source,
                    q.quote_timestamp AS latest_at
                FROM watchlist w
                {quote_join_sql}
                ORDER BY w.pinned DESC, w.group_name ASC, w.updated_at DESC
                """,
                quote_params,
            ).fetchall()
        return [_watchlist_item_from_row(row) for row in rows]

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


def _watchlist_row(conn: sqlite3.Connection, symbol: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM watchlist WHERE symbol = ?", (symbol,)).fetchone()


def _quote_snapshot_join() -> tuple[str, tuple[str, ...]]:
    window = _quote_cache_window()
    if window is None:
        return "LEFT JOIN quote_snapshot q ON 0", ()
    return "LEFT JOIN quote_snapshot q ON q.symbol = w.symbol AND q.fetched_at BETWEEN ? AND ?", window


def _quote_cache_window() -> tuple[str, str] | None:
    ttl_seconds = get_settings().quote_cache_seconds
    if ttl_seconds <= 0:
        return None
    return seconds_ago_text(ttl_seconds), now_text()


def _watchlist_item_from_row(row: sqlite3.Row) -> WatchlistItem:
    item = row_to_watchlist_item(row)
    return item if _valid_latest_quote(item) else _without_latest_quote(item)


def _valid_latest_quote(item: WatchlistItem) -> bool:
    if item.latest_price is None and item.latest_change_pct is None:
        return True
    return finite_float(item.latest_price) is not None and finite_float(item.latest_change_pct) is not None


def _without_latest_quote(item: WatchlistItem) -> WatchlistItem:
    return item.model_copy(
        update={
            "latest_price": None,
            "latest_change_pct": None,
            "latest_source": None,
            "latest_at": None,
        }
    )


def _watchlist_save_values(
    quote: Quote,
    symbol: str,
    existing: sqlite3.Row | None,
    timestamp: str,
    note: str | None,
    group_name: str | None,
    pinned: bool | None,
) -> WatchlistSaveValues:
    return WatchlistSaveValues(
        symbol=symbol,
        code=quote.code,
        market=quote.market,
        name=quote.name,
        note=_clean_watchlist_note(note, existing),
        group_name=_clean_watchlist_group(group_name, existing),
        pinned=_clean_watchlist_pinned(pinned, existing),
        created_at=existing["created_at"] if existing else timestamp,
        updated_at=timestamp,
    )


def _clean_watchlist_note(note: str | None, existing: sqlite3.Row | None) -> str | None:
    if note is not None:
        return note.strip()[:80] or None
    return existing["note"] if existing else None


def _clean_watchlist_group(group_name: str | None, existing: sqlite3.Row | None) -> str:
    if group_name is not None:
        return ((group_name or "默认").strip() or "默认")[:20]
    return existing["group_name"] if existing else "默认"


def _clean_watchlist_pinned(pinned: bool | None, existing: sqlite3.Row | None) -> int:
    if pinned is not None:
        return int(pinned)
    return int(existing["pinned"]) if existing else 0


def _upsert_watchlist_row(conn: sqlite3.Connection, values: WatchlistSaveValues) -> None:
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
            values.symbol,
            values.code,
            values.market,
            values.name,
            values.note,
            values.group_name,
            values.pinned,
            values.created_at,
            values.updated_at,
        ),
    )
