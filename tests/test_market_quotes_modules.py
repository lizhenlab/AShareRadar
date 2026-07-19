from __future__ import annotations

from datetime import datetime, timedelta
import math
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.repositories.market_quotes import (
    QUOTE_HISTORY_COLUMNS,
    QUOTE_SNAPSHOT_COLUMNS,
    _quote_history_row,
    _quote_snapshot_row,
    _quote_trade_date,
)
from app.repositories.maintenance import (
    GLOBAL_LIMIT,
    PARTITION_LIMIT,
    RuntimeCleanupSpec,
    _cleanup_table,
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
    assert row["quote_timestamp"] == "2026-05-13 14:56:00"
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


def test_save_quotes_round_trips_fallback_provenance_in_snapshot_and_history() -> None:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.sqlite3"
        cache = SQLiteCache(path)
        fallback_quote = make_quote(source="备用行情源").model_copy(update={"fallback_used": True})
        cache.save_quotes([fallback_quote])

        cached_quote = cache.get_quotes(["600519.SH"], max_age_seconds=3600)[0]
        history = cache.quote_history("600519.SH", limit=5)[-1]
        with sqlite3.connect(path) as conn:
            persisted = (
                conn.execute("SELECT fallback_used FROM quote_snapshot").fetchone()[0],
                conn.execute("SELECT fallback_used FROM quote_history").fetchone()[0],
            )

    assert cached_quote.from_cache is True
    assert cached_quote.fallback_used is True
    assert history["fallback_used"] is True
    assert persisted == (1, 1)


def test_equal_timestamp_prefers_non_fallback_quote_regardless_of_fetch_order() -> None:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.sqlite3"
        cache = SQLiteCache(path)
        fallback = make_quote(timestamp="2026-05-13 14:00:00", price=1299.0).model_copy(update={"fallback_used": True})
        clean = make_quote(timestamp="2026-05-13 14:00:00", price=1305.0, pe=28.5)
        with patch(
            "app.repositories.market_quotes.now_text",
            side_effect=["2026-05-13 15:05:00", "2026-05-13 15:00:00", "2026-05-13 15:10:00"],
        ):
            cache.save_quotes([fallback])
            cache.save_quotes([clean])
            cache.save_quotes([fallback.model_copy(update={"price": 1298.0})])

        with sqlite3.connect(path) as conn:
            snapshot = conn.execute(
                "SELECT price, fallback_used, source FROM quote_snapshot WHERE symbol = ?",
                ("600519.SH",),
            ).fetchone()
            history = conn.execute(
                "SELECT price, fallback_used, pe FROM quote_history WHERE symbol = ?",
                ("600519.SH",),
            ).fetchone()

    assert snapshot == (1305.0, 0, clean.source)
    assert history == (1305.0, 0, 28.5)


def test_equal_timestamp_prefers_more_complete_quote_before_fetch_time() -> None:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.sqlite3"
        cache = SQLiteCache(path)
        partial = make_quote(timestamp="2026-05-13 14:00:00", pe=None, pb=None, market_cap=None)
        complete = make_quote(timestamp="2026-05-13 14:00:00", pe=28.5, pb=4.2, market_cap=1_000_000_000)
        with patch(
            "app.repositories.market_quotes.now_text",
            side_effect=["2026-05-13 15:05:00", "2026-05-13 15:00:00"],
        ):
            cache.save_quotes([partial])
            cache.save_quotes([complete])

        with sqlite3.connect(path) as conn:
            snapshot = conn.execute(
                "SELECT pe, pb, market_cap, fetched_at FROM quote_snapshot WHERE symbol = ?",
                ("600519.SH",),
            ).fetchone()

    assert snapshot == (28.5, 4.2, 1_000_000_000, "2026-05-13 15:00:00")


def test_save_quotes_does_not_replace_snapshot_with_out_of_order_quote() -> None:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.sqlite3"
        cache = SQLiteCache(path)
        latest = make_quote(timestamp="2026-05-13 14:00:00", price=1305.0)
        stale = make_quote(timestamp="2026/05/13 13:00:00", price=1299.0)

        with patch(
            "app.repositories.market_quotes.now_text",
            side_effect=["2026-05-13 15:00:00", "2026-05-13 15:05:00"],
        ):
            cache.save_quotes([latest])
            cache.save_quotes([stale])

        with sqlite3.connect(path) as conn:
            snapshot = conn.execute(
                "SELECT price, quote_timestamp, fetched_at FROM quote_snapshot WHERE symbol = ?",
                ("600519.SH",),
            ).fetchone()

    assert snapshot == (1305.0, "2026-05-13 14:00:00", "2026-05-13 15:00:00")


def test_save_quotes_compares_mixed_timezone_formats_by_market_instant() -> None:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.sqlite3"
        cache = SQLiteCache(path)
        with patch(
            "app.repositories.market_quotes.now_text",
            side_effect=[
                "2026-05-13 10:04:30",
                "2026-05-13 10:05:30",
                "2026-05-13 10:06:30",
            ],
        ):
            cache.save_quotes([make_quote(timestamp="2026-05-13T02:04:00+00:00", price=1300.0)])
            cache.save_quotes([make_quote(timestamp="2026-05-13 10:05:00", price=1301.0)])
            cache.save_quotes([make_quote(timestamp="2026-05-13T02:03:00Z", price=1299.0)])

        with sqlite3.connect(path) as conn:
            snapshot = conn.execute(
                "SELECT price, quote_timestamp, fetched_at FROM quote_snapshot WHERE symbol = ?",
                ("600519.SH",),
            ).fetchone()
            history = conn.execute(
                "SELECT price, quote_timestamp, trade_date FROM quote_history WHERE symbol = ?",
                ("600519.SH",),
            ).fetchone()

    assert snapshot == (1301.0, "2026-05-13 10:05:00", "2026-05-13 10:05:30")
    assert history == (1301.0, "2026-05-13 10:05:00", "2026-05-13")


def test_save_quotes_upgrades_legacy_mixed_timezone_cache_rows_without_misordering() -> None:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.sqlite3"
        cache = SQLiteCache(path)
        cache.save_quotes([make_quote(timestamp="2026-05-13 10:04:00", price=1300.0)])
        with sqlite3.connect(path) as conn:
            conn.execute(
                "UPDATE quote_snapshot SET quote_timestamp = ? WHERE symbol = ?",
                ("2026-05-13T02:04:00+00:00", "600519.SH"),
            )
            conn.execute(
                "UPDATE quote_history SET quote_timestamp = ? WHERE symbol = ?",
                ("2026-05-13T02:04:00+00:00", "600519.SH"),
            )
            conn.commit()

        cache.save_quotes([make_quote(timestamp="2026-05-13 10:05:00", price=1301.0)])

        with sqlite3.connect(path) as conn:
            snapshot = conn.execute(
                "SELECT price, quote_timestamp FROM quote_snapshot WHERE symbol = ?",
                ("600519.SH",),
            ).fetchone()
            history = conn.execute(
                "SELECT price, quote_timestamp FROM quote_history WHERE symbol = ?",
                ("600519.SH",),
            ).fetchone()

    assert snapshot == (1301.0, "2026-05-13 10:05:00")
    assert history == (1301.0, "2026-05-13 10:05:00")


def test_save_quotes_snapshot_uses_fetched_at_for_equal_quote_timestamp() -> None:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.sqlite3"
        cache = SQLiteCache(path)

        def snapshot() -> tuple[float, str, str]:
            with sqlite3.connect(path) as conn:
                row = conn.execute(
                    "SELECT price, quote_timestamp, fetched_at FROM quote_snapshot WHERE symbol = ?",
                    ("600519.SH",),
                ).fetchone()
            assert row is not None
            return row

        with patch(
            "app.repositories.market_quotes.now_text",
            side_effect=[
                "2026-05-13 15:00:00",
                "2026-05-13 14:59:59",
                "2026-05-13 15:00:00",
                "2026-05-13 15:00:01",
            ],
        ):
            cache.save_quotes([make_quote(timestamp="2026-05-13 14:00:00", price=1300.0)])
            cache.save_quotes([make_quote(timestamp="2026/05/13 14:00:00", price=1299.0)])
            assert snapshot() == (1300.0, "2026-05-13 14:00:00", "2026-05-13 15:00:00")

            cache.save_quotes([make_quote(timestamp="2026-05-13 14:00:00", price=1301.0)])
            assert snapshot() == (1301.0, "2026-05-13 14:00:00", "2026-05-13 15:00:00")

            cache.save_quotes([make_quote(timestamp="2026-05-13 14:00:00", price=1302.0)])
            assert snapshot() == (1302.0, "2026-05-13 14:00:00", "2026-05-13 15:00:01")


def test_save_quotes_upserts_only_the_latest_daily_history_snapshot() -> None:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.sqlite3"
        cache = SQLiteCache(path)
        first = make_quote(timestamp="2026-05-13 10:00:00", price=1300.0)
        latest = make_quote(timestamp="2026-05-13 14:00:00", price=1305.0, pe=28.5)
        stale = make_quote(timestamp="2026/05/13 09:00:00", price=1299.0, pe=20.0)

        cache.save_quotes([first])
        with sqlite3.connect(path) as conn:
            first_id = conn.execute("SELECT id FROM quote_history").fetchone()[0]

        cache.save_quotes([latest])
        cache.save_quotes([latest])
        cache.save_quotes([stale])

        with sqlite3.connect(path) as conn:
            rows = conn.execute("SELECT id, price, pe, quote_timestamp, trade_date FROM quote_history").fetchall()
        history = cache.quote_history("600519.SH", limit=5)

    assert rows == [(first_id, 1305.0, 28.5, "2026-05-13 14:00:00", "2026-05-13")]
    assert len(history) == 1
    assert history[0]["price"] == 1305.0
    assert history[0]["quote_timestamp"] == "2026-05-13 14:00:00"


def test_quote_history_returns_recent_days_in_ascending_order() -> None:
    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        cache.save_quotes([make_quote(timestamp="2026-05-12 14:00:00", price=1298.0)])
        cache.save_quotes([make_quote(timestamp="2026-05-13 14:00:00", price=1305.0)])
        cache.save_quotes([make_quote(timestamp="2026-05-14 14:00:00", price=1308.0)])

        history = cache.quote_history("600519.SH", limit=2)

    assert [row["trade_date"] for row in history] == ["2026-05-13", "2026-05-14"]
    assert [row["price"] for row in history] == [1305.0, 1308.0]


def test_quote_history_latest_days_query_uses_the_unique_index() -> None:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.sqlite3"
        cache = SQLiteCache(path)
        cache.save_quotes([make_quote(timestamp="2026-05-13 14:00:00")])

        with sqlite3.connect(path) as conn:
            plan = conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT price, trade_date
                FROM quote_history
                WHERE symbol = ? AND trade_date <> ''
                ORDER BY trade_date DESC
                LIMIT ?
                """,
                ("600519.SH", 5),
            ).fetchall()

    details = " ".join(str(row[3]) for row in plan)
    assert "uq_quote_history_symbol_trade_date" in details
    assert "USE TEMP B-TREE" not in details


def test_save_quotes_filters_invalid_quotes_before_persistence() -> None:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.sqlite3"
        cache = SQLiteCache(path)

        cache.save_quotes(
            [
                make_quote().model_copy(update={"price": math.nan, "change": math.nan}),
                make_quote().model_copy(update={"pe": math.inf}),
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


def test_partition_cleanup_uses_one_set_delete_for_5500_symbols() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sample_rows (id INTEGER PRIMARY KEY, bucket TEXT NOT NULL, created_at INTEGER NOT NULL)")
    conn.executemany(
        "INSERT INTO sample_rows (bucket, created_at) VALUES (?, ?)",
        ((f"symbol-{symbol:04d}", value) for symbol in range(5500) for value in range(3)),
    )
    spec = RuntimeCleanupSpec(
        "sample_rows",
        "unused",
        "id",
        "created_at DESC, id DESC",
        partition_by=("bucket",),
        limit_scope=PARTITION_LIMIT,
    )
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    removed = _cleanup_table(conn, spec, limit=2)
    conn.set_trace_callback(None)
    count_range = conn.execute(
        "SELECT MIN(row_count), MAX(row_count), COUNT(*) FROM (SELECT COUNT(*) AS row_count FROM sample_rows GROUP BY bucket)"
    ).fetchone()
    conn.close()

    delete_statements = [statement for statement in statements if statement.lstrip().upper().startswith("DELETE")]
    assert removed == 5500
    assert count_range == (2, 2, 5500)
    assert len(delete_statements) == 1
    assert "ROW_NUMBER() OVER (PARTITION BY bucket" in delete_statements[0]


def test_global_cleanup_enforces_one_limit_across_all_rows() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sample_events (id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL)")
    conn.executemany("INSERT INTO sample_events (created_at) VALUES (?)", [(value,) for value in range(6)])
    spec = RuntimeCleanupSpec(
        "sample_events",
        "unused",
        "id",
        "created_at DESC, id DESC",
        limit_scope=GLOBAL_LIMIT,
    )

    removed = _cleanup_table(conn, spec, limit=3, delete_batch_rows=2)
    remaining = [row[0] for row in conn.execute("SELECT created_at FROM sample_events ORDER BY created_at")]
    conn.close()

    assert removed == 3
    assert remaining == [3, 4, 5]


def test_cleanup_below_threshold_remains_a_single_bounded_statement() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sample_events (id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL)")
    conn.executemany("INSERT INTO sample_events (created_at) VALUES (?)", [(value,) for value in range(3)])
    spec = RuntimeCleanupSpec(
        "sample_events",
        "unused",
        "id",
        "created_at DESC, id DESC",
        limit_scope=GLOBAL_LIMIT,
    )
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    assert _cleanup_table(conn, spec, limit=3) == 0
    conn.close()

    delete_statements = [statement for statement in statements if statement.lstrip().upper().startswith("DELETE")]
    assert len(delete_statements) == 1
    assert "ROW_NUMBER() OVER (ORDER BY" in delete_statements[0]
