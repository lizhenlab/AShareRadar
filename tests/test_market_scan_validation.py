from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path

import pytest

from app.services.market_scan_validation import MarketScanRuntimeGuard
from tests.market_scan_test_support import (
    SCAN_AS_OF,
    _MarketScanHub,
    _scanner,
    _wait_for_terminal,
)


def test_market_scan_rejects_pool_missing_required_market(tmp_path: Path) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path)
        hub.rows = [item for item in hub.rows if item.market != "BJ"]
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        final = await _wait_for_terminal(scanner, started.run.id)
        await scanner.stop()
        return final

    final = asyncio.run(scenario())

    assert final.status == "failed"
    assert final.total_count == 0
    assert "BJ" in (final.last_error or "")


def test_market_scan_rejects_truncated_individual_market_pool(tmp_path: Path) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path)
        hub.settings = hub.settings.model_copy(update={"market_scan_min_sh_count": 2})
        scanner = _scanner(hub)
        await scanner.start()
        started = await scanner.create_scan(as_of=SCAN_AS_OF)
        final = await _wait_for_terminal(scanner, started.run.id)
        await scanner.stop()
        return final

    final = asyncio.run(scenario())

    assert final.status == "failed"
    assert "SH 1/2" in (final.last_error or "")


def test_market_scan_rejects_intraday_snapshot_before_daily_bars_are_complete(tmp_path: Path) -> None:
    hub = _MarketScanHub(tmp_path)
    scanner = _scanner(hub)

    with pytest.raises(ValueError, match="15:15"):
        asyncio.run(scanner.create_scan(as_of=datetime(2026, 7, 17, 10, 30)))

    assert scanner.latest_run() is None


def test_market_scan_rejects_historical_as_of_before_any_side_effect(tmp_path: Path) -> None:
    async def scenario():
        hub = _MarketScanHub(tmp_path)
        scanner = _scanner(hub, now=datetime(2026, 7, 20, 16, 30))
        with pytest.raises(ValueError, match="当前快照.*已持久化快照"):
            await scanner.create_scan(as_of=SCAN_AS_OF)
        latest = scanner.latest_run()
        await scanner.stop()
        return hub, latest

    hub, latest = asyncio.run(scenario())

    assert latest is None
    assert hub.stock_pool_calls == 0
    assert hub.kline_calls == {}
    assert Path(f"{hub.cache.path}.market-scan.lock").exists() is False


def test_preopen_runtime_guard_allows_work_before_call_auction() -> None:
    guard = MarketScanRuntimeGuard(
        data_date=date(2026, 7, 17),
        quote_date=date(2026, 7, 17),
        mode="preopen",
        wall_clock_budget_seconds=60,
        now=lambda: datetime(2026, 7, 20, 8, 59),
        monotonic=lambda: 1.0,
        started_monotonic=0.0,
    )

    guard.checkpoint()


@pytest.mark.parametrize(
    "current",
    (
        datetime(2026, 7, 20, 9, 15),
        datetime(2026, 7, 19, 8, 0),
    ),
)
def test_preopen_runtime_guard_stops_at_boundary_or_non_trading_day(
    current: datetime,
) -> None:
    guard = MarketScanRuntimeGuard(
        data_date=date(2026, 7, 17),
        quote_date=date(2026, 7, 17),
        mode="preopen",
        wall_clock_budget_seconds=60,
        now=lambda: current,
        monotonic=lambda: 1.0,
        started_monotonic=0.0,
    )

    with pytest.raises(RuntimeError, match="盘前复盘.*09:15"):
        guard.checkpoint()
