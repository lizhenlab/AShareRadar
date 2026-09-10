"""Public runtime maintenance retains minute rows by exact market instant."""
from __future__ import annotations

from pathlib import Path
import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_datahub
from app.api.routes import local_data
from app.config import Settings
from app.services.cache import SQLiteCache


@pytest.mark.parametrize("entrypoint", ["http", "cache", "automatic"])
def test_minute_retention_keeps_latest_instants_per_partition_without_changing_daily(
    tmp_path: Path, entrypoint: str,
) -> None:
    cache = _cache(tmp_path, 2)
    partitions = {
        ("600519.SH", "5m"): [
            "2026-07-15T11:15:00+09:00", "2026-07-15 10:16:00", "invalid-future-text",
            "2026-07-15T02:20:00.000001Z", "2026-07-15T02:20:00.000002Z",
        ],
        ("000001.SZ", "5m"): ["2026-07-15T10:05:00+08:00", "2026-07-15T02:10:00Z", "zz-invalid"],
        ("600519.SH", "15m"): ["2026-07-15T09:30:00+08:00", "2026-07-15 10:00:00", "2026-07-15T02:15:00Z"],
    }
    expected = {
        ("600519.SH", "5m"): set(partitions[("600519.SH", "5m")][-2:]),
        ("000001.SZ", "5m"): set(partitions[("000001.SZ", "5m")][:2]),
        ("600519.SH", "15m"): set(partitions[("600519.SH", "15m")][-2:]),
    }
    for (symbol, interval), timestamps in partitions.items():
        _seed(cache, timestamps, symbol=symbol, interval=interval)
    with sqlite3.connect(cache.path) as conn:
        conn.execute("""
            INSERT INTO kline_daily(symbol, date, open, close, high, low, volume, source, fetched_at)
            VALUES ('600519.SH', '2026-07-15', 10, 10, 11, 9, 1, 'synthetic', '2026-07-15T04:00:00Z')
        """)
        daily_before = conn.execute("SELECT * FROM kline_daily").fetchall()
    preview, removed = _cleanup(cache, entrypoint)

    assert preview["kline_minute"] == removed["kline_minute"] == 5
    assert removed["kline_daily"] == 0
    with sqlite3.connect(cache.path) as conn:
        remaining = conn.execute("SELECT symbol, interval, timestamp FROM kline_minute").fetchall()
        actual = {partition: {row[2] for row in remaining if row[:2] == partition} for partition in expected}
        assert actual == expected
        assert conn.execute("SELECT * FROM kline_daily").fetchall() == daily_before
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert cache.preview_runtime_cleanup()["kline_minute"] == 0


def test_equal_instants_still_count_as_separate_physical_retention_rows(tmp_path: Path) -> None:
    cache = _cache(tmp_path, 2)
    newest_aliases = {"2026-07-15T02:15:00Z", "2026-07-15 10:15:00"}
    _seed(cache, ["2026-07-15T10:10:00+08:00", *sorted(newest_aliases)])

    preview, removed = _cleanup(cache, "http")

    assert preview["kline_minute"] == removed["kline_minute"] == 1
    assert _timestamps(cache) == newest_aliases


def test_subsecond_retention_keeps_latest_even_when_older_row_was_inserted_last(tmp_path: Path) -> None:
    cache = _cache(tmp_path, 1)
    latest = "2026-07-15 10:15:00.000002"
    _seed(cache, [latest, "2026-07-15T02:15:00.000001Z"])

    _, removed = _cleanup(cache, "http")

    assert removed["kline_minute"] == 1
    assert _timestamps(cache) == {latest}


@pytest.mark.parametrize("later_fetch", [False, True])
def test_equal_instant_retention_uses_latest_observation_tiebreaker(tmp_path: Path, later_fetch: bool) -> None:
    cache = _cache(tmp_path, 1)
    first, latest = "2026-07-15T02:15:00Z", "2026-07-15 10:15:00"
    if later_fetch:
        _seed(cache, [latest], fetched_at="2026-07-15T04:05:00Z")
        _seed(cache, [first])
    else:
        _seed(cache, [first])
        _seed(cache, [latest])

    _, removed = _cleanup(cache, "http")

    assert removed["kline_minute"] == 1
    assert _timestamps(cache) == {latest}


def _cache(tmp_path: Path, limit: int) -> SQLiteCache:
    path = tmp_path / "runtime.sqlite3"
    return SQLiteCache(path, settings=Settings(cache_path=path, scheduler_enabled=False, max_minute_kline_rows=limit))


def _seed(
    cache: SQLiteCache, timestamps: list[str], *, symbol: str = "600519.SH", interval: str = "5m",
    fetched_at: str = "2026-07-15T04:00:00Z",
) -> None:
    with sqlite3.connect(cache.path) as conn:
        conn.executemany("""
            INSERT INTO kline_minute(symbol, interval, timestamp, open, close, high, low, volume, source, fetched_at)
            VALUES (?, ?, ?, 10, 10, 11, 9, 1, 'synthetic', ?)
        """, [(symbol, interval, timestamp, fetched_at) for timestamp in timestamps])


def _timestamps(cache: SQLiteCache) -> set[str]:
    with sqlite3.connect(cache.path) as conn:
        return {row[0] for row in conn.execute("SELECT timestamp FROM kline_minute")}


def _cleanup(cache: SQLiteCache, entrypoint: str) -> tuple[dict[str, int], dict[str, int]]:
    if entrypoint != "http":
        preview = cache.preview_runtime_cleanup()
        removed = cache.cleanup_runtime_rows(compact=False) if entrypoint == "cache" else cache.maintenance_repo.cleanup_regenerable_runtime_rows()
        return preview, removed
    app = FastAPI()
    app.include_router(local_data.router)
    app.dependency_overrides[get_datahub] = lambda: SimpleNamespace(cache=cache, settings=cache.settings)
    with TestClient(app, raise_server_exceptions=False) as client:
        preview = client.get("/api/local-data/cleanup-preview")
        removed = client.post("/api/local-data/cleanup?confirm=retention-cleanup")
    assert preview.status_code == 200, preview.text
    assert removed.status_code == 200 and removed.json()["committed"] is True, removed.text
    return preview.json()["tables"], removed.json()["tables"]
