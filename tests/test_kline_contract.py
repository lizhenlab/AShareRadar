from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from app.models.schemas import Kline
from app.services.cache import SQLiteCache
from app.services.providers import stamp_daily_kline_contract
from app.utils.time import now_text
from tests.factories import make_kline


LEGACY_KLINE_TABLE_SQL = """
CREATE TABLE kline_daily (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL NOT NULL,
    close REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    volume REAL NOT NULL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
)
"""


def test_legacy_daily_rows_migrate_to_unknown_without_polluting_qfq(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(LEGACY_KLINE_TABLE_SQL)
        conn.execute(
            """
            INSERT INTO kline_daily
                (symbol, date, open, close, high, low, volume, source, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("600519.SH", "2026-05-13", 100, 101, 102, 99, 1000, "legacy", now_text()),
        )

    cache = SQLiteCache(path)
    assert cache.get_klines("600519.SH", 10, 10**9) == []
    legacy = cache.get_klines("600519.SH", 10, 10**9, adjustment_mode="unknown")
    assert [item.date for item in legacy] == ["2026-05-13"]
    assert legacy[0].data_version == "legacy"
    assert legacy[0].contract_version == "legacy"

    SQLiteCache(path)
    with sqlite3.connect(path) as conn:
        columns = conn.execute("PRAGMA table_info(kline_daily)").fetchall()
        primary_key = tuple(row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5])
        migration_count = conn.execute(
            "SELECT COUNT(*) FROM schema_migration WHERE name = ?",
            ("20260716_kline_daily_adjustment_contract",),
        ).fetchone()[0]
    assert primary_key == ("symbol", "adjustment_mode", "date")
    assert migration_count == 1


def test_adjustment_modes_can_coexist_for_the_same_symbol_and_date(tmp_path: Path) -> None:
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    qfq = make_kline(date="2026-05-13", close=101)
    raw = qfq.model_copy(
        update={
            "close": 201,
            "open": 200.5,
            "high": 202,
            "low": 200,
            "adjustment_mode": "none",
            "data_version": "test-daily-kline-raw-v1",
        }
    )

    cache.save_klines("600519.SH", [qfq], "qfq-source")
    cache.save_klines("600519.SH", [raw], "raw-source")

    assert cache.get_klines("600519.SH", 1, 10**9)[0].close == 101
    assert cache.get_klines("600519.SH", 1, 10**9, adjustment_mode="none")[0].close == 201


def test_repository_rejects_a_mixed_adjustment_batch(tmp_path: Path) -> None:
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    qfq = make_kline(date="2026-05-12")
    raw = make_kline(date="2026-05-13", adjustment_mode="none", data_version="raw-v1")

    with pytest.raises(ValueError, match="adjustment_mode"):
        cache.save_klines("600519.SH", [qfq, raw], "mixed")


def test_provider_contract_stamp_is_uniform_and_explicit() -> None:
    rows = [
        Kline(date="2026-05-12", open=99, close=100, high=101, low=98, volume=1000),
        Kline(date="2026-05-13", open=100, close=101, high=102, low=99, volume=1200),
    ]

    stamped = stamp_daily_kline_contract(rows, adjustment_mode="qfq", source="test-provider")

    assert {item.adjustment_mode for item in stamped} == {"qfq"}
    assert {item.as_of for item in stamped} == {"2026-05-13"}
    assert len({item.data_version for item in stamped}) == 1
    assert all(item.data_version != "unknown" for item in stamped)
    assert {item.source for item in stamped} == {"test-provider"}
