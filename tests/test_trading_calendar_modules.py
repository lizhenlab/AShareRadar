from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.services import trading_calendar


@pytest.fixture(autouse=True)
def _reset_trade_calendar_cache(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH", "0")
    monkeypatch.setenv("TRADE_CALENDAR_AUTO_FETCH", "0")
    trading_calendar._reset_calendar_caches()
    trading_calendar._log_coverage_warning.cache_clear()
    yield
    _wait_for_auto_refresh()
    trading_calendar._reset_calendar_caches()
    trading_calendar._log_coverage_warning.cache_clear()


def test_bundled_baseline_metadata_matches_canonical_dates() -> None:
    payload = json.loads(trading_calendar.BUNDLED_CALENDAR_PATH.read_text(encoding="utf-8"))
    trade_dates = payload["trade_dates"]

    assert payload == {
        "schema_version": 1,
        "source": "akshare.tool_trade_date_hist_sina",
        "updated_at": "2026-06-11 15:01:53",
        "min_date": "1990-12-19",
        "max_date": "2026-12-31",
        "trade_date_count": 8797,
        "trade_dates": trade_dates,
    }
    assert len(trade_dates) == len(set(trade_dates)) == 8797
    assert trade_dates == sorted(trade_dates)
    assert trade_dates[0] == payload["min_date"]
    assert trade_dates[-1] == payload["max_date"]
    assert datetime.fromisoformat(payload["updated_at"])


def test_bundled_resource_path_is_source_tree_relative_and_cwd_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = Path(trading_calendar.__file__).resolve().parent.parent / "resources" / "trading_calendar.json"

    monkeypatch.chdir(tmp_path)
    loaded, warning = trading_calendar._load_calendar_file(
        trading_calendar.BUNDLED_CALENDAR_PATH,
        trading_calendar.TradeCalendarSource.BUNDLED_BASELINE,
    )

    assert trading_calendar.BUNDLED_CALENDAR_PATH == expected
    assert trading_calendar.BUNDLED_CALENDAR_PATH.is_absolute()
    assert loaded is not None and loaded.min_date == date(1990, 12, 19)
    assert warning is None


def test_fresh_clone_uses_bundled_baseline_and_keeps_weekday_holiday_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(trading_calendar, "CALENDAR_PATH", tmp_path / "missing-runtime.json")
    trading_calendar._reset_calendar_caches()

    assert not trading_calendar.is_trading_day(date(2026, 2, 16))
    assert trading_calendar.is_trading_day(date(2026, 2, 13))
    assert trading_calendar.calendar_source(date(2026, 2, 16)) == "bundled_baseline"


@pytest.mark.parametrize(
    "runtime_contents",
    [
        "{not-json",
        json.dumps({"trade_dates": []}),
        json.dumps({"trade_dates": ["bad-date"]}),
        json.dumps(
            {
                "source": "test-runtime",
                "updated_at": "2026-01-06 12:00:00",
                "min_date": "2026-01-01",
                "max_date": "2026-01-06",
                "trade_date_count": 1,
                "trade_dates": ["2026-01-05", "2026-01-05"],
            }
        ),
    ],
)
def test_damaged_runtime_cannot_hide_valid_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_contents: str,
) -> None:
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(runtime_contents, encoding="utf-8")
    bundle_path = tmp_path / "bundle.json"
    _write_calendar(bundle_path, ["2025-12-31", "2026-01-05", "2026-01-06"], updated_at="2026-01-02 12:00:00")
    _use_paths(monkeypatch, runtime_path, bundle_path)

    status = trading_calendar.calendar_status(date(2026, 1, 1))

    assert status.source is trading_calendar.TradeCalendarSource.BUNDLED_BASELINE
    assert status.covered
    assert status.warning is not None and "运行时交易日历" in status.warning
    assert not trading_calendar.is_trading_day(date(2026, 1, 1))
    assert trading_calendar.previous_trade_date(date(2026, 1, 4)) == date(2025, 12, 31)


