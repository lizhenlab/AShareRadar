from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import app.services.market_scan_modes as market_scan_modes_module
from app.models.market_scan import MarketScanResultItem, MarketScanStartRequest
from app.services.cache import SQLiteCache
from app.services.market_scan_manager import MarketScanManager, market_scan_rule_version
from app.services.market_scan_modes import (
    current_official_source_temporal_contract_matches,
    market_scan_temporal_contract,
)
from app.services.market_scan_scoring import completed_market_scan_klines, score_market_scan_item
from app.services.market_scan_score_dimensions import verify_market_scan_point_in_time_evidence
from app.services.trading_calendar import TradingCalendarCoverageError
from tests.market_scan_test_support import (
    _MarketScanHub,
    _configure_clean_full_market,
    _daily_rows,
    _quote_for,
    _scanner,
    _wait_for_terminal,
)


INTRADAY_AS_OF = datetime(2026, 7, 17, 12, 8)
PREOPEN_AS_OF = datetime(2026, 7, 17, 8, 0)
PREVIOUS_DATA_DATE = date(2026, 7, 16)


@pytest.mark.parametrize(
    ("source_date", "as_of", "expected"),
    (
        (date(2026, 8, 14), "2026-08-14T15:14:59+08:00", False),
        (date(2026, 8, 14), "2026-08-14T15:15:00+08:00", True),
        (date(2026, 8, 14), "2026-08-15T00:38:35+08:00", True),
        (date(2026, 8, 14), "2026-08-16T23:59:59+08:00", True),
        (date(2026, 8, 13), "2026-08-15T00:38:35+08:00", False),
        (date(2026, 8, 15), "2026-08-15T00:38:35+08:00", False),
        (date(2026, 8, 17), "2026-08-15T00:38:35+08:00", False),
        (date(2026, 8, 14), "2026-08-17T15:15:00+08:00", False),
        (date(2026, 8, 17), "2026-08-17T15:15:00+08:00", True),
        (date(2026, 9, 30), "2026-10-05T12:00:00+08:00", True),
    ),
)
def test_current_official_source_temporal_contract_tracks_latest_completed_session(
    source_date: date,
    as_of: str,
    expected: bool,
) -> None:
    parsed = datetime.fromisoformat(as_of)

    assert current_official_source_temporal_contract_matches(
        source_date,
        as_of=parsed,
        captured_at=datetime.fromisoformat("2026-10-09T16:00:00+08:00"),
    ) is expected


def test_current_official_source_temporal_contract_rejects_naive_reverse_and_uncovered_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_date = date(2026, 8, 14)
    aware_as_of = datetime.fromisoformat("2026-08-15T00:38:35+08:00")
    aware_captured = datetime.fromisoformat("2026-08-15T00:49:29+08:00")

    assert not current_official_source_temporal_contract_matches(
        source_date,
        as_of=aware_as_of.replace(tzinfo=None),
        captured_at=aware_captured,
    )
    assert not current_official_source_temporal_contract_matches(
        source_date,
        as_of=aware_as_of,
        captured_at=aware_captured.replace(tzinfo=None),
    )
    assert not current_official_source_temporal_contract_matches(
        source_date,
        as_of=aware_as_of,
        captured_at=aware_as_of.replace(second=34),
    )

    def unavailable(_value: datetime) -> date:
        raise TradingCalendarCoverageError("calendar unavailable")

    monkeypatch.setattr(
        market_scan_modes_module,
        "latest_expected_daily_kline_date",
        unavailable,
    )
    assert not current_official_source_temporal_contract_matches(
        source_date,
        as_of=aware_as_of,
        captured_at=aware_captured,
    )


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


def test_preopen_temporal_contract_uses_previous_completed_session_only() -> None:
    at_midnight = market_scan_temporal_contract(datetime(2026, 7, 17, 0, 0), "preopen")
    before_auction = market_scan_temporal_contract(
        datetime(2026, 7, 17, 9, 14, 59),
        "preopen",
    )

    assert at_midnight.mode == "preopen"
    assert at_midnight.data_date == PREVIOUS_DATA_DATE
    assert at_midnight.quote_date == PREVIOUS_DATA_DATE
    assert before_auction == at_midnight
    assert MarketScanStartRequest(mode="preopen").mode == "preopen"


def test_preopen_temporal_contract_interprets_aware_values_in_shanghai_time() -> None:
    contract = market_scan_temporal_contract(
        datetime(2026, 7, 17, 0, 0, tzinfo=timezone.utc),
        "preopen",
    )

    assert contract.data_date == PREVIOUS_DATA_DATE
    assert contract.quote_date == PREVIOUS_DATA_DATE


