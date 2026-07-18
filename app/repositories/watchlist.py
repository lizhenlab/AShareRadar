from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
import sqlite3
import threading

from app.config import Settings
from app.db.user_mappers import row_to_advice_timeline, row_to_watchlist_item
from app.models.schemas import Quote, ResearchStatus, WatchlistItem, WatchlistPriority, WatchlistUpdate
from app.repositories.base import SQLiteRepository
from app.repositories.update_fields import present_updates, update_sql_parts
from app.services.research_conclusion_change import compare_conclusions
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
    research_status: str
    priority: str
    next_review_date: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class WatchlistSymbolSelection:
    active_symbols: tuple[str, ...]
    excluded_symbols: tuple[str, ...]
    has_entries: bool


_RESEARCH_STATUSES = frozenset({"to_research", "watching", "holding_research", "excluded"})
_PRIORITIES = frozenset({"high", "medium", "low"})

# Stable queue order: active before excluded, due before not due, then priority,
# pinned state, recency, and symbol as a deterministic final tie-breaker.
_WATCHLIST_ORDER_SQL = """
    CASE
        WHEN lower(trim(COALESCE(w.research_status, ''))) = 'excluded' THEN 1
        ELSE 0
    END ASC,
    CASE
        WHEN date(trim(w.next_review_date)) = trim(w.next_review_date)
             AND trim(w.next_review_date) <= ? THEN 0
        ELSE 1
    END ASC,
    CASE lower(trim(COALESCE(w.priority, '')))
        WHEN 'high' THEN 0
        WHEN 'low' THEN 2
        ELSE 1
    END ASC,
    CASE WHEN CAST(w.pinned AS INTEGER) = 1 THEN 1 ELSE 0 END DESC,
    COALESCE(w.updated_at, '') DESC,
    w.symbol ASC
"""

_NORMALIZED_UNREAD_COUNT_SQL = """
    CASE
        WHEN typeof(unread_change_count) IN ('integer', 'real')
             AND unread_change_count >= 0 THEN CAST(unread_change_count AS INTEGER)
        ELSE 0
    END
"""


def increment_watchlist_unread_change_count(
    conn: sqlite3.Connection,
    symbol: str,
    amount: int = 1,
    *,
    timestamp: str | None = None,
) -> bool:
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
        raise ValueError("未读变化计数增量必须是非负整数")
    return _adjust_watchlist_unread_change_count(
        conn,
        standard_symbol(symbol),
        amount,
        timestamp or now_text(),
    )


def cap_watchlist_unread_change_counts_to_viewable(
    conn: sqlite3.Connection,
    symbols: set[str],
) -> None:
    """Keep materialized badges within the conclusion changes still viewable."""
    for symbol in sorted(symbols):
        viewable_change_count = _viewable_conclusion_change_count(conn, symbol)
        conn.execute(
            f"""
            UPDATE watchlist
            SET unread_change_count = ?
            WHERE symbol = ?
              AND ({_NORMALIZED_UNREAD_COUNT_SQL}) > ?
            """,
            (viewable_change_count, symbol, viewable_change_count),
        )