def test_newer_runtime_wins_inside_its_shorter_coverage_and_bundle_fills_outside(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_path = tmp_path / "runtime.json"
    bundle_path = tmp_path / "bundle.json"
    _write_calendar(
        runtime_path,
        ["2026-05-13", "2026-06-30"],
        updated_at="2026-07-01 12:00:00",
        source="runtime-test",
    )
    _write_calendar(
        bundle_path,
        ["2026-05-13", "2026-05-14", "2026-06-30", "2026-12-31"],
        updated_at="2026-06-11 12:00:00",
        source="bundle-test",
    )
    _use_paths(monkeypatch, runtime_path, bundle_path)

    assert trading_calendar.calendar_source(date(2026, 5, 14)) == "runtime_cache"
    assert not trading_calendar.is_trading_day(date(2026, 5, 14))
    assert trading_calendar.calendar_source(date(2026, 12, 31)) == "bundled_baseline"
    assert trading_calendar.is_trading_day(date(2026, 12, 31))

    _days, covered_range = trading_calendar._calendar_resolution(
        date(2026, 6, 30),
        range_start=date(2026, 5, 13),
        allow_auto_refresh=False,
    )
    _days, extended_range = trading_calendar._calendar_resolution(
        date(2026, 12, 31),
        range_start=date(2026, 5, 13),
        allow_auto_refresh=False,
    )

    assert covered_range.source is trading_calendar.TradeCalendarSource.RUNTIME_CACHE
    assert extended_range.source is trading_calendar.TradeCalendarSource.BUNDLED_BASELINE


def test_nonempty_expired_runtime_with_auto_fetch_attempts_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_path = tmp_path / "runtime.json"
    bundle_path = tmp_path / "bundle.json"
    _write_calendar(runtime_path, ["2025-12-30", "2025-12-31"])
    _write_calendar(bundle_path, ["2025-12-30", "2025-12-31"])
    _use_paths(monkeypatch, runtime_path, bundle_path)
    monkeypatch.setenv("ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH", "1")
    monkeypatch.setattr(trading_calendar, "_market_now", lambda: datetime(2026, 1, 5, 12, 0, 0))
    fetch = Mock(
        return_value=trading_calendar.TradeDateFetchResult(
            {date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)}
        )
    )
    monkeypatch.setattr(trading_calendar, "_fetch_akshare_trade_dates_result", fetch)

    assert not trading_calendar.is_trading_day(date(2026, 1, 5))
    _wait_until(lambda: fetch.call_count == 1)
    _wait_until(lambda: trading_calendar.calendar_source(date(2026, 1, 5)) == "runtime_cache")
    assert trading_calendar.is_trading_day(date(2026, 1, 5))
    payload = json.loads(runtime_path.read_text(encoding="utf-8"))
    assert payload["trade_dates"] == ["2026-01-02", "2026-01-05", "2026-01-06"]


def test_auto_fetch_refreshes_stale_runtime_even_when_bundle_covers_current_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_path = tmp_path / "runtime.json"
    bundle_path = tmp_path / "bundle.json"
    _write_calendar(runtime_path, ["2025-12-30", "2025-12-31"])
    _write_calendar(bundle_path, ["2026-01-02", "2026-01-05", "2026-01-06"])
    _use_paths(monkeypatch, runtime_path, bundle_path)
    monkeypatch.setenv("ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH", "1")
    monkeypatch.setattr(trading_calendar, "_market_now", lambda: datetime(2026, 1, 5, 12, 0, 0))
    fetch = Mock(
        return_value=trading_calendar.TradeDateFetchResult(
            {date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)}
        )
    )
    monkeypatch.setattr(trading_calendar, "_fetch_akshare_trade_dates_result", fetch)

    initial = trading_calendar.calendar_status(date(2026, 1, 5))

    assert initial.source is trading_calendar.TradeCalendarSource.BUNDLED_BASELINE
    assert initial.covered
    _wait_until(lambda: fetch.call_count == 1)
    _wait_until(lambda: trading_calendar.calendar_source(date(2026, 1, 5)) == "runtime_cache")
    assert trading_calendar.is_trading_day(date(2026, 1, 5))


