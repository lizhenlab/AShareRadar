from __future__ import annotations

from datetime import date
from pathlib import Path
import sqlite3

from app.db.schema_migrations import COMPAT_COLUMNS
from app.models.schemas import WatchlistUpdate
from app.services.cache import SQLiteCache
from tests.factories import make_quote


QUEUE_COLUMNS = {
    "research_status",
    "priority",
    "next_review_date",
    "last_viewed_at",
    "unread_change_count",
}


def test_legacy_watchlist_migration_preserves_rows_and_matches_fresh_contract(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(legacy_path)
    conn.executescript(
        """
        CREATE TABLE watchlist (
            symbol TEXT PRIMARY KEY,
            code TEXT NOT NULL,
            market TEXT NOT NULL,
            name TEXT NOT NULL,
            note TEXT,
            group_name TEXT NOT NULL DEFAULT '默认',
            pinned INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO watchlist (
            symbol, code, market, name, note, group_name, pinned, created_at, updated_at
        ) VALUES (
            '600519.SH', '600519', 'SH', '贵州茅台', '核心观察', '白酒', 1,
            '2026-07-01 09:00:00', '2026-07-02 10:00:00'
        );
        """
    )
    conn.commit()
    conn.close()

    legacy_cache = SQLiteCache(legacy_path)
    fresh_path = tmp_path / "fresh.sqlite3"
    SQLiteCache(fresh_path)

    item = legacy_cache.watchlist_item("600519")
    assert item is not None
    assert item.note == "核心观察"
    assert item.group_name == "白酒"
    assert item.pinned is True
    assert item.research_status == "watching"
    assert item.priority == "medium"
    assert item.next_review_date is None
    assert item.last_viewed_at is None
    assert item.unread_change_count == 0

    legacy_contract = _column_contract(legacy_path, QUEUE_COLUMNS)
    fresh_contract = _column_contract(fresh_path, QUEUE_COLUMNS)
    assert set(COMPAT_COLUMNS["watchlist"]) == QUEUE_COLUMNS
    assert legacy_contract == fresh_contract
    assert _row_count(legacy_path, "watchlist") == 1


def test_watchlist_defaults_and_post_style_upsert_preserve_queue_metadata(tmp_path: Path) -> None:
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    quote = make_quote()

    default_item = cache.save_watchlist_item(quote)
    assert default_item.research_status == "watching"
    assert default_item.priority == "medium"
    assert default_item.next_review_date is None
    assert default_item.last_viewed_at is None
    assert default_item.unread_change_count == 0

    configured = cache.save_watchlist_item(
        quote,
        note="等待季报",
        group_name="白酒",
        pinned=True,
        research_status="holding_research",
        priority="high",
        next_review_date="2026-07-20",
    )
    counted = cache.increment_watchlist_unread_count(configured.symbol, 3)
    assert counted is not None
    viewed = cache.mark_watchlist_viewed(configured.symbol, clear_unread=False)
    assert viewed is not None

    saved_again = cache.save_watchlist_item(quote.model_copy(update={"name": "贵州茅台A"}))
    assert saved_again.name == "贵州茅台A"
    assert saved_again.note == "等待季报"
    assert saved_again.group_name == "白酒"
    assert saved_again.pinned is True
    assert saved_again.research_status == "holding_research"
    assert saved_again.priority == "high"
    assert saved_again.next_review_date == date(2026, 7, 20)
    assert saved_again.last_viewed_at == viewed.last_viewed_at
    assert saved_again.unread_change_count == 3


def test_watchlist_partial_update_distinguishes_omitted_fields_from_null(tmp_path: Path) -> None:
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    item = cache.save_watchlist_item(
        make_quote(),
        note="保留理由",
        group_name="白酒",
        pinned=True,
        research_status="to_research",
        priority="high",
        next_review_date="2026-07-18",
    )

    priority_only = cache.update_watchlist_item(item.symbol, WatchlistUpdate(priority="low"))
    assert priority_only is not None
    assert priority_only.note == "保留理由"
    assert priority_only.group_name == "白酒"
    assert priority_only.pinned is True
    assert priority_only.research_status == "to_research"
    assert priority_only.next_review_date == date(2026, 7, 18)
    assert priority_only.priority == "low"

    cleared = cache.update_watchlist_item(
        item.symbol,
        WatchlistUpdate(note=None, group_name=None, next_review_date=None),
    )
    assert cleared is not None
    assert cleared.note is None
    assert cleared.group_name == "默认"
    assert cleared.next_review_date is None
    assert cleared.pinned is True
    assert cleared.research_status == "to_research"
    assert cleared.priority == "low"
    assert cache.update_watchlist_item("000001.SZ", WatchlistUpdate(priority="high")) is None


def test_watchlist_sorting_is_stable_and_excluded_symbols_are_not_subscribed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.repositories.watchlist.now_text", lambda: "2026-07-15 12:00:00")
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    rows = [
        ("600001", "overdue-low", "watching", "low", "2026-07-14", True, "2026-07-15 08:00:00"),
        ("600002", "due-high", "watching", "high", "2026-07-15", False, "2026-07-15 07:00:00"),
        ("600003", "future-high", "watching", "high", "2026-07-16", False, "2026-07-15 11:00:00"),
        ("600004", "medium-pinned-old", "watching", "medium", None, True, "2026-07-15 09:00:00"),
        ("600005", "medium-pinned-new", "watching", "medium", None, True, "2026-07-15 10:00:00"),
        ("600006", "excluded-due", "excluded", "high", "2026-07-01", True, "2026-07-15 12:00:00"),
    ]
    for code, name, status, priority, review_date, pinned, updated_at in rows:
        quote = make_quote().model_copy(update={"code": code, "name": name})
        cache.save_watchlist_item(
            quote,
            research_status=status,
            priority=priority,
            next_review_date=review_date,
            pinned=pinned,
        )
        with cache._connect() as conn:
            conn.execute("UPDATE watchlist SET updated_at = ? WHERE symbol = ?", (updated_at, f"{code}.SH"))

    ordered_symbols = [item.symbol for item in cache.watchlist()]
    assert ordered_symbols == [
        "600002.SH",
        "600001.SH",
        "600003.SH",
        "600005.SH",
        "600004.SH",
        "600006.SH",
    ]
    assert set(cache.watchlist_symbols()) == set(ordered_symbols[:-1])
    assert "600006.SH" not in cache.watchlist_symbols()

    selection = cache.watchlist_symbol_selection()
    assert selection.has_entries is True
    assert set(selection.active_symbols) == set(ordered_symbols[:-1])
    assert selection.excluded_symbols == ("600006.SH",)


def test_watchlist_symbol_selection_distinguishes_empty_from_all_excluded(tmp_path: Path) -> None:
    empty_cache = SQLiteCache(tmp_path / "empty.sqlite3")

    empty_selection = empty_cache.watchlist_symbol_selection()

    assert empty_selection.has_entries is False
    assert empty_selection.active_symbols == ()
    assert empty_selection.excluded_symbols == ()

    excluded_cache = SQLiteCache(tmp_path / "excluded.sqlite3")
    excluded_quote = make_quote().model_copy(update={"code": "000001", "market": "SZ"})
    excluded_cache.save_watchlist_item(excluded_quote, research_status="excluded")

    excluded_selection = excluded_cache.watchlist_symbol_selection()

    assert excluded_selection.has_entries is True
    assert excluded_selection.active_symbols == ()
    assert excluded_selection.excluded_symbols == ("000001.SZ",)
    assert excluded_cache.watchlist_symbols() == []


def test_watchlist_mapper_normalizes_dirty_queue_values_without_failing(tmp_path: Path) -> None:
    path = tmp_path / "dirty.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE watchlist (
            symbol TEXT PRIMARY KEY,
            code TEXT,
            market TEXT,
            name TEXT,
            note TEXT,
            group_name TEXT,
            pinned,
            research_status,
            priority,
            next_review_date,
            last_viewed_at,
            unread_change_count,
            created_at TEXT,
            updated_at TEXT
        );
        INSERT INTO watchlist VALUES (
            '600519.SH', '600519', 'SH', NULL,
            'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
            '', 'not-a-bool', 'unknown', 'urgent', '2026-02-30', 'yesterday', -9, NULL, NULL
        );
        """
    )
    conn.commit()
    conn.close()

    cache = SQLiteCache(path)
    item = cache.watchlist_item("600519")

    assert item is not None
    assert item.name == "未知股票"
    assert item.note == "x" * 80
    assert item.group_name == "默认"
    assert item.pinned is False
    assert item.research_status == "watching"
    assert item.priority == "medium"
    assert item.next_review_date is None
    assert item.last_viewed_at is None
    assert item.unread_change_count == 0
    assert item.created_at == ""
    assert item.updated_at == ""


def test_watchlist_unread_count_can_increment_decrement_and_preserve_without_watermark(tmp_path: Path) -> None:
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    symbol = cache.save_watchlist_item(make_quote()).symbol

    incremented = cache.increment_watchlist_unread_count(symbol, 4)
    assert incremented is not None
    assert incremented.unread_change_count == 4

    decremented = cache.adjust_watchlist_unread_count(symbol, -2)
    assert decremented is not None
    assert decremented.unread_change_count == 2

    clamped = cache.adjust_watchlist_unread_count(symbol, -20)
    assert clamped is not None
    assert clamped.unread_change_count == 0

    cache.increment_watchlist_unread_count(symbol, 3)
    preserved = cache.mark_watchlist_viewed(symbol, clear_unread=False)
    assert preserved is not None
    assert preserved.last_viewed_at is not None
    assert preserved.unread_change_count == 3

    legacy_view = cache.mark_watchlist_viewed(symbol, clear_unread=True)
    assert legacy_view is not None
    assert legacy_view.last_viewed_at is not None
    assert legacy_view.unread_change_count == 3
    assert cache.increment_watchlist_unread_count("000001.SZ") is None


def _column_contract(path: Path, columns: set[str]) -> dict[str, tuple[str, int, str | None]]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return {
            row["name"]: (row["type"], row["notnull"], row["dflt_value"])
            for row in conn.execute("PRAGMA table_info(watchlist)")
            if row["name"] in columns
        }
    finally:
        conn.close()


def _row_count(path: Path, table: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()