class WatchlistRepository(SQLiteRepository):
    def __init__(self, path: Path, lock: threading.RLock, *, settings: Settings) -> None:
        super().__init__(path, lock)
        self.settings = settings

    def save_item(
        self,
        quote: Quote,
        note: str | None = None,
        group_name: str | None = None,
        pinned: bool | None = None,
        research_status: ResearchStatus | None = None,
        priority: WatchlistPriority | None = None,
        next_review_date: date | str | None = None,
    ) -> WatchlistItem:
        symbol = standard_symbol(f"{quote.market}{quote.code}")
        timestamp = now_text()
        with self._lock, self._connect() as conn:
            existing = _watchlist_row(conn, symbol)
            values = _watchlist_save_values(
                quote,
                symbol,
                existing,
                timestamp,
                note,
                group_name,
                pinned,
                research_status,
                priority,
                next_review_date,
            )
            _upsert_watchlist_row(conn, values)
        item = self.item(symbol)
        if item is None:
            raise RuntimeError(f"自选股保存失败：{symbol}")
        return item

    def item(self, symbol: str) -> WatchlistItem | None:
        normalized = standard_symbol(symbol)
        quote_join_sql, quote_params = _quote_snapshot_join(self.settings.quote_cache_seconds)
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
        quote_join_sql, quote_params = _quote_snapshot_join(self.settings.quote_cache_seconds)
        today = now_text()[:10]
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
                ORDER BY {_WATCHLIST_ORDER_SQL}
                """,
                (*quote_params, today),
            ).fetchall()
        return [_watchlist_item_from_row(row) for row in rows]

    def update_item(self, symbol: str, payload: WatchlistUpdate) -> WatchlistItem | None:
        normalized = standard_symbol(symbol)
        updates = present_updates(payload, _WATCHLIST_UPDATE_CLEANERS)
        if not updates:
            return self.item(normalized)

        assignments, params = update_sql_parts(updates)
        assignments.append("updated_at = ?")
        params.extend((now_text(), normalized))
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE watchlist SET {', '.join(assignments)} WHERE symbol = ?",
                params,
            )
            if cursor.rowcount <= 0:
                return None
        return self.item(normalized)

    def mark_viewed(
        self,
        symbol: str,
        *,
        clear_unread: bool = True,
        viewed_through_advice_id: int | None = None,
    ) -> WatchlistItem | None:
        if (
            viewed_through_advice_id is not None
            and (
                isinstance(viewed_through_advice_id, bool)
                or not isinstance(viewed_through_advice_id, int)
                or viewed_through_advice_id <= 0
            )
        ):
            raise ValueError("已读建议水位必须是正整数")
        normalized = standard_symbol(symbol)
        timestamp = now_text()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if _watchlist_row(conn, normalized) is None:
                return None
            remaining_unread = None
            if clear_unread and viewed_through_advice_id is not None:
                remaining_unread = _unread_change_count_after_watermark(
                    conn,
                    normalized,
                    viewed_through_advice_id,
                )
            cursor = conn.execute(
                f"""
                UPDATE watchlist
                SET
                    last_viewed_at = ?,
                    unread_change_count = CASE
                        WHEN ? = 1 THEN ?
                        ELSE {_NORMALIZED_UNREAD_COUNT_SQL}
                    END,
                    updated_at = ?
                WHERE symbol = ?
                """,
                (
                    timestamp,
                    int(remaining_unread is not None),
                    remaining_unread or 0,
                    timestamp,
                    normalized,
                ),
            )
            if cursor.rowcount <= 0:
                return None
        return self.item(normalized)

    def adjust_unread_change_count(self, symbol: str, delta: int) -> WatchlistItem | None:
        if isinstance(delta, bool) or not isinstance(delta, int):
            raise ValueError("未读变化计数增量必须是整数")
        normalized = standard_symbol(symbol)
        timestamp = now_text()
        with self._lock, self._connect() as conn:
            if not _adjust_watchlist_unread_change_count(conn, normalized, delta, timestamp):
                return None
        return self.item(normalized)

    def increment_unread_change_count(self, symbol: str, amount: int = 1) -> WatchlistItem | None:
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError("未读变化计数增量必须是非负整数")
        return self.adjust_unread_change_count(symbol, amount)

    def delete(self, symbol: str) -> bool:
        normalized = standard_symbol(symbol)
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM watchlist WHERE symbol = ?", (normalized,))
            return cursor.rowcount > 0

    def symbols(self) -> list[str]:
        return list(self.symbol_selection().active_symbols)

    def symbol_selection(self) -> WatchlistSymbolSelection:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    symbol,
                    CASE
                        WHEN lower(trim(COALESCE(research_status, ''))) = 'excluded' THEN 1
                        ELSE 0
                    END AS is_excluded
                FROM watchlist
                ORDER BY
                    CASE WHEN CAST(pinned AS INTEGER) = 1 THEN 1 ELSE 0 END DESC,
                    COALESCE(updated_at, '') DESC,
                    symbol ASC
                """
            ).fetchall()
        return WatchlistSymbolSelection(
            active_symbols=tuple(row["symbol"] for row in rows if not row["is_excluded"]),
            excluded_symbols=tuple(row["symbol"] for row in rows if row["is_excluded"]),
            has_entries=bool(rows),
        )