def test_implicit_auto_fetch_is_nonblocking_singleflight_and_applies_after_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_path = tmp_path / "runtime.json"
    bundle_path = tmp_path / "bundle.json"
    _write_calendar(runtime_path, ["2025-12-30", "2025-12-31"])
    _write_calendar(bundle_path, ["2025-12-30", "2025-12-31"])
    _use_paths(monkeypatch, runtime_path, bundle_path)
    monkeypatch.setenv("ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH", "1")
    monkeypatch.setattr(trading_calendar, "_market_now", lambda: datetime(2026, 1, 5, 12, 0, 0))
    monkeypatch.setattr(trading_calendar, "TRADE_CALENDAR_FETCH_TIMEOUT_SECONDS", 1.0)
    fetch_started = threading.Event()
    release_fetch = threading.Event()
    fetch_calls: list[int] = []

    def slow_fetch() -> trading_calendar.TradeDateFetchResult:
        fetch_calls.append(1)
        fetch_started.set()
        release_fetch.wait(1)
        return trading_calendar.TradeDateFetchResult(
            {date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)}
        )

    monkeypatch.setattr(trading_calendar, "_fetch_akshare_trade_dates_blocking", slow_fetch)

    started = time.monotonic()
    initial = trading_calendar.is_trading_day(date(2026, 1, 5))
    elapsed = time.monotonic() - started
    try:
        assert not initial
        assert elapsed < 0.2
        assert fetch_started.wait(0.5)
        assert all(not trading_calendar.is_trading_day(date(2026, 1, 5)) for _item in range(5))
        assert len(fetch_calls) == 1
    finally:
        release_fetch.set()

    _wait_until(lambda: trading_calendar.is_trading_day(date(2026, 1, 5)))
    _wait_for_auto_refresh()

    assert trading_calendar.calendar_source(date(2026, 1, 5)) == "runtime_cache"
    assert len(fetch_calls) == 1


def test_missing_all_coverage_fails_closed_without_date_search_loops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_paths(monkeypatch, tmp_path / "missing-runtime.json", tmp_path / "missing-bundle.json")
    target = date(2027, 1, 4)

    assert not trading_calendar.is_trading_day(target)
    assert trading_calendar.market_session_phase(datetime(2027, 1, 4, 10, 0)) is trading_calendar.MarketSessionPhase.CLOSED
    assert trading_calendar.calendar_source(target) == "unavailable"
    with pytest.raises(trading_calendar.TradingCalendarCoverageError, match="无法推导交易日期"):
        trading_calendar.previous_trade_date(target)
    with pytest.raises(trading_calendar.TradingCalendarCoverageError, match="无法推导交易日期"):
        trading_calendar.latest_expected_trade_date(datetime(2027, 1, 4, 10, 0))
    with pytest.raises(trading_calendar.TradingCalendarCoverageError, match="无法推导交易日期"):
        trading_calendar.latest_expected_daily_kline_date(datetime(2027, 1, 4, 16, 0))
    with pytest.raises(trading_calendar.TradingCalendarCoverageError, match="无法推导交易日期"):
        trading_calendar.expected_quote_date(datetime(2027, 1, 4, 10, 0))
    with pytest.raises(trading_calendar.TradingCalendarCoverageError, match="无法推导交易日期"):
        trading_calendar.trading_day_gap(date(2026, 12, 31), target)


def test_out_of_coverage_is_distinct_from_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_path = tmp_path / "bundle.json"
    _write_calendar(bundle_path, ["2026-01-02", "2026-01-05"])
    _use_paths(monkeypatch, tmp_path / "missing-runtime.json", bundle_path)

    status = trading_calendar.calendar_status(date(2027, 1, 4))

    assert status.source is trading_calendar.TradeCalendarSource.OUT_OF_COVERAGE
    assert not status.covered
    assert not trading_calendar.is_trading_day(date(2027, 1, 4))
    assert "bundled_baseline 2026-01-02 至 2026-01-05" in (status.warning or "")


def test_calendar_warnings_are_deduplicated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _use_paths(monkeypatch, tmp_path / "missing-runtime.json", tmp_path / "missing-bundle.json")

    with caplog.at_level(logging.WARNING, logger="app.services.trading_calendar"):
        trading_calendar.is_trading_day(date(2027, 1, 4))
        trading_calendar.is_trading_day(date(2027, 1, 4))

    assert caplog.text.count("没有可用的可信交易日历") == 1