@pytest.mark.parametrize(
    "as_of",
    (
        datetime(2026, 7, 17, 9, 15),
        datetime(2026, 7, 18, 8, 0),
    ),
)
def test_preopen_temporal_contract_rejects_auction_and_non_trading_days(
    as_of: datetime,
) -> None:
    with pytest.raises(ValueError, match="盘前复盘.*09:15"):
        market_scan_temporal_contract(as_of, "preopen")


def test_preopen_mode_has_an_independent_window_error() -> None:
    with pytest.raises(ValueError, match="盘前复盘"):
        market_scan_temporal_contract(INTRADAY_AS_OF, "preopen")
    with pytest.raises(ValueError, match="盘中临时"):
        market_scan_temporal_contract(PREOPEN_AS_OF, "intraday")
    with pytest.raises(ValueError, match="盘后正式"):
        market_scan_temporal_contract(PREOPEN_AS_OF, "official")


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
        "feature_window_contract": "market-scan-feature-windows-v1",
        "snapshot_bar_position": "previous-completed-session",
        "volume_ratio_basis": "completed-daily-bars-5d-vs-20d",
        "volume_data_date": PREVIOUS_DATA_DATE.isoformat(),
        "price_volume_alignment": "intraday-time-aligned-volume-unavailable-neutralized",
        "lifecycle_applied": False,
    }
    features = dimensions["raw_features"]
    closes = [
        row.close
        for row in completed_market_scan_klines(rows, PREVIOUS_DATA_DATE)
    ]
    assert features["return_1d_pct"] == pytest.approx((quote.price / closes[-1] - 1) * 100, abs=1e-4)
    assert features["return_5d_pct"] == pytest.approx((quote.price / closes[-5] - 1) * 100, abs=1e-4)
    assert features["return_20d_pct"] == pytest.approx((quote.price / closes[-20] - 1) * 100, abs=1e-4)
    assert features["return_60d_pct"] == pytest.approx((quote.price / closes[-60] - 1) * 100, abs=1e-4)
    assert features["volume_lifecycle_delta"] == 0
    score_inputs = result.score_details["inputs"]
    assert score_inputs["continuous_trend_return_5d_pct"] == pytest.approx(
        features["return_5d_pct"],
        abs=1e-4,
    )
    assert score_inputs["continuous_trend_return_20d_pct"] == pytest.approx(
        features["return_20d_pct"],
        abs=1e-4,
    )
    assert verify_market_scan_point_in_time_evidence(dimensions["point_in_time_evidence"]) is True


def test_preopen_scoring_uses_previous_completed_quote_and_bar() -> None:
    rows = _daily_rows(PREVIOUS_DATA_DATE, 80)
    change = rows[-1].close - rows[-2].close
    change_pct = change / rows[-2].close * 100
    quote = _quote_for("600001", "SH", "盘前样本", change_pct=change_pct).model_copy(
        update={
            "price": rows[-1].close,
            "prev_close": rows[-2].close,
            "change": change,
            "timestamp": "2026-07-16 15:00:00",
        }
    )
    item = MarketScanResultItem(
        run_id=1,
        symbol="600001.SH",
        code="600001",
        market="SH",
        name="盘前样本",
        status="pending",
        updated_at="2026-07-17 08:00:00",
    )

    result = score_market_scan_item(
        item,
        quote,
        rows,
        as_of=PREOPEN_AS_OF,
        completed_cutoff=PREVIOUS_DATA_DATE,
        expected_data_date=PREVIOUS_DATA_DATE,
        expected_quote_date=PREVIOUS_DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
        mode="preopen",
    )

    assert result.status == "success"
    assert result.data_date == PREVIOUS_DATA_DATE.isoformat()
    assert result.quote_timestamp == "2026-07-16 15:00:00"
    dimensions = result.score_details["components"]["score_dimensions"]
    assert dimensions["volume_context"]["mode"] == "preopen"
    assert dimensions["volume_context"]["lifecycle_applied"] is True
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