def _watchlist_row(conn: sqlite3.Connection, symbol: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM watchlist WHERE symbol = ?", (symbol,)).fetchone()


def _adjust_watchlist_unread_change_count(
    conn: sqlite3.Connection,
    symbol: str,
    delta: int,
    timestamp: str,
) -> bool:
    cursor = conn.execute(
        f"""
        UPDATE watchlist
        SET
            unread_change_count = MAX(0, ({_NORMALIZED_UNREAD_COUNT_SQL}) + ?),
            updated_at = ?
        WHERE symbol = ?
        """,
        (delta, timestamp, symbol),
    )
    return cursor.rowcount > 0


def _unread_change_count_after_watermark(
    conn: sqlite3.Connection,
    symbol: str,
    viewed_through_advice_id: int,
) -> int:
    rows = conn.execute(
        """
        SELECT * FROM advice_history
        WHERE symbol = ?
        ORDER BY id ASC
        """,
        (symbol,),
    ).fetchall()
    if not any(int(row["id"]) == viewed_through_advice_id for row in rows):
        raise ValueError("已读建议水位不存在或不属于该自选股")

    unread_count = 0
    previous = None
    for row in rows:
        current = row_to_advice_timeline(row)
        if current.id > viewed_through_advice_id and previous is not None:
            comparison = compare_conclusions(current, previous)
            if comparison.comparison_status == "comparable" and comparison.has_changes:
                unread_count += 1
        previous = current
    return unread_count


def _viewable_conclusion_change_count(conn: sqlite3.Connection, symbol: str) -> int:
    rows = conn.execute(
        """
        SELECT * FROM advice_history
        WHERE symbol = ?
        ORDER BY id ASC
        """,
        (symbol,),
    ).fetchall()
    change_count = 0
    previous = None
    for row in rows:
        current = row_to_advice_timeline(row)
        if previous is not None:
            comparison = compare_conclusions(current, previous)
            if comparison.comparison_status == "comparable" and comparison.has_changes:
                change_count += 1
        previous = current
    return change_count


def _quote_snapshot_join(ttl_seconds: int) -> tuple[str, tuple[str, ...]]:
    window = _quote_cache_window(ttl_seconds)
    if window is None:
        return "LEFT JOIN quote_snapshot q ON 0", ()
    return "LEFT JOIN quote_snapshot q ON q.symbol = w.symbol AND q.fetched_at BETWEEN ? AND ?", window


def _quote_cache_window(ttl_seconds: int) -> tuple[str, str] | None:
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
    research_status: ResearchStatus | None,
    priority: WatchlistPriority | None,
    next_review_date: date | str | None,
) -> WatchlistSaveValues:
    return WatchlistSaveValues(
        symbol=symbol,
        code=quote.code,
        market=quote.market,
        name=quote.name,
        note=_clean_watchlist_note(note, existing),
        group_name=_clean_watchlist_group(group_name, existing),
        pinned=_clean_watchlist_pinned(pinned, existing),
        research_status=_clean_watchlist_choice(
            research_status,
            existing,
            column="research_status",
            allowed=_RESEARCH_STATUSES,
            default="watching",
        ),
        priority=_clean_watchlist_choice(
            priority,
            existing,
            column="priority",
            allowed=_PRIORITIES,
            default="medium",
        ),
        next_review_date=_clean_watchlist_review_date(next_review_date, existing),
        created_at=_clean_created_at(existing, timestamp),
        updated_at=timestamp,
    )