def test_trading_day_gap_and_previous_date_use_only_trusted_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_path = tmp_path / "bundle.json"
    _write_calendar(bundle_path, ["2025-12-31", "2026-01-05", "2026-01-06", "2026-01-07"])
    _use_paths(monkeypatch, tmp_path / "missing-runtime.json", bundle_path)

    assert trading_calendar.previous_trade_date(date(2026, 1, 4)) == date(2025, 12, 31)
    assert trading_calendar.trading_day_gap(date(2025, 12, 31), date(2026, 1, 7)) == 3
    with pytest.raises(trading_calendar.TradingCalendarCoverageError):
        trading_calendar.trading_day_gap(date(2025, 12, 30), date(2026, 1, 7))


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 5, 13, 9, 14, 59), trading_calendar.MarketSessionPhase.PRE_OPEN),
        (datetime(2026, 5, 13, 9, 15, 0), trading_calendar.MarketSessionPhase.CALL_AUCTION),
        (datetime(2026, 5, 13, 9, 30, 0), trading_calendar.MarketSessionPhase.MORNING),
        (datetime(2026, 5, 13, 11, 30, 0), trading_calendar.MarketSessionPhase.MORNING),
        (datetime(2026, 5, 13, 11, 30, 1), trading_calendar.MarketSessionPhase.MIDDAY_BREAK),
        (datetime(2026, 5, 13, 13, 0, 0), trading_calendar.MarketSessionPhase.AFTERNOON_REOPEN_GRACE),
        (datetime(2026, 5, 13, 13, 15, 0), trading_calendar.MarketSessionPhase.AFTERNOON_REOPEN_GRACE),
        (datetime(2026, 5, 13, 13, 15, 1), trading_calendar.MarketSessionPhase.AFTERNOON),
        (datetime(2026, 5, 13, 15, 0, 0), trading_calendar.MarketSessionPhase.AFTERNOON),
        (datetime(2026, 5, 13, 15, 0, 1), trading_calendar.MarketSessionPhase.CLOSE_PUBLISH_BUFFER),
        (datetime(2026, 5, 13, 15, 14, 59), trading_calendar.MarketSessionPhase.CLOSE_PUBLISH_BUFFER),
        (datetime(2026, 5, 13, 15, 15, 0), trading_calendar.MarketSessionPhase.AFTER_CLOSE),
    ],
)
def test_market_session_phase_has_explicit_second_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
    expected: trading_calendar.MarketSessionPhase,
) -> None:
    bundle_path = tmp_path / "bundle.json"
    _write_calendar(
        bundle_path,
        ["2026-05-11", "2026-05-12", "2026-05-13", "2026-05-14", "2026-05-15", "2026-05-18"],
    )
    _use_paths(monkeypatch, tmp_path / "missing-runtime.json", bundle_path)

    assert trading_calendar.market_session_phase(now) is expected
    assert trading_calendar.is_trading_session(now) is (
        expected
        in {
            trading_calendar.MarketSessionPhase.MORNING,
            trading_calendar.MarketSessionPhase.AFTERNOON_REOPEN_GRACE,
            trading_calendar.MarketSessionPhase.AFTERNOON,
        }
    )
    assert trading_calendar.is_midday_break(now) is (expected is trading_calendar.MarketSessionPhase.MIDDAY_BREAK)
    assert trading_calendar.is_after_close(now) is (
        expected
        in {
            trading_calendar.MarketSessionPhase.CLOSE_PUBLISH_BUFFER,
            trading_calendar.MarketSessionPhase.AFTER_CLOSE,
        }
    )


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 5, 13, 15, 14, 59), date(2026, 5, 12)),
        (datetime(2026, 5, 13, 15, 15, 0), date(2026, 5, 13)),
        (datetime(2026, 5, 16, 12, 0, 0), date(2026, 5, 15)),
    ],
)
def test_latest_expected_daily_kline_date_uses_close_publish_buffer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
    expected: date,
) -> None:
    bundle_path = tmp_path / "bundle.json"
    _write_calendar(
        bundle_path,
        ["2026-05-11", "2026-05-12", "2026-05-13", "2026-05-14", "2026-05-15", "2026-05-18"],
    )
    _use_paths(monkeypatch, tmp_path / "missing-runtime.json", bundle_path)

    assert trading_calendar.latest_expected_daily_kline_date(now) == expected


