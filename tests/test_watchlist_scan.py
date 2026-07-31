from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.reviews import WatchlistScanRequest
from app.services.cache import SQLiteCache
from app.services.watchlist_scan import scan_watchlist_conditions
from tests.factories import make_kline


class _ScanHub:
    def __init__(self, rows_by_symbol: dict[str, list]) -> None:
        self.rows_by_symbol = rows_by_symbol

    async def kline(self, symbol: str, *, limit: int, use_cache: bool):
        value = self.rows_by_symbol[symbol]
        if isinstance(value, Exception):
            raise value
        return value


def _rising_rows() -> list:
    return [
        make_kline(
            date=f"2026-04-{index + 1:02d}",
            close=100 + index,
            high=101 + index,
            low=99 + index,
            volume=1000 if index < 20 else 2000,
        )
        for index in range(21)
    ]


def test_explicit_watchlist_scan_reports_success_missing_and_as_of() -> None:
    hub = _ScanHub({"600519.SH": _rising_rows(), "000001.SZ": RuntimeError("no coverage")})
    payload = WatchlistScanRequest(
        universe="symbols",
        symbols=["600519.SH", "000001.SZ"],
        conditions=["close_above_ma20", "volume_surge_5d"],
    )

    result = asyncio.run(
        scan_watchlist_conditions(hub, payload, now=datetime(2026, 5, 1, 16))
    )

    assert result.universe == ["600519.SH", "000001.SZ"]
    assert result.as_of == "2026-05-01 16:00:00"
    assert len(result.success) == 1
    assert result.success[0].matched is True
    assert result.success[0].data_date == "2026-04-21"
    assert len(result.missing) == 1
    assert result.missing[0].symbol == "000001.SZ"


def test_watchlist_scan_contract_rejects_scripts_and_large_universes() -> None:
    with pytest.raises(ValidationError):
        WatchlistScanRequest(
            universe="symbols",
            symbols=["600519.SH"],
            conditions=["__import__('os').system('id')"],
        )
    with pytest.raises(ValidationError):
        WatchlistScanRequest(
            universe="symbols",
            symbols=[f"{index:06d}.SZ" for index in range(51)],
            conditions=["close_above_ma20"],
        )


def test_watchlist_scan_rejects_mutually_exclusive_ma20_conditions() -> None:
    payload = WatchlistScanRequest(
        universe="symbols",
        symbols=["600519.SH"],
        conditions=["close_above_ma20", "close_below_ma20"],
    )

    with pytest.raises(ValueError, match="不能同时选择"):
        asyncio.run(scan_watchlist_conditions(_ScanHub({"600519.SH": _rising_rows()}), payload))


def test_watchlist_scan_history_persists_full_reproducible_result(tmp_path: Path) -> None:
    payload = WatchlistScanRequest(
        universe="symbols",
        symbols=["600519.SH"],
        conditions=["close_above_ma20", "volume_surge_5d"],
    )
    result = asyncio.run(
        scan_watchlist_conditions(
            _ScanHub({"600519.SH": _rising_rows()}),
            payload,
            now=datetime(2026, 5, 1, 16),
        )
    )
    cache = SQLiteCache(tmp_path / "cache.sqlite3")

    saved = cache.save_watchlist_scan(payload, result)
    history = cache.watchlist_scan_history(limit=20)
    loaded = cache.watchlist_scan_record(saved.id)

    assert saved.universe_kind == "symbols"
    assert history[0].matched_count == 1
    assert history[0].conditions == payload.conditions
    assert loaded is not None
    assert loaded.model_dump() == saved.model_dump()
    assert loaded.success[0].metrics["volume_ratio_5d"] == 2.0