def test_preopen_manager_persists_previous_completed_session_and_independent_rule(
    tmp_path: Path,
) -> None:
    async def scenario():
        gate = asyncio.Event()
        hub = _MarketScanHub(tmp_path, block_klines=gate)
        scanner = _scanner(hub, now=PREOPEN_AS_OF)
        await scanner.start()
        started = await scanner.create_scan(mode="preopen")
        await scanner.stop()
        return hub, started

    hub, started = asyncio.run(scenario())

    assert started.run.mode == "preopen"
    assert started.run.data_date == PREVIOUS_DATA_DATE.isoformat()
    assert started.run.quote_date == PREVIOUS_DATA_DATE.isoformat()
    assert started.run.rule_version == market_scan_rule_version(hub.settings, mode="preopen")
    assert started.run.rule_version not in {
        market_scan_rule_version(hub.settings, mode="official"),
        market_scan_rule_version(hub.settings, mode="intraday"),
    }


def test_preopen_manager_rechecks_window_immediately_before_persisting(tmp_path: Path) -> None:
    moments = iter((PREOPEN_AS_OF, datetime(2026, 7, 17, 9, 15)))
    hub = _MarketScanHub(tmp_path)
    scanner = MarketScanManager(hub, now=lambda: next(moments))  # type: ignore[arg-type]

    async def scenario() -> None:
        await scanner.start()
        with pytest.raises(ValueError, match="盘前复盘.*09:15"):
            await scanner.create_scan(mode="preopen")
        await scanner.stop()

    asyncio.run(scenario())

    assert scanner.latest_run(mode="preopen") is None


def test_preopen_manager_rechecks_window_before_terminal_publication(tmp_path: Path) -> None:
    clock = {"now": PREOPEN_AS_OF}
    hub = _MarketScanHub(tmp_path)
    scanner = MarketScanManager(hub, now=lambda: clock["now"])  # type: ignore[arg-type]

    async def execute_without_market_io(_run, _cancel_event):
        clock["now"] = datetime(2026, 7, 17, 9, 15)
        return ()

    scanner._executor.execute = execute_without_market_io  # type: ignore[method-assign]

    async def scenario():
        await scanner.start()
        response = await scanner.create_scan(mode="preopen")
        terminal = await _wait_for_terminal(scanner, response.run.id)
        await scanner.stop()
        return terminal

    terminal = asyncio.run(scenario())

    assert terminal.status == "failed"
    assert "盘前复盘" in (terminal.last_error or terminal.message or "")
    assert "09:15" in (terminal.last_error or terminal.message or "")


def test_preopen_manager_rechecks_window_inside_terminal_commit(tmp_path: Path) -> None:
    clock = {"now": datetime(2026, 7, 20, 8, 0)}
    hub = _MarketScanHub(tmp_path)
    _configure_clean_full_market(hub)
    scanner = MarketScanManager(hub, now=lambda: clock["now"])  # type: ignore[arg-type]
    validate_publication_window = scanner._validate_publication_window
    validation_times: list[datetime] = []

    def advance_after_first_validation(run) -> None:
        validation_times.append(clock["now"])
        validate_publication_window(run)
        if len(validation_times) == 1:
            clock["now"] = datetime(2026, 7, 20, 9, 15)

    scanner._validate_publication_window = advance_after_first_validation  # type: ignore[method-assign]

    async def scenario():
        await scanner.start()
        response = await scanner.create_scan(mode="preopen")
        terminal = await _wait_for_terminal(scanner, response.run.id)
        await scanner.stop()
        return terminal

    terminal = asyncio.run(scenario())

    assert validation_times == [
        datetime(2026, 7, 20, 8, 0),
        datetime(2026, 7, 20, 9, 15),
    ]
    assert terminal.status == "failed"
    assert "盘前复盘" in (terminal.last_error or terminal.message or "")
    assert "09:15" in (terminal.last_error or terminal.message or "")


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


def test_preopen_retry_preserves_the_independent_cohort(tmp_path: Path) -> None:
    hub = _MarketScanHub(tmp_path)
    cache: SQLiteCache = hub.cache
    original = cache.create_market_scan_run(
        trigger="manual",
        mode="preopen",
        rule_version=market_scan_rule_version(hub.settings, mode="preopen"),
        as_of="2026-07-17 08:00:00",
        data_date="2026-07-16",
        quote_date="2026-07-16",
        scope="test",
    )
    cache.finish_market_scan_run(original.id, "failed", message="测试失败")

    retried = cache.prepare_market_scan_retry(
        original.id,
        as_of="2026-07-17 08:05:00",
    )

    assert retried.mode == "preopen"
    assert retried.data_date == retried.quote_date == "2026-07-16"
    assert retried.rule_version == original.rule_version
    assert retried.retry_of_run_id == original.id
