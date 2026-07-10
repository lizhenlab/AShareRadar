from __future__ import annotations

from datetime import datetime, timedelta
import math
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from app.repositories.market_quotes import (
    QUOTE_HISTORY_COLUMNS,
    QUOTE_SNAPSHOT_COLUMNS,
    _quote_history_row,
    _quote_snapshot_row,
    _quote_trade_date,
)
from app.services.cache import SQLiteCache
from tests.factories import make_quote


def test_quote_snapshot_row_is_driven_by_column_list() -> None:
    quote = make_quote(
        price=123.4,
        turnover_rate=3.2,
        pe=28.5,
        pb=4.2,
        market_cap=1_000_000_000,
        timestamp="2026-05-13 14:56:00",
        source="测试源",
    )

    row = dict(zip(QUOTE_SNAPSHOT_COLUMNS, _quote_snapshot_row("600519.SH", quote, "2026-05-13 15:00:00"), strict=True))

    assert row["symbol"] == "600519.SH"
    assert row["price"] == 123.4
    assert row["turnover_rate"] == 3.2
    assert row["pe"] == 28.5
    assert row["pb"] == 4.2
    assert row["market_cap"] == 1_000_000_000
    assert row["quote_timestamp"] == "2026-05-13 14:56:00"
    assert row["source"] == "测试源"


def test_quote_history_row_keeps_history_specific_fields_aligned() -> None:
    quote = make_quote(pe=28.5, pb=4.2, market_cap=1_000_000_000, timestamp="2026/05/13 14:56:00", source="测试源")

    row = dict(zip(QUOTE_HISTORY_COLUMNS, _quote_history_row("600519.SH", quote, "2026-05-13 15:00:00"), strict=True))

    assert row["price"] == quote.price
    assert row["change_pct"] == quote.change_pct
    assert row["source"] == "测试源"
    assert row["quote_timestamp"] == "2026/05/13 14:56:00"
    assert row["trade_date"] == "2026-05-13"


def test_quote_trade_date_falls_back_and_normalizes_separators() -> None:
    assert _quote_trade_date(None, "2026-05-13 15:00:00") == "2026-05-13"
    assert _quote_trade_date("", "2026-05-13 15:00:00") == "2026-05-13"
    assert _quote_trade_date("2026/05/13 14:56:00", "2026-05-13 15:00:00") == "2026-05-13"


def test_save_quotes_persists_snapshot_and_history_fields() -> None:
    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        quote = make_quote(turnover_rate=3.2, pe=28.5, pb=4.2, market_cap=1_000_000_000, source="测试源")

        cache.save_quotes([quote])
        cached_quote = cache.get_quotes(["600519.SH"], max_age_seconds=3600)[0]
        history = cache.quote_history("600519.SH", limit=5)[-1]

    assert cached_quote.turnover_rate == 3.2
    assert cached_quote.source == "测试源·缓存"
    assert cached_quote.from_cache is True
    assert cached_quote.fallback_used is False
    assert history["pe"] == 28.5
    assert history["pb"] == 4.2
    assert history["market_cap"] == 1_000_000_000


def test_save_quotes_filters_invalid_quotes_before_persistence() -> None:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.sqlite3"
        cache = SQLiteCache(path)

        cache.save_quotes(
            [
                make_quote(price=math.nan),
                make_quote(pe=math.inf),
                make_quote(price=0),
                make_quote(high=1200),
                make_quote().model_copy(update={"amount": -1}),
            ]
        )
        with sqlite3.connect(path) as conn:
            snapshot_count = conn.execute("SELECT COUNT(*) FROM quote_snapshot").fetchone()[0]
            history_count = conn.execute("SELECT COUNT(*) FROM quote_history").fetchone()[0]

    assert snapshot_count == 0
    assert history_count == 0


def test_quote_cache_filters_non_finite_snapshot_and_history_rows() -> None:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.sqlite3"
        cache = SQLiteCache(path)
        cache.save_quotes([make_quote(pe=28.5)])

        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE quote_snapshot SET price = ?", (math.inf,))
            conn.execute("UPDATE quote_history SET pe = ?", (math.inf,))

        cached_quotes = cache.get_quotes(["600519.SH"], max_age_seconds=3600)
        history = cache.quote_history("600519.SH", limit=5)

    assert cached_quotes == []
    assert history == []


def test_quote_cache_filters_finite_but_invalid_snapshot_and_history_rows() -> None:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.sqlite3"
        cache = SQLiteCache(path)
        cache.save_quotes([make_quote(pe=28.5)])

        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE quote_snapshot SET amount = ?", (-1,))
            conn.execute("UPDATE quote_history SET price = ?", (0,))

        cached_quotes = cache.get_quotes(["600519.SH"], max_age_seconds=3600)
        history = cache.quote_history("600519.SH", limit=5)

    assert cached_quotes == []
    assert history == []


def test_quote_cache_rejects_non_positive_max_age_windows() -> None:
    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        cache.save_quotes([make_quote()])

        assert len(cache.get_quotes(["600519.SH"], max_age_seconds=3600)) == 1
        assert cache.get_quotes(["600519.SH"], max_age_seconds=0) == []
        assert cache.get_quotes(["600519.SH"], max_age_seconds=-1) == []


def test_quote_cache_rejects_future_fetch_timestamps() -> None:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.sqlite3"
        cache = SQLiteCache(path)
        cache.save_quotes([make_quote()])

        assert len(cache.get_quotes(["600519.SH"], max_age_seconds=3600)) == 1

        future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE quote_snapshot SET fetched_at = ?", (future,))

        assert cache.get_quotes(["600519.SH"], max_age_seconds=10**9) == []


def test_quote_history_rejects_non_positive_limit() -> None:
    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        cache.save_quotes([make_quote(pe=28.5)])

        assert cache.quote_history("600519.SH", limit=0) == []
        assert cache.quote_history("600519.SH", limit=-1) == []
