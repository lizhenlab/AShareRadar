from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path

import pytest

from app.models.market_scan import MarketScanResultItem
from app.services.cache import SQLiteCache
from app.services.market_scan_manager import market_scan_rule_version
from app.services.market_scan_modes import market_scan_temporal_contract
from app.services.market_scan_scoring import score_market_scan_item
from app.services.market_scan_score_dimensions import verify_market_scan_point_in_time_evidence
from tests.market_scan_test_support import (
    _MarketScanHub,
    _daily_rows,
    _quote_for,
    _scanner,
)


INTRADAY_AS_OF = datetime(2026, 7, 17, 12, 8)
PREVIOUS_DATA_DATE = date(2026, 7, 16)


def test_scan_temporal_contract_separates_intraday_and_official_boundaries() -> None:
    intraday = market_scan_temporal_contract(INTRADAY_AS_OF, "intraday")

    assert intraday.data_date == PREVIOUS_DATA_DATE
    assert intraday.quote_date == INTRADAY_AS_OF.date()

    with pytest.raises(ValueError, match="15:15"):
        market_scan_temporal_contract(INTRADAY_AS_OF, "official")

    official = market_scan_temporal_contract(datetime(2026, 7, 17, 15, 15), "official")
    assert official.data_date == date(2026, 7, 17)
    assert official.quote_date == official.data_date

    with pytest.raises(ValueError, match="09:30 至 15:15"):
        market_scan_temporal_contract(datetime(2026, 7, 17, 15, 15), "intraday")


def test_intraday_scoring_uses_today_quote_and_previous_completed_bar() -> None:
    rows = _daily_rows(PREVIOUS_DATA_DATE, 80)
    quote = _quote_for("600001", "SH", "盘中样本", change_pct=1.9).model_copy(
        update={
            "price": 10.5,
            "prev_close": rows[-1].close,
            "timestamp": "2026-07-17 12:08:00",
        }
    )
    item = MarketScanResultItem(
        run_id=1,
        symbol="600001.SH",
        code="600001",
        market="SH",
        name="盘中样本",
        status="pending",
        updated_at="2026-07-17 12:08:00",
    )

    result = score_market_scan_item(
        item,
        quote,
        rows,
        as_of=INTRADAY_AS_OF,
        completed_cutoff=PREVIOUS_DATA_DATE,
        expected_data_date=PREVIOUS_DATA_DATE,
        expected_quote_date=INTRADAY_AS_OF.date(),
        min_history_rows=60,
        min_data_quality_score=0,
        mode="intraday",
    )

    assert result.status == "success"
    assert result.data_date == PREVIOUS_DATA_DATE.isoformat()
    assert result.quote_timestamp == "2026-07-17 12:08:00"
    dimensions = result.score_details["components"]["score_dimensions"]
    assert dimensions["volume_context"] == {
        "mode": "intraday",
        "volume_ratio_basis": "completed-daily-bars-5d-vs-20d",
        "volume_data_date": PREVIOUS_DATA_DATE.isoformat(),
        "price_volume_alignment": "intraday-time-aligned-volume-unavailable-neutralized",
        "lifecycle_applied": False,
    }
    assert dimensions["raw_features"]["volume_lifecycle_delta"] == 0
    assert verify_market_scan_point_in_time_evidence(dimensions["point_in_time_evidence"]) is True


def test_intraday_manager_persists_explicit_mode_and_both_dates(tmp_path: Path) -> None:
    async def scenario():
        gate = asyncio.Event()
        hub = _MarketScanHub(tmp_path, block_klines=gate)
        scanner = _scanner(hub, now=INTRADAY_AS_OF)
        await scanner.start()
        started = await scanner.create_scan(mode="intraday")
        await scanner.stop()
        return hub, started

    hub, started = asyncio.run(scenario())

    assert started.run.mode == "intraday"
    assert started.run.data_date == PREVIOUS_DATA_DATE.isoformat()
    assert started.run.quote_date == INTRADAY_AS_OF.date().isoformat()
    assert started.run.rule_version == market_scan_rule_version(hub.settings, mode="intraday")
    assert started.run.rule_version != market_scan_rule_version(hub.settings, mode="official")


def test_intraday_retry_preserves_mode_and_temporal_contract(tmp_path: Path) -> None:
    hub = _MarketScanHub(tmp_path)
    cache: SQLiteCache = hub.cache
    original = cache.create_market_scan_run(
        trigger="manual",
        mode="intraday",
        rule_version=market_scan_rule_version(hub.settings, mode="intraday"),
        as_of="2026-07-17 12:08:00",
        data_date="2026-07-16",
        quote_date="2026-07-17",
        scope="test",
    )
    cache.finish_market_scan_run(original.id, "failed", message="测试失败")

    retried = cache.prepare_market_scan_retry(
        original.id,
        as_of="2026-07-17 12:15:00",
    )

    assert retried.mode == "intraday"
    assert retried.as_of == "2026-07-17 12:15:00"
    assert cache.market_scan_run(original.id).as_of == "2026-07-17 12:08:00"
    assert retried.data_date == original.data_date
    assert retried.quote_date == original.quote_date
    assert retried.rule_version == original.rule_version