def test_equivalent_utc_and_shanghai_datetimes_produce_same_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_path = tmp_path / "bundle.json"
    _write_calendar(bundle_path, ["2026-05-12", "2026-05-13", "2026-05-14"])
    _use_paths(monkeypatch, tmp_path / "missing-runtime.json", bundle_path)
    utc_now = datetime(2026, 5, 13, 1, 30, tzinfo=UTC)
    shanghai_now = datetime(2026, 5, 13, 9, 30, tzinfo=trading_calendar.ASHARE_TIMEZONE)

    assert trading_calendar.market_session_phase(utc_now) is trading_calendar.market_session_phase(shanghai_now)
    assert trading_calendar.expected_quote_date(utc_now) == trading_calendar.expected_quote_date(shanghai_now)
    assert trading_calendar.latest_expected_trade_date(utc_now) == trading_calendar.latest_expected_trade_date(shanghai_now)


def test_default_market_clock_is_independent_of_host_timezone() -> None:
    original_timezone = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "UTC"
        time.tzset()
        on_utc_host = trading_calendar._market_now()
        os.environ["TZ"] = "Asia/Shanghai"
        time.tzset()
        on_shanghai_host = trading_calendar._market_now()
    finally:
        if original_timezone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_timezone
        time.tzset()

    assert abs((on_shanghai_host - on_utc_host).total_seconds()) < 1


def test_save_days_is_atomic_and_persists_exact_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_path = tmp_path / "trading_calendar.json"
    monkeypatch.setattr(trading_calendar, "CALENDAR_PATH", runtime_path)
    monkeypatch.setattr(trading_calendar, "_market_now", lambda: datetime(2026, 1, 7, 12, 34, 56))

    trading_calendar._save_days({date(2026, 1, 5), date(2025, 12, 31), date(2026, 1, 5)})

    payload = json.loads(runtime_path.read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": 1,
        "source": "akshare.tool_trade_date_hist_sina",
        "updated_at": "2026-01-07 12:34:56",
        "min_date": "2025-12-31",
        "max_date": "2026-01-05",
        "trade_date_count": 2,
        "trade_dates": ["2025-12-31", "2026-01-05"],
    }
    assert not list(tmp_path.glob(".trading_calendar.json.*.tmp"))


def test_failed_atomic_replace_preserves_previous_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_path = tmp_path / "trading_calendar.json"
    original = b'{"original": true}\n'
    runtime_path.write_bytes(original)
    monkeypatch.setattr(trading_calendar, "CALENDAR_PATH", runtime_path)
    monkeypatch.setattr(trading_calendar.os, "replace", Mock(side_effect=OSError("replace failed")))

    with pytest.raises(OSError, match="replace failed"):
        trading_calendar._save_days({date(2026, 1, 5)})

    assert runtime_path.read_bytes() == original
    assert not list(tmp_path.glob(".trading_calendar.json.*.tmp"))


def test_refresh_writes_only_runtime_and_reports_accurate_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_path = tmp_path / "runtime.json"
    bundle_path = tmp_path / "bundle.json"
    _write_calendar(bundle_path, ["2026-01-02", "2026-01-05"], updated_at="2026-01-01 08:00:00")
    bundle_before = bundle_path.read_bytes()
    _use_paths(monkeypatch, runtime_path, bundle_path)
    monkeypatch.setattr(trading_calendar, "_market_now", lambda: datetime(2026, 1, 6, 12, 0, 0))
    monkeypatch.setattr(
        trading_calendar,
        "_fetch_akshare_trade_dates_result",
        Mock(return_value=trading_calendar.TradeDateFetchResult({date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)})),
    )

    result = trading_calendar.refresh_trade_calendar_result()

    payload = json.loads(runtime_path.read_text(encoding="utf-8"))
    assert result == trading_calendar.TradeCalendarRefreshResult(3, "runtime_cache")
    assert payload["updated_at"] == "2026-01-06 12:00:00"
    assert payload["min_date"] == payload["trade_dates"][0] == "2026-01-02"
    assert payload["max_date"] == payload["trade_dates"][-1] == "2026-01-06"
    assert payload["trade_date_count"] == len(payload["trade_dates"]) == 3
    assert bundle_path.read_bytes() == bundle_before