def _clean_watchlist_note(note: str | None, existing: sqlite3.Row | None) -> str | None:
    if note is not None:
        return _clean_optional_text(note, 80)
    return _clean_optional_text(_existing_value(existing, "note"), 80)


def _clean_watchlist_group(group_name: str | None, existing: sqlite3.Row | None) -> str:
    if group_name is not None:
        return _clean_group_name(group_name)
    return _clean_group_name(_existing_value(existing, "group_name"))


def _clean_watchlist_pinned(pinned: bool | None, existing: sqlite3.Row | None) -> int:
    if pinned is not None:
        return int(pinned)
    parsed = finite_float(_existing_value(existing, "pinned"))
    return int(parsed) if parsed in (0, 1) else 0


def _clean_watchlist_choice(
    value: str | None,
    existing: sqlite3.Row | None,
    *,
    column: str,
    allowed: frozenset[str],
    default: str,
) -> str:
    candidate = value if value is not None else _existing_value(existing, column)
    normalized = str(candidate or "").strip().lower()
    if value is not None and normalized not in allowed:
        raise ValueError(f"{column} 不合法")
    return normalized if normalized in allowed else default


def _clean_watchlist_review_date(
    value: date | str | None,
    existing: sqlite3.Row | None,
) -> str | None:
    if value is not None:
        return _review_date_text(value)
    existing_value = _existing_value(existing, "next_review_date")
    if existing_value is None:
        return None
    try:
        return _review_date_text(existing_value)
    except ValueError:
        return None


def _review_date_text(value: object) -> str:
    text = value.isoformat() if isinstance(value, date) else str(value).strip()
    if len(text) != 10 or text[4:5] != "-" or text[7:8] != "-":
        raise ValueError("复核日期应为 YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("复核日期应为 YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        raise ValueError("复核日期应为 YYYY-MM-DD")
    return text


def _clean_optional_text(value: object, max_length: int) -> str | None:
    text = str(value or "").strip()
    return text[:max_length] or None


def _clean_group_name(value: object) -> str:
    return (str(value or "").strip() or "默认")[:20]


def _clean_created_at(existing: sqlite3.Row | None, timestamp: str) -> str:
    value = str(_existing_value(existing, "created_at") or "").strip()
    return value[:40] or timestamp


def _existing_value(existing: sqlite3.Row | None, column: str) -> object:
    if existing is None:
        return None
    try:
        return existing[column]
    except (IndexError, KeyError):
        return None


def _required_watchlist_choice(value: object, *, allowed: frozenset[str], field: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in allowed:
        raise ValueError(f"{field} 不合法")
    return normalized


def _clean_unread_change_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("未读变化计数必须是非负整数")
    return value


_WATCHLIST_UPDATE_CLEANERS = {
    "note": lambda value: _clean_optional_text(value, 80),
    "group_name": _clean_group_name,
    "pinned": lambda value: int(value),
    "research_status": lambda value: _required_watchlist_choice(
        value,
        allowed=_RESEARCH_STATUSES,
        field="research_status",
    ),
    "priority": lambda value: _required_watchlist_choice(
        value,
        allowed=_PRIORITIES,
        field="priority",
    ),
    "next_review_date": lambda value: _review_date_text(value) if value is not None else None,
    "unread_change_count": _clean_unread_change_count,
}


def _upsert_watchlist_row(conn: sqlite3.Connection, values: WatchlistSaveValues) -> None:
    conn.execute(
        """
        INSERT INTO watchlist (
            symbol, code, market, name, note, group_name, pinned,
            research_status, priority, next_review_date, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            code=excluded.code,
            market=excluded.market,
            name=excluded.name,
            note=excluded.note,
            group_name=excluded.group_name,
            pinned=excluded.pinned,
            research_status=excluded.research_status,
            priority=excluded.priority,
            next_review_date=excluded.next_review_date,
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
            values.research_status,
            values.priority,
            values.next_review_date,
            values.created_at,
            values.updated_at,
        ),
    )
