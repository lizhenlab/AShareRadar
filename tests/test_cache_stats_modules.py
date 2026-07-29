from __future__ import annotations

import sqlite3
import threading

import pytest

from app.models.schemas import Kline, MinuteKline, Quote
from app.repositories import cache_stats as cache_stats_module
from app.repositories.cache_stats import (
    SQLITE_MARKET_DATETIME_FUNCTION,
    _normalize_market_datetime,
    _select_latest_market_datetime,
)
from app.services.cache import SQLiteCache


def test_cache_stats_exposes_fetch_and_market_times_without_changing_legacy_aliases(tmp_path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    cache.save_quotes(
        [
            _quote("2026/05/12 15:00:00", code="000001", market="SZ"),
            _quote("2026-05-13 10:28:00", code="600519", market="SH"),
        ]
    )
    cache.save_klines("600519.SH", [_daily_kline("2026-05-12")], "测试日K")
    cache.save_minute_klines(
        "600519.SH",
        "5m",
        [_minute_kline("2026-05-13 10:25:00")],
        "测试分钟K",
    )
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE quote_snapshot SET fetched_at = ?", ("2026-05-13 10:29:01",))
        conn.execute("UPDATE kline_daily SET fetched_at = ?", ("2026-05-13 10:29:02",))
        conn.execute("UPDATE kline_minute SET fetched_at = ?", ("2026-05-13 10:29:03",))

    stats = cache.stats()

    assert stats.latest_quote_at == "2026-05-13 10:29:01"
    assert stats.latest_kline_at == "2026-05-13 10:29:02"
    assert stats.latest_daily_kline_at == "2026-05-13 10:29:02"
    assert stats.latest_minute_kline_at == "2026-05-13 10:29:03"
    assert stats.latest_quote_fetched_at == "2026-05-13 10:29:01"
    assert stats.latest_daily_kline_fetched_at == "2026-05-13 10:29:02"
    assert stats.latest_minute_kline_fetched_at == "2026-05-13 10:29:03"
    assert stats.latest_quote_timestamp == "2026-05-13 10:28:00"
    assert stats.latest_daily_kline_date == "2026-05-12"
    assert stats.latest_minute_kline_timestamp == "2026-05-13 10:25:00"


@pytest.mark.parametrize(
    "value",
    [
        "2026-05-13 10:30:00",
        "2026/05/13 10:30:00",
        "20260513103000",
        "2026-05-13T10:30:00+08:00",
        "2026-05-13T02:30:00Z",
    ],
)
def test_market_datetime_normalization_supports_legacy_compact_and_iso_values(value: str) -> None:
    assert _normalize_market_datetime(value) == "2026-05-13 10:30:00"


def test_cache_stats_selects_latest_real_market_datetime_and_ignores_dirty_values(tmp_path) -> None:
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    _save_market_times(
        cache,
        quote_values=[
            "2026-05-13 10:32:00",
            "2026/05/13 10:33:00",
            "20260513103400",
            "2026-05-13T10:35:00+08:00",
            "2026-05-13T02:35:00Z",
            "not-a-date",
            "",
            "   ",
        ],
        daily_values=[
            "2026-05-12",
            "2026/05/13",
            "20260514",
            "2026-05-13T16:00:00Z",
            "not-a-date",
            "",
        ],
        minute_values=[
            "2026-05-13 10:32:00",
            "2026/05/13 10:33:00",
            "20260513103400",
            "2026-05-13T02:35:00Z",
            "not-a-date",
            "",
        ],
    )

    stats = cache.stats()

    assert stats.latest_quote_timestamp == "2026-05-13 10:35:00"
    assert stats.latest_daily_kline_date == "2026-05-14"
    assert stats.latest_minute_kline_timestamp == "2026-05-13 10:35:00"


def test_cache_stats_returns_none_when_all_market_times_are_invalid_or_empty(tmp_path) -> None:
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    _save_market_times(
        cache,
        quote_values=["not-a-date", "", "   "],
        daily_values=["not-a-date", "", "   "],
        minute_values=["not-a-date", "", "   "],
    )

    stats = cache.stats()

    assert stats.latest_quote_timestamp is None
    assert stats.latest_daily_kline_date is None
    assert stats.latest_minute_kline_timestamp is None


def test_latest_market_datetime_normalizes_only_distinct_values(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE samples (market_time TEXT)")
    values = [
        "2026-05-13 10:32:00",
        "2026/05/13 10:33:00",
        "not-a-date",
    ]
    conn.executemany(
        "INSERT INTO samples (market_time) VALUES (?)",
        [(value,) for value in values for _repeat in range(500)],
    )
    normalized_values: list[object] = []
    original = cache_stats_module._normalize_market_datetime

    def count_normalization(value: object) -> str | None:
        normalized_values.append(value)
        return original(value)

    monkeypatch.setattr(cache_stats_module, "_normalize_market_datetime", count_normalization)
    conn.create_function(
        SQLITE_MARKET_DATETIME_FUNCTION,
        1,
        cache_stats_module._normalize_market_datetime,
        deterministic=True,
    )

    latest = _select_latest_market_datetime(
        conn,
        "SELECT DISTINCT market_time FROM samples",
    )

    assert latest == "2026-05-13 10:33:00"
    assert sorted(str(value) for value in normalized_values) == sorted(values)
    conn.close()


def test_dashboard_read_snapshots_do_not_wait_for_unrelated_repository_lock(tmp_path) -> None:
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    finished = threading.Event()
    outcomes: list[object] = []

    def read_dashboard_snapshots() -> None:
        outcomes.extend(
            (
                cache.stats(),
                cache.provider_statuses(),
                cache.provider_capability_statuses(),
                cache.latest_market_scan_run(),
            )
        )
        finished.set()

    with cache._lock:
        thread = threading.Thread(target=read_dashboard_snapshots)
        thread.start()
        assert finished.wait(timeout=1)

    thread.join(timeout=1)
    assert len(outcomes) == 4
    assert outcomes[1:] == [[], [], None]


def test_cache_stats_primary_daily_fields_ignore_unknown_and_other_adjustments(tmp_path) -> None:
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    cache.save_klines(
        "600519.SH",
        [_daily_kline("2026-05-11"), _daily_kline("2026-05-12")],
        "前复权日K",
    )
    cache.save_klines(
        "600519.SH",
        [
            Kline(
                date="2026-07-01",
                open=99.0,
                close=100.0,
                high=101.0,
                low=98.0,
                volume=1000.0,
            )
        ],
        "迁移未知日K",
    )
    cache.save_klines(
        "600519.SH",
        [_daily_kline("2026-08-01").model_copy(update={"adjustment_mode": "none", "data_version": "test-daily-kline-raw-v1"})],
        "不复权日K",
    )
    cache.save_minute_klines(
        "600519.SH",
        "5m",
        [_minute_kline("2026-05-13 10:25:00")],
        "测试分钟K",
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE kline_daily SET fetched_at = ? WHERE adjustment_mode = 'qfq'",
            ("2026-05-13 10:29:02",),
        )
        conn.execute(
            "UPDATE kline_daily SET fetched_at = ? WHERE adjustment_mode = 'unknown'",
            ("2026-07-01 16:00:00",),
        )
        conn.execute(
            "UPDATE kline_daily SET fetched_at = ? WHERE adjustment_mode = 'none'",
            ("2026-08-01 16:00:00",),
        )

    stats = cache.stats()

    assert stats.daily_kline_count == 2
    assert stats.minute_kline_count == 1
    assert stats.kline_count == 3
    assert stats.latest_kline_at == "2026-05-13 10:29:02"
    assert stats.latest_daily_kline_at == "2026-05-13 10:29:02"
    assert stats.latest_daily_kline_fetched_at == "2026-05-13 10:29:02"
    assert stats.latest_daily_kline_date == "2026-05-12"


def _save_market_times(
    cache: SQLiteCache,
    *,
    quote_values: list[str],
    daily_values: list[str],
    minute_values: list[str],
) -> None:
    cache.save_quotes([_quote(value, code=f"{index:06d}", market="SZ") for index, value in enumerate(quote_values, start=1)])
    cache.save_klines("600519.SH", [_daily_kline(value) for value in daily_values], "测试日K")
    cache.save_minute_klines(
        "600519.SH",
        "5m",
        [_minute_kline(value) for value in minute_values],
        "测试分钟K",
    )


def _quote(timestamp: str, *, code: str, market: str) -> Quote:
    return Quote(
        code=code,
        name="贵州茅台",
        market=market,
        price=100.0,
        prev_close=99.0,
        open=99.5,
        high=101.0,
        low=99.0,
        volume=1000.0,
        amount=100000.0,
        change=1.0,
        change_pct=1.01,
        timestamp=timestamp,
        source="测试报价",
    )


def _daily_kline(value: str) -> Kline:
    return Kline(
        date=value,
        open=99.0,
        close=100.0,
        high=101.0,
        low=98.0,
        volume=1000.0,
        adjustment_mode="qfq",
        as_of="2026-12-31",
        data_version="test-daily-kline-qfq-v1",
    )


def _minute_kline(timestamp: str) -> MinuteKline:
    return MinuteKline(
        timestamp=timestamp,
        open=99.0,
        close=100.0,
        high=101.0,
        low=98.0,
        volume=1000.0,
        interval="5m",
    )