def test_refresh_replace_failure_preserves_previous_runtime_and_reports_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_path = tmp_path / "runtime.json"
    bundle_path = tmp_path / "bundle.json"
    _write_calendar(runtime_path, ["2026-01-02", "2026-01-05"])
    _write_calendar(bundle_path, ["2026-01-02", "2026-01-05"])
    runtime_before = runtime_path.read_bytes()
    _use_paths(monkeypatch, runtime_path, bundle_path)
    monkeypatch.setattr(trading_calendar, "_market_now", lambda: datetime(2026, 1, 5, 12, 0, 0))
    monkeypatch.setattr(
        trading_calendar,
        "_fetch_akshare_trade_dates_result",
        Mock(return_value=trading_calendar.TradeDateFetchResult({date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)})),
    )
    monkeypatch.setattr(trading_calendar.os, "replace", Mock(side_effect=OSError("replace failed")))

    result = trading_calendar.refresh_trade_calendar_result()

    assert not result.ok
    assert result.trade_date_count == 0
    assert result.source == "runtime_cache"
    assert "保存交易日历失败" in (result.error or "")
    assert runtime_path.read_bytes() == runtime_before
    assert not list(tmp_path.glob(".runtime.json.*.tmp"))


def test_refresh_wait_is_bounded_without_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    monkeypatch.setattr(trading_calendar, "TRADE_CALENDAR_FETCH_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        trading_calendar,
        "_fetch_akshare_trade_dates_blocking",
        lambda: (release.wait(1), trading_calendar.TradeDateFetchResult(set()))[1],
    )

    started = time.monotonic()
    result = trading_calendar._fetch_akshare_trade_dates_result()
    elapsed = time.monotonic() - started
    retry_started = time.monotonic()
    retry = trading_calendar._fetch_akshare_trade_dates_result()
    retry_elapsed = time.monotonic() - retry_started
    release.set()
    deadline = time.monotonic() + 0.5
    while trading_calendar._FETCH_LOCK.locked() and time.monotonic() < deadline:
        time.sleep(0.001)

    assert elapsed < 0.5
    assert not result.days
    assert "超过 0.01 秒" in (result.error or "")
    assert retry_elapsed < 0.1
    assert retry.error == "已有交易日历刷新仍在进行，请稍后重试"
    assert not trading_calendar._FETCH_LOCK.locked()


def _use_paths(monkeypatch: pytest.MonkeyPatch, runtime_path: Path, bundle_path: Path) -> None:
    monkeypatch.setattr(trading_calendar, "CALENDAR_PATH", runtime_path)
    monkeypatch.setattr(trading_calendar, "BUNDLED_CALENDAR_PATH", bundle_path)
    trading_calendar._reset_calendar_caches()


def _wait_until(predicate, *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    pytest.fail("condition did not become true before timeout")


def _wait_for_auto_refresh(*, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        worker = trading_calendar._AUTO_REFRESH_THREAD
        if worker is None:
            return
        worker.join(0.01)
    pytest.fail("automatic calendar refresh did not finish before timeout")


def _write_calendar(
    path: Path,
    trade_dates: list[str],
    *,
    updated_at: str = "2026-01-02 12:00:00",
    source: str = "test-calendar",
) -> None:
    ordered = sorted(set(trade_dates))
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": source,
                "updated_at": updated_at,
                "min_date": ordered[0],
                "max_date": ordered[-1],
                "trade_date_count": len(ordered),
                "trade_dates": ordered,
            }
        ),
        encoding="utf-8",
    )
