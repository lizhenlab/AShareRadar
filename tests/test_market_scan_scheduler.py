from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from tests.market_scan_test_support import _MarketScanHub, _rule_version, _scanner


def test_market_scan_scheduler_respects_publish_floor_and_does_not_repeat_failed_auto_run(
    tmp_path: Path,
) -> None:
    hub = _MarketScanHub(tmp_path)
    hub.settings = hub.settings.model_copy(
        update={
            "market_scan_auto_enabled": True,
            "market_scan_schedule_hour": 14,
            "market_scan_schedule_minute": 0,
        }
    )
    scanner = _scanner(hub)

    async def scenario():
        early = await scanner.scheduled_tick(datetime(2026, 7, 17, 14, 30))
        run = hub.cache.create_market_scan_run(
            trigger="scheduled",
            rule_version=_rule_version(hub),
            as_of="2026-07-17 15:20:00",
            data_date="2026-07-17",
            scope="test",
        )
        hub.cache.start_market_scan_run(run.id)
        hub.cache.finish_market_scan_run(run.id, "failed", message="模拟自动扫描失败")
        repeated = await scanner.scheduled_tick(datetime(2026, 7, 17, 16, 30))
        await scanner.stop()
        return early, repeated

    early, repeated = asyncio.run(scenario())

    assert early is None
    assert repeated is None
    assert scanner.latest_run().id == 1


def test_market_scan_scheduler_respects_same_day_manual_cancellation(tmp_path: Path) -> None:
    hub = _MarketScanHub(tmp_path)
    hub.settings = hub.settings.model_copy(
        update={
            "market_scan_auto_enabled": True,
            "market_scan_schedule_hour": 16,
            "market_scan_schedule_minute": 0,
        }
    )
    run = hub.cache.create_market_scan_run(
        trigger="manual",
        rule_version=_rule_version(hub),
        as_of="2026-07-17 15:20:00",
        data_date="2026-07-17",
        scope="test",
    )
    hub.cache.request_market_scan_cancel(run.id)
    hub.cache.finish_market_scan_run(run.id, "cancelled", message="用户取消")
    scanner = _scanner(hub)

    async def scenario():
        repeated = await scanner.scheduled_tick(datetime(2026, 7, 17, 16, 30))
        await scanner.stop()
        return repeated

    repeated = asyncio.run(scenario())

    assert repeated is None
    assert scanner.latest_run().id == run.id  # type: ignore[union-attr]
